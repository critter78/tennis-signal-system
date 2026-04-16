#!/usr/bin/env python3
"""
Daily Performance Report

Shows signal performance for a specific date or date range.
Reads from the live Render server or local picks.jsonl.

Usage:
    python3 10_daily_report.py                     # Yesterday's report
    python3 10_daily_report.py --date 2026-04-15   # Specific date
    python3 10_daily_report.py --days 7            # Last 7 days
    python3 10_daily_report.py --all               # All-time summary
    python3 10_daily_report.py --server             # Pull from live server
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

PICKS_FILE = Path("logs/picks.jsonl")


def load_picks(from_server=False, server_url=None):
    """Load picks from local file or server."""
    if from_server:
        import requests
        url = server_url or "https://tennis-betting-server.onrender.com"
        try:
            r = requests.get(f"{url}/api/picks", timeout=30)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("picks", [])
        except Exception as e:
            print(f"  Server fetch failed ({e}), falling back to local file")

    if not PICKS_FILE.exists():
        print(f"  No picks file found at {PICKS_FILE}")
        return []

    picks = []
    with open(PICKS_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    picks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return picks


def filter_by_date(picks, target_date):
    """Filter picks logged on a specific date."""
    date_str = target_date.strftime("%Y-%m-%d")
    return [p for p in picks if (p.get("logged_at") or "")[:10] == date_str]


def filter_by_range(picks, start_date, end_date):
    """Filter picks logged within a date range."""
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    return [
        p for p in picks
        if start_str <= (p.get("logged_at") or "")[:10] <= end_str
    ]


def print_report(picks, title="Performance Report"):
    """Print a formatted performance report for a set of picks."""
    print()
    print("=" * 65)
    print(f"  {title}")
    print("=" * 65)

    if not picks:
        print("  No picks found for this period.")
        print("=" * 65)
        return

    total = len(picks)
    resolved = [p for p in picks if p.get("outcome") is not None]
    unresolved = [p for p in picks if p.get("outcome") is None]
    wins = [p for p in resolved if p.get("outcome") == "win"]
    losses = [p for p in resolved if p.get("outcome") == "loss"]

    # Edge breakdown
    has_edge = [p for p in picks if p.get("has_edge")]
    edge_resolved = [p for p in has_edge if p.get("outcome") is not None]
    edge_wins = [p for p in edge_resolved if p.get("outcome") == "win"]

    # PnL
    total_pnl = sum(p.get("pnl") or 0 for p in resolved)

    # Stats by tier
    strong = [p for p in picks if (p.get("edge") or 0) >= 10]
    solid = [p for p in picks if 7 <= (p.get("edge") or 0) < 10]
    watch = [p for p in picks if 5 <= (p.get("edge") or 0) < 7]
    marginal = [p for p in picks if 0 < (p.get("edge") or 0) < 5]

    print(f"\n  Total signals:     {total}")
    print(f"  Resolved:          {len(resolved)} ({len(wins)}W - {len(losses)}L)")
    print(f"  Unresolved:        {len(unresolved)}")
    print(f"  Win Rate:          {len(wins)/len(resolved)*100:.1f}%" if resolved else "  Win Rate:          —")
    print(f"  Total PnL:         ${total_pnl:+,.0f}")

    # Edge picks performance
    print(f"\n  ── Edge Picks Only ──")
    print(f"  With positive edge: {len(has_edge)}")
    if edge_resolved:
        print(f"  Resolved:          {len(edge_resolved)} ({len(edge_wins)}W - {len(edge_resolved)-len(edge_wins)}L)")
        print(f"  Edge Win Rate:     {len(edge_wins)/len(edge_resolved)*100:.1f}%")
        edge_pnl = sum(p.get("pnl") or 0 for p in edge_resolved)
        print(f"  Edge PnL:          ${edge_pnl:+,.0f}")

    # Tier breakdown
    print(f"\n  ── By Signal Tier ──")
    for label, tier_picks in [("STRONG (10%+)", strong), ("SOLID (7-10%)", solid),
                                ("WATCH (5-7%)", watch), ("MARGINAL (0-5%)", marginal)]:
        tr = [p for p in tier_picks if p.get("outcome") is not None]
        tw = [p for p in tr if p.get("outcome") == "win"]
        if tr:
            wr = len(tw) / len(tr) * 100
            pnl = sum(p.get("pnl") or 0 for p in tr)
            print(f"  {label:20s}  {len(tw)}W-{len(tr)-len(tw)}L  ({wr:.0f}%)  ${pnl:+,.0f}")
        else:
            print(f"  {label:20s}  {len(tier_picks)} picks (none resolved)")

    # Surface breakdown
    print(f"\n  ── By Surface ──")
    for surface in ["Hard", "Clay", "Grass"]:
        sp = [p for p in resolved if p.get("surface") == surface]
        if sp:
            sw = [p for p in sp if p.get("outcome") == "win"]
            pnl = sum(p.get("pnl") or 0 for p in sp)
            print(f"  {surface:10s}  {len(sw)}W-{len(sp)-len(sw)}L  ({len(sw)/len(sp)*100:.0f}%)  ${pnl:+,.0f}")

    # Top wins and worst losses
    if resolved:
        print(f"\n  ── Top 5 Wins ──")
        top_wins = sorted([p for p in wins if p.get("pnl") and p.get("pnl") > 0], key=lambda x: x.get("pnl", 0), reverse=True)[:5]
        for p in top_wins:
            print(f"  ${p['pnl']:+.0f}  {p.get('match','?'):40s}  edge={p.get('edge',0):+.1f}%")

        print(f"\n  ── Worst 5 Losses ──")
        worst = sorted([p for p in losses if p.get("pnl")], key=lambda x: x.get("pnl", 0))[:5]
        for p in worst:
            print(f"  ${p['pnl']:+.0f}  {p.get('match','?'):40s}  edge={p.get('edge',0):+.1f}%")

    # Individual picks detail
    print(f"\n  ── All Picks (Resolved) ──")
    for p in sorted(resolved, key=lambda x: x.get("pnl") or 0, reverse=True):
        outcome = p.get("outcome", "?")
        icon = "W" if outcome == "win" else "L" if outcome == "loss" else "?"
        pnl = p.get("pnl") or 0
        match = p.get("match", "?")[:40]
        edge = p.get("edge", 0)
        surface = p.get("surface", "?")
        print(f"  [{icon}] ${pnl:+6.0f}  {match:40s}  edge={edge:+.1f}%  {surface}")

    print()
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Daily performance report")
    parser.add_argument("--date", type=str, help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Last N days")
    parser.add_argument("--all", action="store_true", help="All-time report")
    parser.add_argument("--server", action="store_true", help="Pull from live server")
    parser.add_argument("--url", type=str, default="https://tennis-betting-server.onrender.com",
                        help="Server URL")
    args = parser.parse_args()

    picks = load_picks(from_server=args.server, server_url=args.url)
    if not picks:
        print("No picks data available.")
        return

    print(f"\n  Loaded {len(picks)} total picks")

    if args.all:
        print_report(picks, "ALL-TIME PERFORMANCE")
    elif args.days:
        end = datetime.now()
        start = end - timedelta(days=args.days)
        filtered = filter_by_range(picks, start, end)
        print_report(filtered, f"LAST {args.days} DAYS ({start.strftime('%b %d')} - {end.strftime('%b %d')})")
    elif args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d")
        filtered = filter_by_date(picks, target)
        print_report(filtered, f"REPORT FOR {target.strftime('%A, %B %d, %Y')}")
    else:
        # Default: yesterday
        yesterday = datetime.now() - timedelta(days=1)
        filtered = filter_by_date(picks, yesterday)
        print_report(filtered, f"YESTERDAY ({yesterday.strftime('%A, %B %d, %Y')})")


if __name__ == "__main__":
    main()
