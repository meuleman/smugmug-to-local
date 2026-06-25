#!/usr/bin/env python3
from __future__ import annotations
"""
fix_media.py  —  Post-hoc metadata repair for photos and videos

Scans a directory for media files with wrong or missing metadata and
proposes fixes.  After showing the full plan it asks once whether to apply.

Fixes applied
─────────────
  Dates    Files with no DateTimeOriginal, a null date, or a date that looks
           like a download timestamp (≥ CUTOFF) are fixed from the filename
           where a date pattern is found.  If the filename has no date, the
           nearest NNN_-indexed neighbour in the same directory is used as
           an estimate (date only, time set to 00:00:00).

  GPS      Video files whose GPS longitude is positive (> GPS_LON_THRESHOLD)
           while the JPEG images in the same directory have a median negative
           longitude (Western hemisphere) have their GPS sign corrected.
           This targets the Android bug where QuickTime GPS is stored without
           a sign.  Genuine Eastern-hemisphere videos are unaffected because
           the sibling-image oracle confirms hemisphere.

Requirements:
  exiftool  (https://exiftool.org)

Usage:
  python3 fix_media.py --dir /Volumes/photo
  python3 fix_media.py --dir /Volumes/photo --min-oracle-images 3
"""

import argparse
import csv
import io
import os
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

# ─────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────

MEDIA_EXTS = {".jpg",".jpeg",".png",".heic",".gif",".dng",".raw",
              ".tiff",".tif",".mp4",".mov",".avi",".m4v",".3gp",
              ".wmv",".mts",".mkv"}
VIDEO_EXTS = {".mp4",".mov",".avi",".m4v",".3gp",".wmv",".mts",".mkv"}
IMAGE_EXTS = {".jpg",".jpeg",".png",".heic",".dng",".tiff",".tif",".raw"}
SKIP_DIRS  = {"@eadir","@eaDir","@syno","#recycle"}

CUTOFF   = datetime(2026, 1, 1)
MIN_DATE = datetime(1990, 1, 1)

# Longitude threshold for suspicious video GPS
GPS_LON_THRESHOLD = 50.0

# Max index gap for neighbour interpolation
MAX_NEIGHBOUR_GAP = 50

# ─────────────────────────────────────────────────────────────────
# FILENAME DATE PATTERNS
# ─────────────────────────────────────────────────────────────────

_PREFIX_RE  = re.compile(r"^\d{1,4}_")
_INDEX_RE   = re.compile(r"^(\d{1,4})_")

_PATTERNS = [
    (re.compile(r"BURST(\d{4})(0[1-9]|1[0-2])(\d{2})(\d{2})(\d{2})(\d{2})"),
     "BURST"),
    (re.compile(r"(\d{4})-(0[1-9]|1[0-2])-(\d{2})(\d{2})[_\-](\d{2})[_\-](\d{2})"),
     "runon"),
    (re.compile(r"(?:^|[^0-9])(\d{4})(0[1-9]|1[0-2])(\d{2})[_\-](\d{2})(\d{2})(\d{2})"),
     "android"),
    (re.compile(r"(\d{4})(0[1-9]|1[0-2])(\d{2})[_\-](\d{2})(\d{2})(?:[_\-][^0-9]|[_\-][A-Za-z]|$)"),
     "hhmm"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _]at[ _](\d{1,2})\.(\d{2})\.(\d{2})[ _]?(AM|PM)?",
               re.IGNORECASE), "macos"),
    (re.compile(r"[Ss]creenshot[_\-](\d{4})[_\-](0[1-9]|1[0-2])[_\-](\d{2})[_\-](\d{2})[_\-](\d{2})[_\-](\d{2})"),
     "screenshot"),
    (re.compile(r"(?:IMG|VID)[_\-](\d{4})(0[1-9]|1[0-2])(\d{2})[_\-]WA\d+"),
     "whatsapp"),
]


def _url_decode(s: str) -> str:
    prev = None
    while prev != s:
        prev, s = s, unquote(s)
    return s


def date_from_filename(path: Path) -> datetime | None:
    raw     = path.stem
    decoded = _url_decode(raw)
    for stem in {raw, decoded, _PREFIX_RE.sub("", raw), _PREFIX_RE.sub("", decoded)}:
        for pat, label in _PATTERNS:
            m = pat.search(stem)
            if not m:
                continue
            try:
                g = m.groups()
                if label == "macos":
                    y, mo, d, h, mi, s = int(g[0]),int(g[1]),int(g[2]),int(g[3]),int(g[4]),int(g[5])
                    ap = g[6].upper() if len(g) > 6 and g[6] else None
                    if ap == "PM" and h < 12: h += 12
                    elif ap == "AM" and h == 12: h = 0
                elif label == "whatsapp":
                    y, mo, d, h, mi, s = int(g[0]),int(g[1]),int(g[2]),0,0,0
                elif label == "hhmm":
                    y, mo, d, h, mi, s = int(g[0]),int(g[1]),int(g[2]),int(g[3]),int(g[4]),0
                else:
                    y, mo, d, h, mi, s = (int(x) for x in g[:6])
                dt = datetime(y, mo, d, h, mi, s)
                if MIN_DATE <= dt < CUTOFF:
                    return dt
            except (ValueError, IndexError):
                continue
    return None


