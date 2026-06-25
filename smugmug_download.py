#!/usr/bin/env python3
from __future__ import annotations
"""
smugmug_download.py  —  Download SmugMug photos and videos with full metadata

Downloads albums from SmugMug, writing captions, GPS and timestamps into
EXIF/IPTC/XMP as the files land on disk — so no post-hoc fix scripts are needed.

Metadata applied during download
─────────────────────────────────
  Captions   Written to EXIF:ImageDescription, IPTC:Caption-Abstract, XMP:Description
  GPS        Read from original file EXIF Ref tags first; falls back to SmugMug API
             GPS (only when longitude is negative, i.e. unambiguously West/South).
  Timestamps Derived from filename when EXIF date is absent or clearly wrong
             (after CUTOFF_DATE).  Patterns handled:
               YYYYMMDD_HHMMSS   Android standard
               BURST+YYYYMMDDHHMMSS  Samsung burst (no separator)
               YYYY-MM-DDHH_MM_SS    Run-together date/time
               YYYYMMDD_HHMM     Four-digit time (no seconds)
               Screen Shot YYYY-MM-DD at H.MM.SS [AM|PM]  macOS (URL-decoded)
               Screenshot_YYYY-MM-DD-HH-MM-SS
  Neighbour  After each album is fully downloaded, any file still lacking a
  dates      date receives the date of its nearest NNN_-indexed neighbour.

Ordering prefix
───────────────
  Albums with SortMethod=Position AND captions get an NNN_ prefix on every
  filename, preserving SmugMug's manual curation order in the filesystem.

Requirements:
  pip install rauth requests

Configuration:
  Edit config.json — see config.json.example.

Usage:
  python3 smugmug_download.py --output-dir /Volumes/photo --all
  python3 smugmug_download.py --output-dir /Volumes/photo --folder "Italy Trips"
  python3 smugmug_download.py --output-dir /Volumes/photo --album "Rome 2019"
  python3 smugmug_download.py --output-dir /Volumes/photo --all --redownload
"""

from __future__ import annotations
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from urllib.parse import unquote

try:
    from rauth import OAuth1Session
except ImportError:
    sys.exit("pip install rauth")
try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

CONFIG_JSON = "config.json"
API_ORIGIN  = "https://api.smugmug.com"
AUDIT_TSV   = "smugmug_audit.tsv"

# Files with DateTimeOriginal at or after this date are assumed to have
# the download timestamp rather than the capture date, and are fixed.
CUTOFF_DATE = "2026:01:01"

# Neighbour interpolation: max index gap between a file and its dated neighbour
INTERP_MAX_GAP = 50

# ─────────────────────────────────────────────────────────────────
# SESSION
# ─────────────────────────────────────────────────────────────────

def load_session() -> OAuth1Session:
    try:
        with open(CONFIG_JSON) as fh:
            cfg = json.load(fh)
    except IOError:
        sys.exit(f"Cannot open {CONFIG_JSON}")
    for k in ("key", "secret", "access_token", "access_token_secret"):
        if not cfg.get(k):
            sys.exit(f"Missing '{k}' in {CONFIG_JSON}")
    return OAuth1Session(cfg["key"], cfg["secret"],
                         access_token=cfg["access_token"],
                         access_token_secret=cfg["access_token_secret"])


def api_get(session, uri, params=None):
    r = session.get(API_ORIGIN + uri,
                    headers={"Accept": "application/json"},
                    params=params or {})
    r.raise_for_status()
    return json.loads(r.text).get("Response", {})


def get_all_pages(session, uri, response_key, page_size=100):
    results, start = [], 1
    while True:
        r = api_get(session, uri, {"count": page_size, "start": start})
        page = r.get(response_key, [])
        if not page:
            break
        results.extend(page)
        if r.get("Pages", {}).get("NextPage"):
            start += page_size
        else:
            break
    return results

# ─────────────────────────────────────────────────────────────────
# AUDIT TSV
# ─────────────────────────────────────────────────────────────────

_audit_caption_pct: dict[str, int] = {}
_audit_sort_method: dict[str, str] = {}
_audit_total:       dict[str, int] = {}


