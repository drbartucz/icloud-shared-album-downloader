#!/usr/bin/env python3
"""Sync photos from public iCloud shared album links and resize them for a screen.

Reads a list of public shared-album URLs such as
    https://photos.icloud.com/shared/album/<album token>/
from a text file, finds photos that have not been downloaded yet, downloads
them, resizes them to fit a 1920x1080 screen (configurable), and saves them as
JPEGs with the date taken in their EXIF data. A small state file in each album folder records what has already been
fetched, so later runs only download new or edited photos.

This uses the same unauthenticated CloudKit endpoints that the iCloud Photos web
page uses for public links. Apple does not document them, so they may change.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import plistlib
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageOps
from PIL.TiffImagePlugin import IFDRational

try:  # Optional: lets Pillow open HEIC originals when --source original is used.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

log = logging.getLogger("icloud_album_sync")

CONTAINER = "com.apple.photos.cloud"
RESOLVE_URL = (
    f"https://ckdatabasews.icloud.com/database/1/{CONTAINER}/production/public/records/resolve"
)
QUERY_PATH = f"/database/1/{CONTAINER}/production/shared/records/query"
ASSET_QUERY_TYPE = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
STATE_FILENAME = ".sync-state.json"
STATE_VERSION = 1
# Bump when the saved output changes, so existing photos are re-processed.
OUTPUT_VERSION = 2
URL_TOKEN_RE = re.compile(r"/shared/album/([A-Za-z0-9_-]+)")
VIDEO_TYPES = {"com.apple.quicktime-movie", "public.mpeg-4", "public.movie", "public.avi"}
HEADERS = {
    "Origin": "https://photos.icloud.com",
    "Referer": "https://photos.icloud.com/",
    "Content-Type": "text/plain",
    "User-Agent": "icloud-shared-album-downloader/1.0",
}


class AlbumError(Exception):
    """A problem with one album that should not stop the others."""


@dataclass
class Photo:
    asset_id: str
    filename: str  # Original filename, e.g. IMG_1234.HEIC
    taken: datetime | None
    resource_key: str  # Which resolution was chosen, e.g. resJPEGMedRes
    checksum: str  # Changes when the photo is edited
    download_url: str
    latitude: float | None = None
    longitude: float | None = None


# --------------------------------------------------------------------------- #
# Reading the album list
# --------------------------------------------------------------------------- #


def read_album_urls(path: Path) -> list[str]:
    """Return album URLs from a file, ignoring blank lines and # comments."""
    urls = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def token_from_url(url: str) -> str:
    match = URL_TOKEN_RE.search(url)
    if not match:
        raise AlbumError(
            f"Not a recognised shared album URL: {url} "
            "(expected https://photos.icloud.com/shared/album/<token>/)"
        )
    return match.group(1)


# --------------------------------------------------------------------------- #
# Talking to iCloud
# --------------------------------------------------------------------------- #