def extract_index(filename: str) -> int | None:
    m = _INDEX_RE.match(Path(filename).name)
    return int(m.group(1)) if m else None

# ─────────────────────────────────────────────────────────────────
# EXIFTOOL BATCH READ
# ─────────────────────────────────────────────────────────────────

def read_meta_batch(files: list[Path]) -> dict[Path, dict]:
    """Batch-read DateTimeOriginal, GPS, and SourceFile for all files."""
    if not files:
        return {}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args",
                                     delete=False, encoding="utf-8") as tf:
        tf.write("-csv\n-n\n-fast2\n-DateTimeOriginal\n-CreateDate\n"
                 "-GPSLatitude\n-GPSLongitude\n")
        for f in files:
            tf.write(str(f) + "\n")
        argfile = tf.name
    try:
        r = subprocess.run(["exiftool", "-@", argfile],
                           capture_output=True, text=True)
    finally:
        os.unlink(argfile)
    result: dict[Path, dict] = {}
    for row in csv.DictReader(io.StringIO(r.stdout)):
        p  = Path(row.get("SourceFile", ""))
        dt = (row.get("DateTimeOriginal") or row.get("CreateDate") or "").strip()
        try:
            lat = float(row.get("GPSLatitude",  "") or "x")
        except (ValueError, TypeError):
            lat = None
        try:
            lon = float(row.get("GPSLongitude", "") or "x")
        except (ValueError, TypeError):
            lon = None
        result[p] = {"date": dt, "lat": lat, "lon": lon}
    return result


def date_is_wrong(dt_str: str) -> bool:
    if not dt_str or dt_str in ("0000:00:00 00:00:00", "-", ""):
        return True
    try:
        dt = datetime.strptime(dt_str[:19], "%Y:%m:%d %H:%M:%S")
        return dt >= CUTOFF or dt < MIN_DATE
    except ValueError:
        return True

# ─────────────────────────────────────────────────────────────────
# APPLY FIXES — batch write
# ─────────────────────────────────────────────────────────────────

def apply_date_fixes(updates: list[tuple[Path, datetime, bool]]) -> tuple[int, int]:
    """Write all date updates in one exiftool CSV-import call."""
    if not updates:
        return 0, 0
    import csv as csv_mod
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as tf:
        w = csv_mod.writer(tf)
        w.writerow(["SourceFile","DateTimeOriginal","DateTimeDigitized",
                    "CreateDate","IPTC:DateCreated","IPTC:TimeCreated"])
        for path, dt, _is_vid in updates:
            ts   = dt.strftime("%Y:%m:%d %H:%M:%S")
            date = dt.strftime("%Y:%m:%d")
            time = dt.strftime("%H:%M:%S")
            w.writerow([str(path), ts, ts, ts, date, time])
        csv_path = tf.name
    try:
        r = subprocess.run(
            ["exiftool", f"-csv={csv_path}", "-overwrite_original", "-quiet", "-m"],
            capture_output=True, text=True)
    finally:
        os.unlink(csv_path)
    m = re.search(r"(\d+) image files? updated", r.stdout + r.stderr)
    updated = int(m.group(1)) if m else 0
    return updated, len(updates) - updated


def apply_gps_fix(path: Path, correct_lon: float) -> bool:
    """Flip the longitude sign on a single video file."""
    ref = "W" if correct_lon < 0 else "E"
    r = subprocess.run(
        ["exiftool", "-overwrite_original", "-quiet", "-n",
         f"-GPSLongitude={correct_lon}", f"-GPSLongitudeRef={ref}",
         str(path)],
        capture_output=True, text=True)
    return r.returncode == 0 and "0 image files updated" not in r.stdout

# ─────────────────────────────────────────────────────────────────
# NEIGHBOUR INTERPOLATION
# ─────────────────────────────────────────────────────────────────

