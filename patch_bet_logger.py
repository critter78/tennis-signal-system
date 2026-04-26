#!/usr/bin/env python3
"""Patch 05_bet_logger.py to refresh stats on existing picks instead of skipping duplicates."""
import re

with open("05_bet_logger.py", "r") as f:
    content = f.read()

# Check if already patched
if "REFRESH_FIELDS" in content:
    print("Already patched!")
    exit(0)

OLD = '''def log_picks(signals, run_id=None):
    if not signals: return 0
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    # Load ALL existing picks (resolved AND unresolved) to avoid duplicates.
    # Previously we skipped resolved picks, which meant the same match got
    # re-logged every time the cron ran after the outcome resolver marked it.
    existing = load_picks_log()
    existing_keys = set()
    existing_market_ids = set()
    for p in existing:
        # Primary dedup: market_id (unique per Polymarket market)
        mid = p.get("market_id", "")
        if mid:
            existing_market_ids.add(mid)
        # Secondary dedup: sorted player pair + market_type (catches same match
        # even if market_id differs across API calls)
        pa = p.get("player_a", "").lower().strip()
        pb = p.get("player_b", "").lower().strip()
        mt = p.get("market_type", "")
        key = (tuple(sorted([pa, pb])), mt)
        existing_keys.add(key)

    logged = 0
    skipped = 0
    with open(PICKS_LOG, "a") as f:
        for s in signals:
            # Check for duplicate — market_id first, then player pair fallback
            mid = s.get("market_id", "")
            if mid and mid in existing_market_ids:
                skipped += 1
                continue
            pa = s.get("player_a", "").lower().strip()
            pb = s.get("player_b", "").lower().strip()
            mt = s.get("market_type", "")
            key = (tuple(sorted([pa, pb])), mt)
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)  # prevent dupes within this batch too
            if mid:
                existing_market_ids.add(mid)

            entry = {"run_id": run_id, "logged_at": datetime.now().isoformat(),'''

NEW = '''def log_picks(signals, run_id=None):
    if not signals: return 0
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    # Load ALL existing picks (resolved AND unresolved) to avoid duplicates.
    existing = load_picks_log()

    # Build index of existing picks for dedup and updates
    existing_by_mid = {}    # market_id -> index in existing list
    existing_by_key = {}    # (sorted players, market_type) -> index
    for idx, p in enumerate(existing):
        mid = p.get("market_id", "")
        if mid:
            existing_by_mid[mid] = idx
        pa = p.get("player_a", "").lower().strip()
        pb = p.get("player_b", "").lower().strip()
        mt = p.get("market_type", "")
        key = (tuple(sorted([pa, pb])), mt)
        existing_by_key[key] = idx

    # Fields to refresh on existing unresolved picks (keep outcomes intact)
    REFRESH_FIELDS = [
        "rank", "rank_a", "rank_b", "model_prob", "model_prob_a", "model_prob_b",
        "confidence", "edge", "kelly_stake", "poly_price", "poly_price_a", "poly_price_b",
        "volume", "liquidity", "has_edge",
        "sa_wr", "sb_wr", "sa_swr", "sb_swr", "sa_form", "sb_form",
        "sa_rest", "sb_rest", "sa_elo", "sb_elo", "sa_elo_pct", "sb_elo_pct",
        "sa_momentum", "sb_momentum",
        "sa_vs_top10", "sb_vs_top10", "sa_vs_top20", "sb_vs_top20", "sa_vs_top50", "sb_vs_top50",
        "sa_rank_now", "sb_rank_now", "sa_rank_30d", "sb_rank_30d",
        "sa_rank_180d", "sb_rank_180d", "sa_rank_365d", "sb_rank_365d",
        "sa_rank_move", "sb_rank_move",
        "sa_rank_move_90d", "sb_rank_move_90d", "sa_rank_move_365d", "sb_rank_move_365d",
        "sa_matches_52w", "sb_matches_52w",
        "sa_wins_ytd", "sb_wins_ytd", "sa_losses_ytd", "sb_losses_ytd",
        "base_prob", "lstm_adj", "base_edge", "lstm_edge",
    ]

    logged = 0
    skipped = 0
    updated = 0
    dirty = False  # track if existing picks were modified

    with open(PICKS_LOG, "a") as f:
        for s in signals:
            # Check for duplicate — market_id first, then player pair fallback
            mid = s.get("market_id", "")
            existing_idx = None
            if mid and mid in existing_by_mid:
                existing_idx = existing_by_mid[mid]
            else:
                pa = s.get("player_a", "").lower().strip()
                pb = s.get("player_b", "").lower().strip()
                mt = s.get("market_type", "")
                key = (tuple(sorted([pa, pb])), mt)
                if key in existing_by_key:
                    existing_idx = existing_by_key[key]

            if existing_idx is not None:
                # Existing pick found — update stats if not yet resolved
                pick = existing[existing_idx]
                if pick.get("outcome") is None:
                    for field in REFRESH_FIELDS:
                        val = s.get(field)
                        if val is not None:
                            pick[field] = val
                    updated += 1
                    dirty = True
                else:
                    skipped += 1
                continue

            # New pick — add dedup keys and log
            if mid:
                existing_by_mid[mid] = len(existing)
            pa = s.get("player_a", "").lower().strip()
            pb = s.get("player_b", "").lower().strip()
            mt = s.get("market_type", "")
            key = (tuple(sorted([pa, pb])), mt)
            existing_by_key[key] = len(existing)

            entry = {"run_id": run_id, "logged_at": datetime.now().isoformat(),'''

