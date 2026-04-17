import json, argparse, numpy as np, pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

LOGS_DIR = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
PICKS_LOG = LOGS_DIR / "picks.jsonl"
PERF_LOG = LOGS_DIR / "performance.json"
LSTM_EXPORT = LOGS_DIR / "lstm_training.csv"
DATA_DIR = Path("data")

def log_picks(signals, run_id=None):
    if not signals: return 0
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    # Load ALL existing picks (resolved AND unresolved) to avoid duplicates.
    existing = load_picks_log()

    # Build index of existing picks for dedup and updates
    existing_by_mid = {}    # market_id -> index in existing list
    existing_by_slug = {}   # slug -> index
    existing_by_key = {}    # (sorted players, market_type, tournament_or_slug) -> index
    for idx, p in enumerate(existing):
        mid = p.get("market_id", "")
        if mid:
            existing_by_mid[mid] = idx
        slug = p.get("slug", "")
        if slug:
            existing_by_slug[slug] = idx
        pa = p.get("player_a", "").lower().strip()
        pb = p.get("player_b", "").lower().strip()
        mt = p.get("market_type", "")
        # Include slug or tournament to distinguish same-player outrights across events
        discriminator = slug or p.get("tournament", "").lower().strip()
        key = (tuple(sorted([pa, pb])), mt, discriminator)
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
        "elo_prob_a", "elo_prob_b", "sa_surf_elo", "sb_surf_elo",
        "surf_elo_prob_a", "surf_elo_prob_b", "elo_confidence",
        "rank_elo_alert_a", "rank_elo_alert_b", "surf_mismatch_a", "surf_mismatch_b",
        "base_prob", "lstm_adj", "base_edge", "lstm_edge",
    ]

    logged = 0
    skipped = 0
    updated = 0
    new_picks = []  # collect new picks to add to existing list

    for s in signals:
        # Check for duplicate — market_id first, then player pair fallback
        mid = s.get("market_id", "")
        existing_idx = None
        if mid and mid in existing_by_mid:
            existing_idx = existing_by_mid[mid]
        else:
            # Try slug match first (most reliable after market_id)
            slug = s.get("slug", "")
            if slug and slug in existing_by_slug:
                existing_idx = existing_by_slug[slug]
            else:
                pa = s.get("player_a", "").lower().strip()
                pb = s.get("player_b", "").lower().strip()
                mt = s.get("market_type", "")
                discriminator = slug or s.get("tournament", "").lower().strip()
                key = (tuple(sorted([pa, pb])), mt, discriminator)
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
            else:
                skipped += 1
            continue

        # New pick — add dedup keys and collect
        new_idx = len(existing) + len(new_picks)
        if mid:
            existing_by_mid[mid] = new_idx
        slug = s.get("slug", "")
        if slug:
            existing_by_slug[slug] = new_idx
        pa = s.get("player_a", "").lower().strip()
        pb = s.get("player_b", "").lower().strip()
        mt = s.get("market_type", "")
        discriminator = slug or s.get("tournament", "").lower().strip()
        key = (tuple(sorted([pa, pb])), mt, discriminator)
        existing_by_key[key] = new_idx

        entry = {"run_id": run_id, "logged_at": datetime.now().isoformat(),
            "market_id": s.get("market_id",""), "slug": s.get("slug",""),
            "market_type": s.get("market_type","unknown"), "match": s.get("match",""),
            "question": s.get("question",""), "tournament": s.get("tournament",""),
            "round": s.get("round",""), "surface": s.get("surface",""),
            "player_a": s.get("player_a",""), "player_b": s.get("player_b",""),
            "predicted_winner": s.get("predicted_winner",""), "bet_on": s.get("bet_on",""),
            "model_prob": s.get("model_prob"), "model_prob_a": s.get("model_prob_a"),
            "model_prob_b": s.get("model_prob_b"), "confidence": s.get("confidence"),
            "edge": s.get("edge"), "kelly_stake": s.get("kelly_stake"),
            "has_edge": s.get("has_edge",False), "rank": s.get("rank"),
            "rank_a": s.get("rank_a"), "rank_b": s.get("rank_b"),
            "poly_price": s.get("poly_price"), "poly_price_a": s.get("poly_price_a"),
            "poly_price_b": s.get("poly_price_b"), "volume": s.get("volume"),
            "liquidity": s.get("liquidity"), "end_date": s.get("end_date"),
            "poly_link": s.get("poly_link",""),
            "sa_wr": s.get("sa_wr"), "sb_wr": s.get("sb_wr"),
            "sa_swr": s.get("sa_swr"), "sb_swr": s.get("sb_swr"),
            "sa_form": s.get("sa_form"), "sb_form": s.get("sb_form"),
            "sa_rest": s.get("sa_rest"), "sb_rest": s.get("sb_rest"),
            # Advanced stats
            "sa_elo": s.get("sa_elo"), "sb_elo": s.get("sb_elo"),
            "sa_elo_pct": s.get("sa_elo_pct"), "sb_elo_pct": s.get("sb_elo_pct"),
            "sa_momentum": s.get("sa_momentum"), "sb_momentum": s.get("sb_momentum"),
            "sa_vs_top10": s.get("sa_vs_top10"), "sb_vs_top10": s.get("sb_vs_top10"),
            "sa_vs_top20": s.get("sa_vs_top20"), "sb_vs_top20": s.get("sb_vs_top20"),
            "sa_vs_top50": s.get("sa_vs_top50"), "sb_vs_top50": s.get("sb_vs_top50"),
            "sa_rank_now": s.get("sa_rank_now"), "sb_rank_now": s.get("sb_rank_now"),
            "sa_rank_30d": s.get("sa_rank_30d"), "sb_rank_30d": s.get("sb_rank_30d"),
            "sa_rank_180d": s.get("sa_rank_180d"), "sb_rank_180d": s.get("sb_rank_180d"),
            "sa_rank_365d": s.get("sa_rank_365d"), "sb_rank_365d": s.get("sb_rank_365d"),
            "sa_rank_move": s.get("sa_rank_move"), "sb_rank_move": s.get("sb_rank_move"),
            "sa_rank_move_90d": s.get("sa_rank_move_90d"), "sb_rank_move_90d": s.get("sb_rank_move_90d"),
            "sa_rank_move_365d": s.get("sa_rank_move_365d"), "sb_rank_move_365d": s.get("sb_rank_move_365d"),
            "sa_matches_52w": s.get("sa_matches_52w"), "sb_matches_52w": s.get("sb_matches_52w"),
            "sa_wins_ytd": s.get("sa_wins_ytd"), "sb_wins_ytd": s.get("sb_wins_ytd"),
            "sa_losses_ytd": s.get("sa_losses_ytd"), "sb_losses_ytd": s.get("sb_losses_ytd"),
            # ELO Intelligence
            "elo_prob_a": s.get("elo_prob_a"), "elo_prob_b": s.get("elo_prob_b"),
            "sa_surf_elo": s.get("sa_surf_elo"), "sb_surf_elo": s.get("sb_surf_elo"),
            "surf_elo_prob_a": s.get("surf_elo_prob_a"), "surf_elo_prob_b": s.get("surf_elo_prob_b"),
            "elo_confidence": s.get("elo_confidence"),
            "rank_elo_alert_a": s.get("rank_elo_alert_a"), "rank_elo_alert_b": s.get("rank_elo_alert_b"),
            "surf_mismatch_a": s.get("surf_mismatch_a"), "surf_mismatch_b": s.get("surf_mismatch_b"),
            # LSTM adjustment tracking
            "base_prob": s.get("base_prob"), "lstm_adj": s.get("lstm_adj"),
            "base_edge": s.get("base_edge"), "lstm_edge": s.get("lstm_edge"),
            "outcome": None, "actual_winner": None, "resolved_at": None, "pnl": None}
        new_picks.append(entry); logged += 1

    # Write everything atomically — updated existing + new picks
    if updated > 0 or new_picks:
        save_picks_log(existing + new_picks)

    parts = [f"Logged {logged} new"]
    if updated: parts.append(f"updated {updated} existing")
    if skipped: parts.append(f"skipped {skipped} resolved")
    print(f"  {', '.join(parts)} picks in {PICKS_LOG}")
    return logged

