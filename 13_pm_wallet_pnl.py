"""
Sharp Wallets — Phase 3: Wallet P&L Engine

Streams the 4.65M trades in data/polymarket/pm_trades/*.json,
joins against pm_tennis_markets_hist.json for resolution,
and produces:

    data/polymarket/pm_trades.parquet         — all trades in one columnar file
    data/polymarket/wallet_market_pnl.parquet — one row per (wallet, market)
    data/polymarket/wallet_stats.parquet      — one row per wallet (leaderboard)

Methodology
-----------
For each (wallet, conditionId, asset):
    position   = Σ size[BUY] - Σ size[SELL]          # remaining shares at settlement
    cost       = Σ (size × price)[BUY]                # cash out buying
    proceeds   = Σ (size × price)[SELL]               # cash in selling
    settlement = position × 1.0  if asset won, else 0.0
    pnl        = proceeds + settlement - cost

Per market P&L = sum over both tokens (handles hedgers who traded both sides).
Per wallet    = sum over all markets.

Scale: ~30-60 sec wall clock on a laptop.
"""
import os
import json
import time
import glob
import pandas as pd
from pathlib import Path

DATA = Path("data/polymarket")
TRADES_DIR = DATA / "pm_trades"

def load_markets() -> pd.DataFrame:
    raw = json.load(open(DATA / "pm_tennis_markets_hist.json"))
    rows = []
    for m in raw:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid:
            continue
        tokens = m.get("clobTokenIds")
        prices = m.get("outcomePrices")
        # parse — both are stringified JSON arrays in gamma responses
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
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        rows.append({
            "conditionId": cid,
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "eventSlug": (m.get("events") or [{}])[0].get("slug", "") if isinstance(m.get("events"), list) else "",
            "endDate": m.get("endDate", ""),
            "volume": float(m.get("volumeNum") or m.get("volume") or 0),
            "outcomes": outcomes,
            "clob_tokens": [str(t) for t in tokens] if tokens else [],
            "winning_token": winning_token,
            "resolved": winning_token is not None,
        })
    df = pd.DataFrame(rows)
    print(f"  markets: {len(df):,}  resolved: {df.resolved.sum():,}  total_volume: ${df.volume.sum():,.0f}")
    return df

def stream_trades() -> pd.DataFrame:
    files = sorted(glob.glob(str(TRADES_DIR / "*.json")))
    rows, t0 = [], time.time()
    for i, f in enumerate(files):
        try:
            trades = json.load(open(f))
        except Exception:
            continue
        for t in trades:
            rows.append((
                t.get("proxyWallet", ""),
                t.get("conditionId", ""),
                str(t.get("asset", "")),
                t.get("side", ""),
                float(t.get("size") or 0),
                float(t.get("price") or 0),
                int(t.get("timestamp") or 0),
                t.get("outcome", ""),
                t.get("pseudonym", ""),
                t.get("name", ""),
            ))
        if (i + 1) % 5000 == 0:
            print(f"  streamed {i+1:,}/{len(files):,} market files  "
                  f"rows={len(rows):,}  {(i+1)/(time.time()-t0):.0f} files/sec")
    df = pd.DataFrame(rows, columns=[
        "wallet","conditionId","asset","side","size","price","ts","outcome","pseudonym","name"
    ])
    df["usdc"] = df["size"] * df["price"]
    print(f"  total trades: {len(df):,}")
    return df

