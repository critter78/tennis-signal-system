#!/usr/bin/env python3
"""
Tennis Betting Signal System — Peak Price Tracker

Pulls Polymarket price history for resolved picks to compute:
- Peak price reached during the match (max opportunity)
- Realistic exit price (90% of peak — accounts for slippage)
- Trade P&L (based on realistic exit vs entry price)
- Trade outcome: WIN if peak >= 1.10x entry (10% gain threshold)

This enables the LSTM to learn from TWO signals:
1. Match outcomes (who won)
2. Trading opportunities (which picks created profitable price windows)

A pick can be a match LOSS but a trade WIN — e.g., an undervalued player
who wins the first set, pushing their price from $0.20 to $0.45, even if
they ultimately lose the match.

Usage:
    python3 10_peak_tracker.py              # Track all resolved picks
    python3 10_peak_tracker.py --dry-run    # Preview without writing
    python3 10_peak_tracker.py --status     # Show trade stats
    python3 10_peak_tracker.py --force      # Re-track even if already tracked
"""

import json
import argparse
import requests
import time
import re
from pathlib import Path
from datetime import datetime, timedelta

LOGS_DIR = Path("logs")
PICKS_FILE = LOGS_DIR / "picks.jsonl"
TRACK_LOG = LOGS_DIR / "peak_tracking.log"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ── Configuration ──
SLIPPAGE_FACTOR = 0.90       # Realistic exit = 90% of peak (10% slippage)
TRADE_WIN_THRESHOLD = 1.10   # Peak must be >= 1.10x entry to count as trade win


