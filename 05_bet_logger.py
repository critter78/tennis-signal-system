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

    # Load existing unresolved picks to avoid duplicates
    existing = load_picks_log()
    existing_keys = set()
    for p in existing:
        if p.get("outcome") is not None:
            continue  # resolved picks are fine to re-pick
        # Key by sorted player pair + market_type to catch same match
        pa = p.get("player_a", "").lower().strip()
        pb = p.get("player_b", "").lower().strip()
        mt = p.get("market_type", "")
        key = (tuple(sorted([pa, pb])), mt)
        existing_keys.add(key)

    logged = 0
    skipped = 0
    with open(PICKS_LOG, "a") as f:
        for s in signals:
            # Check for duplicate
            pa = s.get("player_a", "").lower().strip()
            pb = s.get("player_b", "").lower().strip()
            mt = s.get("market_type", "")
            key = (tuple(sorted([pa, pb])), mt)
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)  # prevent dupes within this batch too

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
                "sa_momentum": s.get("sa_momentum"), "sb_momentum": s.get("sb_momentum"),
                "sa_vs_top10": s.get("sa_vs_top10"), "sb_vs_top10": s.get("sb_vs_top10"),
                "sa_vs_top20": s.get("sa_vs_top20"), "sb_vs_top20": s.get("sb_vs_top20"),
                "sa_vs_top50": s.get("sa_vs_top50"), "sb_vs_top50": s.get("sb_vs_top50"),
                "sa_rank_now": s.get("sa_rank_now"), "sb_rank_now": s.get("sb_rank_now"),
                "sa_rank_30d": s.get("sa_rank_30d"), "sb_rank_30d": s.get("sb_rank_30d"),
                "sa_rank_180d": s.get("sa_rank_180d"), "sb_rank_180d": s.get("sb_rank_180d"),
                "sa_rank_365d": s.get("sa_rank_365d"), "sb_rank_365d": s.get("sb_rank_365d"),
                "sa_rank_move": s.get("sa_rank_move"), "sb_rank_move": s.get("sb_rank_move"),
                "outcome": None, "actual_winner": None, "resolved_at": None, "pnl": None}
            f.write(json.dumps(entry) + "\n"); logged += 1
    if skipped:
        print(f"  Logged {logged} picks to {PICKS_LOG} (skipped {skipped} duplicates)")
    else:
        print(f"  Logged {logged} picks to {PICKS_LOG}")
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["log","resolve","report","export-lstm"])
    parser.add_argument("--signals-json", type=str)
    args = parser.parse_args()
    if args.action == "log":
        if args.signals_json:
            with open(args.signals_json) as f: signals = json.load(f)
            log_picks(signals)
    elif args.action == "resolve": resolve_outcomes()
    elif args.action == "report": generate_report()
    elif args.action == "export-lstm": export_lstm_data()
