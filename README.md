# smugmug-to-local — SmugMug migration toolkit

A set of Python scripts for migrating a SmugMug photo library to a
self-hosted solution (e.g. Immich on a Synology NAS).

The scripts download your entire SmugMug library with full metadata
(captions, GPS, timestamps, manual ordering) and help you merge it
with any existing local photo collection.

---

## Prerequisites

| Dependency | Install |
|---|---|
| Python 3.8+ | bundled with macOS / most Linux distros |
| exiftool | `brew install exiftool` / `apt install libimage-exiftool-perl` |
| rauth | `pip install rauth` |
| requests | `pip install requests` |

---

## Quick-start

```bash
# 1. Copy and fill in your SmugMug API credentials
cp config.json.example config.json
# Edit config.json with your key, secret, access_token, access_token_secret

# 2. Audit your SmugMug library (optional but recommended)
python3 smugmug_audit.py

# 3. Download everything
python3 smugmug_download.py --output-dir ./photos --all

# 4. Verify the download
python3 smugmug_verify.py --dir ./photos --all --by-album

# 5. (If you have an existing local library) Reconcile
python3 smugmug_reconcile_dirs.py \
    --downloaded ./photos \
    --local /Volumes/photo

# 6. Merge into local library
python3 generate_sync_commands.py \
    --tsv reconcile_dirs.tsv \
    --downloaded ./photos \
    --local /Volumes/photo

# 7. (Optional) Fix any remaining metadata issues
python3 fix_media.py --dir /Volumes/photo
```

---

## Getting SmugMug API credentials

