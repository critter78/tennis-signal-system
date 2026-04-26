"""
Sharp Wallets — Phase 4: Edge Analysis Engine

For each focus wallet, reverse-engineers *why* they win by computing:

    1.  Surface breakdown           (hard / clay / grass / unknown)
    2.  Round breakdown             (R128 → F, from event/market text)
    3.  Tournament tier             (GS / ATP1000 / ATP500-250 / Challenger / WTA / other)
    4.  Entry timing                (median hours before market close)
    5.  Favorite vs. dog bias       (avg entry price, weighted by USD size)
    6.  Side tendency               (% of BUY volume on eventual winner)
    7.  Closing-line value proxy    (entry price vs. last price in final hour)

Inputs (produced by 12/13):
    data/polymarket/pm_trades.parquet
    data/polymarket/wallet_market_pnl.parquet
    data/polymarket/wallet_stats.parquet
    data/polymarket/pm_tennis_markets_hist.json

Outputs:
    data/polymarket/wallet_edge_profiles.json        — per-wallet deep profile
    data/polymarket/wallet_edge_summary.parquet      — flat table for dashboard join

Usage:
    python3 15_pm_edge_analysis.py                   # top 10 by P&L (vol>=$50k, bets>=20)
    python3 15_pm_edge_analysis.py --top 25
    python3 15_pm_edge_analysis.py --wallets 0x76062e7b,0x5d58e38c   # specific wallets
"""
import os, json, argparse, time, re
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

DATA = Path("data/polymarket")

# ── Tournament tier lookup by event slug fragments ──
GS_SLUGS       = ["australian-open", "french-open", "roland-garros", "wimbledon", "us-open"]
ATP1000_SLUGS  = ["indian-wells", "miami-open", "monte-carlo", "madrid-open",
                  "italian-open", "rome-", "canadian-open", "toronto-masters",
                  "cincinnati", "shanghai-masters", "paris-masters", "atp-finals"]
CHALLENGER_KEY = "challenger"
WTA_KEY        = "wta-"

# ── Surface inference by event slug ──
SLUG_SURFACE = {
    "wimbledon": "Grass",
    "halle":     "Grass",
    "queens":    "Grass",
    "stuttgart": "Grass",  # grass in summer window
    "eastbourne": "Grass",
    "french-open": "Clay", "roland-garros": "Clay",
    "monte-carlo": "Clay", "madrid-open": "Clay",
    "italian-open": "Clay", "rome-": "Clay",
    "barcelona": "Clay", "hamburg": "Clay",
    "munich": "Clay", "estoril": "Clay",
    "australian-open": "Hard", "us-open": "Hard",
    "indian-wells": "Hard", "miami-open": "Hard",
    "canadian-open": "Hard", "toronto-masters": "Hard",
    "cincinnati": "Hard", "shanghai-masters": "Hard",
    "paris-masters": "Hard", "atp-finals": "Hard",
    "dubai-": "Hard", "acapulco": "Hard", "basel": "Hard",
    "vienna": "Hard", "tokyo": "Hard",
}

ROUND_RE = re.compile(
    r"\b(final|semifinal|quarterfinal|round[- ]of[- ](16|32|64|128)|r16|r32|r64|r128)\b",
    re.IGNORECASE,
)

def classify_tier(slug: str) -> str:
    s = (slug or "").lower()
    if CHALLENGER_KEY in s: return "Challenger"
    if WTA_KEY in s: return "WTA"
    if any(g in s for g in GS_SLUGS): return "Grand Slam"
    if any(m in s for m in ATP1000_SLUGS): return "ATP 1000"
    return "ATP 500/250"

def classify_surface(slug: str, question: str) -> str:
    s = (slug or "").lower()
    q = (question or "").lower()
    for key, surf in SLUG_SURFACE.items():
        if key in s: return surf
    # fallback: try text
    if "clay" in q: return "Clay"
    if "grass" in q: return "Grass"
    if "hard" in q: return "Hard"
    return "Unknown"