def interpolate_neighbour(idx: int, dir_known: dict[int, datetime]) -> datetime | None:
    known = sorted(dir_known.items())
    lower = [(i, dt) for i, dt in known if i < idx]
    upper = [(i, dt) for i, dt in known if i > idx]
    lo    = lower[-1] if lower else None
    hi    = upper[0]  if upper else None
    if lo and (idx - lo[0]) > MAX_NEIGHBOUR_GAP: lo = None
    if hi and (hi[0] - idx) > MAX_NEIGHBOUR_GAP: hi = None
    if lo and hi:
        use = lo[1] if lo[1].date() == hi[1].date() \
              else (lo[1] if (idx - lo[0]) <= (hi[0] - idx) else hi[1])
    elif lo:
        use = lo[1]
    elif hi:
        use = hi[1]
    else:
        return None
    return datetime(use.year, use.month, use.day, 0, 0, 0)

# ─────────────────────────────────────────────────────────────────
# LOCATION NAMES (for GPS display)
# ─────────────────────────────────────────────────────────────────

_LOCS = [
    ("Seattle",        46.0,49.0,-124.0,-121.0),("San Francisco",37.0,38.5,-123.5,-121.5),
    ("Los Angeles",    33.5,34.5,-119.0,-117.5), ("Hawaii",       18.0,23.0,-161.0,-154.0),
    ("New York",       40.4,41.2, -74.5, -73.5), ("Philadelphia", 39.7,40.4, -75.8, -74.5),
    ("Boston",         42.1,42.6, -71.5, -70.5), ("US West Coast",32.0,49.0,-125.0,-114.0),
    ("US East Coast",  25.0,47.0, -82.0, -66.0), ("Canada",       42.0,70.0,-141.0, -52.0),
    ("Netherlands",    50.7,53.6,   3.3,   7.3), ("Belgium",      49.4,51.6,   2.4,   6.5),
    ("Paris",          48.6,49.1,   1.8,   2.7), ("France",       42.0,51.2,  -5.0,   8.5),
    ("UK/Ireland",     49.9,59.0,  -9.0,   2.0), ("Germany",      47.0,55.2,   5.8,  15.2),
    ("Rest of Europe", 35.0,71.0, -10.0,  35.0), ("Japan",        24.0,46.0, 122.0, 146.0),
    ("Mongolia",       41.0,52.0,  87.0, 120.0), ("China",        18.0,53.0,  73.0, 135.0),
    ("South Asia",      5.0,37.0,  60.0,  90.0), ("Middle East",  12.0,42.0,  25.0,  60.0),
    ("Africa",        -35.0,38.0, -18.0,  52.0), ("Australia",   -44.0,-10.0,112.0, 154.0),
    ("South America", -56.0,13.0, -82.0, -34.0),
]

