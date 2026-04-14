#!/usr/bin/env python3
"""
Tennis Betting Signal System — Outcome Resolver

Checks Polymarket for resolved (closed) tennis markets,
matches them against picks in picks.jsonl, and updates
outcomes (win/loss) and P&L.

This is the critical link between signal generation and learning:
- Feeds the My Bets tab with W/L results
- Enables P&L tracking
- Provides data for LSTM learner training
- Powers the Model Accuracy charts

Usage:
    python3 08_outcome_resolver.py              # Resolve all unresolved picks
    python3 08_outcome_resolver.py --dry-run    # Preview without writing
    python3 08_outcome_resolver.py --status     # Show resolution stats
"""

import json
import argparse
import requests
import re
from pathlib import Path
from datetime import datetime, timedelta
from difflib import SequenceMatcher

LOGS_DIR = Path("logs")
PICKS_FILE = LOGS_DIR / "picks.jsonl"
RESOLVED_LOG = LOGS_DIR / "resolutions.log"

GAMMA_API = "https://gamma-api.polymarket.com"


def load_picks():
    """Load all picks from picks.jsonl."""
    picks = []
    if not PICKS_FILE.exists():
        return picks
    with open(PICKS_FILE, 'r') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if line:
                try:
                    pick = json.loads(line)
                    pick["_line_idx"] = i
                    picks.append(pick)
                except json.JSONDecodeError:
                    continue
    return picks


def save_picks(picks):
    """Rewrite picks.jsonl with updated data."""
    PICKS_FILE.parent.mkdir(exist_ok=True)
    with open(PICKS_FILE, 'w') as f:
        for p in picks:
            # Remove internal tracking field
            row = {k: v for k, v in p.items() if k != "_line_idx"}
            f.write(json.dumps(row) + "\n")