def load_audit_tsv():
    if not os.path.exists(AUDIT_TSV):
        print(f"  ⚠️  {AUDIT_TSV} not found — ordering prefix and progress % disabled")
        return
    with open(AUDIT_TSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            path = row.get("Path", row.get("Name", "")).strip()
            if not path:
                continue
            try:
                _audit_caption_pct[path] = int(row.get("Caption%") or 0)
            except (ValueError, TypeError):
                pass
            _audit_sort_method[path] = row.get("SortMethod", "").strip()
            try:
                _audit_total[path] = int(row.get("Total") or 0)
            except (ValueError, TypeError):
                pass
    manual = sum(1 for v in _audit_sort_method.values() if v == "Position")
    print(f"  📋 Audit: {len(_audit_caption_pct)} albums  ({manual} manually ordered)")

# ─────────────────────────────────────────────────────────────────
# ALBUM SELECTION
# ─────────────────────────────────────────────────────────────────

def album_needs_prefix(name: str, folder_path: str = "",
                       force_include: bool = False,
                       min_caption_pct: int = 1) -> bool:
    """NNN_ prefix is applied when the album is manually ordered AND captioned."""
    if force_include:
        return True
    full_path = f"{folder_path}/{name}" if folder_path else name
    has_captions = _audit_caption_pct.get(full_path, 0) >= max(min_caption_pct, 1)
    is_manual    = _audit_sort_method.get(full_path, "") == "Position"
    return has_captions and is_manual


def album_matches(name: str, folder_path: str = "", album_filter: str = "",
                  folder_filter: list = [], min_caption_pct: int = 1) -> bool:
    if album_filter:
        return album_filter.lower() in name.lower()
    if folder_filter:
        return False  # only albums inside matched folders (via force_include)
    if min_caption_pct > 0:
        full_path = f"{folder_path}/{name}" if folder_path else name
        return _audit_caption_pct.get(full_path, 0) >= min_caption_pct
    return True


def folder_matches(name: str, folder_filter: list = []) -> bool:
    return any(name.lower().strip() == f.lower().strip() for f in folder_filter)


def collect_albums(session, node_uri, args, albums=None, smug_folder_path="",
                   force_include=False):
    if albums is None:
        albums = []
    children     = get_all_pages(session, node_uri + "!children", "Node")
    depth_indent = "  " + "  " * smug_folder_path.count("/")
    ffilter      = getattr(args, "folder", []) or []
    min_cap      = getattr(args, "min_caption_pct", 1)

    for child in children:
        node_type  = child.get("Type", "")
        name       = child.get("Name", "")
        child_path = f"{smug_folder_path}/{name}" if smug_folder_path else name

        if node_type == "Album":
            include = (force_include or getattr(args, "all", False) or
                       album_matches(name, smug_folder_path,
                                     getattr(args, "album", ""), ffilter, min_cap))
            prefix  = album_needs_prefix(name, smug_folder_path, force_include, min_cap)
            marker  = "✅" if include else "⏭️ "
            print(f"{depth_indent}[album] {marker} {name}"
                  f"{' [prefix]' if (include and prefix) else ''}")
            if include:
                album_uri = child.get("Uris", {}).get("Album", {}).get("Uri")
                if album_uri:
                    est = _audit_total.get(child_path, 0)
                    albums.append({"name": name, "uri": album_uri,
                                   "folder_path": smug_folder_path,
                                   "use_prefix": prefix, "image_count": est})
        elif node_type == "Folder":
            child_force = force_include or folder_matches(name, ffilter)
            print(f"{depth_indent}[folder] 📁 {name}"
                  f"{' ← --folder match' if (folder_matches(name, ffilter) and not force_include) else ''}"
                  f"{' [force]' if child_force else ''}")
            collect_albums(session, child["Uri"], args, albums, child_path, child_force)
        else:
            print(f"{depth_indent}[{node_type}] {name}")
    return albums

# ─────────────────────────────────────────────────────────────────
# FILE HELPERS
# ─────────────────────────────────────────────────────────────────

_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".m4v", ".3gp", ".wmv", ".mts", ".mkv"}
_MEDIA_EXTS = _VIDEO_EXTS | {".jpg", ".jpeg", ".png", ".heic", ".gif",
                               ".dng", ".raw", ".tiff", ".tif"}


def is_video(img) -> bool:
    fn = img.get("FileName", "") if isinstance(img, dict) else str(img)
    return Path(fn).suffix.lower() in _VIDEO_EXTS


def ext_from_url(url: str) -> str:
    p = url.split("?")[0].split("/")[-1]
    return Path(p).suffix.lower() or ".jpg"