def location_name(lat: float, lon: float) -> str:
    for name, la0,la1,lo0,lo1 in _LOCS:
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return name
    return f"{abs(lat):.1f}°{'N' if lat>=0 else 'S'},{abs(lon):.1f}°{'E' if lon>=0 else 'W'}"

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fix missing/wrong dates and GPS in media files")
    parser.add_argument("--dir",               required=True,
                        help="Root directory to scan")
    parser.add_argument("--min-oracle-images", type=int, default=2,
                        help="Min JPEG files with GPS needed to use as hemisphere "
                             "oracle for video GPS correction (default: 2)")
    parser.add_argument("--chunk",             type=int, default=2000,
                        help="Files per exiftool read batch (default: 2000)")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        sys.exit(f"Not found: {root}")

    # ── Collect all media files ───────────────────────────────
    print(f"🔍 Scanning {root}…")
    media: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("@"))
        for fn in filenames:
            if Path(fn).suffix.lower() in MEDIA_EXTS:
                media.append(Path(dirpath) / fn)
    print(f"   {len(media):,} media files found\n")

    # ── Batch-read existing metadata ─────────────────────────
    n_chunks = (len(media) + args.chunk - 1) // args.chunk
    existing: dict[Path, dict] = {}
    for i in range(n_chunks):
        chunk = media[i * args.chunk:(i + 1) * args.chunk]
        pct   = 100 * i * args.chunk // max(len(media), 1)
        print(f"   [{pct:3d}%] Reading metadata {i+1}/{n_chunks}…",
              end="\r", flush=True)
        existing.update(read_meta_batch(chunk))
    print(f"   [100%] Done reading.               \n")

    # ── Build per-directory known-date maps ───────────────────
    from collections import defaultdict
    dir_known: dict[Path, dict[int, datetime]] = defaultdict(dict)
    for path in media:
        m     = existing.get(path, {})
        dt_s  = m.get("date", "")
        dt    = None
        if not date_is_wrong(dt_s):
            try:
                dt = datetime.strptime(dt_s[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                pass
        if dt is None:
            dt = date_from_filename(path)
        if dt is not None:
            idx = extract_index(path.name)
            if idx is not None:
                dir_known[path.parent][idx] = dt

    # ── Collect proposed changes ──────────────────────────────
    date_fixes: list[tuple[Path, datetime, bool, str]] = []   # (path, dt, is_vid, source)
    gps_fixes:  list[tuple[Path, float, float, float]] = []   # (path, lat, old_lon, new_lon)
    already_ok_dates = already_ok_gps = no_fix = 0

    # Group by directory for GPS oracle
    dirs = defaultdict(list)
    for p in media:
        dirs[p.parent].append(p)

    for d, files in sorted(dirs.items()):
        images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
        videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]

        # GPS oracle: median longitude of images in this directory
        img_lons = [existing[f]["lon"] for f in images
                    if existing.get(f) and existing[f]["lon"] is not None]
        median_lon = statistics.median(img_lons) if len(img_lons) >= args.min_oracle_images else None

        for path in sorted(files):
            m        = existing.get(path, {})
            is_video = path.suffix.lower() in VIDEO_EXTS

            # ── Date ─────────────────────────────────────────
            if date_is_wrong(m.get("date", "")):
                dt = date_from_filename(path)
                source = "filename"
                if dt is None:
                    idx = extract_index(path.name)
                    dt  = interpolate_neighbour(idx, dir_known.get(d, {})) \
                          if idx is not None else None
                    source = "neighbour"
                if dt is not None:
                    date_fixes.append((path, dt, is_video, source))
                else:
                    no_fix += 1
            else:
                already_ok_dates += 1

            # ── GPS (videos only, wrong-sign correction) ─────
            if is_video:
                lon = m.get("lon")
                lat = m.get("lat")
                if (lon is not None and lat is not None
                        and lon > GPS_LON_THRESHOLD
                        and median_lon is not None and median_lon < 0):
                    gps_fixes.append((path, lat, lon, -lon))
                elif lon is not None:
                    already_ok_gps += 1

    # ── Print proposed changes ────────────────────────────────
    print(f"{'═'*70}")
    print(f"  PROPOSED CHANGES")
    print(f"{'═'*70}")

    if date_fixes:
        print(f"\n📅 DATE FIXES ({len(date_fixes)} files):\n")
        for path, dt, is_vid, source in date_fixes:
            rel     = path.relative_to(root)
            ts      = dt.strftime("%Y:%m:%d %H:%M:%S")
            src_tag = f"  [{source}]" if source == "neighbour" else ""
            print(f"  {ts}{src_tag}  →  {rel}")
    else:
        print(f"\n📅 Dates: all OK")

    if gps_fixes:
        print(f"\n🗺️  GPS FIXES ({len(gps_fixes)} video files):\n")
        for path, lat, old_lon, new_lon in gps_fixes:
            rel = path.relative_to(root)
            old_loc = location_name(lat,  old_lon)
            new_loc = location_name(lat, new_lon)
            lat_s = f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'}"
            print(f"  {lat_s}  {old_lon:.4f}°E [{old_loc}]"
                  f"  →  {abs(new_lon):.4f}°W [{new_loc}]")
            print(f"    {rel}")
    else:
        print(f"\n🗺️  GPS: no wrong-sign videos found")

    total_fixes = len(date_fixes) + len(gps_fixes)
    print(f"\n{'─'*70}")
    print(f"  Already OK — dates: {already_ok_dates:,}   GPS: {already_ok_gps:,}")
    print(f"  Proposed fixes:     {total_fixes:,}   "
          f"(dates: {len(date_fixes)}, GPS: {len(gps_fixes)})")
    print(f"  No fix possible:    {no_fix:,}  (no date in filename or neighbours)")
    print(f"{'─'*70}")

    if total_fixes == 0:
        print("\n✅ Nothing to fix.")
        return

    ans = input(f"\nApply all {total_fixes} fix(es)? [y/N] ")
    if ans.lower() != "y":
        print("Aborted — no changes made.")
        return

    # ── Apply date fixes ──────────────────────────────────────
    if date_fixes:
        print(f"\n⚡ Writing {len(date_fixes)} date updates…")
        updated, failed = apply_date_fixes(
            [(p, dt, v) for p, dt, v, _ in date_fixes])
        print(f"   Updated: {updated}   Failed: {failed}")

    # ── Apply GPS fixes ───────────────────────────────────────
    gps_ok = gps_fail = 0
    if gps_fixes:
        print(f"\n⚡ Writing {len(gps_fixes)} GPS corrections…")
        for path, lat, old_lon, new_lon in gps_fixes:
            if apply_gps_fix(path, new_lon):
                gps_ok += 1
            else:
                gps_fail += 1
                print(f"   ✗ failed: {path.name}")
        print(f"   Updated: {gps_ok}   Failed: {gps_fail}")

    print(f"\n{'═'*70}")
    print(f"  Done.  Date fixes: {updated if date_fixes else 0}  "
          f"GPS fixes: {gps_ok if gps_fixes else 0}")
    print(f"{'═'*70}")


if __name__ == "__main__":
    main()
