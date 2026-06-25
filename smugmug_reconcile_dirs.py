#!/usr/bin/env python3
from __future__ import annotations
"""
smugmug_reconcile_dirs.py

Compares a downloaded SmugMug tree against an existing local photo library
and recommends which side to keep for each matched pair of folders.

Key insight: downloaded files are always better than local because they have:
  • Captions written to EXIF/IPTC
  • Fixed timestamps (from filename patterns)
  • Correct GPS (original EXIF, not overwritten from API)
  • NNN_ ordering prefix where album has manual ordering

So when the same photos exist in both places, ALWAYS keep the downloaded copy.
The only reason to look at the local copy is if it contains photos NOT on SmugMug.

KEEP DOWNLOADED:
  Same photos on both sides → use downloaded (better metadata), remove local.

KEEP DOWNLOADED + RESCUE LOCAL-ONLY FILES:
  Local has extra photos not in downloaded (not on SmugMug). Copy those extra
  files from local into the downloaded folder, then replace local with downloaded.

ADD (no local match):
  Downloaded album has no local counterpart → copy it into the local library.

REVIEW:
  Uncertain match or complex situation → inspect manually.

Usage:
  python3 smugmug_reconcile_dirs.py \\
    --downloaded ./photos \\
    --local      /your/photo/library \\
    [--depth 5] [--min-files 2]

Output:
  • Terminal report
  • reconcile_dirs.tsv   — full results for review in Excel/Numbers
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

SKIP_DIRS  = {"@eadir","@eaDir","@syno","#recycle",".ds_store","lost+found","thumbnails"}
MEDIA_EXTS = {".jpg",".jpeg",".png",".heic",".gif",".dng",".raw",
              ".tiff",".tif",".mp4",".mov",".avi",".m4v",".3gp",
              ".wmv",".mts",".mkv"}

# Thresholds
HIGH_OVERLAP   = 0.75
MEDIUM_OVERLAP = 0.35
HIGH_NAME      = 0.72
MEDIUM_NAME    = 0.45

TSV_OUT = "reconcile_dirs.tsv"

_PREFIX_RE  = re.compile(r"^\d{1,4}_(.+)$")
_DATE_RE    = re.compile(r"^\d{4}([_\-]\d{2})?$")

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def norm_filename(fn: str) -> str:
    """Strip NNN_ prefix, lowercase. '001_IMG_20120607.jpg' → 'img_20120607.jpg'"""
    stem = Path(fn).stem
    ext  = Path(fn).suffix.lower()
    m = _PREFIX_RE.match(stem)
    return ((m.group(1) if m else stem).lower()) + ext


def has_ordering_prefix(files_raw: list[str]) -> bool:
    """True if any file in folder has a NNN_ ordering prefix."""
    return any(_PREFIX_RE.match(Path(f).stem) for f in files_raw)


def is_skip(name: str) -> bool:
    return name.lower() in SKIP_DIRS or name.startswith("@") or name.startswith(".")


def norm_name(n: str) -> str:
    n = n.lower()
    n = re.sub(r"[_\-]+", " ", n)
    n = re.sub(r"([a-z])(\d)", r"\1 \2", n)
    n = re.sub(r"(\d)([a-z])", r"\1 \2", n)
    n = re.sub(r"[^\w\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def tokenise(n: str) -> set[str]:
    STOP = {"a","an","the","of","in","at","on","to","and","my","de","van","het","een"}
    return set(norm_name(n).split()) - STOP


def name_score(a: str, b: str) -> float:
    na, nb = norm_name(a), norm_name(b)
    seq  = SequenceMatcher(None, na, nb).ratio()
    ta, tb = tokenise(a), tokenise(b)
    jacc = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    ya = set(re.findall(r"\b(19|20)\d{2}\b", a))
    yb = set(re.findall(r"\b(19|20)\d{2}\b", b))
    year_bonus = 0.15 if ya and ya == yb else 0.0
    return min(0.35 * seq + 0.65 * jacc + year_bonus, 1.0)


# ─────────────────────────────────────────────────────────────────
# DIRECTORY WALKING
# ─────────────────────────────────────────────────────────────────

def collect_dirs(root: Path, max_depth: int,
                 exclude: Path | None = None) -> list[dict]:
    results = []

    def walk(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: e.name.lower())
        except PermissionError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            if is_skip(entry.name):
                continue
            if exclude and entry.resolve() == exclude.resolve():
                continue

            raw_files = [f.name for f in entry.iterdir()
                         if f.is_file() and f.suffix.lower() in MEDIA_EXTS]
            norm_files = {norm_filename(f) for f in raw_files}

            results.append({
                "name":        entry.name,
                "path":        entry,
                "rel":         entry.relative_to(root),
                "files":       norm_files,     # normalised for matching
                "raw_files":   raw_files,      # original names
                "count":       len(norm_files),
                "has_prefix":  has_ordering_prefix(raw_files),
            })
            walk(entry, depth + 1)

    walk(root, 0)
    return results


# ─────────────────────────────────────────────────────────────────
# MATCHING
# ─────────────────────────────────────────────────────────────────

def file_overlap(dl: dict, lo: dict) -> float:
    if not dl["files"]:
        return 0.0
    return len(dl["files"] & lo["files"]) / len(dl["files"])


def combined_score(ns: float, fo: float) -> float:
    return 0.30 * ns + 0.70 * fo


def classify(fo: float, ns: float, dl_count: int, lo_count: int,
             local_only_count: int) -> tuple[str, str]:
    """Return (action, keep_side).
    action:    REPLACE | MERGE | ADD | REVIEW
    keep_side: DOWNLOADED | DOWNLOADED+RESCUE | REVIEW | ADD
    """
    if fo >= HIGH_OVERLAP:
        if local_only_count > 0:
            return "MERGE",   "DOWNLOADED + rescue local-only files"
        return "REPLACE",     "DOWNLOADED  (local copy can be removed)"

    if fo >= MEDIUM_OVERLAP:
        if ns >= MEDIUM_NAME:
            return "MERGE",   "DOWNLOADED + rescue local-only files"
        return "REVIEW",      "REVIEW — inspect manually"

    if ns >= HIGH_NAME and fo < 0.15:
        return "REVIEW",      "REVIEW — names match but files differ"

    return "ADD", "DOWNLOADED is new content — copy to local library"


def find_matches(dl_dirs: list[dict], lo_dirs: list[dict]) -> list[dict]:
    results = []
    for dl in dl_dirs:
        if dl["count"] == 0:
            continue

        candidates = []
        for lo in lo_dirs:
            if lo["count"] == 0:
                continue
            ns = name_score(dl["name"], lo["name"])
            fo = file_overlap(dl, lo)
            cs = combined_score(ns, fo)
            if cs > 0.05 or fo > 0.05:
                shared         = dl["files"] & lo["files"]
                dl_only_files  = sorted(dl["files"] - lo["files"])
                lo_only_files  = sorted(lo["files"] - dl["files"])
                candidates.append({
                    "lo":           lo,
                    "ns":           ns,
                    "fo":           fo,
                    "cs":           cs,
                    "shared":       len(shared),
                    "dl_only":      dl_only_files,
                    "lo_only":      lo_only_files,
                })

        candidates.sort(key=lambda x: -x["cs"])
        best = candidates[0] if candidates else None

        if best and best["cs"] > 0.10:
            action, keep = classify(
                best["fo"], best["ns"],
                dl["count"], best["lo"]["count"],
                len(best["lo_only"]),
            )
        else:
            best   = None
            action = "ADD"
            keep   = "DOWNLOADED is new content — copy to local library"

        results.append({
            "dl":     dl,
            "best":   best,
            "alts":   candidates[1:3] if best else [],
            "action": action,
            "keep":   keep,
        })

    order = {"REPLACE": 0, "MERGE": 1, "REVIEW": 2, "ADD": 3}
    results.sort(key=lambda r: (order.get(r["action"], 9), -r["dl"]["count"]))
    return results


# ─────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────

ACTION_ICONS = {"REPLACE": "🔄", "MERGE": "🔀", "REVIEW": "❓", "ADD": "➕"}
ACTION_DESC  = {
    "REPLACE": "Same photos on both sides — use downloaded (has captions/timestamps), remove local",
    "MERGE":   "Local has extra photos not on SmugMug — rescue those, then use downloaded",
    "REVIEW":  "Uncertain — inspect manually before doing anything",
    "ADD":     "No local counterpart — copy downloaded folder into local library",
}


def print_report(results: list[dict]):
    by_action: dict[str, list] = defaultdict(list)
    for r in results:
        by_action[r["action"]].append(r)

    for act in ["REPLACE", "MERGE", "REVIEW", "ADD"]:
        group = by_action.get(act, [])
        if not group:
            continue
        icon = ACTION_ICONS[act]
        desc = ACTION_DESC[act]
        print(f"\n{'═'*72}")
        print(f" {icon}  {act}  ·  {len(group)} folders")
        print(f"     {desc}")
        print(f"{'═'*72}")

        for r in group:
            dl   = r["dl"]
            best = r["best"]
            prefix_tag = "  [has ordering prefix]" if dl["has_prefix"] else ""

            print(f"\n  📥 DOWNLOADED: {dl['rel']}"
                  f"  ({dl['count']} files{prefix_tag})")

            if best:
                lo   = best["lo"]
                print(f"  💾 LOCAL:      {lo['rel']}"
                      f"  ({lo['count']} files)")
                print(f"     match quality:  name={best['ns']:.2f}  "
                      f"file-overlap={best['fo']*100:.0f}%  "
                      f"shared={best['shared']}")
                print(f"     ✅ KEEP:  DOWNLOADED copy")

                if best["lo_only"]:
                    print(f"     ⚠️  LOCAL has {len(best['lo_only'])} files"
                          f" NOT in downloaded — rescue these before removing local:")
                    for fn in best["lo_only"][:8]:
                        print(f"          {fn}")
                    if len(best["lo_only"]) > 8:
                        print(f"          … and {len(best['lo_only'])-8} more")

                if best["dl_only"]:
                    print(f"     ℹ️  DOWNLOADED has {len(best['dl_only'])} files"
                          f" not in local (new/renamed):")
                    for fn in best["dl_only"][:4]:
                        print(f"          {fn}")
                    if len(best["dl_only"]) > 4:
                        print(f"          … and {len(best['dl_only'])-4} more")

                # Show alternative matches if score is not overwhelming
                if best["fo"] < HIGH_OVERLAP and r["alts"]:
                    print(f"     alt matches:")
                    for alt in r["alts"]:
                        if alt["cs"] > 0.08:
                            print(f"       • {alt['lo']['rel']}"
                                  f"  name={alt['ns']:.2f}"
                                  f"  overlap={alt['fo']*100:.0f}%")

            else:
                print(f"  💾 LOCAL:      (no match found)")
                print(f"     ✅ KEEP:  DOWNLOADED — copy to local library")


# ─────────────────────────────────────────────────────────────────
# TSV OUTPUT
# ─────────────────────────────────────────────────────────────────

def write_tsv(results: list[dict]):
    with open(TSV_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            "Action", "Keep",
            "Downloaded_Folder", "DL_Files", "DL_HasPrefix",
            "Local_Folder", "Local_Files",
            "NameScore", "FileOverlap%", "Shared",
            "DL_only", "Local_only (rescue these!)",
            "Downloaded_Path", "Local_Path",
        ])
        for r in results:
            dl   = r["dl"]
            best = r["best"]
            if best:
                lo = best["lo"]
                w.writerow([
                    r["action"], r["keep"],
                    str(dl["rel"]),  dl["count"], "yes" if dl["has_prefix"] else "no",
                    str(lo["rel"]),  lo["count"],
                    f"{best['ns']:.3f}",
                    f"{best['fo']*100:.0f}",
                    best["shared"],
                    len(best["dl_only"]),
                    len(best["lo_only"]),
                    str(dl["path"]),
                    str(lo["path"]),
                ])
            else:
                w.writerow([
                    r["action"], r["keep"],
                    str(dl["rel"]), dl["count"], "yes" if dl["has_prefix"] else "no",
                    "", "", "", "", "", "", "",
                    str(dl["path"]), "",
                ])
    print(f"💾 TSV saved → {Path(TSV_OUT).resolve()}")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--downloaded", required=True)
    parser.add_argument("--local",      required=True)
    parser.add_argument("--depth",     type=int, default=5)
    parser.add_argument("--min-files", type=int, default=2,
                        help="Ignore folders with fewer media files (default: 2)")
    args = parser.parse_args()

    dl_root = Path(args.downloaded).resolve()
    lo_root = Path(args.local).resolve()

    for p, label in [(dl_root, "--downloaded"), (lo_root, "--local")]:
        if not p.exists():
            sys.exit(f"{label} path not found: {p}")

    print(f"📥 Walking downloaded tree: {dl_root}")
    dl_dirs = [d for d in collect_dirs(dl_root, args.depth)
               if d["count"] >= args.min_files]
    print(f"   {len(dl_dirs)} folders with ≥{args.min_files} media files")

    print(f"\n💾 Walking local tree:      {lo_root}")
    lo_dirs = [d for d in collect_dirs(lo_root, args.depth, exclude=dl_root)
               if d["count"] >= args.min_files]
    print(f"   {len(lo_dirs)} folders with ≥{args.min_files} media files")

    print(f"\n🔍 Matching {len(dl_dirs)} downloaded ↔ {len(lo_dirs)} local folders...\n")
    results = find_matches(dl_dirs, lo_dirs)

    # Summary
    counts = defaultdict(int)
    lo_only_total = 0
    for r in results:
        counts[r["action"]] += 1
        if r["best"]:
            lo_only_total += len(r["best"]["lo_only"])

    print(f"{'═'*72}")
    print(f"  🔄 REPLACE  {counts['REPLACE']:4d}  — keep downloaded, remove local")
    print(f"  🔀 MERGE    {counts['MERGE']:4d}  — keep downloaded + rescue local-only files")
    print(f"  ❓ REVIEW   {counts['REVIEW']:4d}  — inspect manually")
    print(f"  ➕ ADD      {counts['ADD']:4d}  — copy downloaded into local library")
    if lo_only_total:
        print(f"\n  ⚠️  {lo_only_total} local-only files across all MERGE cases need rescuing")
    print(f"{'═'*72}")

    print_report(results)
    write_tsv(results)
    print()


if __name__ == "__main__":
    main()