def load_picks_log():
    if not PICKS_LOG.exists(): return []
    picks = []
    with open(PICKS_LOG) as f:
        for line in f:
            line = line.strip()
            if line: picks.append(json.loads(line))
    return picks

def save_picks_log(picks):
    with open(PICKS_LOG, "w") as f:
        for p in picks: f.write(json.dumps(p) + "\n")

def resolve_outcomes():
    picks = load_picks_log()
    if not picks: print("  No picks to resolve."); return
    unresolved = [p for p in picks if p["outcome"] is None]
    if not unresolved: print("  All picks already resolved."); return
    print(f"  Checking {len(unresolved)} unresolved picks...")
    hist_path = DATA_DIR / "raw" / "matches_combined.parquet"
    df = pd.read_parquet(hist_path) if hist_path.exists() else pd.DataFrame()
    if not df.empty: df["date"] = pd.to_datetime(df["date"], errors="coerce")
    resolved_count = 0
    for pick in picks:
        if pick["outcome"] is not None: continue
        pa, pb, mtype = pick["player_a"], pick["player_b"], pick["market_type"]
        if mtype == "h2h" and not df.empty:
            logged_date = pd.Timestamp(pick["logged_at"])
            match = df[(((df["winner"]==pa)&(df["loser"]==pb))|((df["winner"]==pb)&(df["loser"]==pa)))&(df["date"]>=logged_date-timedelta(days=1))&(df["date"]<=logged_date+timedelta(days=14))]
            if not match.empty:
                actual_winner = match.iloc[-1]["winner"]
                pick["actual_winner"] = actual_winner; pick["resolved_at"] = datetime.now().isoformat()
                buy_price = pick.get("poly_price", 50)
                if actual_winner == pick["predicted_winner"]:
                    pick["outcome"] = "win"; pick["pnl"] = round(100-buy_price, 1)
                else:
                    pick["outcome"] = "loss"; pick["pnl"] = round(-buy_price, 1)
                resolved_count += 1
    save_picks_log(picks)
    print(f"  Resolved {resolved_count} picks ({len([p for p in picks if p['outcome'] is None])} still pending)")