def log_resolution(msg):
    """Append to resolution log."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"  {line}")
    RESOLVED_LOG.parent.mkdir(exist_ok=True)
    with open(RESOLVED_LOG, 'a') as f:
        f.write(line + "\n")


def fetch_resolved_markets():
    """
    Fetch recently resolved (closed) tennis markets from Polymarket.
    Returns list of market dicts with outcome info.
    """
    resolved = []

    for tag in ["tennis", "atp-tennis", "wta-tennis", "atp", "wta"]:
        try:
            # Fetch closed markets
            r = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "tag_slug": tag,
                    "closed": "true",
                    "limit": 100,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=15,
            )
            r.raise_for_status()
            events = r.json()

            for ev in events:
                markets = ev.get("markets", [])
                if markets:
                    for m in markets:
                        parsed = _parse_resolved(m)
                        if parsed:
                            if not parsed.get("slug"):
                                parsed["slug"] = ev.get("slug", "")
                            resolved.append(parsed)
                else:
                    parsed = _parse_resolved(ev)
                    if parsed:
                        resolved.append(parsed)
        except Exception as e:
            print(f"  [warn] resolved events/{tag}: {e}")

    # Also try markets endpoint directly
    for tag in ["tennis", "atp-tennis", "wta-tennis", "atp", "wta"]:
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "tag_slug": tag,
                    "closed": "true",
                    "limit": 200,
                },
                timeout=15,
            )
            r.raise_for_status()
            for m in r.json():
                parsed = _parse_resolved(m)
                if parsed:
                    resolved.append(parsed)
        except Exception as e:
            print(f"  [warn] resolved markets/{tag}: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for r in resolved:
        mid = r["market_id"]
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(r)

    return unique


def _parse_resolved(m):
    """Parse a resolved market to extract the winner."""
    market_id = m.get("id") or m.get("condition_id") or m.get("conditionId", "")
    question = m.get("question", "")
    slug = m.get("slug", "")

    if not question:
        return None

    # Check if truly resolved
    resolved_at = m.get("resolvedAt") or m.get("resolved_at")
    # Also check outcome prices — settled markets have 1.0/0.0
    outcomes_raw = m.get("outcomes", "")
    prices_raw = m.get("outcomePrices", "")

    try:
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw or []
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw or []
    except (json.JSONDecodeError, TypeError):
        prices = []

    # Determine winner from settled prices
    winner = None
    if outcomes and prices and len(outcomes) == len(prices):
        for name, price in zip(outcomes, prices):
            try:
                p = float(price)
                # Winner has price ~1.0 (or >0.95), loser has ~0.0
                if p >= 0.95:
                    winner = str(name)
                    break
            except (ValueError, TypeError):
                continue

    # If no clear winner from prices, check resolution field
    if not winner and resolved_at:
        # Some markets have a "resolution" or "resolvedOutcome" field
        winner = m.get("resolution") or m.get("resolvedOutcome")

    if not winner:
        return None

    return {
        "market_id": market_id,
        "question": question,
        "slug": slug,
        "winner": winner,
        "resolved_at": resolved_at or datetime.utcnow().isoformat(),
    }


def normalize_name(name):
    """Normalize a player name for matching."""
    if not name:
        return ""
    # Remove common prefixes/suffixes, lowercase, strip whitespace
    name = name.strip().lower()
    name = re.sub(r'\s+', ' ', name)
    return name


def names_match(name1, name2):
    """Check if two player names match (fuzzy)."""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if not n1 or not n2:
        return False

    # Exact match
    if n1 == n2:
        return True

    # Last name match
    last1 = n1.split()[-1] if n1.split() else ""
    last2 = n2.split()[-1] if n2.split() else ""
    if last1 and last2 and last1 == last2:
        return True

    # Fuzzy match
    ratio = SequenceMatcher(None, n1, n2).ratio()
    if ratio >= 0.80:
        return True

    # Check if one contains the other's last name
    if last1 in n2 or last2 in n1:
        return True

    return False


def match_pick_to_resolved(pick, resolved_markets):
    """
    Try to match a pick to a resolved market.
    Returns the resolved market dict if matched, None otherwise.
    """
    pick_match = pick.get("match", "")
    pick_question = pick.get("question", "")
    pick_player_a = pick.get("player_a", "")
    pick_player_b = pick.get("player_b", "")

    for rm in resolved_markets:
        rm_question = rm.get("question", "")

        # Method 1: Question text similarity
        if pick_question and rm_question:
            ratio = SequenceMatcher(None, pick_question.lower(), rm_question.lower()).ratio()
            if ratio >= 0.85:
                return rm

        # Method 2: Both player names appear in the resolved question
        if pick_player_a and pick_player_b:
            q_lower = rm_question.lower()
            a_last = pick_player_a.split()[-1].lower() if pick_player_a.split() else ""
            b_last = pick_player_b.split()[-1].lower() if pick_player_b.split() else ""
            if a_last and b_last and a_last in q_lower and b_last in q_lower:
                return rm

        # Method 3: Match name contains both last names
        if pick_match:
            match_lower = pick_match.lower()
            q_lower = rm_question.lower()
            # Extract last names from pick match
            parts = re.split(r'\s+vs\.?\s+', match_lower)
            if len(parts) == 2:
                last_a = parts[0].strip().split()[-1]
                last_b = parts[1].strip().split()[-1]
                if last_a in q_lower and last_b in q_lower:
                    return rm

    return None


def determine_outcome(pick, resolved_market):
    """
    Determine if a pick was a win or loss based on the resolved market.
    Returns (outcome, pnl) tuple.
    """
    winner_raw = resolved_market.get("winner", "")
    bet_on = pick.get("bet_on", "")
    player_a = pick.get("player_a", "")
    player_b = pick.get("player_b", "")
    poly_price = pick.get("poly_price", 50)

    # The winner from Polymarket might be "Yes"/"No" or a player name
    winner_name = winner_raw

    # If winner is "Yes", it means the first-named player (player_a) won
    if winner_raw.lower() in ["yes", "true", "1"]:
        winner_name = player_a
    elif winner_raw.lower() in ["no", "false", "0"]:
        winner_name = player_b
    else:
        # Try to match winner name to player_a or player_b
        if names_match(winner_raw, player_a):
            winner_name = player_a
        elif names_match(winner_raw, player_b):
            winner_name = player_b

    # Check if our bet won
    bet_won = names_match(bet_on, winner_name)

    # Calculate P&L (based on $100 unit bet at Polymarket price)
    # If we bet at 40c and won, profit = $100 * (1 - 0.40) = $60
    # If we bet at 40c and lost, loss = -$100 * 0.40 = -$40
    stake = 100  # $100 unit
    price_decimal = poly_price / 100

    if bet_won:
        pnl = stake * (1 - price_decimal)  # profit
        outcome = "win"
    else:
        pnl = -stake * price_decimal  # loss
        outcome = "loss"

    return outcome, round(pnl, 2), winner_name


def resolve_picks(dry_run=False):
    """Main resolution logic."""
    picks = load_picks()
    if not picks:
        print("  No picks found.")
        return

    # Find unresolved picks
    unresolved = [p for p in picks if p.get("outcome") is None]
    already_resolved = len(picks) - len(unresolved)

    print(f"\n{'='*60}")
    print(f"  OUTCOME RESOLVER")
    print(f"{'='*60}")
    print(f"  Total picks:     {len(picks)}")
    print(f"  Already resolved: {already_resolved}")
    print(f"  Unresolved:      {len(unresolved)}")

    if not unresolved:
        print(f"  All picks are resolved!")
        return

    # Fetch resolved markets from Polymarket
    print(f"\n  Fetching resolved markets from Polymarket...")
    resolved_markets = fetch_resolved_markets()
    print(f"  Found {len(resolved_markets)} resolved tennis markets")

    # Match and resolve
    new_resolutions = 0
    for pick in picks:
        if pick.get("outcome") is not None:
            continue  # Already resolved

        rm = match_pick_to_resolved(pick, resolved_markets)
        if rm:
            outcome, pnl, actual_winner = determine_outcome(pick, rm)

            log_msg = (
                f"{'WIN' if outcome == 'win' else 'LOSS'}: "
                f"{pick.get('match', '?')} | "
                f"Bet: {pick.get('bet_on', '?')} | "
                f"Winner: {actual_winner} | "
                f"PnL: ${pnl:+.0f} | "
                f"Edge: {pick.get('edge', 0):.1f}%"
            )

            if dry_run:
                print(f"  [DRY RUN] {log_msg}")
            else:
                pick["outcome"] = outcome
                pick["pnl"] = pnl
                pick["actual_winner"] = actual_winner
                pick["resolved_at"] = rm.get("resolved_at", datetime.utcnow().isoformat())
                log_resolution(log_msg)

            new_resolutions += 1

    print(f"\n  New resolutions: {new_resolutions}")

    if new_resolutions > 0 and not dry_run:
        save_picks(picks)
        print(f"  Updated picks.jsonl")

        # Show summary
        all_resolved = [p for p in picks if p.get("outcome") is not None]
        wins = sum(1 for p in all_resolved if p["outcome"] == "win")
        losses = sum(1 for p in all_resolved if p["outcome"] == "loss")
        total_pnl = sum(p.get("pnl", 0) for p in all_resolved)

        print(f"\n  ── OVERALL STATS ──")
        print(f"  Resolved: {len(all_resolved)}")
        print(f"  Wins:     {wins}")
        print(f"  Losses:   {losses}")
        print(f"  Win Rate: {wins/len(all_resolved)*100:.1f}%")
        print(f"  Total PnL: ${total_pnl:+,.0f}")

    print(f"\n{'='*60}\n")


def show_status():
    """Show resolution status summary."""
    picks = load_picks()
    resolved = [p for p in picks if p.get("outcome") is not None]
    unresolved = [p for p in picks if p.get("outcome") is None]
    wins = [p for p in resolved if p["outcome"] == "win"]
    losses = [p for p in resolved if p["outcome"] == "loss"]
    total_pnl = sum(p.get("pnl", 0) for p in resolved)

    print(f"\n{'='*60}")
    print(f"  OUTCOME RESOLVER — STATUS")
    print(f"{'='*60}")
    print(f"  Total picks:     {len(picks)}")
    print(f"  Resolved:        {len(resolved)}")
    print(f"  Unresolved:      {len(unresolved)}")

    if resolved:
        print(f"\n  Wins:            {len(wins)}")
        print(f"  Losses:          {len(losses)}")
        print(f"  Win Rate:        {len(wins)/len(resolved)*100:.1f}%")
        print(f"  Total PnL:       ${total_pnl:+,.0f}")
        print(f"  Avg PnL/bet:     ${total_pnl/len(resolved):+,.0f}")

        # Edge tier breakdown
        for tier_name, min_e, max_e in [("STRONG (>=10%)", 10, 999), ("SOLID (7-10%)", 7, 10), ("WATCH (5-7%)", 5, 7)]:
            tier = [p for p in resolved if min_e <= abs(p.get("edge", 0)) < max_e]
            if tier:
                tw = sum(1 for p in tier if p["outcome"] == "win")
                tpnl = sum(p.get("pnl", 0) for p in tier)
                print(f"\n  {tier_name}:")
                print(f"    Record:  {tw}W-{len(tier)-tw}L ({tw/len(tier)*100:.0f}%)")
                print(f"    PnL:     ${tpnl:+,.0f}")

    # LSTM readiness
    lstm_min = 50
    progress = min(len(resolved), lstm_min)
    bar_len = 30
    filled = int(bar_len * progress / lstm_min)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\n  LSTM Training:   [{bar}] {progress}/{lstm_min}")
    if len(resolved) >= lstm_min:
        print(f"  Status:          READY TO TRAIN")
    else:
        print(f"  Status:          Need {lstm_min - len(resolved)} more resolved picks")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve tennis bet outcomes from Polymarket")
    parser.add_argument("--dry-run", action="store_true", help="Preview resolutions without writing")
    parser.add_argument("--status", action="store_true", help="Show resolution status")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        resolve_picks(dry_run=args.dry_run)