1. Log in to SmugMug → Account Settings → API Keys → Apply for an API key
2. Once approved, go to https://api.smugmug.com/api/v2!authuser to get
   your OAuth access token and secret (you'll need to authorise the app).

A simple way to obtain the access token/secret is to use the
[smugmug-oauth](https://github.com/marekrei/smufiler) helper or any
standard OAuth 1.0a flow against `https://api.smugmug.com`.

---

## Script reference

### 1. `smugmug_audit.py` — Library inventory

Scans every SmugMug album and reports sort method, caption coverage,
GPS coverage, and video count.  Saves `smugmug_audit.tsv`, which
`smugmug_download.py` uses for ordering-prefix logic and progress
estimates.

```bash
python3 smugmug_audit.py
```

**Output columns in `smugmug_audit.tsv`:**

| Column | Meaning |
|---|---|
| Name | Album name |
| Path | Full SmugMug folder path |
| SortMethod | `Position` = manually ordered |
| Total | Total image/video count |
| Sampled | Images actually checked |
| Captioned | Count with captions |
| Caption% | % captioned (estimated from sample) |
| GPSCount | Count with GPS coordinates |
| GPS% | % with GPS (reflects availability, not correctness) |
| Videos | Video file count |

**Configuration inside the script:**

| Variable | Default | Purpose |
|---|---|---|
| `SAMPLE_SIZE` | `100` | Images sampled per album; `0` = all |

---

### 2. `smugmug_download.py` — Download with full metadata

Downloads albums from SmugMug.  All metadata handling happens inline —
no post-hoc fix scripts are needed for material downloaded this way.

```bash
# Download entire library
python3 smugmug_download.py --output-dir ./photos --all

# Download specific SmugMug folders
python3 smugmug_download.py --output-dir ./photos --folder "Family Photos"

# Download albums whose name contains a string
python3 smugmug_download.py --output-dir ./photos --album "Italy"

# Re-download everything (overwrite existing files)
python3 smugmug_download.py --output-dir ./photos --all --redownload
```

**CLI flags:**

| Flag | Description |
|---|---|
| `--output-dir DIR` | Root directory to save files (required) |
| `--folder FOLDER` | SmugMug folder to download in full (repeatable) |
| `--album TEXT` | Download albums whose name contains TEXT |
| `--min-caption-pct N` | Min % captioned images to include album (default: 1) |
| `--all` | Download entire library |
| `--redownload` | Re-download files that already exist |

**Metadata applied during download:**

- **Captions** → IPTC:Caption-Abstract, XMP:Description, EXIF:ImageDescription
- **GPS** → reads original file's EXIF Ref tags (`GPSLatitudeRef`, `GPSLongitudeRef`)
  for the correct sign.  Falls back to SmugMug API GPS only when longitude is
  negative (unambiguously West/South).
- **Timestamps** → derived from filename if EXIF date is missing or looks like
  a download date.  Patterns: Android `YYYYMMDD_HHMMSS`, Samsung `BURST…`,
  run-together `YYYY-MM-DDHH_MM_SS`, 4-digit time `YYYYMMDD_HHMM`,
  macOS `Screen Shot … at …`, WhatsApp.  After each album is fully downloaded,
  any file still without a date receives the date of its nearest NNN_-indexed
  neighbour in the same directory.
- **Ordering prefix** → albums with `SortMethod=Position` AND captions ≥
  `--min-caption-pct` get a `NNN_` prefix on every filename, preserving
  SmugMug's manual curation order.

**Configuration constants inside the script:**

| Constant | Default | Purpose |
|---|---|---|
| `CUTOFF_DATE` | `"2026:01:01"` | Dates ≥ this are treated as download timestamps |
| `INTERP_MAX_GAP` | `50` | Max index gap for neighbour date interpolation |

---

### 3. `smugmug_verify.py` — Verify downloaded data

Randomly samples files from a download directory and reports date
distribution, GPS coverage by region, and caption coverage.  Use this
to sanity-check a download before merging.

```bash
# Quick spot-check (1000 random files)
python3 smugmug_verify.py --dir ./photos

# Full scan with per-album breakdown
python3 smugmug_verify.py --dir ./photos --all --by-album

# Custom sample size
python3 smugmug_verify.py --dir ./photos --samples 2000
```

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--dir DIR` | — | Directory to scan (required) |
| `--samples N` | `1000` | Files to sample |
| `--all` | off | Read every file (slow on large libraries) |
| `--by-album` | off | Show per-album breakdown |
| `--seed N` | `42` | Random seed for reproducibility |

---

### 4. `smugmug_reconcile_dirs.py` — Map downloaded → local

Compares the downloaded directory tree against an existing local
library and suggests a mapping for each album.

```bash
python3 smugmug_reconcile_dirs.py \
    --downloaded ./photos \
    --local /Volumes/photo
```

**Output actions in `reconcile_dirs.tsv`:**

| Action | Meaning |
|---|---|
| REPLACE | High-confidence match; downloaded version is better |
| MERGE | Match found but local has extra files not in SmugMug |
| ADD | No local match; copy downloaded album to library |
| REVIEW | Low confidence; inspect before acting |

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--downloaded DIR` | — | SmugMug download root (required) |
| `--local DIR` | — | Local library root (required) |
| `--max-depth N` | `6` | How deep to walk for album directories |
| `--min-files N` | `2` | Minimum files for a directory to count as an album |

---

### 5. `generate_sync_commands.py` — Merge into local library

Reads `reconcile_dirs.tsv`, shows a complete plan, and generates
`sync_commands.sh`.  Optionally runs the script immediately.

```bash
python3 generate_sync_commands.py \
    --tsv reconcile_dirs.tsv \
    --downloaded ./photos \
    --local /Volumes/photo
```

**What the generated `sync_commands.sh` does:**

- `REPLACE` and `MERGE`: `rsync -a --checksum` from downloaded to local.
  Files with identical bytes are **skipped** (local mtime preserved).
  Files with improved EXIF (captions/GPS added) are overwritten.
  Local-only files are **never deleted**.
- `ADD`: creates the target directory and rsyncs the new album.
- `REVIEW`: listed as comments for manual action.

After running `sync_commands.sh`, restore file modification dates:
```bash
exiftool -r -q '-FileModifyDate<DateTimeOriginal' /Volumes/photo
```

---

### 6. `fix_media.py` — Post-hoc metadata repair (optional)

Fixes missing/wrong dates and wrong-sign GPS in an existing photo
library.  Useful for:
- Material not downloaded via `smugmug_download.py`
- Old downloads made before GPS/date handling was added

Scans the directory, proposes all changes, then asks once whether to
apply — no repeated scanning, no separate dry-run pass.

```bash
python3 fix_media.py --dir /Volumes/photo
```

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--dir DIR` | — | Directory to scan (required) |
| `--min-oracle-images N` | `2` | Min JPEG files with GPS to use as hemisphere oracle |
| `--chunk N` | `2000` | Files per exiftool read batch |

**Date fixes:** files with no `DateTimeOriginal`, a null date (`0000:…`), or a
date ≥ `CUTOFF` (looks like a download timestamp) are fixed from the filename
(same patterns as the download script).  Files with no recognisable filename
pattern receive the date of their nearest NNN_-indexed neighbour.

**GPS fixes:** video files (MP4, MOV, etc.) whose GPS longitude is positive and
large (> 50°E) while the JPEG images in the same directory have a median
negative longitude (Western hemisphere) have their GPS sign corrected.  This
targets the Android bug where QuickTime GPS is stored unsigned.  Genuine
Eastern-hemisphere videos are unaffected because the sibling-image oracle
confirms hemisphere.

---

## Typical full workflow

```
smugmug_audit.py          →  smugmug_audit.tsv
        ↓
smugmug_download.py       →  photos/
        ↓
smugmug_verify.py         →  review output
        ↓
smugmug_reconcile_dirs.py →  reconcile_dirs.tsv
        ↓
generate_sync_commands.py →  sync_commands.sh  →  run it
        ↓
fix_media.py              →  clean up any remaining issues
        ↓
Import into Immich / other photo manager
```

---

## Notes for Immich users

After the sync, set up an **External Library** in Immich pointing at your library
directory (e.g. `/volume1/photo` on Synology).  Immich will pick
up `DateTimeOriginal` from EXIF, captions from
`IPTC:Caption-Abstract` / `XMP:Description`, and GPS from both EXIF
and XMP GPS tags.

To create Immich albums from folder names, see the community tool
[immich-folder-album-creator](https://github.com/salvoxia/immich-folder-album-creator).

---

## FAQ

**Q: Do I need to run `fix_media.py` after `smugmug_download.py`?**
No — the download script handles GPS, dates, and captions inline.
`fix_media.py` is for existing libraries or material from other sources.

**Q: Why does `GPS%` in the audit reflect SmugMug's data, not the
downloaded file?**
The audit queries the SmugMug API, which stores GPS separately from
the original file.  A photo can have GPS in the API (and therefore in
the audit) but none in the original file if the camera didn't record
it.  The download script reads the original file's Ref tags first and
only falls back to the API.

**Q: Some videos have no GPS even though the location is known.**
If SmugMug's API returns a positive (unsigned) longitude for a video,
the download script skips it — positive longitude is ambiguous (could
be East or unsigned West).  Run `fix_media.py` on the downloaded
directory after download to correct wrong-sign GPS using the sibling
JPEG images as a hemisphere oracle.

**Q: Why do some files have a date at midnight (00:00:00)?**
That date was estimated from a neighbouring file via the NNN_ index.
The ordering tells us which day the photo was taken but not the exact
time.

**Q: The sync is slow — can I speed it up?**
`rsync --checksum` reads every file to compute its hash.  On a NAS
over a network mount this is IO-bound.  You can remove `--checksum`
from `sync_commands.sh` to use size+mtime comparison instead, which is
faster but may overwrite some files unnecessarily.