# Also fix the old entry template to include new fields
OLD_ENTRY_END = '''                "sa_rank_move": s.get("sa_rank_move"), "sb_rank_move": s.get("sb_rank_move"),
                "outcome": None, "actual_winner": None, "resolved_at": None, "pnl": None}
            f.write(json.dumps(entry) + "\\n"); logged += 1
    if skipped:
        print(f"  Logged {logged} picks to {PICKS_LOG} (skipped {skipped} duplicates)")
    else:
        print(f"  Logged {logged} picks to {PICKS_LOG}")
    return logged'''

NEW_ENTRY_END = '''                "sa_rank_move": s.get("sa_rank_move"), "sb_rank_move": s.get("sb_rank_move"),
                "sa_rank_move_90d": s.get("sa_rank_move_90d"), "sb_rank_move_90d": s.get("sb_rank_move_90d"),
                "sa_rank_move_365d": s.get("sa_rank_move_365d"), "sb_rank_move_365d": s.get("sb_rank_move_365d"),
                "sa_matches_52w": s.get("sa_matches_52w"), "sb_matches_52w": s.get("sb_matches_52w"),
                "sa_wins_ytd": s.get("sa_wins_ytd"), "sb_wins_ytd": s.get("sb_wins_ytd"),
                "sa_losses_ytd": s.get("sa_losses_ytd"), "sb_losses_ytd": s.get("sb_losses_ytd"),
                # LSTM adjustment tracking
                "base_prob": s.get("base_prob"), "lstm_adj": s.get("lstm_adj"),
                "base_edge": s.get("base_edge"), "lstm_edge": s.get("lstm_edge"),
                "outcome": None, "actual_winner": None, "resolved_at": None, "pnl": None}
            f.write(json.dumps(entry) + "\\n"); logged += 1

    # If any existing picks were updated with fresh stats, rewrite the file
    if dirty:
        save_picks_log(existing)

    parts = [f"Logged {logged} new"]
    if updated: parts.append(f"updated {updated} existing")
    if skipped: parts.append(f"skipped {skipped} resolved")
    print(f"  {\\", \\".join(parts)} picks in {PICKS_LOG}")
    return logged'''

if OLD in content:
    content = content.replace(OLD, NEW)
    print("Patched log_picks function (dedup -> refresh)")
else:
    print("WARNING: Could not find old log_picks pattern. File may have been modified.")
    exit(1)

if OLD_ENTRY_END in content:
    content = content.replace(OLD_ENTRY_END, NEW_ENTRY_END)
    print("Patched entry template (added new fields + rewrite logic)")
else:
    print("WARNING: Could not find old entry end pattern.")
    exit(1)

with open("05_bet_logger.py", "w") as f:
    f.write(content)

print("Done! 05_bet_logger.py patched successfully.")