def classify_round(question: str) -> str:
    m = ROUND_RE.search(question or "")
    if not m: return "Pre-R128/Unknown"
    text = m.group(1).lower()
    if "final" in text and "semi" not in text and "quarter" not in text: return "Final"
    if "semi" in text: return "Semifinal"
    if "quarter" in text: return "Quarterfinal"
    if "16" in text: return "R16"
    if "32" in text: return "R32"
    if "64" in text: return "R64"
    if "128" in text: return "R128"
    return "Other"

def load_markets() -> pd.DataFrame:
    """Load market metadata from json → DataFrame with slug, surface, tier, round, end_ts."""
    raw = json.load(open(DATA / "pm_tennis_markets_hist.json"))
    rows = []
    for m in raw:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid: continue
        ev_slug = ""
        if isinstance(m.get("events"), list) and m["events"]:
            ev_slug = m["events"][0].get("slug", "") or ""
        slug = (m.get("slug") or "") + " " + ev_slug
        question = m.get("question", "") or ""
        tokens = m.get("clobTokenIds")
        prices = m.get("outcomePrices")
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except: tokens = []
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []
        winning_token = None
        if tokens and prices and len(tokens) == 2 and len(prices) == 2:
            try:
                p0, p1 = float(prices[0]), float(prices[1])
                if p0 == 1.0 and p1 == 0.0:   winning_token = str(tokens[0])
                elif p0 == 0.0 and p1 == 1.0: winning_token = str(tokens[1])
            except: pass
        end_dt = m.get("endDate") or m.get("end_date") or ""
        try:
            end_ts = int(pd.Timestamp(end_dt).timestamp()) if end_dt else 0
        except Exception:
            end_ts = 0
        rows.append({
            "conditionId": cid,
            "event_slug": ev_slug,
            "question": question,
            "tier": classify_tier(slug),
            "surface": classify_surface(slug, question),
            "round": classify_round(question),
            "winning_token": winning_token,
            "resolved": winning_token is not None,
            "end_ts": end_ts,
        })
    df = pd.DataFrame(rows)
    print(f"  markets enriched: {len(df):,}  resolved: {df.resolved.sum():,}")
    return df

def pick_focus_wallets(top: int, explicit: list[str] | None, vol_floor: float, bets_floor: int) -> list[str]:
    stats = pd.read_parquet(DATA / "wallet_stats.parquet")
    if explicit:
        keep = [w.lower() for w in explicit]
        hit = stats[stats.wallet.str.lower().str.startswith(tuple(keep))]
        return hit.wallet.tolist()
    f = stats[(stats.total_volume >= vol_floor) & (stats.markets_traded >= bets_floor)]
    return f.sort_values("total_pnl", ascending=False).head(top).wallet.tolist()

def load_trades_for(wallets: list[str]) -> pd.DataFrame:
    """Read pm_trades.parquet, filter to focus wallets. Much faster than full scan."""
    wset = set(wallets)
    print(f"  loading pm_trades.parquet (filtering to {len(wset)} wallets) ...")
    df = pd.read_parquet(DATA / "pm_trades.parquet")
    df = df[df.wallet.isin(wset)].copy()
    print(f"  trades for focus wallets: {len(df):,}")
    return df

def compute_clv_proxy(trades_all: pd.DataFrame, focus_trades: pd.DataFrame) -> pd.DataFrame:
    """
    For each (wallet, conditionId, asset) entry, compute CLV = avg price in final hour - wallet's avg entry price.
    Positive = bought below closing price (market agreed with them).
    """
    # Need end_ts joined already; use ts to determine "last hour" per market
    # Build market-level closing price: mean of trade prices in [end_ts - 3600, end_ts]
    if "end_ts" not in trades_all.columns:
        return pd.DataFrame(columns=["wallet","conditionId","asset","clv"])
    in_window = trades_all[trades_all["ts"] >= (trades_all["end_ts"] - 3600)]
    close_px = in_window.groupby(["conditionId","asset"], as_index=False)["price"].mean() \
                        .rename(columns={"price":"close_px"})
    # Wallet entry price (volume-weighted on BUY side only)
    buys = focus_trades[focus_trades["side"] == "BUY"].copy()
    buys["weighted"] = buys["price"] * buys["size"]
    entry = buys.groupby(["wallet","conditionId","asset"], as_index=False).agg(
        entry_usd=("size","sum"),
        wsum=("weighted","sum"),
    )
    entry["entry_px"] = entry["wsum"] / entry["entry_usd"].clip(lower=1e-9)
    m = entry.merge(close_px, on=["conditionId","asset"], how="left")
    m["clv"] = m["close_px"] - m["entry_px"]
    return m[["wallet","conditionId","asset","entry_px","close_px","clv","entry_usd"]]

