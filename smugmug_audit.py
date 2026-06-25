#!/usr/bin/env python3
from __future__ import annotations
"""
smugmug_audit.py  —  SmugMug library inventory

Scans every album in your SmugMug account and reports:
  • Sort method  (Position = manually curated ordering)
  • Caption coverage
  • GPS coverage
  • Video count

Output:  prints a table to stdout  AND  saves to smugmug_audit.tsv.
The TSV is consumed by smugmug_download.py (ordering prefix logic
and progress estimates).

Requirements:
  pip install rauth

Configuration:
  Edit config.json — see config.json.example.

Usage:
  python3 smugmug_audit.py
"""

import json
import sys
import time
from pathlib import Path

try:
    from rauth import OAuth1Session
except ImportError:
    sys.exit("Missing dependency:  pip install rauth")

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

CONFIG_JSON = "config.json"
API_ORIGIN  = "https://api.smugmug.com"

# Images sampled per album for caption/GPS counts.
# 0 = check every image (accurate but slow for large albums).
# 100 is a fast, reliable estimate.
SAMPLE_SIZE = 100

OUTPUT_TSV  = "smugmug_audit.tsv"

# ─────────────────────────────────────────────────────────────────


def load_config() -> OAuth1Session:
    try:
        with open(CONFIG_JSON) as fh:
            cfg = json.load(fh)
    except IOError:
        sys.exit(f"Cannot open {CONFIG_JSON} — copy config.json.example and fill in your keys.")
    for key in ("key", "secret", "access_token", "access_token_secret"):
        if not cfg.get(key):
            sys.exit(f"Missing '{key}' in {CONFIG_JSON}")
    return OAuth1Session(
        cfg["key"], cfg["secret"],
        access_token=cfg["access_token"],
        access_token_secret=cfg["access_token_secret"],
    )


def api_get(session, uri, params=None):
    r = session.get(API_ORIGIN + uri,
                    headers={"Accept": "application/json"},
                    params=params or {})
    r.raise_for_status()
    return json.loads(r.text).get("Response", {})


def get_all_pages(session, uri, key, page_size=100):
    results, start = [], 1
    while True:
        r = api_get(session, uri, {"count": page_size, "start": start})
        page = r.get(key, [])
        if not page:
            break
        results.extend(page)
        if r.get("Pages", {}).get("NextPage"):
            start += page_size
        else:
            break
    return results


def collect_albums(session, node_uri, albums=None, path=""):
    """Recursively collect every album with its SmugMug folder path."""
    if albums is None:
        albums = []
    for child in get_all_pages(session, node_uri + "!children", "Node"):
        name       = child.get("Name", "")
        child_path = f"{path}/{name}" if path else name
        if child.get("Type") == "Album":
            uri = child.get("Uris", {}).get("Album", {}).get("Uri")
            if uri:
                albums.append({"name": name, "path": child_path,
                               "node_uri": child["Uri"], "album_uri": uri})
        elif child.get("Type") == "Folder":
            collect_albums(session, child["Uri"], albums, child_path)
    return albums


def _has_gps(img: dict) -> bool:
    """True when SmugMug holds a non-zero GPS coordinate for this image.
    Reflects data *availability* — not whether the sign is correct."""
    try:
        lat = float(img.get("Latitude") or "0")
        lon = float(img.get("Longitude") or "0")
    except (ValueError, TypeError):
        return False
    return abs(lat) > 0.001 or abs(lon) > 0.001


_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".m4v", ".wmv", ".3gp", ".mts", ".mkv"}