def get_image_download_url(session, img: dict) -> str:
    uris = img.get("Uris", {})
    # Videos: try LargestVideo first
    if is_video(img):
        for key in ("LargestVideo", "VideoUrl"):
            uri_obj = uris.get(key, {})
            if uri_obj.get("Url"):
                return uri_obj["Url"]
            if uri_obj.get("Uri"):
                try:
                    r = api_get(session, uri_obj["Uri"])
                    url = (r.get(key, {}).get("Url") or
                           r.get("Video", {}).get("Url") or "")
                    if url:
                        return url
                except Exception:
                    pass
    # Images: try LargestImage → ImageDownload → ArchivedUri
    for key in ("LargestImage",):
        uri_obj = uris.get(key, {})
        if uri_obj.get("Uri"):
            try:
                r   = api_get(session, uri_obj["Uri"])
                url = r.get("LargestImage", {}).get("Url", "")
                if url:
                    return url
            except Exception:
                pass
    for key in ("ImageDownload",):
        uri_obj = uris.get(key, {})
        if uri_obj.get("Uri"):
            try:
                r   = api_get(session, uri_obj["Uri"])
                url = r.get("ImageDownload", {}).get("Url", "")
                if url:
                    return url
            except Exception:
                pass
    archived = img.get("ArchivedUri", "")
    if archived:
        return archived
    return ""

# ─────────────────────────────────────────────────────────────────
# DATE EXTRACTION FROM FILENAME
# ─────────────────────────────────────────────────────────────────

_PREFIX_RE = re.compile(r"^\d{1,4}_")
_INDEX_RE  = re.compile(r"^(\d{1,4})_")

_DATE_PATTERNS = [
    # Samsung BURST: BURST20180408154555 (date+time concatenated)
    (re.compile(r"BURST(\d{4})(0[1-9]|1[0-2])(\d{2})(\d{2})(\d{2})(\d{2})"),
     "BURST"),
    # Date+time run together: 2019-05-2422_10_29
    (re.compile(r"(\d{4})-(0[1-9]|1[0-2])-(\d{2})(\d{2})[_\-](\d{2})[_\-](\d{2})"),
     "runon"),
    # Standard Android: 20180712_192247  (also VID_/IMG_/PXL_ prefixes)
    (re.compile(r"(?:^|[^0-9])(\d{4})(0[1-9]|1[0-2])(\d{2})[_\-](\d{2})(\d{2})(\d{2})"),
     "android"),
    # 4-digit time: 20140712_2052_img6821
    (re.compile(r"(\d{4})(0[1-9]|1[0-2])(\d{2})[_\-](\d{2})(\d{2})(?:[_\-][^0-9]|[_\-][A-Za-z]|$)"),
     "hhmm"),
    # macOS screenshot (after URL-decode): 2013-01-11 at 3.53.42 PM
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _]at[ _](\d{1,2})\.(\d{2})\.(\d{2})[ _]?(AM|PM)?",
               re.IGNORECASE), "macos"),
    # Screenshot_2013-01-11-15-53-42
    (re.compile(r"[Ss]creenshot[_\-](\d{4})[_\-](0[1-9]|1[0-2])[_\-](\d{2})[_\-](\d{2})[_\-](\d{2})[_\-](\d{2})"),
     "screenshot"),
    # WhatsApp: IMG-20180712-WA0042
    (re.compile(r"(?:IMG|VID)[_\-](\d{4})(0[1-9]|1[0-2])(\d{2})[_\-]WA\d+"),
     "whatsapp"),
]

_CUTOFF_DT  = None
_MIN_DT     = None

def _dt_limits():
    global _CUTOFF_DT, _MIN_DT
    if _CUTOFF_DT is None:
        from datetime import datetime
        _CUTOFF_DT = datetime.strptime(CUTOFF_DATE, "%Y:%m:%d")
        _MIN_DT    = datetime(1990, 1, 1)
    return _CUTOFF_DT, _MIN_DT