def profile_wallet(wallet: str, tr: pd.DataFrame, mkt_pnl_w: pd.DataFrame,
                   clv: pd.DataFrame, display_name: str,
                   stats_row: pd.Series) -> dict:
    """Compute all 7 breakdowns for one wallet."""
    t = tr[tr["wallet"] == wallet].copy()
    if t.empty:
        return {"wallet": wallet, "error": "no trades"}

    # BUYs only for entry analysis
    buys = t[t["side"] == "BUY"].copy()

    # ─── 1. Surface ───
    surf = t.groupby("surface").agg(
        volume=("usdc","sum"), trades=("side","count")
    ).reset_index()
    surf_pnl = mkt_pnl_w.groupby("surface", dropna=False).agg(
        pnl=("pnl","sum"), markets=("conditionId","nunique")
    ).reset_index()
    surface_bd = surf.merge(surf_pnl, on="surface", how="outer").fillna(0)

    # ─── 2. Round ───
    round_bd = mkt_pnl_w.groupby("round", dropna=False).agg(
        pnl=("pnl","sum"), markets=("conditionId","nunique"), volume=("volume_usdc","sum")
    ).reset_index()

    # ─── 3. Tier ───
    tier_bd = mkt_pnl_w.groupby("tier", dropna=False).agg(
        pnl=("pnl","sum"), markets=("conditionId","nunique"), volume=("volume_usdc","sum")
    ).reset_index()

    # ─── 4. Entry timing ───
    buys_with_end = buys[buys["end_ts"] > 0].copy()
    buys_with_end["hours_before_close"] = (buys_with_end["end_ts"] - buys_with_end["ts"]) / 3600.0
    buys_with_end = buys_with_end[buys_with_end["hours_before_close"] > 0]
    timing = {
        "median_hours_before_close": float(buys_with_end["hours_before_close"].median()) if len(buys_with_end) else None,
        "p25_hours_before_close":    float(buys_with_end["hours_before_close"].quantile(0.25)) if len(buys_with_end) else None,
        "p75_hours_before_close":    float(buys_with_end["hours_before_close"].quantile(0.75)) if len(buys_with_end) else None,
        "late_scalper_share":        float((buys_with_end["hours_before_close"] < 3).mean()) if len(buys_with_end) else None,
    }

    # ─── 5. Favorite vs. dog bias (avg ENTRY price weighted by USD) ───
    if len(buys):
        wavg_entry = float((buys["price"] * buys["size"]).sum() / buys["size"].sum())
        bins = [0, 0.30, 0.50, 0.70, 1.0]
        labels = ["heavy_dog","dog","favorite","heavy_favorite"]
        buys["bucket"] = pd.cut(buys["price"], bins=bins, labels=labels, include_lowest=True)
        bias_dist = buys.groupby("bucket", observed=False)["size"].sum()
        bias_dist = (bias_dist / bias_dist.sum()).to_dict() if bias_dist.sum() else {}
    else:
        wavg_entry, bias_dist = None, {}

    # ─── 6. Side tendency (did they buy the eventual winner?) ───
    buys["on_winner"] = (buys["asset"] == buys["winning_token"]) & buys["resolved"]
    if len(buys) and buys["resolved"].any():
        resolved_buys = buys[buys["resolved"]]
        yes_on_winner_rate = float((resolved_buys["on_winner"] * resolved_buys["size"]).sum() /
                                   resolved_buys["size"].sum()) if len(resolved_buys) else None
    else:
        yes_on_winner_rate = None

    # ─── 7. Closing-line value ───
    clv_w = clv[clv["wallet"] == wallet]
    if len(clv_w):
        # weight CLV by wallet's USD exposure on that leg
        w = clv_w["entry_usd"]
        clv_weighted = float((clv_w["clv"] * w).sum() / w.sum()) if w.sum() > 0 else None
        clv_median   = float(clv_w["clv"].median())
        # positive CLV rate = % of entries where they bought below close
        clv_positive = float((clv_w["clv"] > 0).mean())
    else:
        clv_weighted, clv_median, clv_positive = None, None, None

    return {
        "wallet": wallet,
        "display_name": display_name,
        "stats": {
            "total_pnl": float(stats_row.total_pnl),
            "total_volume": float(stats_row.total_volume),
            "markets_traded": int(stats_row.markets_traded),
            "hit_rate": float(stats_row.hit_rate),
            "roi": float(stats_row.roi),
        },
        "surface_breakdown": surface_bd.to_dict("records"),
        "round_breakdown": round_bd.to_dict("records"),
        "tier_breakdown": tier_bd.to_dict("records"),
        "entry_timing": timing,
        "price_bias": {
            "weighted_avg_entry_price": wavg_entry,
            "distribution": bias_dist,
        },
        "side_tendency": {
            "yes_on_winner_rate": yes_on_winner_rate,
        },
        "closing_line_value": {
            "clv_weighted": clv_weighted,
            "clv_median": clv_median,
            "clv_positive_rate": clv_positive,
            "entries_analyzed": int(len(clv_w)),
        },
    }