def compute_pnl(trades: pd.DataFrame, markets: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate trades → (wallet, market) P&L and then → wallet stats."""
    mk = markets.set_index("conditionId")[["winning_token","resolved","question","endDate","volume"]]
    df = trades.join(mk, on="conditionId", how="left")

    # keep only trades on resolved markets for P&L accuracy
    df = df[df["resolved"] == True].copy()
    df["is_winning_token"] = df["asset"] == df["winning_token"]
    df["signed_size"] = df.apply(lambda r: r["size"] if r["side"]=="BUY" else -r["size"], axis=1)
    df["signed_usdc"] = df.apply(lambda r: -r["usdc"] if r["side"]=="BUY" else r["usdc"], axis=1)

    # per (wallet, market, asset) → position + cash-flow
    g = df.groupby(["wallet","conditionId","asset","is_winning_token"], sort=False).agg(
        position=("signed_size","sum"),
        cashflow=("signed_usdc","sum"),
        trades=("side","count"),
        volume_usdc=("usdc","sum"),
    ).reset_index()

    # settlement payout for remaining position on the winning token
    g["settlement"] = (g["position"].clip(lower=0) * g["is_winning_token"].astype(float)).round(6)
    g["pnl"] = g["cashflow"] + g["settlement"]

    # roll up to (wallet, market)
    market_pnl = g.groupby(["wallet","conditionId"], as_index=False).agg(
        pnl=("pnl","sum"),
        trades=("trades","sum"),
        volume_usdc=("volume_usdc","sum"),
    )
    market_pnl = market_pnl.merge(
        markets[["conditionId","question","endDate","volume"]].rename(columns={"volume":"market_volume"}),
        on="conditionId", how="left"
    )
    market_pnl["won"] = market_pnl["pnl"] > 0
    market_pnl["lost"] = market_pnl["pnl"] < 0

    # roll up to wallet
    wallet = market_pnl.groupby("wallet", as_index=False).agg(
        total_pnl=("pnl","sum"),
        total_volume=("volume_usdc","sum"),
        markets_traded=("conditionId","nunique"),
        wins=("won","sum"),
        losses=("lost","sum"),
    )
    wallet["hit_rate"] = wallet["wins"] / (wallet["wins"] + wallet["losses"]).clip(lower=1)
    wallet["roi"] = wallet["total_pnl"] / wallet["total_volume"].clip(lower=1)

    # attach display name (most frequently used pseudonym per wallet)
    disp = trades[trades["pseudonym"] != ""].groupby("wallet")["pseudonym"].agg(
        lambda s: s.value_counts().index[0]
    ).rename("display_name")
    wallet = wallet.merge(disp, on="wallet", how="left")

    return market_pnl, wallet

def main():
    t0 = time.time()
    print("[1/4] loading markets ...")
    markets = load_markets()

    print("[2/4] streaming trades ...")
    trades = stream_trades()

    print("[3/4] writing pm_trades.parquet ...")
    trades.astype({"size":"float64","price":"float64","usdc":"float64"}).to_parquet(
        DATA / "pm_trades.parquet", index=False, compression="zstd"
    )

    print("[4/4] computing P&L ...")
    market_pnl, wallet = compute_pnl(trades, markets)
    market_pnl.to_parquet(DATA / "wallet_market_pnl.parquet", index=False, compression="zstd")
    wallet.to_parquet(DATA / "wallet_stats.parquet", index=False, compression="zstd")

    print(f"\n✓ done in {time.time()-t0:.0f}s")
    print(f"  wallets: {len(wallet):,}")
    print(f"  (wallet, market) pairs: {len(market_pnl):,}")
    print(f"  total volume: ${wallet.total_volume.sum():,.0f}")
    print(f"  total P&L (zero-sum check): ${wallet.total_pnl.sum():,.0f} (should be ≈ -fees, ~0)")

    # quick preview of top 20 by risk-adjusted P&L
    # filter to wallets with enough sample to be meaningful
    SHARP_VOL_FLOOR = 50_000  # $50k settled volume minimum
    sharp = wallet[wallet["total_volume"] >= SHARP_VOL_FLOOR].copy()
    sharp = sharp.sort_values("total_pnl", ascending=False).head(20)
    print(f"\n— TOP 20 WALLETS (min ${SHARP_VOL_FLOOR:,} volume) —")
    print(f"{'#':>3}  {'wallet':<14}  {'pseudonym':<22}  {'pnl':>12}  {'volume':>12}  {'roi':>6}  {'hit':>5}  {'bets':>5}")
    for i, r in enumerate(sharp.itertuples(), 1):
        print(f"{i:>3}  {r.wallet[:10]+'..':<14}  {(r.display_name or '')[:22]:<22}  "
              f"${r.total_pnl:>10,.0f}  ${r.total_volume:>10,.0f}  "
              f"{r.roi*100:>5.1f}%  {r.hit_rate*100:>4.1f}%  {r.markets_traded:>5}")

if __name__ == "__main__":
    main()
