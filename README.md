# icloud-shared-album-downloader

Download and resize images from shared iCloud albums.

`icloud_album_sync.py` reads a list of public iCloud shared album links, checks each album for photos it hasn't downloaded yet, downloads them, and resizes them to fit a 1080p (1920×1080) screen. It was built to feed [MagicMirror²](https://magicmirror.builders/) with [MMM-BackgroundSlideshow](https://github.com/darickc/MMM-BackgroundSlideshow) from albums that friends and family add to. It works just as well for a digital photo frame or a screensaver folder.

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
| `--no-prune` | pruning on | Keep local photos even after they are removed from the album |
| `--keep-location` | off | Write GPS coordinates into saved photos |
| `--dry-run` | off | Show what would happen without changing anything |
| `-d`, `--debug` | off | Show progress and debug output |

**Resize modes**

- `fit` keeps the whole photo and scales it to fit inside 1920×1080. A portrait photo becomes 810×1080 and a 4:3 landscape becomes 1440×1080.
- `fill` scales and centre-crops so every image is exactly 1920×1080 with no borders. Portrait photos lose a lot of their top and bottom.
- `pad` does `fit`, then centres the photo on a 1920×1080 canvas with borders.

Photos are rotated upright using their EXIF orientation. The saved JPEGs keep only the date and time taken, so MMM-BackgroundSlideshow can display it. GPS location is dropped unless you pass `--keep-location`. Camera details and everything else are always dropped.

**Pruning.** Photos removed from an iCloud album are deleted locally on the next run. If iCloud ever returns an empty album when photos were saved before, nothing is deleted and a warning is logged, in case iCloud had a glitch. Only files the script downloaded itself are ever deleted.

**Source quality.** By default the script downloads Apple's "medium" JPEG, which is about 2048 pixels on the long edge. That is more than enough for 1080p and avoids HEIC decoding. Use `--source original` for 4K screens. It needs `pip install pillow-heif` because most iPhone originals are HEIC.

If a photo is edited in Photos, the script notices the change and downloads it again. Changing `--width`, `--height`, `--mode`, `--quality`, `--source` or `--keep-location` also re-processes existing photos so the folder stays consistent.

### Output

By default the script prints nothing unless something goes wrong. Only warnings and errors are shown, which keeps scheduled runs from filling a log file. Use `--debug` to see each photo as it is saved, plus detailed debugging output. `--dry-run` always prints what it would do.

### Exit codes

`0` means success, `1` means at least one photo or album failed, and `2` means the album list is missing or empty. This makes it easy to spot failures from cron or Task Scheduler.

## Using it with MagicMirror²

On the Raspberry Pi (or whatever runs MagicMirror), clone this repo, set it up as above, and point the output at a folder MagicMirror can read:

```bash
cd ~/icloud-shared-album-downloader
.venv/bin/python icloud_album_sync.py -o ~/MagicMirror/photos
```

Each album gets its own subfolder, so turn on `recursiveSubDirectories` in the module config:

```js
{
  module: "MMM-BackgroundSlideshow",
  position: "fullscreen_below",
  config: {
    imagePaths: ["photos/"],          // relative to the MagicMirror folder
    recursiveSubDirectories: true,    // one subfolder per album
    randomizeImageOrder: true,
    randomizeImagesLoopFolders: true, // alternate between albums fairly
    showAllImagesBeforeRestart: true,
    slideshowSpeed: 30000,
    backgroundSize: "contain",        // show the whole photo
    backgroundBlurEnabled: true,      // fill the sides with a blurred copy
    transitionImages: true,
    showImageInfo: true,
    imageInfo: "date",
    imageInfoDateFormat: "MMMM D, YYYY"
  }
},
```

Notes:

- **Keep the default `fit` mode.** The module's `backgroundBlurEnabled` fills the empty sides of portrait photos with a blurred copy, which looks better than the black bars from `pad` or the heavy crop from `fill`.
- **No need for the module's own `resizeImages`.** Photos are already sized for the screen, so the module doesn't need the `sharp` library.
- **New photos appear without a restart.** The module re-reads its folders each time it finishes a full pass through the images.
- **For place names**, run the script with `--keep-location`, then add `position` to `imageInfo` and set `useOpenStreetMap: true` in the module config.
- **To use one folder instead of subfolders**, run with `--flat` and leave `recursiveSubDirectories` off.

## Running it automatically

**Linux / Raspberry Pi (cron)**, every 15 minutes. Run `crontab -e` and add:

```cron
*/15 * * * * cd /home/pi/icloud-shared-album-downloader && .venv/bin/python icloud_album_sync.py -o /home/pi/MagicMirror/photos >> sync.log 2>&1
```

Because normal runs print nothing, `sync.log` only grows when something goes wrong.

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

Videos are skipped. Live Photos are saved as still images.

## Caveats

- Apple does not document these endpoints, so they may change without notice. If the script suddenly finds no photos or gets HTTP errors, that is the likely cause.
- Anyone with a public album link can view the album, and with the default sharing settings they can also add photos. Keep `albums.txt` private. It is listed in `.gitignore` for that reason.
- Only the new `photos.icloud.com/shared/album/...` links are supported. Older `www.icloud.com/sharedalbum/#...` links use a different API.
- MagicMirror modules that read iCloud albums directly, such as MMM-Wallpaper, use that older API and don't work with the new links. That's the main reason this script exists.

## License

[MIT](LICENSE)
