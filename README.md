# icloud-shared-album-downloader

Download and resize images from shared iCloud albums.

`icloud_album_sync.py` reads a list of public iCloud shared album links, checks each album for photos it hasn't downloaded yet, downloads them, and resizes them to fit a 1080p (1920×1080) screen. It is handy for feeding a digital photo frame, a MagicMirror slideshow, or a screensaver folder from albums that friends and family add to.

No Apple ID or login is needed. The album only has to have **Public Website** sharing turned on.

## Requirements

- Python 3.10 or newer
- `requests` and `Pillow`
- Optional: `pillow-heif`, only if you use `--source original`

## Setup

```bash
git clone <this repo>
cd icloud-shared-album-downloader
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example album list and add your links:

```bash
cp albums.example.txt albums.txt
```

```text
# albums.txt — one link per line, # starts a comment
https://photos.icloud.com/shared/album/<album token>/
https://photos.icloud.com/shared/album/<another token>/
```

To get a link, open the shared album in the Photos app, tap the people icon, turn on **Public Website**, and copy the link.

## Usage

```bash
python icloud_album_sync.py
```

Photos are saved under `photos/<Album Title>/` with names like `20261118-143005_IMG_2222.jpg`, so they sort by the date they were taken. Run it again any time and only new photos are downloaded.

### Options

| Option | Default | What it does |
| --- | --- | --- |
| `-a`, `--albums FILE` | `albums.txt` | File containing album links |
| `-o`, `--output DIR` | `photos` | Where to save photos |
| `--width`, `--height` | `1920`, `1080` | Target screen size |
| `--mode fit\|fill\|pad` | `fit` | How to fit the screen (see below) |
| `--background COLOR` | `black` | Border colour for `pad` mode |
| `--upscale` | off | Enlarge photos smaller than the screen |
| `--quality N` | `90` | JPEG quality, 1–95 |
| `--source medium\|original` | `medium` | Which version to download from iCloud |
| `--flat` | off | Put all albums in one folder instead of one per album |
| `--prune` | off | Delete local photos that were removed from the album |
| `--dry-run` | off | Show what would happen without changing anything |
| `-v`, `--verbose` | off | Show debug output |

**Resize modes**

- `fit` keeps the whole photo and scales it to fit inside 1920×1080. A portrait photo becomes 810×1080 and a 4:3 landscape becomes 1440×1080.
- `fill` scales and centre-crops so every image is exactly 1920×1080 with no borders. Portrait photos lose a lot of their top and bottom.
- `pad` does `fit`, then centres the photo on a 1920×1080 canvas with borders.

Photos are rotated upright using their EXIF orientation. The saved JPEGs contain no metadata, so GPS location and camera details are not kept.

**Source quality.** By default the script downloads Apple's "medium" JPEG, which is about 2048 pixels on the long edge. That is more than enough for 1080p and avoids HEIC decoding. Use `--source original` for 4K screens. It needs `pip install pillow-heif` because most iPhone originals are HEIC.

If a photo is edited in Photos, the script notices the change and downloads it again. Changing `--width`, `--height`, `--mode`, `--quality` or `--source` also re-processes existing photos so the folder stays consistent.

### Exit codes

`0` means success, `1` means at least one photo or album failed, and `2` means the album list is missing or empty. This makes it easy to spot failures from cron or Task Scheduler.

## Running it automatically

**Linux / Raspberry Pi (cron)**, every 15 minutes:

```cron
*/15 * * * * cd /home/pi/icloud-shared-album-downloader && .venv/bin/python icloud_album_sync.py >> sync.log 2>&1
```

**Windows (Task Scheduler)**:

```powershell
schtasks /Create /SC MINUTE /MO 15 /TN "iCloud album sync" /TR "C:\path\to\.venv\Scripts\python.exe C:\path\to\icloud_album_sync.py -o C:\path\to\photos -a C:\path\to\albums.txt"
```

## How it works

The script uses the same endpoints as the iCloud Photos web page for public links:

1. `records/resolve` turns the token in the link into the album's storage zone, server and a short-lived anonymous access token.
2. `records/query` lists every photo in the album, following pagination.
3. Each photo has signed download links for several sizes. The script picks one, downloads it, and resizes it with Pillow.

A `.sync-state.json` file in each output folder records which photos have been saved and their checksums. Delete it to force a full re-download.

Videos are skipped.

## Caveats

- Apple does not document these endpoints, so they may change without notice. If the script suddenly finds no photos or gets HTTP errors, that is the likely cause.
- Anyone with a public album link can view the album, and with the default sharing settings they can also add photos. Keep `albums.txt` private. It is listed in `.gitignore` for that reason.
- Only the new `photos.icloud.com/shared/album/...` links are supported. Older `www.icloud.com/sharedalbum/#...` links use a different API.

## License

[MIT](LICENSE)
