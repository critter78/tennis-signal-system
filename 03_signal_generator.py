"""
TENNIS POLYMARKET SIGNAL SYSTEM
Script 3: Signal Generator

Pulls live Polymarket tennis markets, runs model predictions,
and flags matches where model probability diverges from Polymarket price.

Output signals table:
  match | surface | poly_price_a | model_prob_a | edge | rec | kelly

Usage:
    python 03_signal_generator.py [--min-edge 0.05] [--min-volume 5000]
"""

import pickle
import json
import argparse
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests
from pathlib import Path
from datetime import datetime, timedelta
from difflib import SequenceMatcher

DATA_DIR   = Path("data")
MODELS_DIR = Path("models")

# ─── KELLY CRITERION ─────────────────────────────────────────────────────────

def kelly_fraction(prob: float, poly_price: float,
                   kelly_frac: float = 0.25) -> float:
    """
    Fractional Kelly stake given model probability and Polymarket implied odds.
    poly_price is in cents (0-100) → convert to decimal odds.
    Returns fraction of bankroll to bet (0 if no edge).
    """
    if poly_price <= 0 or poly_price >= 100:
        return 0.0
    b    = (100 / poly_price) - 1   # net odds (e.g., 60¢ → 0.667)
    p    = prob
    q    = 1 - p
    k    = (b * p - q) / b
    return max(0.0, round(k * kelly_frac, 4))


# ─── NAME MATCHING ────────────────────────────────────────────────────────────

def fuzzy_match(name_a: str, name_b: str, threshold: float = 0.6) -> bool:
    """Fuzzy name matching for linking Polymarket player names to historical data."""
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a == b:
        return True
    # Last name match
    a_last = a.split()[-1] if a else ""
    b_last = b.split()[-1] if b else ""
    if a_last and b_last and a_last == b_last:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def find_player_in_df(df: pd.DataFrame, poly_name: str) -> str:
    """Find closest match to a Polymarket player name in historical data."""
    all_players = set(df["winner"].dropna()) | set(df["loser"].dropna())
    best, best_score = "", 0.0
    for p in all_players:
        score = SequenceMatcher(None, poly_name.lower(), p.lower()).ratio()
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= 0.55 else ""


# ─── LOAD MODEL ──────────────────────────────────────────────────────────────

def load_model():
    meta_path = MODELS_DIR / "latest_model.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            "No trained model found — run 02_features_and_train.py first"
        )
    with open(meta_path) as f:
        meta = json.load(f)
    with open(meta["path"], "rb") as f:
        bundle = pickle.load(f)
    print(f"Loaded model v{meta['version']} "
          f"(AUC {meta['mean_auc']:.4f}, {meta['n_samples']:,} training samples)")
    return bundle["model"], bundle["feature_cols"]


# ─── LIVE POLYMARKET FETCH ────────────────────────────────────────────────────