def _url_decode(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = unquote(s)
    return s


def extract_date_from_filename(filename: str):
    """Try to extract a plausible (year, month, day, hour, min, sec) tuple."""
    from datetime import datetime
    cutoff, min_dt = _dt_limits()
    raw     = Path(filename).stem
    decoded = _url_decode(raw)
    stems   = list({raw, decoded, _PREFIX_RE.sub("", raw),
                    _PREFIX_RE.sub("", decoded)})

    for stem in stems:
        for pat, label in _DATE_PATTERNS:
            m = pat.search(stem)
            if not m:
                continue
            try:
                g = m.groups()
                if label == "macos":
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    h, mi, s = int(g[3]), int(g[4]), int(g[5])
                    ap = g[6].upper() if len(g) > 6 and g[6] else None
                    if ap == "PM" and h < 12: h += 12
                    elif ap == "AM" and h == 12: h = 0
                elif label == "whatsapp":
                    y, mo, d, h, mi, s = int(g[0]), int(g[1]), int(g[2]), 0, 0, 0
                elif label == "hhmm":
                    y, mo, d, h, mi, s = int(g[0]),int(g[1]),int(g[2]),int(g[3]),int(g[4]),0
                else:
                    y, mo, d, h, mi, s = (int(x) for x in g[:6])
                dt = datetime(y, mo, d, h, mi, s)
                if min_dt <= dt < cutoff:
                    return y, mo, d, h, mi, s
            except (ValueError, IndexError):
                continue
    return None


def extract_index(filename: str):
    m = _INDEX_RE.match(Path(filename).name)
    return int(m.group(1)) if m else None


def _valid_date(y, mo, d, h, mi, s) -> bool:
    try:
        from datetime import datetime
        cutoff, min_dt = _dt_limits()
        return min_dt <= datetime(y, mo, d, h, mi, s) < cutoff
    except ValueError:
        return False


def _ampm_to_24h(h, mi, s, mer):
    if mer and mer.upper() == "PM" and h < 12:  h += 12
    if mer and mer.upper() == "AM" and h == 12: h = 0
    return h, mi, s

# ─────────────────────────────────────────────────────────────────
# GPS HELPERS
# ─────────────────────────────────────────────────────────────────

def read_gps_from_file(filepath: Path):
    """Read GPS via explicit EXIF Ref tags — correct for any location."""
    r = subprocess.run(
        ["exiftool", "-s3", "-n",
         "-EXIF:GPSLatitude", "-EXIF:GPSLongitude",
         "-EXIF:GPSLatitudeRef", "-EXIF:GPSLongitudeRef",
         str(filepath)], capture_output=True, text=True)
    vals = [v.strip() for v in r.stdout.strip().splitlines() if v.strip()]
    if len(vals) < 4:
        return None
    try:
        raw_lat, raw_lon = float(vals[0]), float(vals[1])
        lat_ref, lon_ref = vals[2].upper(), vals[3].upper()
    except (ValueError, IndexError):
        return None
    if raw_lat == 0.0 and raw_lon == 0.0:
        return None
    if lat_ref not in ("N", "S") or lon_ref not in ("E", "W"):
        return None
    return (-raw_lat if lat_ref == "S" else raw_lat,
            -raw_lon if lon_ref == "W" else raw_lon)


def api_gps_signed(api_lat, api_lon):
    """Trust API GPS only when longitude is negative (unambiguously West)."""
    try:
        lat, lon = float(api_lat), float(api_lon)
    except (ValueError, TypeError):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if lon >= 0:
        return None  # positive = ambiguous (could be East or unsigned West)
    return lat, lon

# ─────────────────────────────────────────────────────────────────
# METADATA HELPERS
# ─────────────────────────────────────────────────────────────────

def build_metadata_args(caption, lat=None, lon=None) -> list:
    args = []
    if caption and caption.strip():
        c = caption.strip()
        args += [f"-IPTC:Caption-Abstract={c}",
                 f"-XMP:Description={c}",
                 f"-EXIF:ImageDescription={c}"]
    if lat is not None and lon is not None:
        args += [f"-GPSLatitude={abs(lat)}",
                 f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                 f"-GPSLongitude={abs(lon)}",
                 f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    return args


def build_date_args(filename: str, is_video_file: bool) -> tuple[list, str]:
    result = extract_date_from_filename(filename)
    if not result:
        return [], ""
    y, mo, d, h, mi, s = result
    ts = f"{y:04d}:{mo:02d}:{d:02d} {h:02d}:{mi:02d}:{s:02d}"
    if is_video_file:
        return [f"-XMP:DateTimeOriginal={ts}", f"-CreateDate={ts}"], ts
    return [f"-DateTimeOriginal={ts}", f"-DateTimeDigitized={ts}",
            f"-IPTC:DateCreated={y:04d}:{mo:02d}:{d:02d}",
            f"-IPTC:TimeCreated={h:02d}:{mi:02d}:{s:02d}"], ts


def read_datetime_original(filepath: Path) -> str:
    r = subprocess.run(["exiftool", "-s3", "-DateTimeOriginal", str(filepath)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def date_needs_fixing(filepath: Path) -> bool:
    dt = read_datetime_original(filepath)
    if not dt:
        return True
    if dt == "0000:00:00 00:00:00":
        return True
    return dt[:10].replace(":", "") >= CUTOFF_DATE[:10].replace(":", "")


def fix_missing_eoi(filepath: Path) -> bool:
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        if data[-2:] != b"\xff\xd9":
            with open(filepath, "ab") as f:
                f.write(b"\xff\xd9")
            return True
    except Exception:
        pass
    return False


def strip_corrupt_embedded_images(filepath: Path):
    try:
        r = subprocess.run(
            ["exiftool", "-overwrite_original", "-q",
             "-OtherImageStart=", "-OtherImageLength=", str(filepath)],
            capture_output=True, text=True)
    except Exception:
        pass


def write_metadata(filepath: Path, tag_args: list) -> bool:
    if not tag_args:
        return True
    ext = filepath.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        fix_missing_eoi(filepath)
        strip_corrupt_embedded_images(filepath)
    r = subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-m"] + tag_args + [str(filepath)],
        capture_output=True, text=True)
    return r.returncode == 0


def download_file(url: str, dest_path: Path):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)

# ─────────────────────────────────────────────────────────────────
# NEIGHBOUR DATE INTERPOLATION (post-album pass)
# ─────────────────────────────────────────────────────────────────

def _batch_read_dates(files: list[Path]) -> dict[Path, str]:
    """Read DateTimeOriginal for a list of files in one exiftool call."""
    if not files:
        return {}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args",
                                     delete=False, encoding="utf-8") as tf:
        tf.write("-csv\n-fast2\n-DateTimeOriginal\n-CreateDate\n")
        for f in files:
            tf.write(str(f) + "\n")
        argfile = tf.name
    try:
        r = subprocess.run(["exiftool", "-@", argfile],
                           capture_output=True, text=True)
    finally:
        os.unlink(argfile)
    result: dict[Path, str] = {}
    for row in csv.DictReader(io.StringIO(r.stdout)):
        p  = Path(row.get("SourceFile", ""))
        dt = (row.get("DateTimeOriginal") or row.get("CreateDate") or "").strip()
        result[p] = dt
    return result


def _interpolate_neighbour(idx: int, dir_known: dict) -> tuple | None:
    """Return (y,mo,d,0,0,0) from nearest indexed neighbour, or None."""
    if not dir_known:
        return None
    known  = sorted(dir_known.items())
    lower  = [(i, dt) for i, dt in known if i < idx]
    upper  = [(i, dt) for i, dt in known if i > idx]
    lo     = lower[-1] if lower else None
    hi     = upper[0]  if upper else None
    if lo and (idx - lo[0]) > INTERP_MAX_GAP: lo = None
    if hi and (hi[0] - idx) > INTERP_MAX_GAP: hi = None
    if lo and hi:
        use = lo[1] if lo[1].date() == hi[1].date() \
              else (lo[1] if (idx - lo[0]) <= (hi[0] - idx) else hi[1])
    elif lo:
        use = lo[1]
    elif hi:
        use = hi[1]
    else:
        return None
    return use.year, use.month, use.day, 0, 0, 0


def neighbour_date_pass(album_dir: Path, downloaded_files: list[Path],
                        is_video_fn) -> int:
    """After album download: fix any remaining missing dates via neighbour index.
    Returns count of files fixed."""
    from datetime import datetime
    cutoff, min_dt = _dt_limits()

    # Batch-read current dates for all files
    existing = _batch_read_dates(downloaded_files)

    # Build index→datetime map from files that have valid dates
    dir_known: dict[int, datetime] = {}
    for path in downloaded_files:
        dt_str = existing.get(path, "")
        # Accept date from EXIF or derived from filename
        tup = None
        if dt_str and dt_str not in ("0000:00:00 00:00:00", ""):
            try:
                dt = datetime.strptime(dt_str[:19], "%Y:%m:%d %H:%M:%S")
                if min_dt <= dt < cutoff:
                    tup = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
            except ValueError:
                pass
        if tup is None:
            tup = extract_date_from_filename(path.name)
        if tup is not None:
            idx = extract_index(path.name)
            if idx is not None:
                y, mo, d, h, mi, s = tup
                try:
                    dir_known[idx] = datetime(y, mo, d, h, mi, s)
                except ValueError:
                    pass

    # Find files that still need a date
    fixed = 0
    for path in downloaded_files:
        dt_str = existing.get(path, "")
        has_valid = False
        if dt_str and dt_str not in ("0000:00:00 00:00:00", ""):
            try:
                dt = datetime.strptime(dt_str[:19], "%Y:%m:%d %H:%M:%S")
                has_valid = min_dt <= dt < cutoff
            except ValueError:
                pass
        if has_valid:
            continue
        # Already has a date from filename? Skip (was written inline)
        if extract_date_from_filename(path.name) is not None:
            continue
        # Try neighbour interpolation
        idx  = extract_index(path.name)
        tup  = _interpolate_neighbour(idx, dir_known) if idx is not None else None
        if tup is None:
            continue
        y, mo, d, h, mi, s = tup
        ts = f"{y:04d}:{mo:02d}:{d:02d} {h:02d}:{mi:02d}:{s:02d}"
        darg = f"{y:04d}:{mo:02d}:{d:02d}"
        targ = f"{h:02d}:{mi:02d}:{s:02d}"
        if is_video_fn(path.name):
            date_args = [f"-XMP:DateTimeOriginal={ts}", f"-CreateDate={ts}"]
        else:
            date_args = [f"-DateTimeOriginal={ts}", f"-DateTimeDigitized={ts}",
                         f"-IPTC:DateCreated={darg}", f"-IPTC:TimeCreated={targ}"]
        if write_metadata(path, date_args):
            fixed += 1
            print(f"  [neighbour-date({ts[:10]})]", end="")
    return fixed

# ─────────────────────────────────────────────────────────────────
# ALBUM DOWNLOAD
# ─────────────────────────────────────────────────────────────────

def process_album(session, album_name: str, album_uri: str, args,
                  output_dir=None, use_prefix=False, skip_existing=True) -> dict:
    counts = {"downloaded": 0, "skipped": 0, "failed": 0, "meta_fixed": 0}
    base   = Path(output_dir) if output_dir else Path(".")
    album_dir = base / album_name
    album_dir.mkdir(parents=True, exist_ok=True)

    images = get_all_pages(session, album_uri + "!images", "AlbumImage",
                           page_size=200)
    total  = len(images)
    downloaded_files: list[Path] = []

    for idx_1, img in enumerate(images, 1):
        filename = img.get("FileName", f"photo_{idx_1}.jpg")
        caption  = img.get("Caption", "")
        kind     = "video" if is_video(img) else "photo"

        try:
            api_lat = float(img.get("Latitude",  "") or "x")
            api_lon = float(img.get("Longitude", "") or "x")
        except (ValueError, TypeError):
            api_lat = api_lon = None

        # ── Build output filename ─────────────────────────────
        dl_url = get_image_download_url(session, img)
        if not dl_url:
            print(f"\n  ⚠️  [{idx_1}/{total}] {filename}  — no download URL")
            counts["failed"] += 1
            continue

        ext = ext_from_url(dl_url) or Path(filename).suffix.lower() or ".jpg"
        stem = Path(filename).stem
        if use_prefix:
            dest_name = f"{idx_1:03d}_{stem}{ext}"
        else:
            dest_name = f"{stem}{ext}"
        dest_path = album_dir / dest_name

        if skip_existing and dest_path.exists():
            counts["skipped"] += 1
            downloaded_files.append(dest_path)
            continue

        # ── Download ──────────────────────────────────────────
        pct = 100 * idx_1 // total
        print(f"  [{pct:3d}%  {idx_1}/{total}] {dest_name}", end="", flush=True)
        try:
            download_file(dl_url, dest_path)
        except Exception as e:
            print(f"  ✗ {e}")
            counts["failed"] += 1
            continue
        counts["downloaded"] += 1
        downloaded_files.append(dest_path)

        # ── Per-file metadata ─────────────────────────────────
        meta_args  = []
        meta_notes = []

        # GPS (file Ref-tags first, then signed API GPS)
        gps_lat = gps_lon = None
        file_gps = read_gps_from_file(dest_path)
        if file_gps:
            gps_lat, gps_lon = file_gps
            meta_notes.append("gps✓")
        else:
            api_coords = api_gps_signed(api_lat, api_lon)
            if api_coords:
                gps_lat, gps_lon = api_coords
                meta_notes.append("gps-api")
            elif api_lat is not None and api_lon is not None:
                meta_notes.append("gps-skip")

        # Caption + GPS
        cap_gps = build_metadata_args(caption, gps_lat, gps_lon)
        if cap_gps:
            meta_args += cap_gps
            if caption.strip():
                meta_notes.append("caption")

        # Date from filename
        if date_needs_fixing(dest_path):
            date_args, ts = build_date_args(filename, kind == "video")
            if date_args:
                meta_args += date_args
                meta_notes.append(f"date({ts[:10]})")

        if meta_args:
            ok = write_metadata(dest_path, meta_args)
            if ok and meta_notes:
                print(f"  [{', '.join(meta_notes)}]", end="")
            elif not ok:
                print(f"  ⚠️ metadata failed", end="")
            counts["meta_fixed"] += 1

        print()

    # ── Post-album: neighbour date interpolation ──────────────
    if downloaded_files:
        fixed = neighbour_date_pass(album_dir, downloaded_files,
                                    lambda fn: Path(fn).suffix.lower() in _VIDEO_EXTS)
        if fixed:
            print(f"  ↳ {fixed} file(s) got date from nearest neighbour")
            counts["meta_fixed"] += fixed

    return counts

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SmugMug photo/video exporter")
    parser.add_argument("--output-dir",      required=True,
                        help="Root directory to save files to")
    parser.add_argument("--folder",          action="append", default=[],
                        metavar="FOLDER",
                        help="SmugMug folder to download in full (repeatable)")
    parser.add_argument("--album",           default="",
                        help="Download albums whose name contains TEXT")
    parser.add_argument("--min-caption-pct", type=int, default=1,
                        help="Min %% captioned images to include album (default: 1)")
    parser.add_argument("--all",             action="store_true",
                        help="Download entire library")
    parser.add_argument("--redownload",      action="store_true",
                        help="Re-download files that already exist")
    args       = parser.parse_args()
    output_dir = args.output_dir

    flags = []
    if args.redownload: flags.append("♻️  --redownload")
    if args.all:        flags.append("📚 --all")
    if args.folder:     flags.append(f"📁 folders: {args.folder}")
    if args.album:      flags.append(f"🔍 album: {args.album!r}")
    if flags:
        print("  ".join(flags) + "\n")

    print("📋 Loading audit data…")
    load_audit_tsv()

    print("\n🔑 Connecting to SmugMug…")
    session = load_session()
    user    = api_get(session, "/api/v2!authuser")
    nick    = user.get("User", {}).get("NickName", "?")
    print(f"   Authenticated as: {nick}")
    root_node_uri = user["User"]["Uris"]["Node"]["Uri"]

    if args.all:
        fd = "entire library"
    elif args.folder:
        fd = f"folders: {', '.join(args.folder)}"
    elif args.album:
        fd = f"album name contains {args.album!r}"
    else:
        fd = f"caption% ≥ {args.min_caption_pct}"
    print(f"\n🔍 Discovering albums [{fd}]…")
    albums = collect_albums(session, root_node_uri, args)

    if not albums:
        print("No matching albums found. Check --folder / --album / --all flags.")
        return

    total_expected = sum(a.get("image_count", 0) for a in albums)
    print(f"\n{'─'*60}")
    print(f"  {len(albums)} album(s)  ·  ~{total_expected} files")
    print(f"  Output: {output_dir}")
    print(f"{'─'*60}")
    action = "Re-download" if args.redownload else "Download"
    confirm = input(f"\n{action} {len(albums)} album(s) to {output_dir}? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    total_dl = total_skip = total_fail = 0
    for i, album in enumerate(albums, 1):
        folder_path = album.get("folder_path", "")
        out_subdir  = os.path.join(output_dir, folder_path) if folder_path else output_dir
        print(f"\n[{i}/{len(albums)}] {album['name']}"
              f"  ({album.get('image_count', '?')} files)")
        counts = process_album(
            session, album["name"], album["uri"], args,
            output_dir=out_subdir,
            use_prefix=album.get("use_prefix", False),
            skip_existing=not args.redownload,
        )
        total_dl   += counts["downloaded"]
        total_skip += counts["skipped"]
        total_fail += counts["failed"]

    print(f"\n{'═'*60}")
    print(f"  Downloaded: {total_dl}  Skipped: {total_skip}  Failed: {total_fail}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
