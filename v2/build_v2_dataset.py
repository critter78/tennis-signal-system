"""
V2 DATASET BUILDER — ISOLATED FROM PRODUCTION
================================================

Builds `data/matches_combined_v2.parquet` from:
  - TML-Database ATP tour-level 2019–2026
  - TML-Database ATP challenger 2019–2026
  - Sackmann WTA 2018–2024 (preserved; read-only from ../data/raw/)

Does NOT touch:
  - Any file outside v2_research/
  - Production models, parquet, or picks

Run:
    cd v2_research
    python build_v2_dataset.py
"""

import sys
from pathlib import Path
from datetime import datetime
import pandas as pd

# ─── PATHS ────────────────────────────────────────────────────────────────────
V2_ROOT     = Path(__file__).parent
TML_DIR     = V2_ROOT / "data" / "raw" / "tml"
OUT_PATH    = V2_ROOT / "data" / "matches_combined_v2.parquet"
REPORT_PATH = V2_ROOT / "reports" / "data_diff.md"

# Self-contained WTA path (was originally pointing to v1 production data,
# but on Render the v2 service has rootDir=v2 and can't see siblings).
# WTA CSVs are now copied into v2/data/raw/ at deploy time.
LOCAL_WTA_DIR      = V2_ROOT / "data" / "raw"
PROD_RAW_DIR       = V2_ROOT.parent / "data" / "raw"  # local-dev fallback only
PROD_V1_PARQUET    = PROD_RAW_DIR / "matches_combined.parquet"

# Rolling 6-year window: 2020-2026 today, slides forward automatically.
ROLLING_YEARS = 6
CURRENT_YEAR  = datetime.now().year
MIN_YEAR      = CURRENT_YEAR - ROLLING_YEARS + 1
YEARS = list(range(MIN_YEAR, CURRENT_YEAR + 1))  # rolling window


# ─── LOADERS ──────────────────────────────────────────────────────────────────

def load_tml_year(year: int, is_challenger: bool) -> pd.DataFrame:
    suffix = "_challenger" if is_challenger else ""
    path = TML_DIR / f"{year}{suffix}.csv"
    if not path.exists():
        print(f"  [skip] {path.name} — file missing", file=sys.stderr)
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    df["tour"]       = "atp"
    df["source"]     = "tml_challenger" if is_challenger else "tml_tour"
    df["is_challenger"] = is_challenger
    return df


def load_sackmann_wta() -> pd.DataFrame:
    frames = []
    # Sackmann WTA ends at 2024; obey the rolling window lower bound.
    wta_years = range(max(MIN_YEAR, 2018), 2025)
    for y in wta_years:
        # Prefer self-contained v2/data/raw/ — fall back to v1 production path for local dev
        p = LOCAL_WTA_DIR / f"wta_{y}.csv"
        if not p.exists():
            p = PROD_RAW_DIR / f"wta_{y}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, low_memory=False)
        df["tour"]       = "wta"
        df["source"]     = "sackmann_wta"
        df["is_challenger"] = False
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ─── NORMALIZATION ────────────────────────────────────────────────────────────

RENAME_MAP = {
    "winner_name":        "winner",
    "loser_name":         "loser",
    "winner_rank":        "w_rank",
    "loser_rank":         "l_rank",
    "winner_rank_points": "w_rank_pts",
    "loser_rank_points":  "l_rank_pts",
    "tourney_date":       "date",
    "tourney_name":       "tournament",
}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})
    # Date parsing (YYYYMMDD as int/str)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
    # Indoor flag: TML uses 'I'/'O', normalize to boolean
    if "indoor" in df.columns:
        df["indoor_flag"] = df["indoor"].astype(str).str.upper().eq("I")
    else:
        df["indoor_flag"] = False
    return df


