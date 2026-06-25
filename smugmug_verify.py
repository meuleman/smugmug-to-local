#!/usr/bin/env python3
from __future__ import annotations
"""
smugmug_verify.py

Randomly samples N media files from a download directory, reads their EXIF
metadata in one batch exiftool call, and produces a summary showing:

  • Date distribution by year  (spot 1921 outliers immediately)
  • GPS coverage by region      (spot Mongolia outliers immediately)
  • Caption coverage
  • Missing-date / missing-GPS counts

Usage:
  python3 smugmug_verify.py --dir ./photos
  python3 smugmug_verify.py --dir ./photos --samples 2000
  python3 smugmug_verify.py --dir ./photos --all --by-album
"""

import argparse
import csv
import io
import math
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

MEDIA_EXTS = {
    ".jpg",".jpeg",".png",".heic",".gif",".dng",".raw",
    ".tiff",".tif",".mp4",".mov",".avi",".m4v",".3gp",
    ".wmv",".mts",".mkv",
}
SKIP_DIRS = {"@eadir","@eaDir","@syno","#recycle",".ds_store"}

# ── Rough geographic regions ──────────────────────────────────────
# Each entry: (label, lat_min, lat_max, lon_min, lon_max)
REGIONS = [
    ("Netherlands",      50.5, 53.5,   3.0,   7.5),
    ("Belgium/France",   49.0, 51.5,   2.0,   6.5),
    ("Germany",          47.5, 55.0,   6.0,  15.0),
    ("UK/Ireland",       50.0, 59.5,  -8.5,   2.0),
    ("Rest of Europe",   35.0, 71.0, -10.0,  35.0),
    ("US East Coast",    37.0, 47.0, -78.0, -66.0),
    ("US West Coast",    32.0, 49.0,-125.0,-114.0),
    ("US Other",         24.0, 50.0,-125.0, -66.0),
    ("Canada",           42.0, 70.0,-141.0, -52.0),
    ("South America",   -56.0, 13.0, -82.0, -34.0),
    ("Asia",             -10.0, 60.0,  60.0, 145.0),
    ("Africa",           -35.0, 38.0, -18.0,  52.0),
    ("Oceania",          -47.0,  0.0, 110.0, 180.0),
    ("Middle East",       12.0, 42.0,  25.0,  60.0),
]

SUSPICIOUS_GPS = [
    ("0,0 (null GPS)",      -1.0,  1.0,  -1.0,  1.0),
    ("Mongolia/Manchuria",  42.0, 52.0,  85.0, 130.0),
    ("Gulf of Guinea",      -5.0,  5.0,  -5.0,   5.0),
]

CURRENT_YEAR = 2026
MIN_PLAUSIBLE_YEAR = 1990


def collect_files(root: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith("@")]
        for fn in filenames:
            if Path(fn).suffix.lower() in MEDIA_EXTS:
                files.append(Path(dirpath) / fn)
    return files


VERIFY_CHUNK = 500   # files per exiftool call


def read_exif_batch(filepaths: list[Path], show_progress: bool = False) -> list[dict]:
    """Read DateTimeOriginal, GPS and Caption in chunks with -fast2.
    -fast2 skips MakerNotes and file trailers — all tags we need
    (DateTimeOriginal, GPS, IPTC Caption, XMP Description) are in the
    EXIF/IPTC/XMP headers at the start of the file, so nothing is lost."""
    tags = [
        "-DateTimeOriginal",
        "-GPSLatitude#",
        "-GPSLongitude#",
        "-Caption-Abstract",
        "-Description",
    ]

    total    = len(filepaths)
    n_chunks = (total + VERIFY_CHUNK - 1) // VERIFY_CHUNK
    all_rows: list[dict] = []

    for chunk_i in range(n_chunks):
        chunk = filepaths[chunk_i * VERIFY_CHUNK : (chunk_i + 1) * VERIFY_CHUNK]

        if show_progress and n_chunks > 1:
            pct  = 100 * chunk_i * VERIFY_CHUNK // total
            done = chunk_i * VERIFY_CHUNK
            print(f"   [{pct:3d}%  {done:,}/{total:,}]", end="\r", flush=True)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".args",
                                         delete=False, encoding="utf-8") as tf:
            tf.write("-csv\n-n\n-fast2\n")
            for t in tags:
                tf.write(t + "\n")
            for p in chunk:
                tf.write(str(p) + "\n")
            argfile = tf.name

        try:
            r = subprocess.run(["exiftool", "-@", argfile],
                               capture_output=True, text=True)
        finally:
            os.unlink(argfile)

        if r.returncode != 0 and not r.stdout:
            print(f"\n⚠️  exiftool error on chunk {chunk_i+1}: {r.stderr[:200]}")
            continue

        all_rows.extend(csv.DictReader(io.StringIO(r.stdout)))

    if show_progress and n_chunks > 1:
        print(f"   [100%  {total:,}/{total:,}]")

    return all_rows