def print_summary(profiles: list[dict]):
    print(f"\n— EDGE PROFILES —\n")
    for p in profiles:
        if "error" in p: continue
        s = p["stats"]
        t = p["entry_timing"]
        pb = p["price_bias"]
        st = p["side_tendency"]
        cv = p["closing_line_value"]
        print(f"🎯 {p['display_name'][:25]:<25} ({p['wallet'][:10]}..)")
        print(f"   P&L: ${s['total_pnl']:>10,.0f} | Vol: ${s['total_volume']:>10,.0f} | "
              f"Hit: {s['hit_rate']*100:4.1f}% | ROI: {s['roi']*100:5.1f}% | Markets: {s['markets_traded']}")
        if t.get('median_hours_before_close') is not None:
            print(f"   ⏱  Entry timing: median {t['median_hours_before_close']:.1f}h before close | "
                  f"late scalp share: {t['late_scalper_share']*100:.0f}%")
        if pb.get('weighted_avg_entry_price') is not None:
            dist = pb['distribution']
            print(f"   💰 Avg entry price: {pb['weighted_avg_entry_price']:.3f} | "
                  f"fav/dog mix: "
                  + " ".join([f"{k}:{v*100:.0f}%" for k, v in dist.items() if v > 0.01]))
        if st.get('yes_on_winner_rate') is not None:
            print(f"   ✓  Bought YES on eventual WINNER: {st['yes_on_winner_rate']*100:.1f}% of vol")
        if cv.get('clv_weighted') is not None:
            sign = '+' if cv['clv_weighted'] >= 0 else ''
            print(f"   📈 CLV (vs final hour): {sign}{cv['clv_weighted']*100:.2f}¢ weighted | "
                  f"positive-CLV entries: {cv['clv_positive_rate']*100:.0f}% of {cv['entries_analyzed']}")
        # Top tier breakdown line
        tiers = sorted([r for r in p["tier_breakdown"] if r.get("markets", 0) > 0],
                       key=lambda r: r.get("pnl", 0), reverse=True)[:3]
        if tiers:
            print(f"   🏆 Top tiers by pnl: "
                  + " | ".join([f"{r['tier']}: ${r['pnl']:,.0f} ({r['markets']}mk)" for r in tiers]))
        # Top surface
        surfs = sorted([r for r in p["surface_breakdown"] if r.get("markets", 0) > 0],
                       key=lambda r: r.get("pnl", 0), reverse=True)[:3]
        if surfs:
            print(f"   🎾 Top surfaces:    "
                  + " | ".join([f"{r['surface']}: ${r['pnl']:,.0f} ({r['markets']:.0f}mk)" for r in surfs]))
        print()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--vol", type=float, default=50_000)
    ap.add_argument("--bets", type=int, default=20)
    ap.add_argument("--wallets", type=str, help="comma-separated wallet prefixes (0xABC,0xDEF)")
    args = ap.parse_args()

    t0 = time.time()
    explicit = [w.strip() for w in args.wallets.split(",")] if args.wallets else None

    print("[1/5] picking focus wallets ...")
    wallets = pick_focus_wallets(args.top, explicit, args.vol, args.bets)
    print(f"  {len(wallets)} wallets selected")

    print("[2/5] loading + enriching markets ...")
    markets = load_markets()

    print("[3/5] loading trades for focus wallets ...")
    focus_trades = load_trades_for(wallets)
    # enrich with market metadata
    mk_idx = markets.set_index("conditionId")[["event_slug","tier","surface","round","winning_token","resolved","end_ts"]]
    focus_trades = focus_trades.join(mk_idx, on="conditionId", how="left")

    # market-level pnl (per wallet) enriched with tier/surface/round
    mkt_pnl = pd.read_parquet(DATA / "wallet_market_pnl.parquet")
    mkt_pnl = mkt_pnl[mkt_pnl.wallet.isin(set(wallets))]
    mkt_pnl = mkt_pnl.merge(markets[["conditionId","tier","surface","round"]], on="conditionId", how="left")

    print("[4/5] computing CLV proxy ...")
    # For CLV we need the full per-market trade prices near end_ts — use ALL trades in focus markets
    focus_cids = focus_trades["conditionId"].unique().tolist()
    # Load all trades for these markets (they're already in focus_trades for OUR wallets — but for CLV we need ALL wallets' trades on the same markets)
    if focus_cids:
        print(f"   loading ALL trades on {len(focus_cids)} focus markets for closing-price proxy ...")
        all_trades = pd.read_parquet(DATA / "pm_trades.parquet",
                                     columns=["conditionId","asset","side","size","price","ts"])
        all_trades = all_trades[all_trades["conditionId"].isin(set(focus_cids))]
        all_trades = all_trades.merge(markets[["conditionId","end_ts"]], on="conditionId", how="left")
        clv = compute_clv_proxy(all_trades, focus_trades)
    else:
        clv = pd.DataFrame(columns=["wallet","conditionId","asset","clv","entry_usd"])

    print("[5/5] profiling wallets ...")
    stats = pd.read_parquet(DATA / "wallet_stats.parquet").set_index("wallet")
    profiles = []
    for w in wallets:
        tr_w = focus_trades[focus_trades["wallet"] == w]
        pnl_w = mkt_pnl[mkt_pnl["wallet"] == w]
        sr = stats.loc[w] if w in stats.index else pd.Series(dtype=object)
        disp = sr.get("display_name", "") if len(sr) else ""
        try:
            profiles.append(profile_wallet(w, tr_w, pnl_w, clv, disp, sr))
        except Exception as e:
            profiles.append({"wallet": w, "error": str(e)})

    # Save JSON
    out_json = DATA / "wallet_edge_profiles.json"
    out_json.write_text(json.dumps(profiles, indent=2, default=str))
    print(f"\n✓ wrote {out_json}  ({len(profiles)} profiles)")

    # Flat summary parquet for dashboard
    flat = []
    for p in profiles:
        if "error" in p: continue
        s = p["stats"]; t = p["entry_timing"]; pb = p["price_bias"]
        st = p["side_tendency"]; cv = p["closing_line_value"]
        flat.append({
            "wallet": p["wallet"],
            "display_name": p.get("display_name"),
            "total_pnl": s["total_pnl"],
            "total_volume": s["total_volume"],
            "markets_traded": s["markets_traded"],
            "hit_rate": s["hit_rate"],
            "roi": s["roi"],
            "median_hours_before_close": t.get("median_hours_before_close"),
            "late_scalper_share": t.get("late_scalper_share"),
            "weighted_avg_entry_price": pb.get("weighted_avg_entry_price"),
            "yes_on_winner_rate": st.get("yes_on_winner_rate"),
            "clv_weighted": cv.get("clv_weighted"),
            "clv_positive_rate": cv.get("clv_positive_rate"),
        })
    if flat:
        pd.DataFrame(flat).to_parquet(DATA / "wallet_edge_summary.parquet", index=False, compression="zstd")
        print(f"✓ wrote {DATA / 'wallet_edge_summary.parquet'}")

    print_summary(profiles)
    print(f"\n✓ done in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
