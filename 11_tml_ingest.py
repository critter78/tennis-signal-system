"""
TML Historical Ingest — 2016-2026 (ATP main + Challenger)
Consolidates the Tennismylife/TML-Database zip into a single parquet
for Polymarket matching + wallet-edge analysis.

Output: data/tml_history_10y.parquet
        data/tml_history_10y_meta.json
"""
import os
import json
import glob
import pandas as pd
from pathlib import Path
from datetime import datetime

SRC_DIR = Path(os.environ.get("TML_SRC", "/sessions/nice-determined-brown/tml_unzip"))
OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True)

YEARS = list(range(2016, 2027))
TIERS = ["main", "challenger"]  # skip futures/amateur for PM matching

def load_year(year: int, tier: str) -> pd.DataFrame:
    fname = f"{year}.csv" if tier == "main" else f"{year}_challenger.csv"
    path = SRC_DIR / fname
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    df["year"] = year
    df["tier"] = tier
    return df

def main():
    frames = []
    for y in YEARS:
        for t in TIERS:
            df = load_year(y, t)
            if not df.empty:
                frames.append(df)
                print(f"  {y} {t:10s} → {len(df):>6,} matches")

    if not frames:
        raise SystemExit("No files found — check TML_SRC path")

    df = pd.concat(frames, ignore_index=True)

    # drop any leaked header rows (defensive — one row in 2026.csv repeated the header as data)
    df = df[df["tourney_name"] != "tourney_name"].copy()

    # normalize dates (Sackmann format: YYYYMMDD int)
    df["match_date"] = pd.to_datetime(df["tourney_date"].astype(str), format="%Y%m%d", errors="coerce")

    # trim boundary rows (late-Dec 2015 tournaments filed under 2016.csv)
    df = df[df["match_date"] >= "2016-01-01"].copy()

    # lower-case names for fuzzy matching against Polymarket market titles
    df["winner_name_lc"] = df["winner_name"].astype(str).str.lower().str.strip()
    df["loser_name_lc"]  = df["loser_name"].astype(str).str.lower().str.strip()

    # sort canonical
    df = df.sort_values(["match_date", "tourney_name", "match_num"]).reset_index(drop=True)

    # coerce numeric-looking cols; stringify remaining object cols to avoid pyarrow mixed-type errors
    numeric_cols = ["draw_size", "match_num", "winner_id", "winner_seed", "loser_id", "loser_seed",
                    "winner_ht", "loser_ht", "winner_age", "loser_age",
                    "winner_rank", "winner_rank_points", "loser_rank", "loser_rank_points",
                    "best_of", "minutes",
                    "w_ace","w_df","w_svpt","w_1stIn","w_1stWon","w_2ndWon","w_SvGms","w_bpSaved","w_bpFaced",
                    "l_ace","l_df","l_svpt","l_1stIn","l_1stWon","l_2ndWon","l_SvGms","l_bpSaved","l_bpFaced"]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    obj_cols = df.select_dtypes(include="object").columns
    for c in obj_cols:
        df[c] = df[c].astype("string")

    out = OUT_DIR / "tml_history_10y.parquet"
    df.to_parquet(out, index=False)
    print(f"\n✓ wrote {out}  rows={len(df):,}  cols={len(df.columns)}")

    meta = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "rows": int(len(df)),
        "years": [int(df["year"].min()), int(df["year"].max())],
        "tiers": sorted(df["tier"].unique().tolist()),
        "surfaces": sorted(df["surface"].dropna().unique().tolist()),
        "tournament_count": int(df["tourney_name"].nunique()),
        "player_count": int(pd.concat([df["winner_name"], df["loser_name"]]).nunique()),
        "date_range": [str(df["match_date"].min().date()), str(df["match_date"].max().date())],
    }
    (OUT_DIR / "tml_history_10y_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))

if __name__ == "__main__":
    main()