def drop_bad_dates(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Sanity-filter dates: must be within [2017-01-01, today+7 days]."""
    if "date" not in df.columns:
        return df, 0
    lo = pd.Timestamp("2017-01-01")
    hi = pd.Timestamp(datetime.now().date()) + pd.Timedelta(days=7)
    mask = (df["date"] >= lo) & (df["date"] <= hi)
    bad = (~mask).sum()
    return df[mask].copy(), int(bad)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("  V2 DATASET BUILD — TML ATP + SACKMANN WTA")
    print("=" * 64)

    frames = []
    stats = {"tour": 0, "challenger": 0, "wta": 0, "bad_dates": 0}

    # ATP tour
    print("\n[1] Loading TML ATP tour-level 2019-2026…")
    for y in YEARS:
        df = load_tml_year(y, is_challenger=False)
        if not df.empty:
            frames.append(df)
            stats["tour"] += len(df)
            print(f"    {y}: {len(df):>5,} rows")

    # ATP challenger
    print("\n[2] Loading TML ATP challenger 2019-2026…")
    for y in YEARS:
        df = load_tml_year(y, is_challenger=True)
        if not df.empty:
            frames.append(df)
            stats["challenger"] += len(df)
            print(f"    {y}: {len(df):>5,} rows")

    # Sackmann WTA
    print("\n[3] Loading Sackmann WTA 2018-2024…")
    wta = load_sackmann_wta()
    if not wta.empty:
        frames.append(wta)
        stats["wta"] = len(wta)
        print(f"    {len(wta):,} WTA rows")

    # Combine
    combined = pd.concat(frames, ignore_index=True)
    print(f"\n[4] Combined raw rows: {len(combined):,}")

    # Normalize columns
    combined = normalize(combined)

    # Drop bad dates
    combined, bad = drop_bad_dates(combined)
    stats["bad_dates"] = bad
    print(f"[5] Dropped {bad} rows with out-of-range dates")

    # Dedupe on (tourney_id, match_num, tour, source)
    before = len(combined)
    if {"tourney_id", "match_num"}.issubset(combined.columns):
        combined = combined.drop_duplicates(subset=["tourney_id", "match_num", "tour"], keep="first")
    dedup_removed = before - len(combined)
    print(f"[6] Dedup removed {dedup_removed} rows")

    # Force all object cols to string for parquet safety
    for col in combined.columns:
        if combined[col].dtype == object:
            combined[col] = combined[col].astype(str).replace("nan", pd.NA)

    # Sort by date
    combined = combined.sort_values("date").reset_index(drop=True)

    # Write parquet
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUT_PATH, index=False)
    print(f"\n✓ Wrote {OUT_PATH} ({len(combined):,} rows, "
          f"{OUT_PATH.stat().st_size / 1024 / 1024:.2f} MB)")

    # ── Diff report vs v1 ──────────────────────────────────────────────────
    v1_rows = None
    if PROD_V1_PARQUET.exists():
        v1 = pd.read_parquet(PROD_V1_PARQUET)
        v1_rows = len(v1)

    lines = [
        "# V2 Dataset Diff Report",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        "## Composition",
        f"- TML ATP tour-level (2019–2026): **{stats['tour']:,}** matches",
        f"- TML ATP challenger (2019–2026): **{stats['challenger']:,}** matches",
        f"- Sackmann WTA (2018–2024): **{stats['wta']:,}** matches",
        f"- Dropped bad-date rows: {stats['bad_dates']}",
        f"- Dedup removed: {dedup_removed}",
        f"- **V2 total: {len(combined):,} matches**",
        f"- V1 total (production): {v1_rows:,}" if v1_rows else "- V1 parquet not found",
        f"- Δ: **+{len(combined) - (v1_rows or 0):,}** matches vs v1" if v1_rows else "",
        "",
        "## Coverage",
        f"- Date range: {combined['date'].min().date()} → {combined['date'].max().date()}",
        f"- Sources:",
    ]
    for src, count in combined["source"].value_counts().items():
        lines.append(f"  - `{src}`: {count:,}")
    lines += [
        "",
        "## Tour / level breakdown",
    ]
    for tour in combined["tour"].unique():
        sub = combined[combined["tour"] == tour]
        levels = sub["tourney_level"].value_counts().to_dict() if "tourney_level" in sub.columns else {}
        lines.append(f"- **{tour.upper()}** ({len(sub):,}): {levels}")
    lines += [
        "",
        "## Indoor/outdoor (TML only)",
        f"- Indoor flag true: {int(combined['indoor_flag'].sum()):,}",
        f"- Indoor flag false: {int((~combined['indoor_flag']).sum()):,}",
        "",
        "## Notes",
        "- 2018 Sackmann ATP data was **excluded** (TML starts 2019 per user request)",
        "- 2025 and 2026 YTD fully included (16 months of previously-missing data)",
        "- Challenger matches tagged via `tourney_level='C'` and `is_challenger=True`",
        "- `indoor_flag` is new — can be added to feature set in v2 model training",
        "- WTA remains at Sackmann 2018–2024 until a WTA continuation source is wired",
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))
    print(f"✓ Wrote diff report → {REPORT_PATH}")

    print("\n" + "=" * 64)
    print("  DONE — v1 production files untouched")
    print("=" * 64)


if __name__ == "__main__":
    main()