def _post_json(session: requests.Session, url: str, body: dict, params: dict) -> dict:
    resp = session.post(url, params=params, data=json.dumps(body), timeout=60)
    if resp.status_code != 200:
        raise AlbumError(f"iCloud returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def resolve_album(session: requests.Session, token: str) -> dict:
    """Turn a share token into the album title, zone, host and an access token."""
    data = _post_json(
        session,
        RESOLVE_URL,
        {"shortGUIDs": [{"value": token}]},
        {"remapEnums": "true", "sharing_url_key": token},
    )
    results = data.get("results") or []
    if not results or "anonymousPublicAccess" not in results[0]:
        reason = results[0].get("reason") if results else data
        raise AlbumError(f"Album is not publicly accessible or no longer exists ({reason})")
    result = results[0]
    access = result["anonymousPublicAccess"]
    title = (
        result.get("share", {}).get("fields", {}).get("cloudkit.title", {}).get("value")
        or token
    )
    return {
        "title": title,
        "zone": result["zoneID"],
        "partition": access["databasePartition"].replace(":443", ""),
        "access_token": access["token"],
    }


def fetch_records(session: requests.Session, token: str, album: dict) -> list[dict]:
    """Return every CPLAsset and CPLMaster record in the album, following pages."""
    url = album["partition"] + QUERY_PATH
    params = {
        "remapEnums": "true",
        "sharing_url_key": token,
        "publicAccessAuthToken": album["access_token"],
    }
    records: list[dict] = []
    marker = None
    while True:
        body = {
            "query": {
                "recordType": ASSET_QUERY_TYPE,
                "filterBy": [
                    {"fieldName": "startRank", "comparator": "EQUALS",
                     "fieldValue": {"type": "INT64", "value": 0}},
                    {"fieldName": "direction", "comparator": "EQUALS",
                     "fieldValue": {"type": "STRING", "value": "ASCENDING"}},
                ],
            },
            "zoneID": album["zone"],
            "resultsLimit": 200,
        }
        if marker:
            body["continuationMarker"] = marker
        data = _post_json(session, url, body, params)
        records.extend(data.get("records", []))
        marker = data.get("continuationMarker")
        if not marker:
            return records


def _field(record: dict, name: str):
    return record.get("fields", {}).get(name, {}).get("value")


def _location(asset: dict) -> tuple[float | None, float | None]:
    """Decode the asset's location, a base64 binary plist with lat/lon keys."""
    encoded = _field(asset, "locationEnc")
    if not encoded:
        return None, None
    try:
        loc = plistlib.loads(base64.b64decode(encoded))
        return float(loc["lat"]), float(loc["lon"])
    except (ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        return None, None


def build_photos(records: list[dict], source: str) -> list[Photo]:
    """Pair each asset with its master record and pick the image to download.

    An asset carries edits (crops, filters); its master is the untouched
    original. Edited renders on the asset win over the master's versions.
    """
    masters = {r["recordName"]: r for r in records if r.get("recordType") == "CPLMaster"}
    if source == "original":
        preference = [("asset", "resJPEGFullRes"), ("master", "resOriginalRes"),
                      ("asset", "resJPEGMedRes"), ("master", "resJPEGMedRes")]
    else:
        preference = [("asset", "resJPEGMedRes"), ("master", "resJPEGMedRes"),
                      ("asset", "resJPEGFullRes"), ("master", "resOriginalRes")]

    photos = []
    for asset in records:
        if asset.get("recordType") != "CPLAsset" or asset.get("deleted"):
            continue
        master_ref = _field(asset, "masterRef") or {}
        master = masters.get(master_ref.get("recordName"))
        if master is None:
            log.debug("Asset %s has no master record; skipping", asset["recordName"])
            continue
        if _field(master, "itemType") in VIDEO_TYPES:
            log.debug("Skipping video %s", asset["recordName"])
            continue

        chosen = None
        for owner, key in preference:
            res = _field(asset if owner == "asset" else master, key)
            if res and res.get("downloadURL"):
                chosen = (key, res)
                break
        if chosen is None:
            log.warning("No downloadable image for asset %s", asset["recordName"])
            continue

        key, res = chosen
        filename = asset["recordName"]
        encoded_name = _field(master, "filenameEnc")
        if encoded_name:
            try:
                filename = base64.b64decode(encoded_name).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass

        taken_ms = _field(asset, "assetDate") or _field(master, "originalCreationDate")
        taken = None
        if taken_ms:
            # Use the local time where the photo was taken, not UTC.
            offset = timedelta(seconds=_field(asset, "timeZoneOffset") or 0)
            taken = datetime.fromtimestamp(taken_ms / 1000, tz=timezone(offset))

        latitude, longitude = _location(asset)
        photos.append(Photo(
            asset_id=asset["recordName"],
            filename=filename,
            taken=taken,
            resource_key=key,
            checksum=res.get("fileChecksum", ""),
            download_url=res["downloadURL"].replace("${f}", "image"),
            latitude=latitude,
            longitude=longitude,
        ))
    return photos


# --------------------------------------------------------------------------- #
# Image processing
# --------------------------------------------------------------------------- #


def resize_for_screen(img: Image.Image, width: int, height: int, mode: str,
                      upscale: bool, background: str) -> Image.Image:
    """Resize an image for a width x height screen.

    fit  - scale to fit inside the screen, keeping the whole photo (default)
    fill - scale and centre-crop so the photo covers the whole screen
    pad  - like fit, then centre on a background-coloured canvas of exactly
           width x height
    """
    img = ImageOps.exif_transpose(img)  # Apply the camera's rotation flag.
    if img.mode != "RGB":
        img = img.convert("RGB")

    if mode == "fill":
        return ImageOps.fit(img, (width, height), Image.Resampling.LANCZOS)

    scale = min(width / img.width, height / img.height)
    if scale < 1 or upscale:
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    if mode == "pad":
        canvas = Image.new("RGB", (width, height), background)
        canvas.paste(img, ((width - img.width) // 2, (height - img.height) // 2))
        return canvas
    return img


def _gps_dms(value: float) -> tuple[IFDRational, IFDRational, IFDRational]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round(((value - degrees) * 60 - minutes) * 60 * 1000)
    return IFDRational(degrees, 1), IFDRational(minutes, 1), IFDRational(seconds, 1000)


def build_exif(photo: Photo, keep_location: bool) -> Image.Exif:
    """Build minimal EXIF: the date taken (shown by MMM-BackgroundSlideshow's
    imageInfo 'date') and, only if asked, GPS coordinates (used by its
    'position' option). Nothing else from the original is kept."""
    exif = Image.Exif()
    if photo.taken:
        stamp = photo.taken.strftime("%Y:%m:%d %H:%M:%S")
        offset = photo.taken.strftime("%z")  # e.g. -0600
        exif[0x0132] = stamp  # DateTime
        exif_ifd = exif.get_ifd(0x8769)
        exif_ifd[0x9003] = stamp  # DateTimeOriginal
        exif_ifd[0x9004] = stamp  # DateTimeDigitized
        if offset:
            exif_ifd[0x9011] = f"{offset[:3]}:{offset[3:]}"  # OffsetTimeOriginal
    if keep_location and photo.latitude is not None and photo.longitude is not None:
        gps = exif.get_ifd(0x8825)
        gps[0x0000] = bytes([2, 2, 0, 0])  # GPSVersionID
        gps[0x0001] = "N" if photo.latitude >= 0 else "S"
        gps[0x0002] = _gps_dms(photo.latitude)
        gps[0x0003] = "E" if photo.longitude >= 0 else "W"
        gps[0x0004] = _gps_dms(photo.longitude)
    return exif


def save_jpeg_atomic(img: Image.Image, dest: Path, quality: int, exif: Image.Exif) -> None:
    """Write to a temp file first so a crash never leaves a half-written photo."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            img.save(fh, "JPEG", quality=quality, optimize=True, progressive=True,
                     exif=exif.tobytes())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Local state
# --------------------------------------------------------------------------- #


def safe_name(text: str) -> str:
    """Make a string safe to use as a file or folder name on any OS."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return text or "untitled"


def load_state(folder: Path) -> dict:
    path = folder / STATE_FILENAME
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("version") == STATE_VERSION:
                return state
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s (%s); starting fresh", path, exc)
    return {"version": STATE_VERSION, "albums": {}}


def save_state(folder: Path, state: dict) -> None:
    path = folder / STATE_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def output_name(photo: Photo, taken_names: set[str]) -> str:
    """Name files by date taken so they sort chronologically, e.g.
    20261118-143005_IMG_2222.jpg. Adds a suffix if the name is already used."""
    stem = safe_name(Path(photo.filename).stem)
    prefix = photo.taken.strftime("%Y%m%d-%H%M%S_") if photo.taken else ""
    name = f"{prefix}{stem}.jpg"
    if name.lower() in taken_names:
        name = f"{prefix}{stem}_{safe_name(photo.asset_id)[:8]}.jpg"
    return name


# --------------------------------------------------------------------------- #
# Main sync logic
# --------------------------------------------------------------------------- #


def sync_album(session: requests.Session, url: str, args: argparse.Namespace) -> tuple[int, int]:
    """Sync one album. Returns (photos downloaded, photos that failed)."""
    token = token_from_url(url)
    album = resolve_album(session, token)
    photos = build_photos(fetch_records(session, token, album), args.source)

    # In flat mode every album shares the output folder and its state file.
    folder = args.output if args.flat else args.output / safe_name(album["title"])
    folder.mkdir(parents=True, exist_ok=True)
    state = load_state(folder)
    album_entries = state["albums"].setdefault(token, {})
    in_use = {e["file"].lower() for entries in state["albums"].values() for e in entries.values()}

    todo = []
    for photo in photos:
        entry = album_entries.get(photo.asset_id)
        if entry is None:
            todo.append((photo, "new"))
        elif entry.get("checksum") != photo.checksum or entry.get("settings") != args.settings_key:
            todo.append((photo, "changed"))
        elif not (folder / entry["file"]).exists():
            todo.append((photo, "missing"))

    log.info("Album '%s': %d photos, %d to download", album["title"], len(photos), len(todo))

    downloaded = failed = 0
    for photo, reason in todo:
        entry = album_entries.get(photo.asset_id)
        name = entry["file"] if entry else output_name(photo, in_use)
        if args.dry_run:
            log.info("  [dry run] would save %s (%s)", name, reason)
            continue
        try:
            resp = session.get(photo.download_url, timeout=120)
            resp.raise_for_status()
            with Image.open(io.BytesIO(resp.content)) as img:
                out = resize_for_screen(img, args.width, args.height, args.mode,
                                        args.upscale, args.background)
            save_jpeg_atomic(out, folder / name, args.quality,
                             build_exif(photo, args.keep_location))
        except Image.UnidentifiedImageError:
            hint = " (install pillow-heif to read HEIC originals)" if not HEIF_AVAILABLE else ""
            log.error("  Could not read image %s%s", photo.filename, hint)
            failed += 1
            continue
        except (requests.RequestException, OSError) as exc:
            log.error("  Failed to download %s: %s", photo.filename, exc)
            failed += 1
            continue

        in_use.add(name.lower())
        album_entries[photo.asset_id] = {
            "file": name,
            "checksum": photo.checksum,
            "settings": args.settings_key,
        }
        save_state(folder, state)  # Save after each photo so progress survives a crash.
        downloaded += 1
        log.info("  Saved %s (%s, %dx%d)", name, reason, out.width, out.height)

    if args.prune and not photos and album_entries:
        log.warning("  iCloud listed no photos but %d were saved before; not pruning "
                    "in case this is an iCloud glitch", len(album_entries))
    elif args.prune:
        current = {p.asset_id for p in photos}
        for asset_id in [a for a in album_entries if a not in current]:
            file = folder / album_entries[asset_id]["file"]
            if args.dry_run:
                log.info("  [dry run] would remove %s (no longer in album)", file.name)
                continue
            file.unlink(missing_ok=True)
            del album_entries[asset_id]
            log.info("  Removed %s (no longer in album)", file.name)
        if not args.dry_run:
            save_state(folder, state)

    return downloaded, failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download new photos from public iCloud shared albums and resize them.",
    )
    parser.add_argument("-a", "--albums", type=Path, default=Path("albums.txt"),
                        help="text file with one shared album URL per line (default: albums.txt)")
    parser.add_argument("-o", "--output", type=Path, default=Path("photos"),
                        help="folder to save photos in (default: photos)")
    parser.add_argument("--width", type=int, default=1920, help="screen width (default: 1920)")
    parser.add_argument("--height", type=int, default=1080, help="screen height (default: 1080)")
    parser.add_argument("--mode", choices=["fit", "fill", "pad"], default="fit",
                        help="fit: whole photo inside the screen (default); "
                             "fill: crop to cover the screen; pad: fit then add borders")
    parser.add_argument("--background", default="black",
                        help="border colour for --mode pad (default: black)")
    parser.add_argument("--upscale", action="store_true",
                        help="enlarge photos smaller than the screen (off by default)")
    parser.add_argument("--quality", type=int, default=90, help="JPEG quality 1-95 (default: 90)")
    parser.add_argument("--source", choices=["medium", "original"], default="medium",
                        help="medium: Apple's ~2048px JPEG, plenty for 1080p (default); "
                             "original: full resolution, usually HEIC, needs pillow-heif")
    parser.add_argument("--flat", action="store_true",
                        help="save every album into the output folder itself, not per-album subfolders")
    parser.add_argument("--prune", action=argparse.BooleanOptionalAction, default=True,
                        help="delete local photos that were removed from the album "
                             "(default: on; use --no-prune to keep them)")
    parser.add_argument("--keep-location", action="store_true",
                        help="write GPS coordinates into saved photos, for "
                             "MMM-BackgroundSlideshow's 'position' info (off by default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen without downloading or deleting anything")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    args = parser.parse_args(argv)

    if not 1 <= args.quality <= 95:
        parser.error("--quality must be between 1 and 95")
    if args.width < 1 or args.height < 1:
        parser.error("--width and --height must be positive")
    if args.source == "original" and not HEIF_AVAILABLE:
        log.warning("pillow-heif is not installed; HEIC originals will fail. "
                    "Run: pip install pillow-heif")
    # If any of these change, already-downloaded photos are re-processed.
    args.settings_key = (f"v{OUTPUT_VERSION}/{args.width}x{args.height}/{args.mode}/"
                         f"{args.upscale}/{args.background}/{args.quality}/{args.source}/"
                         f"gps={args.keep_location}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not args.albums.exists():
        log.error("Album list %s not found. Copy albums.example.txt to albums.txt and "
                  "add your shared album URLs.", args.albums)
        return 2
    urls = read_album_urls(args.albums)
    if not urls:
        log.error("No album URLs found in %s", args.albums)
        return 2

    session = requests.Session()
    session.headers.update(HEADERS)
    total = errors = 0
    for url in urls:
        try:
            downloaded, failed = sync_album(session, url, args)
            total += downloaded
            errors += failed
        except AlbumError as exc:
            log.error("Skipping %s: %s", url, exc)
            errors += 1
        except requests.RequestException as exc:
            log.error("Network error for %s: %s", url, exc)
            errors += 1

    log.info("Done: %d new photo(s) saved, %d error(s)", total, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
