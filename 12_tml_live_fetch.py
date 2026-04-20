#!/usr/bin/env python3
"""
TML Live Data Fetch — pulls fresh match CSVs from the TML API
(stats.tennismylife.org) and rebuilds the consolidated parquet.

This replaces 11_tml_ingest.py's static file approach with a live API pull,
ensuring match data stays current through the latest completed tournaments.

API:  GET https://stats.tennismylife.org/api/data-files
      Returns JSON: { "files": [ { "url": "...", "name": "2026.csv" }, ... ] }

Output: data/tml_history_10y.parquet     (consolidated match history)
        data/tml_history_10y_meta.json   (metadata)
        data/tml-live/                    (raw CSVs cached locally)

Usage:
    python3 12_tml_live_fetch.py              # full refresh
    python3 12_tml_live_fetch.py --recent     # only re-download current + last year
"""
import os
import json
import argparse
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TML_LIVE_DIR = DATA_DIR / "tml-live"
API_URL = "https://stats.tennismylife.org/api/data-files"

# Years to keep (10-year window of useful training data)
MIN_YEAR = 2016
MAX_YEAR = 2027  # inclusive upper bound to catch future files

# Tiers we care about for Polymarket matching
WANTED_TIERS = {"main", "challenger"}


def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")


def fetch_file_list():
    """Get available CSV file list from TML API."""
    log(f"Fetching file list from {API_URL}")
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    files = data.get("files", [])
    log(f"  API returned {len(files)} files")
    return files


def classify_file(name):
    """Classify a TML CSV filename → (year, tier) or None.

    Expected patterns:
      2026.csv              → (2026, 'main')
      2026_challenger.csv   → (2026, 'challenger')
      2025_futures.csv      → (2025, 'futures')   [skipped]
    """
    stem = name.replace(".csv", "")
    parts = stem.split("_", 1)
    try:
        year = int(parts[0])
    except (ValueError, IndexError):
        return None

    tier = parts[1] if len(parts) > 1 else "main"
    if year < MIN_YEAR or year > MAX_YEAR:
        return None
    return (year, tier)


def download_csvs(files, recent_only=False):
    """Download CSV files from TML API to tml-live/ directory."""
    TML_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    current_year = datetime.utcnow().year

    downloaded = []
    skipped = 0

    for finfo in files:
        name = finfo.get("name", "")
        url = finfo.get("url", "")
        if not name or not url:
            continue

        classified = classify_file(name)
        if classified is None:
            continue

        year, tier = classified
        if tier not in WANTED_TIERS:
            skipped += 1
            continue

        # In --recent mode, only download current year and last year
        if recent_only and year < current_year - 1:
            skipped += 1
            continue

        dest = TML_LIVE_DIR / name
        log(f"  Downloading {name} ...")
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            downloaded.append((dest, year, tier))
        except Exception as e:
            log(f"  ⚠ Failed to download {name}: {e}")

    log(f"  Downloaded {len(downloaded)} files, skipped {skipped}")
    return downloaded


def build_parquet(recent_only=False):
    """Build consolidated parquet from all CSVs in tml-live/ directory."""
    frames = []

    for csv_path in sorted(TML_LIVE_DIR.glob("*.csv")):
        classified = classify_file(csv_path.name)
        if classified is None:
            continue

        year, tier = classified
        if tier not in WANTED_TIERS:
            continue

        try:
            df = pd.read_csv(csv_path, low_memory=False)
            if df.empty:
                continue

            # Drop leaked header rows
            if "tourney_name" in df.columns:
                df = df[df["tourney_name"] != "tourney_name"].copy()

            df["year"] = year
            df["tier"] = tier
            frames.append(df)
            log(f"  {csv_path.name:30s} → {len(df):>6,} matches")
        except Exception as e:
            log(f"  ⚠ Failed to parse {csv_path.name}: {e}")

    if not frames:
        log("⚠ No CSV files found in tml-live/")
        return None

    df = pd.concat(frames, ignore_index=True)

    # Normalize dates (Sackmann format: YYYYMMDD int)
    df["match_date"] = pd.to_datetime(
        df["tourney_date"].astype(str), format="%Y%m%d", errors="coerce"
    )

    # Trim boundary rows
    df = df[df["match_date"] >= f"{MIN_YEAR}-01-01"].copy()

    # Lower-case names for fuzzy matching
    df["winner_name_lc"] = df["winner_name"].astype(str).str.lower().str.strip()
    df["loser_name_lc"] = df["loser_name"].astype(str).str.lower().str.strip()

    # Sort
    df = df.sort_values(["match_date", "tourney_name", "match_num"]).reset_index(drop=True)

    # Coerce numeric columns
    numeric_cols = [
        "draw_size", "match_num", "winner_id", "winner_seed", "loser_id", "loser_seed",
        "winner_ht", "loser_ht", "winner_age", "loser_age",
        "winner_rank", "winner_rank_points", "loser_rank", "loser_rank_points",
        "best_of", "minutes",
        "w_ace", "w_df", "w_svpt", "w_1stIn", "w_1stWon", "w_2ndWon", "w_SvGms", "w_bpSaved", "w_bpFaced",
        "l_ace", "l_df", "l_svpt", "l_1stIn", "l_1stWon", "l_2ndWon", "l_SvGms", "l_bpSaved", "l_bpFaced",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Stringify remaining object cols for Parquet compatibility
    obj_cols = df.select_dtypes(include="object").columns
    for c in obj_cols:
        df[c] = df[c].astype("string")

    # Write parquet
    out = DATA_DIR / "tml_history_10y.parquet"
    df.to_parquet(out, index=False)
    log(f"✓ Wrote {out}  rows={len(df):,}  cols={len(df.columns)}")

    # Write metadata
    meta = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": "stats.tennismylife.org/api/data-files",
        "rows": int(len(df)),
        "years": [int(df["year"].min()), int(df["year"].max())],
        "tiers": sorted(df["tier"].unique().tolist()),
        "surfaces": sorted(df["surface"].dropna().unique().tolist()),
        "tournament_count": int(df["tourney_name"].nunique()),
        "player_count": int(pd.concat([df["winner_name"], df["loser_name"]]).nunique()),
        "date_range": [str(df["match_date"].min().date()), str(df["match_date"].max().date())],
        "serve_data_coverage": round(float(df["w_ace"].notna().sum() / len(df) * 100), 1),
    }
    meta_path = DATA_DIR / "tml_history_10y_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    log(f"✓ Wrote {meta_path}")
    print(json.dumps(meta, indent=2))

    return df


def main():
    parser = argparse.ArgumentParser(description="TML Live Data Fetch")
    parser.add_argument("--recent", action="store_true",
                        help="Only re-download current + last year (faster)")
    parser.add_argument("--build-only", action="store_true",
                        help="Skip download, just rebuild parquet from cached CSVs")
    args = parser.parse_args()

    print("\n═══ TML LIVE FETCH ═══")

    if not args.build_only:
        try:
            files = fetch_file_list()
            download_csvs(files, recent_only=args.recent)
        except requests.exceptions.ConnectionError as e:
            log(f"⚠ Cannot reach TML API: {e}")
            log("  Falling back to cached CSVs in data/tml-live/")
        except Exception as e:
            log(f"⚠ API error: {e}")
            log("  Falling back to cached CSVs in data/tml-live/")

    df = build_parquet(recent_only=args.recent)
    if df is None:
        print("\n⚠ No data built — check tml-live/ directory")
        sys.exit(1)

    print("\n✓ TML live fetch complete")


if __name__ == "__main__":
    main()
