"""
V2 PICK GENERATOR — ranks by MODEL_PROB (winner conviction), not edge.

Philosophy shift from v1:
  - v1 ranked by edge = model_prob − poly_price
  - v2 analysis (edge_analysis.py 2026-04-20) showed edge has ~0 correlation
    with returns (Pearson r = -0.245). The bankable signal is raw winner-pick
    accuracy (71.2% WR on 66 resolved H2H bets).
  - Therefore v2 ranks picks by `model_prob` itself, with a soft filter to
    avoid markets where poly already agrees at ≥90c (no room to run).

Output: logs/v2_picks.jsonl  — one JSON object per pick, one run per line.

Run:
    python3 generate_v2_picks.py [--min-prob 0.65] [--min-volume 3000]
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ─── PATHS ────────────────────────────────────────────────────────────────────
V2_DIR   = Path(__file__).parent.resolve()
MODELS   = V2_DIR / "models"
DATA     = V2_DIR / "data"
LOGS     = V2_DIR / "logs"

# Persistent disk on Render — fall back to local
PERSIST  = Path("/data")
if PERSIST.exists() and PERSIST.is_dir():
    LOGS = PERSIST / "v2_logs"
    DATA = PERSIST / "v2_data"
LOGS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

PICKS_FILE = LOGS / "v2_picks.jsonl"
MODEL_PATH = MODELS / "tennis_xgb_v2.pkl"
MATCHES_PQT = DATA / "matches_combined_v2.parquet"

# ─── POLYMARKET ───────────────────────────────────────────────────────────────
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

def fetch_tennis_markets(min_volume: float = 3000) -> list[dict]:
    """Pull open tennis H2H markets from Polymarket gamma."""
    # Broad tag query; filter client-side so we can adapt easily.
    url = f"{GAMMA}/markets"
    params = {"closed": "false", "limit": 500, "tag_id": 100639}  # tennis tag
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"[gamma] failed: {e}", file=sys.stderr)
        return []

    out = []
    for m in rows:
        try:
            vol = float(m.get("volume", 0) or 0)
            if vol < min_volume:
                continue
            # Only H2H (2-outcome) markets
            outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
            if len(outcomes) != 2:
                continue
            out.append(m)
        except Exception:
            continue
    return out


def parse_market_players(market: dict) -> tuple[str, str] | None:
    """Extract player names from a polymarket question like 'Rome: Sinner vs Alcaraz'."""
    q = market.get("question", "")
    # Patterns: "X vs Y", "Tournament: X vs Y", "X beat Y"
    for sep in (" vs. ", " vs ", " v ", " beat "):
        if sep in q:
            tail = q.split(":", 1)[-1] if ":" in q else q
            parts = [p.strip() for p in tail.split(sep)]
            if len(parts) == 2:
                return parts[0], parts[1]
    return None


# ─── MODEL FEATURES ──────────────────────────────────────────────────────────
SURFACE_MAP = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}

def _fuzzy(a: str, b: str, thr: float = 0.75) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= thr


def find_player(all_players: set[str], name: str) -> str | None:
    name_l = name.lower().strip()
    for p in all_players:
        if p.lower() == name_l:
            return p
    # Last-name match
    last = name_l.split()[-1] if name_l else ""
    if last:
        matches = [p for p in all_players if p.lower().split()[-1] == last]
        if len(matches) == 1:
            return matches[0]
    # Fuzzy
    best, best_s = None, 0.0
    for p in all_players:
        s = SequenceMatcher(None, name_l, p.lower()).ratio()
        if s > best_s:
            best, best_s = p, s
    return best if best_s >= 0.75 else None


def build_single_feature_row(df: pd.DataFrame, pa: str, pb: str,
                              surface: str, tour: str) -> dict | None:
    """Build one feature row for (pa, pb) at today's date."""
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    # Per-player 52w + surface + recent form
    def stats(player):
        p_all = df[(df["winner"] == player) | (df["loser"] == player)]
        p_all = p_all[p_all["date"] < today]
        cutoff = today - pd.Timedelta(days=365)
        p_52 = p_all[p_all["date"] >= cutoff]
        if len(p_52) == 0:
            return None
        wins_52 = (p_52["winner"] == player).sum()
        n_52 = len(p_52)
        p_surf = p_52[p_52["surface"] == surface]
        wins_surf = (p_surf["winner"] == player).sum() if len(p_surf) else 0
        last10 = p_all.sort_values("date").tail(10)
        wins_10 = (last10["winner"] == player).sum()
        last20 = p_all.sort_values("date").tail(20)
        wins_20 = (last20["winner"] == player).sum()
        recent = p_all[p_all["date"] >= today - pd.Timedelta(days=7)]
        sets_7d = 0
        for sc in recent.get("score", pd.Series(dtype=str)).astype(str):
            sets_7d += sum(1 for s in sc.split() if "-" in s and "[" not in s)
        last = p_all["date"].max()
        days_since = int((today - last).days) if pd.notna(last) else 30
        return {
            "wr_52w":   wins_52 / n_52 if n_52 > 5 else np.nan,
            "wr_surf":  wins_surf / len(p_surf) if len(p_surf) > 3 else np.nan,
            "wr_l10":   wins_10 / len(last10) if len(last10) else np.nan,
            "wr_l20":   wins_20 / len(last20) if len(last20) else np.nan,
            "n_52w":    n_52,
            "n_surf":   len(p_surf),
            "days_since": days_since,
            "sets_7d":  sets_7d,
        }

    sa = stats(pa); sb = stats(pb)
    if sa is None or sb is None:
        return None

    # H2H
    h2h = df[
        (((df["winner"] == pa) & (df["loser"] == pb)) |
         ((df["winner"] == pb) & (df["loser"] == pa)))
        & (df["date"] < today)
    ]
    h2h_total = len(h2h)
    h2h_p1_wins = (h2h["winner"] == pa).sum() / h2h_total if h2h_total > 0 else 0.5
    h2h_surf = h2h[h2h["surface"] == surface] if surface else h2h
    h2h_surf_p1 = (h2h_surf["winner"] == pa).sum() / len(h2h_surf) if len(h2h_surf) > 0 else 0.5

    # Rank — pull from most recent match
    pa_rank = _latest_rank(df, pa, today)
    pb_rank = _latest_rank(df, pb, today)

    rank_diff = pa_rank - pb_rank if not (pd.isna(pa_rank) or pd.isna(pb_rank)) else np.nan
    rank_ratio = pa_rank / pb_rank if pb_rank and pb_rank > 0 and not pd.isna(pa_rank) else np.nan
    log_rank_ratio = np.log(rank_ratio) if rank_ratio and rank_ratio > 0 else np.nan

    return {
        "surface": SURFACE_MAP.get(surface, 0),
        "tour": 1 if tour == "wta" else 0,
        "round": 3,
        "tourney_level": 2,
        "rank_diff": rank_diff,
        "rank_ratio": rank_ratio,
        "log_rank_ratio": log_rank_ratio,
        "a_win_rate_52w": sa["wr_52w"], "b_win_rate_52w": sb["wr_52w"],
        "a_win_rate_surf_52w": sa["wr_surf"], "b_win_rate_surf_52w": sb["wr_surf"],
        "a_win_rate_l10": sa["wr_l10"], "b_win_rate_l10": sb["wr_l10"],
        "a_win_rate_l20": sa["wr_l20"], "b_win_rate_l20": sb["wr_l20"],
        "form_diff_52w": (sa["wr_52w"] - sb["wr_52w"]) if not (pd.isna(sa["wr_52w"]) or pd.isna(sb["wr_52w"])) else np.nan,
        "form_diff_surf": (sa["wr_surf"] - sb["wr_surf"]) if not (pd.isna(sa["wr_surf"]) or pd.isna(sb["wr_surf"])) else np.nan,
        "a_days_since_last": sa["days_since"], "b_days_since_last": sb["days_since"],
        "a_sets_last_7d": sa["sets_7d"], "b_sets_last_7d": sb["sets_7d"],
        "fatigue_diff": sa["sets_7d"] - sb["sets_7d"],
        "h2h_total": h2h_total, "h2h_p1_wins": h2h_p1_wins,
        "h2h_surf_p1": h2h_surf_p1, "h2h_advantage": int(h2h_p1_wins > 0.5),
        "ace_diff": np.nan, "df_diff": np.nan, "first_in_diff": np.nan,
        "win1st_diff": np.nan, "win2nd_diff": np.nan,
        "a_n_matches_52w": sa["n_52w"], "b_n_matches_52w": sb["n_52w"],
    }