def generate_report():
    picks = load_picks_log()
    if not picks: print("  No picks logged yet."); return
    total = len(picks)
    resolved = [p for p in picks if p["outcome"] is not None]
    unresolved = total - len(resolved)
    wins = [p for p in resolved if p["outcome"] == "win"]
    losses = [p for p in resolved if p["outcome"] == "loss"]
    h2h = [p for p in resolved if p["market_type"] == "h2h"]
    outright = [p for p in resolved if p["market_type"] == "outright"]
    h2h_wins = len([p for p in h2h if p["outcome"] == "win"])
    out_wins = len([p for p in outright if p["outcome"] == "win"])
    total_pnl = sum(p.get("pnl",0) or 0 for p in resolved)
    edge_picks = [p for p in resolved if p.get("has_edge")]
    edge_pnl = sum(p.get("pnl",0) or 0 for p in edge_picks)
    high_conf = [p for p in resolved if p.get("confidence",0) >= 65]
    med_conf = [p for p in resolved if 55 <= p.get("confidence",0) < 65]
    low_conf = [p for p in resolved if p.get("confidence",0) < 55]
    hit_rate = round(len(wins)/len(resolved)*100,1) if resolved else 0
    report = {"generated_at": datetime.now().isoformat(), "total_picks": total,
        "resolved": len(resolved), "unresolved": unresolved, "wins": len(wins), "losses": len(losses),
        "hit_rate": hit_rate, "total_pnl_cents": round(total_pnl,1)}
    with open(PERF_LOG, "w") as f: json.dump(report, f, indent=2)
    print("\n" + "="*60)
    print("  PERFORMANCE REPORT")
    print("="*60)
    print(f"  Total Picks:    {total}")
    print(f"  Resolved:       {len(resolved)} ({unresolved} pending)")
    print(f"  Record:         {len(wins)}W - {len(losses)}L")
    print(f"  Hit Rate:       {hit_rate}%")
    print(f"  Total P&L:      {total_pnl:+.0f} cents")
    print(f"  Edge Picks P&L: {edge_pnl:+.0f} cents ({len(edge_picks)} picks)")
    hr = lambda l: f"{len([p for p in l if p['outcome']=='win'])}-{len([p for p in l if p['outcome']=='loss'])}" if l else "0-0"
    print(f"  H2H Record:     {hr(h2h)}")
    print(f"  Outright Record: {hr(outright)}")
    print(f"\n  By Confidence:")
    print(f"    HIGH (>=65%):  {hr(high_conf)}")
    print(f"    MED  (55-65%): {hr(med_conf)}")
    print(f"    LOW  (<55%):   {hr(low_conf)}")
    print(f"\n  Report saved to {PERF_LOG}")