def load_picks():
    """Load all picks from picks.jsonl."""
    picks = []
    if not PICKS_FILE.exists():
        return picks
    with open(PICKS_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    picks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return picks


def save_picks(picks):
    """Rewrite picks.jsonl with updated data."""
    PICKS_FILE.parent.mkdir(exist_ok=True)
    with open(PICKS_FILE, 'w') as f:
        for p in picks:
            f.write(json.dumps(p) + "\n")


def log_track(msg):
    """Append to tracking log."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"  {line}")
    TRACK_LOG.parent.mkdir(exist_ok=True)
    with open(TRACK_LOG, 'a') as f:
        f.write(line + "\n")


def extract_slug(pick):
    """Extract Polymarket event slug from pick data."""
    slug = pick.get("slug", "")
    if slug:
        return slug
    poly_link = pick.get("poly_link", "")
    if "/event/" in poly_link:
        return poly_link.split("/event/")[-1].split("/")[0].split("?")[0]
    return ""


def fetch_price_history_via_gamma(slug):
    """
    Fetch price history for a market via the Gamma API.
    Returns list of {timestamp, price} dicts for the bet-on outcome,
    or raw market data if timeseries not available.
    """
    if not slug:
        return None

    try:
        # Get the event/market details including token IDs
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        events = r.json()

        if not events:
            return None

        ev = events[0] if isinstance(events, list) else events
        markets = ev.get("markets", [])

        if not markets:
            # Event itself might be the market
            markets = [ev]

        for m in markets:
            # Get token IDs for price history lookup
            clob_token_ids = m.get("clobTokenIds", "")
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except (json.JSONDecodeError, TypeError):
                    clob_token_ids = []

            outcomes_raw = m.get("outcomes", "")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except (json.JSONDecodeError, TypeError):
                    outcomes = []
            else:
                outcomes = outcomes_raw or []

            # Return market info with token IDs for CLOB lookup
            return {
                "market_id": m.get("id") or m.get("conditionId", ""),
                "question": m.get("question", ""),
                "outcomes": outcomes,
                "clob_token_ids": clob_token_ids,
                "slug": slug,
            }

    except Exception as e:
        pass

    return None


def fetch_clob_price_history(token_id):
    """
    Fetch price timeseries from Polymarket CLOB API.
    Returns list of {t: timestamp, p: price} entries.
    """
    if not token_id:
        return []

    try:
        # CLOB prices endpoint — get historical prices
        r = requests.get(
            f"{CLOB_API}/prices-history",
            params={
                "market": token_id,
                "interval": "max",
                "fidelity": 5,  # 5-minute intervals
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        if isinstance(data, dict) and "history" in data:
            return data["history"]
        elif isinstance(data, list):
            return data

    except Exception:
        pass

    # Fallback: try the timeseries endpoint
    try:
        r = requests.get(
            f"{CLOB_API}/prices-history",
            params={
                "market": token_id,
                "interval": "1d",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "history" in data:
            return data["history"]
        elif isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def fetch_market_trades(token_id, limit=500):
    """
    Fetch actual trades for a market token from the CLOB API.
    This gives us real transaction prices which may show peaks
    that the mid-price timeseries misses.
    """
    if not token_id:
        return []

    try:
        r = requests.get(
            f"{CLOB_API}/trades",
            params={
                "asset_id": token_id,
                "limit": limit,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("data", data.get("trades", []))
    except Exception:
        pass

    return []


def compute_peak_from_prices(price_history, entry_price_cents):
    """
    Compute peak price from a price history series.
    entry_price_cents: the buy price in cents (e.g., 20 for $0.20)

    Returns dict with peak metrics or None if insufficient data.
    """
    if not price_history:
        return None

    prices = []
    for point in price_history:
        try:
            if isinstance(point, dict):
                p = float(point.get("p", point.get("price", 0)))
            elif isinstance(point, (int, float)):
                p = float(point)
            else:
                continue

            # Normalize: if price is 0-1 range, convert to cents
            if 0 < p <= 1.0:
                p = p * 100
            if p > 0:
                prices.append(p)
        except (ValueError, TypeError):
            continue

    if not prices:
        return None

    peak_price = max(prices)
    entry = entry_price_cents or 50  # default to 50c if unknown

    # Realistic exit: 90% of peak (10% slippage)
    realistic_exit = peak_price * SLIPPAGE_FACTOR

    # Trade P&L per $1 unit (in cents)
    trade_pnl = realistic_exit - entry

    # Trade outcome: win if peak >= 1.10x entry (10% gain threshold)
    trade_win = peak_price >= (entry * TRADE_WIN_THRESHOLD)

    return {
        "peak_price": round(peak_price, 1),
        "realistic_exit": round(realistic_exit, 1),
        "trade_pnl": round(trade_pnl, 1),
        "trade_outcome": "win" if trade_win else "loss",
        "entry_price": round(entry, 1),
        "price_range": round(max(prices) - min(prices), 1),
        "price_points": len(prices),
        "peak_multiple": round(peak_price / entry, 2) if entry > 0 else 0,
    }


def compute_peak_from_trades(trades, entry_price_cents, bet_on_name):
    """
    Compute peak price from actual trade data.
    Looks at trades on the token matching our bet-on player.
    """
    if not trades:
        return None

    prices = []
    bet_on_last = bet_on_name.split()[-1].lower() if bet_on_name else ""

    for trade in trades:
        try:
            price = float(trade.get("price", 0))
            if 0 < price <= 1.0:
                price = price * 100
            if price > 0:
                prices.append(price)
        except (ValueError, TypeError):
            continue

    if not prices:
        return None

    return compute_peak_from_prices(
        [{"p": p / 100} for p in prices],
        entry_price_cents
    )


def identify_bet_token(market_info, bet_on_name):
    """
    Figure out which CLOB token corresponds to our bet-on player.
    Returns the token_id for the player we bet on.
    """
    if not market_info:
        return None

    outcomes = market_info.get("outcomes", [])
    token_ids = market_info.get("clob_token_ids", [])

    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        # If we can't match, return first token (usually the "Yes" / player A token)
        return token_ids[0] if token_ids else None

    bet_on_last = bet_on_name.split()[-1].lower() if bet_on_name else ""

    for i, outcome in enumerate(outcomes):
        outcome_str = str(outcome).lower()
        # Check if this outcome matches our bet-on player
        if bet_on_last and (bet_on_last in outcome_str or outcome_str in bet_on_name.lower()):
            return token_ids[i] if i < len(token_ids) else None

    # Default: first token if no match found
    return token_ids[0] if token_ids else None


def track_peak_prices(dry_run=False, force=False):
    """Main peak tracking logic."""
    picks = load_picks()
    if not picks:
        print("  No picks found.")
        return

    # Find picks that need peak tracking:
    # - Must have an outcome (match is resolved) OR be >48h old (match likely done)
    # - Must not already have peak_price (unless force=True)
    # - Must have a poly_link or slug (so we can find the market)
    cutoff = datetime.utcnow() - timedelta(hours=48)
    eligible = []

    for i, pick in enumerate(picks):
        # Skip if already tracked (unless force)
        if pick.get("peak_price") is not None and not force:
            continue

        # Must have a way to find the market
        slug = extract_slug(pick)
        if not slug:
            continue

        # Must be resolved or old enough that match is likely done
        has_outcome = pick.get("outcome") is not None
        logged_at = pick.get("logged_at", "")
        is_old_enough = False
        if logged_at:
            try:
                pt = datetime.fromisoformat(logged_at.replace("Z", ""))
                is_old_enough = pt < cutoff
            except (ValueError, TypeError):
                pass

        if has_outcome or is_old_enough:
            eligible.append((i, pick, slug))

    print(f"\n{'='*60}")
    print(f"  PEAK PRICE TRACKER")
    print(f"{'='*60}")
    print(f"  Total picks:      {len(picks)}")
    print(f"  Already tracked:  {sum(1 for p in picks if p.get('peak_price') is not None)}")
    print(f"  Eligible to track: {len(eligible)}")

    if not eligible:
        print(f"  Nothing to track!")
        return

    # Batch by unique slug to minimize API calls
    slug_to_indices = {}
    for i, pick, slug in eligible:
        if slug not in slug_to_indices:
            slug_to_indices[slug] = []
        slug_to_indices[slug].append((i, pick))

    print(f"  Unique markets:   {len(slug_to_indices)}")
    print()

    tracked = 0
    failed = 0
    api_calls = 0
    MAX_API_CALLS = 80  # Rate limit protection

    for slug, pick_list in slug_to_indices.items():
        if api_calls >= MAX_API_CALLS:
            print(f"  [rate limit] Stopping at {MAX_API_CALLS} API calls")
            break

        # Fetch market info (1 API call per unique slug)
        market_info = fetch_price_history_via_gamma(slug)
        api_calls += 1

        if not market_info:
            failed += len(pick_list)
            continue

        # For each pick sharing this slug, get peak data
        for pick_idx, pick in pick_list:
            bet_on = pick.get("bet_on", "")
            entry_price = pick.get("poly_price", 50)

            # Identify which token is ours
            token_id = identify_bet_token(market_info, bet_on)

            if not token_id:
                failed += 1
                continue

            # Try price history first (less API-intensive)
            price_data = fetch_clob_price_history(token_id)
            api_calls += 1

            peak_result = None
            if price_data:
                peak_result = compute_peak_from_prices(price_data, entry_price)

            # Fallback: try actual trades
            if not peak_result:
                trades = fetch_market_trades(token_id)
                api_calls += 1
                if trades:
                    peak_result = compute_peak_from_trades(trades, entry_price, bet_on)

            if peak_result:
                if not dry_run:
                    pick["peak_price"] = peak_result["peak_price"]
                    pick["realistic_exit"] = peak_result["realistic_exit"]
                    pick["trade_pnl"] = peak_result["trade_pnl"]
                    pick["trade_outcome"] = peak_result["trade_outcome"]
                    pick["peak_multiple"] = peak_result["peak_multiple"]
                    pick["peak_tracked_at"] = datetime.utcnow().isoformat()

                log_track(
                    f"{'TRADE-WIN' if peak_result['trade_outcome'] == 'win' else 'TRADE-LOSS'}: "
                    f"{pick.get('match', '?')} | Entry: {entry_price}c | "
                    f"Peak: {peak_result['peak_price']}c ({peak_result['peak_multiple']}x) | "
                    f"Exit: {peak_result['realistic_exit']}c | "
                    f"Trade PnL: {peak_result['trade_pnl']:+.0f}c | "
                    f"Match: {pick.get('outcome', 'pending')}"
                )
                tracked += 1
            else:
                failed += 1

            # Rate limit: brief pause every 10 API calls
            if api_calls % 10 == 0:
                time.sleep(0.5)

    if tracked > 0 and not dry_run:
        save_picks(picks)
        print(f"\n  Updated picks.jsonl")

    # Summary
    all_tracked = [p for p in picks if p.get("peak_price") is not None]
    trade_wins = sum(1 for p in all_tracked if p.get("trade_outcome") == "win")
    match_wins = sum(1 for p in all_tracked if p.get("outcome") == "win")

    # The interesting metric: trade wins that were match losses
    trade_win_match_loss = sum(
        1 for p in all_tracked
        if p.get("trade_outcome") == "win" and p.get("outcome") == "loss"
    )

    print(f"\n  ── TRACKING RESULTS ──")
    print(f"  Newly tracked: {tracked}")
    print(f"  Failed:        {failed}")
    print(f"  API calls:     {api_calls}")

    if all_tracked:
        avg_peak_mult = sum(p.get("peak_multiple", 1) for p in all_tracked) / len(all_tracked)
        avg_trade_pnl = sum(p.get("trade_pnl", 0) for p in all_tracked) / len(all_tracked)
        print(f"\n  ── TRADE INTELLIGENCE ──")
        print(f"  Total tracked:           {len(all_tracked)}")
        print(f"  Match outcomes:          {match_wins}W-{len(all_tracked)-match_wins}L")
        print(f"  Trade outcomes:          {trade_wins}W-{len(all_tracked)-trade_wins}L")
        print(f"  Trade wins on match losses: {trade_win_match_loss} (hidden edge!)")
        print(f"  Avg peak multiple:       {avg_peak_mult:.2f}x")
        print(f"  Avg trade PnL:           {avg_trade_pnl:+.0f}c")

    print(f"\n{'='*60}\n")


def show_status():
    """Show peak tracking status summary."""
    picks = load_picks()
    tracked = [p for p in picks if p.get("peak_price") is not None]
    untracked = [p for p in picks if p.get("peak_price") is None and p.get("outcome") is not None]

    print(f"\n{'='*60}")
    print(f"  PEAK PRICE TRACKER — STATUS")
    print(f"{'='*60}")
    print(f"  Total picks:     {len(picks)}")
    print(f"  Peak tracked:    {len(tracked)}")
    print(f"  Resolved but untracked: {len(untracked)}")

    if tracked:
        trade_wins = [p for p in tracked if p.get("trade_outcome") == "win"]
        trade_losses = [p for p in tracked if p.get("trade_outcome") == "loss"]
        match_wins = [p for p in tracked if p.get("outcome") == "win"]
        match_losses = [p for p in tracked if p.get("outcome") == "loss"]

        # Cross-tabulation: the key insight
        tw_mw = sum(1 for p in tracked if p.get("trade_outcome") == "win" and p.get("outcome") == "win")
        tw_ml = sum(1 for p in tracked if p.get("trade_outcome") == "win" and p.get("outcome") == "loss")
        tl_mw = sum(1 for p in tracked if p.get("trade_outcome") == "loss" and p.get("outcome") == "win")
        tl_ml = sum(1 for p in tracked if p.get("trade_outcome") == "loss" and p.get("outcome") == "loss")

        print(f"\n  ── MATCH vs TRADE OUTCOMES ──")
        print(f"  Trade WIN  + Match WIN:   {tw_mw}  (best case)")
        print(f"  Trade WIN  + Match LOSS:  {tw_ml}  (hidden edge — the money maker)")
        print(f"  Trade LOSS + Match WIN:   {tl_mw}  (won but no trade window)")
        print(f"  Trade LOSS + Match LOSS:  {tl_ml}  (full loss)")

        avg_peak = sum(p.get("peak_multiple", 1) for p in tracked) / len(tracked)
        avg_trade_pnl = sum(p.get("trade_pnl", 0) for p in tracked) / len(tracked)
        total_trade_pnl = sum(p.get("trade_pnl", 0) for p in tracked)
        total_match_pnl = sum(p.get("pnl", 0) for p in tracked)

        print(f"\n  ── P&L COMPARISON ──")
        print(f"  Match-based P&L:  ${total_match_pnl/100:+,.2f}")
        print(f"  Trade-based P&L:  ${total_trade_pnl/100:+,.2f}")
        print(f"  Avg peak multiple: {avg_peak:.2f}x entry")
        print(f"  Avg trade PnL:     {avg_trade_pnl:+.0f}c per pick")

        # Top trade wins
        best_trades = sorted(tracked, key=lambda p: p.get("trade_pnl", 0), reverse=True)[:5]
        if best_trades:
            print(f"\n  ── TOP TRADE OPPORTUNITIES ──")
            for p in best_trades:
                print(f"    {p.get('match', '?')[:35]:35s} "
                      f"Entry: {p.get('entry_price', p.get('poly_price', '?'))}c → "
                      f"Peak: {p.get('peak_price', '?')}c "
                      f"({p.get('peak_multiple', '?')}x) | "
                      f"Match: {(p.get('outcome') or 'pending').upper()}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track peak prices for tennis bets on Polymarket")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--status", action="store_true", help="Show tracking status")
    parser.add_argument("--force", action="store_true", help="Re-track already tracked picks")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        track_peak_prices(dry_run=args.dry_run, force=args.force)