def _latest_rank(df: pd.DataFrame, player: str, as_of: pd.Timestamp) -> float:
    recent = df[
        ((df["winner"] == player) | (df["loser"] == player))
        & (df["date"] < as_of)
    ].sort_values("date").tail(1)
    if recent.empty:
        return np.nan
    row = recent.iloc[0]
    if row["winner"] == player:
        return float(row.get("w_rank", np.nan) or np.nan)
    return float(row.get("l_rank", np.nan) or np.nan)


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-prob", type=float, default=0.65,
                    help="Minimum model_prob to include as a pick (default 0.65)")
    ap.add_argument("--max-poly", type=float, default=0.90,
                    help="Skip markets where poly already prices >= this (default 0.90)")
    ap.add_argument("--min-volume", type=float, default=3000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print(f"  V2 PICK GENERATOR — ranking by model_prob (conviction)")
    print(f"  min_prob={args.min_prob}  max_poly={args.max_poly}  "
          f"min_volume=${args.min_volume:,.0f}")
    print("=" * 64)

    # Load model
    if not MODEL_PATH.exists():
        print(f"[fatal] no model at {MODEL_PATH}", file=sys.stderr)
        return 2
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    model    = bundle["model"]
    feat_cols = bundle["feature_cols"]
    print(f"[model] loaded {MODEL_PATH.name} ({len(feat_cols)} features)")

    # Load match history
    if not MATCHES_PQT.exists():
        print(f"[fatal] no parquet at {MATCHES_PQT}", file=sys.stderr)
        return 2
    df = pd.read_parquet(MATCHES_PQT)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    print(f"[data ] {len(df):,} historical matches "
          f"({df['date'].min().date()} → {df['date'].max().date()})")

    all_players = set(df["winner"].dropna().astype(str)) | set(df["loser"].dropna().astype(str))

    # Pull live tennis markets
    mkts = fetch_tennis_markets(min_volume=args.min_volume)
    print(f"[poly ] {len(mkts)} tennis H2H markets with volume >= ${args.min_volume:,.0f}")

    picks = []
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    for m in mkts:
        players = parse_market_players(m)
        if not players:
            continue
        poly_name_a, poly_name_b = players

        pa = find_player(all_players, poly_name_a)
        pb = find_player(all_players, poly_name_b)
        if not pa or not pb:
            continue

        # Surface: gamma doesn't expose it cleanly. Default to Hard; override
        # via event title if clearly clay/grass/indoor. Good enough signal.
        title = (m.get("question", "") + " " + m.get("eventTitle", "")).lower()
        surface = "Clay" if "clay" in title or "rolland" in title or "roland" in title \
                 else "Grass" if "wimbledon" in title or "grass" in title \
                 else "Hard"
        tour = "wta" if any(k in title for k in ["wta", "women"]) else "atp"

        feats = build_single_feature_row(df, pa, pb, surface, tour)
        if feats is None:
            continue
        X = pd.DataFrame([feats])[feat_cols]
        prob_a = float(model.predict_proba(X)[0, 1])  # label==1 means pa wins
        prob_b = 1.0 - prob_a

        # Poly prices (outcomes are [yes_a, yes_b] roughly)
        try:
            prices_json = m.get("outcomePrices")
            if isinstance(prices_json, str):
                prices = json.loads(prices_json)
            else:
                prices = prices_json or []
            poly_a = float(prices[0]) if prices else np.nan
            poly_b = float(prices[1]) if len(prices) > 1 else np.nan
        except Exception:
            continue

        # Pick side based on v2 philosophy: follow the model's conviction
        if prob_a >= prob_b:
            side, prob, poly = "A", prob_a, poly_a
            pick_name = poly_name_a
            other = poly_name_b
        else:
            side, prob, poly = "B", prob_b, poly_b
            pick_name = poly_name_b
            other = poly_name_a

        # Filters
        if prob < args.min_prob:
            continue
        if not np.isnan(poly) and poly >= args.max_poly:
            # Market already agrees — no room to run
            continue

        token_ids = []
        try:
            tj = m.get("clobTokenIds")
            token_ids = json.loads(tj) if isinstance(tj, str) else (tj or [])
        except Exception:
            pass
        pick_token = token_ids[0 if side == "A" else 1] if len(token_ids) >= 2 else None

        edge = prob - poly if not np.isnan(poly) else np.nan
        picks.append({
            "run_id": run_id,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
            "market_id": m.get("id"),
            "slug": m.get("slug"),
            "question": m.get("question"),
            "tournament": m.get("eventTitle", "") or m.get("event", ""),
            "surface": surface,
            "tour": tour,
            "player_a": poly_name_a,
            "player_b": poly_name_b,
            "pick": pick_name,
            "pick_side": side,
            "fade": other,
            "model_prob": round(prob, 4),
            "model_prob_a": round(prob_a, 4),
            "model_prob_b": round(prob_b, 4),
            "poly_price": round(poly, 4) if not np.isnan(poly) else None,
            "poly_price_a": round(poly_a, 4) if not np.isnan(poly_a) else None,
            "poly_price_b": round(poly_b, 4) if not np.isnan(poly_b) else None,
            "edge": round(edge, 4) if not np.isnan(edge) else None,
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidityNum", m.get("liquidity", 0)) or 0),
            "poly_link": f"https://polymarket.com/event/{m.get('slug','')}",
            "token_id": pick_token,
            "end_date": m.get("endDate"),
        })

    # Rank by model_prob DESC (winner conviction)
    picks.sort(key=lambda p: -p["model_prob"])
    for i, p in enumerate(picks, 1):
        p["rank"] = i

    print(f"\n[picks] {len(picks)} picks pass filters")
    for p in picks[:15]:
        poly_str = f"poly={p['poly_price']:.2f}" if p["poly_price"] is not None else "poly=?"
        print(f"  #{p['rank']:<2} {p['pick']:<24} "
              f"prob={p['model_prob']:.3f} {poly_str}  [{p['tournament']}]")

    if args.dry_run:
        print("\n[dry-run] not writing")
        return 0

    # Append run to jsonl
    with open(PICKS_FILE, "a") as f:
        for p in picks:
            f.write(json.dumps(p) + "\n")
    print(f"\n✓ appended {len(picks)} picks to {PICKS_FILE}")

    # Also write "latest only" for fast dashboard reads
    latest = LOGS / "v2_picks_latest.json"
    latest.write_text(json.dumps({
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "picks": picks,
    }, indent=2))
    print(f"✓ wrote latest snapshot → {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