def audit_album(session, album: dict) -> dict:
    """Return one audit row for an album."""
    name = album["name"]
    try:
        alb = api_get(session, album["album_uri"]).get("Album", {})
        sort_method = alb.get("SortMethod", "unknown")
        image_count = alb.get("ImageCount", "?")
    except Exception as e:
        return {**album, "sort_method": "ERROR", "sort_dir": "",
                "total": 0, "sampled": 0, "captioned": 0,
                "caption_pct": 0, "gps_count": 0, "gps_pct": 0,
                "videos": 0, "error": str(e)}

    try:
        if SAMPLE_SIZE == 0:
            images = get_all_pages(session, album["album_uri"] + "!images",
                                   "AlbumImage", page_size=500)
        else:
            r      = api_get(session, album["album_uri"] + "!images",
                             {"count": SAMPLE_SIZE, "start": 1})
            images = r.get("AlbumImage", [])

        sampled   = len(images)
        captioned = sum(1 for i in images if i.get("Caption", "").strip())
        gps_count = sum(1 for i in images if _has_gps(i))
        videos    = sum(1 for i in images
                        if Path("." + i.get("FileName", "").rsplit(".", 1)[-1].lower())
                           in _VIDEO_EXTS)
        caption_pct = round(100 * captioned / sampled) if sampled else 0
        gps_pct     = round(100 * gps_count / sampled) if sampled else 0
    except Exception:
        sampled = captioned = gps_count = videos = caption_pct = gps_pct = 0

    time.sleep(0.15)

    return {
        "name": name, "path": album["path"],
        "sort_method": sort_method, "total": image_count,
        "sampled": sampled, "captioned": captioned, "caption_pct": caption_pct,
        "gps_count": gps_count, "gps_pct": gps_pct, "videos": videos,
    }


def main():
    print("Connecting to SmugMug…")
    session   = load_config()
    user_resp = api_get(session, "/api/v2!authuser")
    username  = user_resp.get("User", {}).get("NickName", "?")
    root_node = user_resp["User"]["Uris"]["Node"]["Uri"]
    print(f"Authenticated as: {username}\n")

    print("Discovering albums…")
    albums = collect_albums(session, root_node)
    print(f"Found {len(albums)} albums.  Auditing…\n")

    results = []
    for i, album in enumerate(albums, 1):
        print(f"  [{i:3d}/{len(albums)}] {album['name'][:50]}", end="", flush=True)
        row = audit_album(session, album)
        results.append(row)
        flags = ""
        if row["sort_method"] == "Position": flags += " 📌"
        if row["caption_pct"] >= 20:         flags += " 💬"
        print(f"  sort={row['sort_method']:<18} "
              f"captions={row['caption_pct']:3d}%  "
              f"GPS={row['gps_pct']:3d}%  "
              f"total={row['total']}{flags}")

    results.sort(key=lambda r: (
        0 if r["sort_method"] == "Position" else 1,
        -r["caption_pct"], -r["gps_pct"], r["name"].lower()
    ))

    # ── Print table ───────────────────────────────────────────────
    print("\n" + "═" * 100)
    print(f"{'Album':<40} {'Sort Method':<18} {'Total':>6} "
          f"{'Captions':>9} {'GPS':>6} {'Videos':>7}")
    print("═" * 100)
    for r in results:
        flags = ""
        if r["sort_method"] == "Position": flags += " [MANUAL]"
        if r["caption_pct"] >= 20:         flags += " [CAPTIONS]"
        if r["gps_pct"] == 0:              flags += " [NO GPS]"
        print(f"{r['name'][:39]:<40} {r['sort_method']:<18} {str(r['total']):>6} "
              f"{str(r['caption_pct'])+'%':>9} {str(r['gps_pct'])+'%':>6} "
              f"{r['videos']:>7}{flags}")

    # ── Save TSV ─────────────────────────────────────────────────
    tsv_path = Path(OUTPUT_TSV)
    with open(tsv_path, "w", encoding="utf-8") as f:
        f.write("Name\tPath\tSortMethod\tTotal\tSampled\tCaptioned\t"
                "Caption%\tGPSCount\tGPS%\tVideos\n")
        for r in results:
            f.write(f"{r['name']}\t{r['path']}\t{r['sort_method']}\t"
                    f"{r['total']}\t{r['sampled']}\t{r['captioned']}\t"
                    f"{r['caption_pct']}\t{r['gps_count']}\t{r['gps_pct']}\t"
                    f"{r['videos']}\n")

    manual_count  = sum(1 for r in results if r["sort_method"] == "Position")
    caption_count = sum(1 for r in results if r["caption_pct"] >= 20)
    print("═" * 100)
    print(f"\nTotal albums:  {len(results)}")
    print(f"  Manually ordered (Position):  {manual_count}")
    print(f"  With ≥20 % captions:          {caption_count}")
    print(f"\nSaved to: {tsv_path.resolve()}")


if __name__ == "__main__":
    main()