def fetch_live_markets(min_volume: float = 0) -> pd.DataFrame:
    """Pull current ATP + WTA markets from Polymarket Gamma API."""
    url = "https://gamma-api.polymarket.com/markets"
    rows = []

    for tag in ["tennis", "atp-tennis", "wta-tennis"]:
        params = {
            "tag_slug": tag,
            "active":   "true",
            "closed":   "false",
            "limit":    500,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            markets = r.json()
        except Exception as e:
            print(f"  [warn] tag={tag} failed: {e}")
            continue

        for m in markets:
            vol = float(m.get("volume", 0) or 0)
            if vol < min_volume:
                continue

            tokens   = m.get("tokens", [])
            prices   = {}
            token_ids = {}

            # Gamma API returns outcomes/clobTokenIds/outcomePrices as JSON
            # strings — parse them into real lists
            raw_outcomes = m.get("outcomes", [])
            raw_clob     = m.get("clobTokenIds", [])
            raw_prices   = m.get("outcomePrices", [])
            if isinstance(raw_outcomes, str):
                try: raw_outcomes = json.loads(raw_outcomes)
                except: raw_outcomes = []
            if isinstance(raw_clob, str):
                try: raw_clob = json.loads(raw_clob)
                except: raw_clob = []
            if isinstance(raw_prices, str):
                try: raw_prices = json.loads(raw_prices)
                except: raw_prices = []
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: tokens = []

            # Build prices from tokens array first (keyed by outcome name)
            for t in tokens:
                if isinstance(t, dict):
                    name = t.get("outcome", t.get("name", ""))
                    price = t.get("price")
                    if name and price is not None:
                        prices[name] = float(price) * 100  # convert to cents

            # If tokens was empty, use outcomePrices + outcomes
            if not prices and raw_outcomes and raw_prices and len(raw_outcomes) == len(raw_prices):
                for i, name in enumerate(raw_outcomes):
                    try:
                        prices[name] = float(raw_prices[i]) * 100
                    except (ValueError, TypeError):
                        pass

            # Build token_ids from clobTokenIds mapped to outcome names
            if raw_clob and raw_outcomes and len(raw_clob) == len(raw_outcomes):
                for i, name in enumerate(raw_outcomes):
                    token_ids[name] = raw_clob[i]
            else:
                # Fallback: try token_id from tokens array
                for t in tokens:
                    if isinstance(t, dict):
                        name = t.get("outcome", t.get("name", ""))
                        tid = t.get("token_id", "")
                        if name and tid:
                            token_ids[name] = tid

            rows.append({
                "market_id": m.get("id") or m.get("condition_id"),
                "condition_id": m.get("condition_id", ""),
                "question":  m.get("question", ""),
                "slug":      m.get("slug", ""),
                "end_date":  m.get("end_date_iso") or m.get("endDateIso"),
                "volume":    vol,
                "liquidity": float(m.get("liquidity", 0) or 0),
                "prices":    prices,
                "token_ids": token_ids,
                "outcomes":  raw_outcomes,
            })

    # Deduplicate by market_id
    seen, deduped = set(), []
    for r in rows:
        if r["market_id"] not in seen:
            seen.add(r["market_id"])
            deduped.append(r)

    df = pd.DataFrame(deduped)
    print(f"  Fetched {len(df)} active tennis markets "
          f"(min volume: ${min_volume:,.0f})")
    return df


def parse_match_from_question(question: str):
    """
    Extract (player_a, player_b) from Polymarket question string.
    Handles formats like:
      - "Will X beat Y?"
      - "X vs Y"
      - "X to win vs Y"
    """
    import re
    q = question.strip()

    patterns = [
        r"Will (.+?) beat (.+?)\??$",
        r"([A-Z][a-zA-Z\s\-\.\']+?) vs\.? ([A-Z][a-zA-Z\s\-\.\']+)",
        r"([A-Z][a-zA-Z\s\-\.\']+?) to win",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            groups = m.groups()
            pa = groups[0].strip() if len(groups) > 0 else ""
            pb = groups[1].strip() if len(groups) > 1 else ""
            return pa, pb
    return "", ""


# ─── SIGNAL GENERATION ───────────────────────────────────────────────────────

def build_live_features(player_a: str, player_b: str,
                        df_hist: pd.DataFrame,
                        surface: str = "Hard",
                        feature_cols: list = None) -> pd.Series:
    """
    Build a single feature row for a live match using historical data.
    Falls back to ranking-only features if player not found.
    """
    from features_and_train import PlayerStatsEngine, get_h2h, FEATURE_COLS
    feature_cols = feature_cols or FEATURE_COLS

    today  = pd.Timestamp.now()
    engine = PlayerStatsEngine(df_hist)

    # Try to find matched player names
    pa_hist = find_player_in_df(df_hist, player_a)
    pb_hist = find_player_in_df(df_hist, player_b)

    sa = engine.get_stats(pa_hist, today, surface) if pa_hist else {}
    sb = engine.get_stats(pb_hist, today, surface) if pb_hist else {}
    h2 = get_h2h(df_hist, pa_hist, pb_hist, today, surface) if pa_hist and pb_hist else {
        "h2h_total": 0, "h2h_p1_wins": 0.5, "h2h_surf_p1": 0.5, "h2h_advantage": 0
    }

    def gs(stats, k, default=np.nan):
        return stats.get(k, default)

    row = {
        "surface":       {"Hard": 0, "Clay": 1, "Grass": 2}.get(surface, 0),
        "tour":          0,
        "round":         3,
        "tourney_level": 2,
        "rank_diff":     np.nan,
        "rank_ratio":    np.nan,
        "log_rank_ratio": np.nan,
        "a_win_rate_52w":      gs(sa, "win_rate_52w"),
        "b_win_rate_52w":      gs(sb, "win_rate_52w"),
        "a_win_rate_surf_52w": gs(sa, "win_rate_surf_52w"),
        "b_win_rate_surf_52w": gs(sb, "win_rate_surf_52w"),
        "a_win_rate_l10":      gs(sa, "win_rate_l10"),
        "b_win_rate_l10":      gs(sb, "win_rate_l10"),
        "a_win_rate_l20":      gs(sa, "win_rate_l20"),
        "b_win_rate_l20":      gs(sb, "win_rate_l20"),
        "form_diff_52w":   gs(sa,"win_rate_52w",0.5) - gs(sb,"win_rate_52w",0.5),
        "form_diff_surf":  gs(sa,"win_rate_surf_52w",0.5) - gs(sb,"win_rate_surf_52w",0.5),
        "a_days_since_last": gs(sa, "days_since_last", 7),
        "b_days_since_last": gs(sb, "days_since_last", 7),
        "a_sets_last_7d":    gs(sa, "sets_last_7d", 0),
        "b_sets_last_7d":    gs(sb, "sets_last_7d", 0),
        "fatigue_diff":      gs(sa,"sets_last_7d",0) - gs(sb,"sets_last_7d",0),
        "h2h_total":    h2["h2h_total"],
        "h2h_p1_wins":  h2["h2h_p1_wins"],
        "h2h_surf_p1":  h2["h2h_surf_p1"],
        "h2h_advantage": h2["h2h_advantage"],
        "ace_diff":      gs(sa,"ace_rate_w",0) - gs(sb,"ace_rate_w",0),
        "df_diff":       gs(sa,"df_rate_w",0) - gs(sb,"df_rate_w",0),
        "first_in_diff": gs(sa,"first_in_pct_w",0) - gs(sb,"first_in_pct_w",0),
        "win1st_diff":   gs(sa,"win_on_1st_w",0) - gs(sb,"win_on_1st_w",0),
        "win2nd_diff":   gs(sa,"win_on_2nd_w",0) - gs(sb,"win_on_2nd_w",0),
        "a_n_matches_52w": gs(sa,"n_matches_52w",0),
        "b_n_matches_52w": gs(sb,"n_matches_52w",0),
    }

    # Fill NaN with 0 for numeric cols that model expects
    series = pd.Series(row)
    series = series.fillna(0)
    return series[feature_cols]


def generate_signals(markets_df: pd.DataFrame, model, feature_cols: list,
                     df_hist: pd.DataFrame,
                     min_edge: float = 0.05,
                     kelly_frac: float = 0.25) -> pd.DataFrame:
    """
    For each Polymarket tennis match market, generate model prediction
    and compute edge vs. Polymarket price.
    """
    signals = []

    for _, mkt in markets_df.iterrows():
        q      = str(mkt.get("question", ""))
        prices = mkt.get("prices", {})
        tids   = mkt.get("token_ids", {})
        vol    = mkt.get("volume", 0)
        liq    = mkt.get("liquidity", 0)

        # Only process match-winner markets (binary)
        if len(prices) != 2:
            continue

        pa, pb = parse_match_from_question(q)
        if not pa:
            continue

        # Get Polymarket prices and token_ids for each player
        # Build parallel lists so we can fall back to positional matching
        price_keys = list(prices.keys())
        price_vals = list(prices.values())
        tid_keys   = list(tids.keys())
        tid_vals   = list(tids.values())

        poly_pa = poly_pb = None
        tid_a = tid_b = None

        # Try name-matching against price keys
        for i, key in enumerate(price_keys):
            if pa.split()[-1].lower() in key.lower():
                poly_pa = price_vals[i]
                # Try exact key in tids first, then positional fallback
                tid_a = tids.get(key, "")
                if not tid_a and i < len(tid_vals):
                    tid_a = tid_vals[i]
            elif pb.split()[-1].lower() in key.lower():
                poly_pb = price_vals[i]
                tid_b = tids.get(key, "")
                if not tid_b and i < len(tid_vals):
                    tid_b = tid_vals[i]

        # Fallback: just take the two prices/tids in order
        if poly_pa is None or poly_pb is None:
            if len(price_vals) == 2:
                poly_pa, poly_pb = price_vals[0], price_vals[1]
            else:
                continue
        # Always ensure token_ids are set — positional fallback
        if (not tid_a or not tid_b) and len(tid_vals) >= 2:
            tid_a = tid_a or tid_vals[0]
            tid_b = tid_b or tid_vals[1]

        # Build features + predict
        try:
            feat_row = build_live_features(pa, pb, df_hist,
                                           feature_cols=feature_cols)
            feat_df  = pd.DataFrame([feat_row])
            model_prob_a = model.predict_proba(feat_df)[0, 1]
        except Exception as e:
            # BUG FIX: Don't silently fall back to poly_price!
            # This causes model_prob == poly_price, making edge always 0
            print(f"WARNING: Model prediction failed for {pa} vs {pb}: {type(e).__name__}: {e}")
            # Instead of falling back to poly_pa/100, use a neutral 50% estimate
            model_prob_a = 0.5

        poly_prob_a = poly_pa / 100
        edge_a = model_prob_a - poly_prob_a
        edge_b = (1 - model_prob_a) - (poly_pb / 100)

        # Best edge direction
        if abs(edge_a) >= abs(edge_b):
            best_player = pa
            edge        = edge_a
            poly_price  = poly_pa
            model_prob  = model_prob_a
            best_tid    = tid_a
        else:
            best_player = pb
            edge        = edge_b
            poly_price  = poly_pb
            model_prob  = 1 - model_prob_a
            best_tid    = tid_b

        kelly = kelly_fraction(model_prob, poly_price, kelly_frac)

        if abs(edge) >= min_edge:
            signals.append({
                "match":       f"{pa} vs {pb}",
                "player_a":    pa,
                "player_b":    pb,
                "surface":     "Hard",  # surface detection can be added
                "bet_on":      best_player,
                "poly_price":  round(poly_price, 1),
                "model_prob":  round(model_prob * 100, 1),
                "edge":        round(edge * 100, 1),
                "kelly_stake": round(kelly * 100, 2),
                "volume":      round(vol, 0),
                "liquidity":   round(liq, 0),
                "question":    q,
                "token_id":    best_tid or "",
                "condition_id": mkt.get("condition_id", ""),
            })

    df = pd.DataFrame(signals)
    if not df.empty:
        df = df.sort_values("edge", ascending=False, key=abs)
    return df


# ─── DISPLAY ─────────────────────────────────────────────────────────────────

def print_signals(df: pd.DataFrame):
    if df.empty:
        print("\n  No signals above threshold.")
        return

    print(f"\n{'='*80}")
    print(f"  🎾  TENNIS POLYMARKET SIGNALS  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}")
    print(f"\n{'MATCH':<38} {'BET ON':<22} {'POLY':>6} {'MODEL':>7} {'EDGE':>7} {'KELLY':>7} {'VOL':>8}")
    print("-" * 100)

    for _, row in df.iterrows():
        match_str  = row["match"][:37]
        player_str = row["bet_on"][:21]
        direction  = "▲" if row["edge"] > 0 else "▼"
        print(
            f"  {match_str:<36} {player_str:<22} "
            f"{row['poly_price']:>5.1f}¢ {row['model_prob']:>6.1f}%  "
            f"{direction}{abs(row['edge']):>5.1f}%  "
            f"{row['kelly_stake']:>6.2f}%  "
            f"${row['volume']:>7,.0f}"
        )

    print(f"\n  {len(df)} signal(s) found")
    print(f"\n  Legend: POLY = Polymarket price (¢), MODEL = model probability (%)")
    print(f"          EDGE = model - market, KELLY = fractional Kelly stake (%)")
    print(f"\n  ⚠️  Always verify current Polymarket price before trading.")
    print(f"  ⚠️  Kelly stakes are fractional (25%). Size to your risk tolerance.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-edge",   type=float, default=0.05,
                        help="Minimum |model - market| edge to flag (default 0.05 = 5%%)")
    parser.add_argument("--min-volume", type=float, default=5000,
                        help="Minimum Polymarket market volume (default $5K)")
    parser.add_argument("--kelly-frac", type=float, default=0.25,
                        help="Kelly fraction multiplier (default 0.25)")
    parser.add_argument("--save",  action="store_true",
                        help="Save signals to signals/output_YYYYMMDD.csv")
    args = parser.parse_args()

    print("=" * 60)
    print("  TENNIS POLYMARKET SYSTEM — STEP 3: SIGNALS")
    print("=" * 60)

    # Load model
    model, feature_cols = load_model()

    # Load historical data for live feature computation
    hist_path = DATA_DIR / "raw" / "matches_combined.parquet"
    if not hist_path.exists():
        raise FileNotFoundError("Run 01_data_pipeline.py first to download historical data")
    df_hist = pd.read_parquet(hist_path)
    df_hist["date"] = pd.to_datetime(df_hist["date"], errors="coerce")
    print(f"Historical data loaded: {len(df_hist):,} matches")

    # Fetch live markets
    print("\nFetching live Polymarket tennis markets...")
    markets = fetch_live_markets(min_volume=args.min_volume)

    if markets.empty:
        print("No markets found.")
        return

    # Generate signals
    print("\nGenerating signals...")
    # Import feature building from script 2
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    signals = generate_signals(
        markets, model, feature_cols, df_hist,
        min_edge=args.min_edge,
        kelly_frac=args.kelly_frac,
    )

    print_signals(signals)

    if args.save and not signals.empty:
        out_dir = Path("signals")
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"signals_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        signals.drop(columns=["question"]).to_csv(out, index=False)
        print(f"\n  ✓ Signals saved → {out}")


if __name__ == "__main__":
    main()