def classify_region(lat: float, lon: float) -> str:
    for label, lat_min, lat_max, lon_min, lon_max in REGIONS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return label
    return "Other / Unknown"


def classify_suspicious(lat: float, lon: float) -> str | None:
    for label, lat_min, lat_max, lon_min, lon_max in SUSPICIOUS_GPS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return label
    return None


def bar(count: int, total: int, width: int = 30) -> str:
    filled = round(width * count / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def fmt_pct(n: int, total: int) -> str:
    return f"{n:5d}  {100*n/total:5.1f}%  " if total else f"{n:5d}         "


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir",     required=True, help="Downloaded root directory")
    parser.add_argument("--samples", type=int, default=1000,
                        help="Number of files to sample (default: 1000)")
    parser.add_argument("--seed",    type=int, default=42,
                        help="Random seed for reproducibility")
    parser.add_argument("--all", action="store_true",
                        help="Read all files, no sampling (slow on large libraries)")
    parser.add_argument("--by-album", action="store_true",
                        help="Show per-album breakdown after overall summary")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        sys.exit(f"Not found: {root}")

    # ── Collect & sample ──────────────────────────────────────────
    print(f"📁 Scanning {root}...")
    all_files = collect_files(root)
    n_total = len(all_files)
    print(f"   {n_total:,} media files found")

    all_files.sort()
    if args.all:
        sample = all_files
        print(f"   Reading all {n_total:,} files\n")
    else:
        n_sample = min(args.samples, n_total)
        if n_sample >= n_total:
            sample = all_files
            print(f"   Using all {n_total:,} files (no sampling needed)\n")
        else:
            random.seed(args.seed)
            sample = random.sample(all_files, n_sample)
            print(f"   Sampling {n_sample:,} of {n_total:,} files "
                  f"(without replacement, seed={args.seed})\n")

    # ── Read EXIF ─────────────────────────────────────────────────
    show_prog = len(sample) > VERIFY_CHUNK
    label = "chunked" if show_prog else "single batch"
    print(f"📖 Reading EXIF ({label}, -fast2)...")
    rows = read_exif_batch(sample, show_progress=show_prog)
    n_read = len(rows)
    print(f"   {n_read:,} rows returned\n")

    if not rows:
        sys.exit("No EXIF data returned — check exiftool is installed.")

    # ── Parse ─────────────────────────────────────────────────────
    years:       Counter = Counter()
    regions:     Counter = Counter()
    suspicious:  Counter = Counter()
    no_date      = 0
    no_gps       = 0
    has_caption  = 0
    bad_dates          = []
    no_gps_examples    = []
    suspicious_examples: dict = {}   # susp_key → [(filename, lat, lon), ...]
    # Per-album tracking keyed by parent directory name
    album_stats: dict = {}   # dir_name → {years, no_date, no_gps, total, suspicious}

    for row in rows:
        src = row.get("SourceFile", "")

        # Per-album init — must be first so 'a' is defined for all tracking below
        try:
            album = str(Path(src).parent.relative_to(root))
        except ValueError:
            album = Path(src).parent.name
        if album not in album_stats:
            album_stats[album] = {"years": Counter(), "no_date": 0,
                                   "no_gps": 0, "suspicious": 0,
                                   "captions": 0, "total": 0}
        a = album_stats[album]
        a["total"] += 1

        # Date
        dt = row.get("DateTimeOriginal", "").strip()
        if dt and dt not in ("-", "0000:00:00 00:00:00"):
            try:
                year = int(dt[:4])
                years[year] += 1
                a["years"][year] += 1
                if year < MIN_PLAUSIBLE_YEAR or year > CURRENT_YEAR:
                    bad_dates.append((Path(src).name, dt))
            except ValueError:
                no_date += 1
                a["no_date"] += 1
        else:
            no_date += 1
            a["no_date"] += 1

        # GPS
        lat_raw = row.get("GPSLatitude", "").strip()
        lon_raw = row.get("GPSLongitude", "").strip()
        if lat_raw and lon_raw and lat_raw not in ("-", "") and lon_raw not in ("-", ""):
            try:
                lat, lon = float(lat_raw), float(lon_raw)
                susp = classify_suspicious(lat, lon)
                if susp:
                    # Distinguish unsigned QuickTime GPS (video files with positive lon)
                    # from genuine suspicious coordinates.
                    ext = Path(src).suffix.lower()
                    if ext in {".mp4",".mov",".avi",".m4v",".3gp",".wmv",".mts",".mkv"} and lon > 0:
                        susp_key = f"unsigned QuickTime GPS ({susp})"
                    else:
                        susp_key = susp
                    suspicious[susp_key] += 1
                    a["suspicious"] += 1
                    if susp_key not in suspicious_examples:
                        suspicious_examples[susp_key] = []
                    if len(suspicious_examples[susp_key]) < 3:
                        suspicious_examples[susp_key].append(
                            (Path(src).name, lat, lon))
                else:
                    regions[classify_region(lat, lon)] += 1
            except ValueError:
                no_gps += 1
        else:
            no_gps += 1
            a["no_gps"] += 1
            if len(no_gps_examples) < 5:
                no_gps_examples.append(Path(src).name)

        # Caption
        cap = row.get("Caption-Abstract", "") or row.get("Description", "")
        if cap.strip():
            has_caption += 1
            a["captions"] += 1

    # ── Report ────────────────────────────────────────────────────
    print(f"{'═'*62}")
    print(f"  SAMPLE SUMMARY  —  {n_read:,} files from {n_total:,} total")
    print(f"{'═'*62}")

    # ── Date distribution ─────────────────────────────────────────
    print(f"\n📅 DATE DISTRIBUTION  ({no_date} files have no date)\n")
    if years:
        yr_min, yr_max = min(years), max(years)
        all_yrs = list(range(yr_min, yr_max + 1))
        yr_total = sum(years.values())

        # Group into 3-year buckets if range is large
        if yr_max - yr_min > 20:
            bucket_size = 3
            buckets: dict[str, int] = defaultdict(int)
            for yr, cnt in years.items():
                bucket = f"{(yr//bucket_size)*bucket_size}–{(yr//bucket_size)*bucket_size+bucket_size-1}"
                buckets[bucket] += cnt
            for bk in sorted(buckets):
                cnt  = buckets[bk]
                flag = "  ⚠️  SUSPICIOUS" if any(
                    int(bk.split("–")[0]) < MIN_PLAUSIBLE_YEAR or
                    int(bk.split("–")[0]) > CURRENT_YEAR
                    for _ in [1]) else ""
                print(f"  {bk:<12} {fmt_pct(cnt, yr_total)}{bar(cnt, yr_total)}{flag}")
        else:
            for yr in all_yrs:
                cnt  = years.get(yr, 0)
                flag = "  ⚠️  SUSPICIOUS" if yr < MIN_PLAUSIBLE_YEAR or yr > CURRENT_YEAR else ""
                print(f"  {yr}        {fmt_pct(cnt, yr_total)}{bar(cnt, yr_total)}{flag}")

    if bad_dates:
        print(f"\n  ⚠️  {len(bad_dates)} files with suspicious dates:")
        for fn, dt in bad_dates[:10]:
            print(f"       {fn:<50}  {dt}")
        if len(bad_dates) > 10:
            print(f"       … and {len(bad_dates)-10} more")

    # ── GPS distribution ──────────────────────────────────────────
    has_gps = n_read - no_gps
    print(f"\n🗺️  GPS COVERAGE  ({no_gps} files have no GPS,  "
          f"{has_gps} have GPS)\n")

    all_regions = dict(regions)
    all_regions.update({f"🚨 SUSPICIOUS: {k}": v for k, v in suspicious.items()})
    gps_total = sum(all_regions.values())

    if gps_total:
        for region in sorted(all_regions, key=lambda k: -all_regions[k]):
            cnt  = all_regions[region]
            flag = "  ⚠️  CHECK THESE" if "SUSPICIOUS" in region else ""
            print(f"  {region:<35} {fmt_pct(cnt, gps_total)}{bar(cnt, gps_total, 20)}{flag}")

    if no_gps_examples:
        print(f"\n  Examples of files with no GPS ({no_gps} total):")
        for fn in no_gps_examples:
            print(f"       {fn}")

    if suspicious:
        print(f"\n  Suspicious GPS detail:")
        for key, count in suspicious.items():
            is_qt = "QuickTime" in key
            print(f"\n  {'📹' if is_qt else '📍'} {key}: {count} file(s)")
            if is_qt:
                print(f"     Cause: Android MP4 stores GPS as unsigned positive longitude.")
                print(f"     The phone omitted the W/E sign — exiftool reads it as East.")
                print(f"     Fix:  exiftool -r -if \'$GPSLongitude > 50 and")
                print(f"             defined $QuickTime:GPSCoordinates\'")
                print(f"           \'-GPSLongitude<0-$GPSLongitude\' -GPSLongitudeRef=W")
                print(f"           -overwrite_original <your_dir>")
            examples = suspicious_examples.get(key, [])
            for fn, lat, lon in examples:
                print(f"     e.g. {fn}  →  {lat:.5f}°N, {lon:.5f}°E (stored)")

    # ── Caption coverage ──────────────────────────────────────────
    print(f"\n💬 CAPTION COVERAGE\n")
    print(f"  Has caption   {fmt_pct(has_caption, n_read)}"
          f"{bar(has_caption, n_read)}")
    print(f"  No caption    {fmt_pct(n_read-has_caption, n_read)}"
          f"{bar(n_read-has_caption, n_read)}")

    # ── Overall verdict ───────────────────────────────────────────
    print(f"\n{'═'*62}")
    issues = []
    if bad_dates:
        issues.append(f"⚠️  {len(bad_dates)} files with implausible dates")
    if suspicious:
        zone_names = ", ".join(f"{k} ({v})" for k, v in suspicious.items())
        issues.append(f"⚠️  {sum(suspicious.values())} files with suspicious GPS: {zone_names}")
    if no_date > n_read * 0.30:
        issues.append(f"⚠️  {no_date} files ({100*no_date//n_read}%) have no date")
    if not issues:
        print("  ✅ No obvious problems detected in sample")
    else:
        print("  Issues found:")
        for issue in issues:
            print(f"    {issue}")
    print(f"{'═'*62}\n")

    # ── Per-album breakdown ────────────────────────────────────────
    if args.by_album and album_stats:
        print(f"{'═'*78}")
        print(f"  PER-ALBUM BREAKDOWN  ({len(album_stats)} albums in sample)")
        print(f"{'═'*78}")
        print(f"  {'Album':<38} {'N':>5}  {'Dates':<11}  "
              f"{'GPS%':>4}  {'Cap%':>4}  Issues")
        print(f"  {'─'*38}  {'─'*5}  {'─'*11}  {'─'*4}  {'─'*4}  {'─'*20}")

        # Sort paths so parent always appears before children
        for album in sorted(album_stats, key=lambda p: p.replace(os.sep, "\x00")):
            a       = album_stats[album]
            n       = a["total"]
            yr      = a["years"]
            no_gp   = a["no_gps"]
            susp    = a["suspicious"]
            caps    = a["captions"]
            has_gps = n - no_gp

            # Indentation based on path depth
            depth   = len(Path(album).parts) - 1
            indent  = "  " * depth
            label   = Path(album).name
            display = (indent + label)[:38]

            yr_range = ("no date" if not yr
                        else str(min(yr)) if min(yr)==max(yr)
                        else f"{min(yr)}–{max(yr)}")

            gps_pct = 100 * has_gps / n if n else 0
            cap_pct = 100 * caps       / n if n else 0

            issues = []
            if no_gp  > 0:   issues.append(f"⚠️ {no_gp} no-GPS")
            if susp   > 0:   issues.append(f"🚨 {susp} bad-GPS")
            if a["no_date"] > 0: issues.append(f"📅 {a['no_date']} no-date")
            if any(y < MIN_PLAUSIBLE_YEAR or y > CURRENT_YEAR for y in yr):
                issues.append("⚠️ bad-year")
            issue_str = "  ".join(issues)

            print(f"  {display:<38}  {n:>5}  {yr_range:<11}  "
                  f"{gps_pct:>3.0f}%  {cap_pct:>3.0f}%  {issue_str}")
        print()


if __name__ == "__main__":
    main()
