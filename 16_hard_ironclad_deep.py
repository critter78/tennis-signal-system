"""
Hard-Ironclad Deep Dive — is the 95-97¢ favorite arbitrage reproducible?

Examines wallet 0xb595d09c in detail:
  • Entry price distribution (histogram)
  • Cumulative P&L over time — is edge consistent, or front-loaded?
  • Loss analysis — what were the 0.3% that missed?
  • Size distribution per trade
  • Market-tier breakdown
  • Naive-follower simulation: "buy every heavy favorite at entry price P, size $S"

Usage:
    python3 16_hard_ironclad_deep.py                    # Hard-Ironclad (default)
    python3 16_hard_ironclad_deep.py --wallet 0x34ed5b3a   # another wallet
"""
import argparse, json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

DATA = Path("data/polymarket")
DEFAULT_WALLET = "0xb595d09c"  # Hard-Ironclad

def hist_ascii(values, bins, width=40, label_fmt="{:.2f}"):
    """Print ASCII histogram."""
    counts, edges = np.histogram(values, bins=bins)
    maxc = max(counts) if len(counts) else 1
    for i, c in enumerate(counts):
        pct = (c / counts.sum() * 100) if counts.sum() else 0
        bar = "█" * int(c / maxc * width)
        print(f"  {label_fmt.format(edges[i])}-{label_fmt.format(edges[i+1])}: {bar} {c:>5,} ({pct:4.1f}%)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", default=DEFAULT_WALLET)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    # ── Find wallet ──
    stats = pd.read_parquet(DATA / "wallet_stats.parquet")
    hit = stats[stats.wallet.str.lower().str.startswith(args.wallet.lower())]
    if hit.empty:
        raise SystemExit(f"wallet prefix '{args.wallet}' not found")
    wrow = hit.iloc[0]
    wallet_full = wrow.wallet
    display = wrow.display_name or "unknown"
    label = args.label or display
    print(f"\n╭─ DEEP DIVE: {label} ({wallet_full[:14]}..) ─╮")
    print(f"│ Headline: P&L ${wrow.total_pnl:,.0f} · Vol ${wrow.total_volume:,.0f} · "
          f"Hit {wrow.hit_rate*100:.1f}% · ROI {wrow.roi*100:.1f}% · {wrow.markets_traded} markets")
    print(f"╰───")

    # ── Load their trades ──
    print(f"\n[1] loading their trades ...")
    df = pd.read_parquet(DATA / "pm_trades.parquet")
    t = df[df.wallet == wallet_full].copy()
    print(f"    {len(t):,} trades ({(t.side=='BUY').sum():,} BUYs, {(t.side=='SELL').sum():,} SELLs)")

    # Enrich with market + resolution
    raw = json.load(open(DATA / "pm_tennis_markets_hist.json"))
    mk_rows = []
    for m in raw:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid: continue
        tokens = m.get("clobTokenIds"); prices = m.get("outcomePrices")
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except: tokens = []
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []
        win = None
        if tokens and prices and len(tokens) == 2 and len(prices) == 2:
            try:
                p0, p1 = float(prices[0]), float(prices[1])
                if p0 == 1.0: win = str(tokens[0])
                elif p1 == 1.0: win = str(tokens[1])
            except: pass
        ev = ""
        if isinstance(m.get("events"), list) and m["events"]:
            ev = m["events"][0].get("slug", "")
        mk_rows.append({"conditionId": cid, "question": m.get("question",""),
                        "winning_token": win, "event_slug": ev,
                        "endDate": m.get("endDate","")})
    mk = pd.DataFrame(mk_rows).set_index("conditionId")

    t = t.join(mk, on="conditionId", how="left")
    t["is_winner"] = t["asset"] == t["winning_token"]
    t["resolved"] = t["winning_token"].notna()

    # ── 1. Entry price distribution ──
    buys = t[t.side == "BUY"].copy()
    print(f"\n[2] ENTRY PRICE DISTRIBUTION ({len(buys):,} BUYs)")
    print(f"    mean={buys.price.mean():.3f}  median={buys.price.median():.3f}  "
          f"p25={buys.price.quantile(0.25):.3f}  p75={buys.price.quantile(0.75):.3f}")
    hist_ascii(buys.price, bins=[0, 0.50, 0.70, 0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99, 1.01])

    # ── 2. Size distribution ──
    print(f"\n[3] TRADE SIZE (USDC) DISTRIBUTION")
    print(f"    mean=${buys.usdc.mean():,.0f}  median=${buys.usdc.median():,.0f}  "
          f"p90=${buys.usdc.quantile(0.9):,.0f}  max=${buys.usdc.max():,.0f}")
    edges = [0, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 100000, 1e9]
    hist_ascii(buys.usdc.clip(upper=100000), bins=edges, label_fmt="${:,.0f}")

    # ── 3. Entry price vs. win rate (the arbitrage test) ──
    print(f"\n[4] WIN RATE BY ENTRY PRICE BUCKET (resolved buys only)")
    rb = buys[buys.resolved].copy()
    rb["price_bucket"] = pd.cut(rb.price,
        bins=[0, 0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99, 1.01],
        labels=["<80¢","80-85","85-90","90-93","93-95","95-97","97-99","99¢+"])
    br = rb.groupby("price_bucket", observed=False).agg(
        n=("is_winner","count"), wins=("is_winner","sum"),
        avg_price=("price","mean"), total_usdc=("usdc","sum"),
    )
    br["win_rate"] = (br.wins / br.n.clip(lower=1) * 100).round(1)
    br["pnl_per_dollar"] = ((1 - br.avg_price) * br.win_rate/100 - br.avg_price * (1 - br.win_rate/100)).round(3)
    print(f"    {'bucket':<8}  {'bets':>6}  {'avg_px':>7}  {'win%':>6}  {'vol$':>12}  {'PnL/$':>7}")
    for idx, r in br.iterrows():
        if r.n == 0: continue
        print(f"    {str(idx):<8}  {int(r.n):>6,}  {r.avg_price:>7.3f}  {r.win_rate:>5.1f}%  "
              f"${r.total_usdc:>10,.0f}  {r.pnl_per_dollar:>+7.3f}")

    # ── 4. Cumulative P&L over time ──
    print(f"\n[5] CUMULATIVE P&L OVER TIME (by month of trade)")
    rb["dt"] = pd.to_datetime(rb["ts"], unit="s", utc=True)
    rb["month"] = rb["dt"].dt.to_period("M").astype(str)
    # trade-level pnl: settle=1 if winner, 0 if loser; cost = price
    rb["trade_pnl"] = np.where(rb["is_winner"], (1 - rb.price) * rb["size"], -rb.price * rb["size"])
    monthly = rb.groupby("month").agg(
        bets=("size","count"), pnl=("trade_pnl","sum"), vol=("usdc","sum")
    ).reset_index()
    monthly["cum_pnl"] = monthly.pnl.cumsum()
    print(f"    {'month':<8}  {'bets':>5}  {'vol$':>10}  {'pnl$':>10}  {'cum_pnl$':>12}")
    for r in monthly.itertuples():
        print(f"    {r.month:<8}  {int(r.bets):>5,}  ${r.vol:>8,.0f}  ${r.pnl:>+8,.0f}  ${r.cum_pnl:>+10,.0f}")

    # ── 5. Losses: what went wrong? ──
    losses = rb[~rb.is_winner].copy().sort_values("trade_pnl").head(15)
    print(f"\n[6] TOP 15 BIGGEST LOSSES ({len(rb[~rb.is_winner])} total losses, "
          f"{len(rb[~rb.is_winner])/len(rb)*100:.1f}% of resolved buys)")
    for r in losses.itertuples():
        q = (r.question or "")[:60]
        print(f"    ${r.trade_pnl:>+8,.0f}  @ {r.price:.3f}  size=${r.size:>6,.0f}  {q}")

    # ── 6. Naive follower simulation ──
    print(f"\n[7] NAIVE-FOLLOWER SIMULATION")
    print(f"    Q: if you'd bought every PM tennis favorite above a price threshold,")
    print(f"       size-unit $1,000 per market, what's your P&L + ROI?")
    print(f"       (universe: ALL resolved tennis markets, not just theirs)")
    # Load ALL trades briefly to find ALL favorites — use market endpoint prices instead
    print(f"       loading all trades for favorite-price detection ...")
    all_tr = pd.read_parquet(DATA / "pm_trades.parquet",
                             columns=["conditionId","asset","price","size","ts"])
    # Find average price per (market, asset) — quick proxy for "market price"
    avg_px = all_tr.groupby(["conditionId","asset"], as_index=False).agg(
        market_px=("price","mean"), market_vol=("size","sum"))
    avg_px = avg_px.merge(mk[["winning_token"]].reset_index(), on="conditionId", how="inner")
    avg_px = avg_px[avg_px.winning_token.notna()]
    avg_px["is_winner"] = avg_px.asset == avg_px.winning_token

    for threshold in [0.85, 0.90, 0.93, 0.95, 0.97]:
        subset = avg_px[(avg_px.market_px >= threshold) & (avg_px.market_px < threshold + 0.02)]
        if subset.empty: continue
        # Assume buy at market_px, $1000 size → shares = 1000/price, payout if winner = shares*1
        subset = subset.copy()
        subset["pnl"] = np.where(subset.is_winner,
                                 1000/subset.market_px - 1000,
                                 -1000)
        n = len(subset); wins = subset.is_winner.sum()
        tot_pnl = subset.pnl.sum()
        roi = tot_pnl / (n * 1000) * 100 if n else 0
        print(f"    threshold {threshold:.2f}-{threshold+0.02:.2f}: "
              f"{n:>5,} markets, {wins:>4,} wins ({wins/n*100:4.1f}%), "
              f"pnl=${tot_pnl:>+10,.0f} on ${n*1000:>10,.0f} staked, ROI {roi:+5.1f}%")

    print(f"\n✓ deep-dive done")

if __name__ == "__main__":
    main()
