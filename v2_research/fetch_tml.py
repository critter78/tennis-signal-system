"""
TML-Database incremental fetcher.

Pulls https://stats.tennismylife.org/api/data-files, compares against the
local manifest cache, and only re-downloads files whose size/mtime/etag changed.

No external deps beyond the stdlib. Safe to run hourly.

Usage:
    cd v2_research
    python fetch_tml.py              # fetch, write to data/raw/tml/
    python fetch_tml.py --dry-run    # just print what would change
    python fetch_tml.py --rebuild    # fetch, then rebuild matches_combined_v2.parquet
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

V2_ROOT      = Path(__file__).parent
TML_DIR      = V2_ROOT / "data" / "raw" / "tml"
MANIFEST_URL = "https://stats.tennismylife.org/api/data-files"
CACHE_PATH   = TML_DIR / ".manifest_cache.json"

UA = "CritterLabs-tennis-signal/0.2 (+non-commercial research)"
TIMEOUT = 30

# Rolling window — CRITTER wants the last 6 years only.
ROLLING_YEARS = 6
YEAR_RE = re.compile(r"(?:^|[^0-9])(\d{4})(?:[^0-9]|$)")


def file_year(name: str) -> int | None:
    """Pull the first 4-digit year out of a filename. Returns None if none found."""
    m = YEAR_RE.search(name)
    return int(m.group(1)) if m else None


def in_window(name: str, min_year: int, keep_non_year: bool = True) -> bool:
    """
    Keep files where the embedded year >= min_year. Non-year files (ATP_Database.csv,
    ongoing_tourneys.csv, atp_matches_amateur.csv) are kept iff keep_non_year=True.
    """
    y = file_year(name)
    if y is None:
        return keep_non_year
    return y >= min_year


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def file_signature(entry: dict) -> str:
    """Build a change-detection key from whatever the manifest gives us."""
    return "|".join(str(entry.get(k, "")) for k in ("size", "mtime", "etag", "sha256"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="Run build_v2_dataset.py after a successful fetch")
    ap.add_argument("--only", help="Substring filter on filenames (e.g. '2026')")
    ap.add_argument("--min-year", type=int,
                    default=datetime.now().year - ROLLING_YEARS + 1,
                    help=f"Skip files older than this year (default: rolling {ROLLING_YEARS}-year window)")
    ap.add_argument("--all-years", action="store_true",
                    help="Disable the rolling-year filter — fetch everything (1968+)")
    ap.add_argument("--prune", action="store_true",
                    help="Delete local CSVs that fall outside the rolling window")
    args = ap.parse_args()
    min_year = 0 if args.all_years else args.min_year
    print(f"[window] keeping files with year >= {min_year if min_year else '(all)'}")

    TML_DIR.mkdir(parents=True, exist_ok=True)

    try:
        manifest = fetch_json(MANIFEST_URL)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[fatal] cannot reach {MANIFEST_URL}: {e}", file=sys.stderr)
        return 2

    files = manifest.get("files", manifest if isinstance(manifest, list) else [])
    if not files:
        print("[fatal] manifest empty or unexpected shape; keys="
              f"{list(manifest)[:8]}", file=sys.stderr)
        return 2

    cache = load_cache()
    new_cache = {}
    stats = {"checked": 0, "updated": 0, "new": 0, "unchanged": 0, "failed": 0}

    for f in files:
        name = f.get("name") or f.get("filename") or f.get("path")
        url  = f.get("url")  or f.get("download_url") or f.get("href")
        if not name or not url:
            continue
        if args.only and args.only not in name:
            continue
        if not in_window(name, min_year):
            continue
        stats["checked"] += 1

        sig = file_signature(f)
        prev = cache.get(name)
        target = TML_DIR / name

        if prev == sig and target.exists():
            stats["unchanged"] += 1
            new_cache[name] = sig
            continue

        action = "new" if not target.exists() else "updated"
        if args.dry_run:
            print(f"  [would-fetch:{action}] {name}")
            stats[action] += 1
            new_cache[name] = sig  # don't actually persist in dry-run
            continue

        try:
            data = fetch_bytes(url)
            target.write_bytes(data)
            new_cache[name] = sig
            stats[action] += 1
            print(f"  [{action:7s}] {name}  ({len(data):>9,} bytes)")
            time.sleep(0.25)  # be polite
        except Exception as e:
            stats["failed"] += 1
            print(f"  [failed ] {name}: {e}", file=sys.stderr)

    # Optional prune of files outside the rolling window
    pruned = 0
    if args.prune and not args.all_years:
        for f in TML_DIR.glob("*.csv"):
            if not in_window(f.name, min_year, keep_non_year=True):
                if args.dry_run:
                    print(f"  [would-prune] {f.name}")
                else:
                    f.unlink()
                    print(f"  [pruned ] {f.name}")
                pruned += 1
        stats["pruned"] = pruned

    if not args.dry_run:
        save_cache(new_cache)

    print(f"\nsummary: {stats}")

    if args.rebuild and not args.dry_run and (stats["new"] + stats["updated"] > 0):
        print("\n[rebuild] regenerating matches_combined_v2.parquet…")
        rc = subprocess.call(
            [sys.executable, str(V2_ROOT / "build_v2_dataset.py")],
            cwd=V2_ROOT,
        )
        return rc

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
