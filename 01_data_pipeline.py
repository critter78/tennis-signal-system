"""
TENNIS POLYMARKET SIGNAL SYSTEM
Script 1: Data Pipeline

- Downloads Jeff Sackmann ATP/WTA historical match data (GitHub CSVs)
- Pulls live Polymarket CLOB API for current tennis markets
- Optionally pulls Pinnacle odds via The Odds API (set API key in config)
- Saves everything to /data/ for feature engineering

Usage:
    python 01_data_pipeline.py [--years 2018 2019 2020 2021 2022 2023 2024] [--tour atp wta]
"""

import os
import time
import json
import argparse
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime

# ─── CONFIG ────────────────────────────────────────────────────────────────────

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
(DATA_DIR / "raw").mkdir(exist_ok=True)
(DATA_DIR / "polymarket").mkdir(exist_ok=True)
(DATA_DIR / "odds").mkdir(exist_ok=True)

# Sackmann GitHub raw CSVs
SACKMANN_ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
SACKMANN_WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"

# Polymarket CLOB API — no key needed for market reads
POLYMARKET_MARKETS_URL = "https://clob.polymarket.com/markets"
POLYMARKET_GAMMA_URL   = "https://gamma-api.polymarket.com/markets"

# The Odds API (optional — sharp Pinnacle lines as ground truth feature)
# Sign up free at https://the-odds-api.com/ — 500 req/month free tier
ODDS_API_KEY  = os.getenv("ODDS_API_KEY", "")   # set env var or paste here
ODDS_API_URL  = "https://api.the-odds-api.com/v4/sports/tennis/odds"

DEFAULT_YEARS = list(range(2018, 2025))
DEFAULT_TOURS = ["atp", "wta"]

# ─── SACKMANN DATA ─────────────────────────────────────────────────────────────

