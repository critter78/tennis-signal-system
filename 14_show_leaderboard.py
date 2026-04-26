"""
Sharp Wallets Leaderboard Viewer — reads wallet_stats.parquet and prints rankings.

Usage:
    python3 14_show_leaderboard.py                      # top 20 by P&L, $50k vol floor
    python3 14_show_leaderboard.py --top 50             # top 50
    python3 14_show_leaderboard.py --vol 100000         # $100k vol floor
    python3 14_show_leaderboard.py --sort roi           # sort by ROI
    python3 14_show_leaderboard.py --sort hit --bets 50 # by hit rate, min 50 bets
    python3 14_show_leaderboard.py --find starry        # search pseudonym/wallet
    python3 14_show_leaderboard.py --archetype scalper  # preset: late scalpers
    python3 14_show_leaderboard.py --archetype alpha    # preset: pre-match edge
"""
import argparse
import pandas as pd
from pathlib import Path

DATA = Path("data/polymarket")
FILE = DATA / "wallet_stats.parquet"

def load():
    if not FILE.exists():
        raise SystemExit(f"✗ {FILE} not found — run 13_pm_wallet_pnl.py first")
    return pd.read_parquet(FILE)

def print_table(df, title):
    print(f"\n— {title} —")
    print(f"{'#':>3}  {'wallet':<14}  {'pseudonym':<24}  {'pnl':>12}  {'volume':>12}  {'roi':>7}  {'hit':>6}  {'bets':>5}")
    print("-" * 100)
    for i, r in enumerate(df.itertuples(), 1):
        print(f"{i:>3}  {r.wallet[:10]+'..':<14}  {(r.display_name or '')[:24]:<24}  "
              f"${r.total_pnl:>10,.0f}  ${r.total_volume:>10,.0f}  "
              f"{r.roi*100:>6.1f}%  {r.hit_rate*100:>5.1f}%  {r.markets_traded:>5}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--vol", type=float, default=50_000, help="min total volume (USD)")
    ap.add_argument("--bets", type=int, default=0, help="min markets traded")
    ap.add_argument("--sort", choices=["pnl","roi","hit","volume","bets"], default="pnl")
    ap.add_argument("--find", type=str, help="search pseudonym or wallet substring")
    ap.add_argument("--archetype", choices=["scalper","alpha","whale"], help="preset filter")
    args = ap.parse_args()

    df = load()
    print(f"loaded {len(df):,} wallets  total_volume=${df.total_volume.sum():,.0f}  total_pnl=${df.total_pnl.sum():,.0f}")

    # Archetype presets
    if args.archetype == "scalper":
        df = df[(df.hit_rate >= 0.80) & (df.markets_traded >= 50) & (df.total_volume >= args.vol)]
        title = f"LATE SCALPERS (hit>=80%, bets>=50, vol>=${args.vol:,.0f})"
    elif args.archetype == "alpha":
        df = df[(df.hit_rate.between(0.40, 0.70)) & (df.roi >= 0.20) & (df.markets_traded >= 20) & (df.total_volume >= args.vol)]
        title = f"PRE-MATCH ALPHA (hit 40-70%, roi>=20%, bets>=20, vol>=${args.vol:,.0f})"
    elif args.archetype == "whale":
        df = df[df.total_volume >= max(args.vol, 1_000_000)]
        title = f"WHALES (vol>=${max(args.vol,1_000_000):,.0f})"
    else:
        df = df[(df.total_volume >= args.vol) & (df.markets_traded >= args.bets)]
        title = f"TOP {args.top} BY {args.sort.upper()} (vol>=${args.vol:,.0f}, bets>={args.bets})"

    if args.find:
        needle = args.find.lower()
        mask = df.display_name.fillna("").str.lower().str.contains(needle) | df.wallet.str.lower().str.contains(needle)
        df = df[mask]
        title = f"MATCHES for '{args.find}'"

    sort_map = {"pnl":"total_pnl", "roi":"roi", "hit":"hit_rate", "volume":"total_volume", "bets":"markets_traded"}
    df = df.sort_values(sort_map[args.sort], ascending=False).head(args.top)

    if df.empty:
        print("\n(no wallets match filters)")
        return
    print_table(df, title)

if __name__ == "__main__":
    main()