def export_lstm_data():
    picks = load_picks_log()
    resolved = [p for p in picks if p["outcome"] is not None]
    if not resolved: print("  No resolved picks to export."); return
    rows = []
    for p in sorted(resolved, key=lambda x: x.get("logged_at","")):
        rows.append({"timestamp": p.get("logged_at",""),
            "market_type": 1 if p["market_type"]=="h2h" else 0,
            "surface": {"Hard":0,"Clay":1,"Grass":2}.get(p.get("surface","Hard"),0),
            "model_prob": p.get("model_prob",50), "poly_price": p.get("poly_price",50),
            "edge": p.get("edge",0), "confidence": p.get("confidence",50),
            "kelly_stake": p.get("kelly_stake",0), "volume": p.get("volume",0),
            "rank": p.get("rank") or 50, "has_edge": 1 if p.get("has_edge") else 0,
            "outcome": 1 if p["outcome"]=="win" else 0, "pnl": p.get("pnl",0) or 0})
    df = pd.DataFrame(rows); df.to_csv(LSTM_EXPORT, index=False)
    print(f"  Exported {len(df)} resolved picks to {LSTM_EXPORT}")

def dedup_picks():
    """Remove duplicate picks, keeping the first occurrence of each match.
    For resolved duplicates, keeps the one with an outcome (if any).
    Uses slug/tournament to distinguish same-player outrights across events."""
    picks = load_picks_log()
    if not picks:
        print("  No picks to deduplicate.")
        return

    seen_keys = set()
    seen_market_ids = set()
    seen_slugs = set()
    unique = []
    dupes = 0

    # Sort so resolved picks come first (we want to keep resolved over unresolved)
    picks_sorted = sorted(picks, key=lambda p: (
        0 if p.get("outcome") is not None else 1,  # resolved first
        p.get("logged_at", ""),                      # then by time
    ))

    for p in picks_sorted:
        mid = p.get("market_id", "")
        slug = p.get("slug", "")
        pa = p.get("player_a", "").lower().strip()
        pb = p.get("player_b", "").lower().strip()
        mt = p.get("market_type", "")
        # Include slug or tournament so outrights for different events stay distinct
        discriminator = slug or p.get("tournament", "").lower().strip()
        key = (tuple(sorted([pa, pb])), mt, discriminator)

        # Skip if we've seen this market_id, slug, or player pair already
        if mid and mid in seen_market_ids:
            dupes += 1
            continue
        if slug and slug in seen_slugs:
            dupes += 1
            continue
        if key in seen_keys:
            dupes += 1
            continue

        seen_keys.add(key)
        if mid:
            seen_market_ids.add(mid)
        if slug:
            seen_slugs.add(slug)
        unique.append(p)

    # Re-sort by logged_at for clean output
    unique.sort(key=lambda p: p.get("logged_at", ""))

    print(f"  Before: {len(picks)} picks")
    print(f"  Removed: {dupes} duplicates")
    print(f"  After:  {len(unique)} unique picks")

    resolved_before = len([p for p in picks if p.get("outcome") is not None])
    resolved_after = len([p for p in unique if p.get("outcome") is not None])
    print(f"  Resolved picks preserved: {resolved_after}/{resolved_before}")

    save_picks_log(unique)
    print(f"  Saved to {PICKS_LOG}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["log","resolve","report","export-lstm","dedup"])
    parser.add_argument("--signals-json", type=str)
    args = parser.parse_args()
    if args.action == "log":
        if args.signals_json:
            with open(args.signals_json) as f: signals = json.load(f)
            log_picks(signals)
    elif args.action == "resolve": resolve_outcomes()
    elif args.action == "report": generate_report()
    elif args.action == "export-lstm": export_lstm_data()
    elif args.action == "dedup": dedup_picks()