def download_sackmann(years=DEFAULT_YEARS, tours=DEFAULT_TOURS):
    """Download historical match CSVs from Jeff Sackmann's GitHub repos."""
    frames = []
    for tour in tours:
        url_template = SACKMANN_ATP_URL if tour == "atp" else SACKMANN_WTA_URL
        for year in years:
            url  = url_template.format(year=year)
            dest = DATA_DIR / "raw" / f"{tour}_{year}.csv"
            if dest.exists():
                print(f"  [skip] {dest.name} already exists")
                df = pd.read_csv(dest, low_memory=False)
            else:
                print(f"  [fetch] {url}")
                try:
                    r = requests.get(url, timeout=15)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                    df = pd.read_csv(dest, low_memory=False)
                    print(f"         → {len(df):,} matches saved")
                except requests.HTTPError as e:
                    print(f"         ✗ {e} (year may not exist yet)")
                    continue
                except Exception as e:
                    print(f"         ✗ unexpected error: {e}")
                    continue
            df["tour"] = tour
            frames.append(df)
            time.sleep(0.2)  # polite crawl delay

    if not frames:
        print("No data downloaded. Check years/tours params.")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_columns(combined)

    # Fix mixed-type columns that break parquet serialization
    # Object columns with mixed str/numeric values cause pyarrow errors
    for col in combined.columns:
        if combined[col].dtype == object:
            # Force all object columns to string — safe for parquet
            combined[col] = combined[col].astype(str).replace("nan", pd.NA)

    out = DATA_DIR / "raw" / "matches_combined.parquet"
    combined.to_parquet(out, index=False)
    print(f"\n✓ Combined dataset: {len(combined):,} matches → {out}")
    return combined


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Standardise column names and types across ATP/WTA datasets."""
    rename = {
        "winner_name": "winner",
        "loser_name":  "loser",
        "winner_rank": "w_rank",
        "loser_rank":  "l_rank",
        "winner_rank_points": "w_rank_pts",
        "loser_rank_points":  "l_rank_pts",
        "tourney_date": "date",
        "tourney_name": "tournament",
        "surface":      "surface",
        "round":        "round",
        "score":        "score",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
    return df


# ─── POLYMARKET CLOB API ───────────────────────────────────────────────────────

def fetch_polymarket_tennis() -> pd.DataFrame:
    """
    Pull all active tennis markets from Polymarket's CLOB API.
    Returns a DataFrame with market_id, question, outcomes, current prices.
    """
    print("\n[Polymarket] Fetching active tennis markets...")

    # Gamma API gives richer metadata including sport tag filtering
    params = {
        "tag_slug": "tennis",
        "active":   "true",
        "closed":   "false",
        "limit":    500,
    }
    try:
        r = requests.get(POLYMARKET_GAMMA_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ✗ Gamma API failed: {e}")
        data = []

    if not data:
        print("  → Falling back to CLOB markets endpoint...")
        # CLOB endpoint — paginate through all markets
        data = []
        cursor = ""
        while True:
            p = {"next_cursor": cursor, "limit": 100}
            r = requests.get(POLYMARKET_MARKETS_URL, params=p, timeout=15)
            r.raise_for_status()
            page = r.json()
            batch = page.get("data", [])
            if not batch:
                break
            data.extend(batch)
            cursor = page.get("next_cursor", "")
            if not cursor or cursor == "LTE=":
                break

    rows = []
    for m in data:
        # Parse outcome tokens + prices
        tokens   = m.get("tokens", []) or m.get("outcomes", [])
        outcomes = m.get("outcomes", [])
        clob_tkn = m.get("clobTokenIds", [])

        # Build price dict: outcome_name -> price
        prices = {}
        for t in tokens:
            if isinstance(t, dict):
                name  = t.get("outcome", t.get("name", ""))
                price = t.get("price", None)
                if name:
                    prices[name] = price

        rows.append({
            "market_id":   m.get("id") or m.get("condition_id"),
            "question":    m.get("question", ""),
            "slug":        m.get("slug", ""),
            "end_date":    m.get("end_date_iso") or m.get("endDateIso"),
            "volume":      m.get("volume", 0),
            "liquidity":   m.get("liquidity", 0),
            "outcomes":    outcomes if isinstance(outcomes, list) else [],
            "prices":      prices,
            "raw":         m,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("  ✗ No tennis markets found.")
        return df

    # ── Parse match markets: extract player names + polymarket implied prob ──
    df = _parse_match_markets(df)

    out = DATA_DIR / "polymarket" / f"tennis_markets_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    df_save = df.drop(columns=["raw"], errors="ignore")
    df_save.to_json(out, orient="records", indent=2)
    print(f"  ✓ {len(df)} markets → {out}")
    return df


def _parse_match_markets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract player_a / player_b / poly_prob_a from question text.
    Polymarket tennis questions follow: "Player A vs Player B" or "Player A to win"
    """
    import re
    player_a, player_b, prob_a = [], [], []

    for _, row in df.iterrows():
        q      = str(row.get("question", ""))
        prices = row.get("prices", {})

        # "Will X beat Y?" or "X vs Y" formats
        m = re.search(r"([A-Z][a-zA-Z\s\-\.\']+?)\s+vs\.?\s+([A-Z][a-zA-Z\s\-\.\']+)", q)
        if m:
            pa, pb = m.group(1).strip(), m.group(2).strip()
        else:
            # "Will X win?" format — only one player
            m2 = re.search(r"Will (.+?) win", q)
            pa = m2.group(1).strip() if m2 else ""
            pb = ""

        # Match player names to price dict keys
        prob = None
        if pa and prices:
            for key, val in prices.items():
                if pa.split()[-1].lower() in key.lower():
                    prob = val
                    break

        player_a.append(pa)
        player_b.append(pb)
        prob_a.append(prob)

    df = df.copy()
    df["player_a"]    = player_a
    df["player_b"]    = player_b
    df["poly_prob_a"] = prob_a
    return df


# ─── PINNACLE / THE ODDS API ───────────────────────────────────────────────────

def fetch_pinnacle_odds() -> pd.DataFrame:
    """
    Fetch current Pinnacle tennis moneyline odds via The Odds API.
    These serve as the 'sharp line' feature — the most important external signal.
    Requires ODDS_API_KEY env var. Skip gracefully if not set.
    """
    if not ODDS_API_KEY:
        print("\n[Pinnacle] ODDS_API_KEY not set — skipping sharp line fetch.")
        print("  → Set env var ODDS_API_KEY to enable. Free tier: 500 req/month.")
        print("  → https://the-odds-api.com/")
        return pd.DataFrame()

    print("\n[Pinnacle] Fetching sharp tennis odds...")
    params = {
        "apiKey":  ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "bookmakers": "pinnacle",
    }
    try:
        r = requests.get(ODDS_API_URL, params=params, timeout=15)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        print(f"  ✗ Odds API failed: {e}")
        return pd.DataFrame()

    rows = []
    for ev in events:
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        for bm in ev.get("bookmakers", []):
            if bm["key"] != "pinnacle":
                continue
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                # Convert decimal odds to implied probability (remove vig)
                home_dec = outcomes.get(home, None)
                away_dec = outcomes.get(away, None)
                if home_dec and away_dec:
                    raw_home = 1 / home_dec
                    raw_away = 1 / away_dec
                    total    = raw_home + raw_away
                    rows.append({
                        "player_a":      home,
                        "player_b":      away,
                        "pinnacle_prob_a": round(raw_home / total, 4),
                        "pinnacle_prob_b": round(raw_away / total, 4),
                        "pinnacle_odds_a": home_dec,
                        "pinnacle_odds_b": away_dec,
                        "commence_time":  ev.get("commence_time"),
                        "sport_key":      ev.get("sport_key"),
                    })

    df = pd.DataFrame(rows)
    if not df.empty:
        out = DATA_DIR / "odds" / f"pinnacle_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(out, index=False)
        print(f"  ✓ {len(df)} Pinnacle lines → {out}")
    return df


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tennis Polymarket Data Pipeline")
    parser.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--tour",  nargs="+", choices=["atp","wta"], default=DEFAULT_TOURS)
    parser.add_argument("--skip-historical", action="store_true",
                        help="Skip Sackmann download (if already done)")
    args = parser.parse_args()

    print("=" * 60)
    print("  TENNIS POLYMARKET SYSTEM — STEP 1: DATA PIPELINE")
    print("=" * 60)

    # 1. Historical match data
    if not args.skip_historical:
        print(f"\n[Sackmann] Downloading {args.tour} data for {args.years}...")
        df_hist = download_sackmann(years=args.years, tours=args.tour)
        print(f"\n  Tours: {df_hist['tour'].value_counts().to_dict()}")
        print(f"  Surfaces: {df_hist['surface'].value_counts().to_dict()}")
        print(f"  Date range: {df_hist['date'].min()} → {df_hist['date'].max()}")
    else:
        print("\n[Sackmann] Skipping historical download (--skip-historical set)")

    # 2. Live Polymarket tennis markets
    df_poly = fetch_polymarket_tennis()
    if not df_poly.empty:
        print(f"\n  Sample markets:")
        cols = ["question", "poly_prob_a", "volume", "liquidity"]
        print(df_poly[cols].head(10).to_string(index=False))

    # 3. Sharp Pinnacle lines (if API key set)
    df_odds = fetch_pinnacle_odds()

    print("\n" + "=" * 60)
    print("  ✓ DATA PIPELINE COMPLETE")
    print(f"  → Historical: data/raw/matches_combined.parquet")
    print(f"  → Polymarket: data/polymarket/tennis_markets_*.json")
    print(f"  → Odds:       data/odds/pinnacle_*.csv (if key set)")
    print("  Next: python 02_features_and_train.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
