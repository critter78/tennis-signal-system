"""
TML ↔ Polymarket market join.

Goal: enrich every PM tennis market with REAL tournament / surface / round / winner data
from your 78k TML matches (2016-2026 ATP + Challenger).

Strategy
--------
1. Parse player names from market question text (multiple formats supported).
2. Extract market close date from endDate → match date proxy.
3. Fuzzy-match against TML:
   - both players' last names (case-insensitive) appear in the same match
   - match_date within ±3 days of market endDate
   - if multiple candidates, prefer closest date
4. Pull TML winner → compare to PM resolution to validate the join.

Outputs
-------
    data/polymarket/pm_settled.parquet   — one row per matched PM market with:
        conditionId, question, market_end_ts, pm_winner_token,
        player_a, player_b, tml_winner, tml_loser,
        surface, round, tourney_name, tourney_level, match_date,
        join_confidence (0-3: 3=both names + date+result match)

Usage:
    python3 17_tml_pm_join.py                      # full join
    python3 17_tml_pm_join.py --limit 500          # smoke test
    python3 17_tml_pm_join.py --show-unmatched 50  # show unmatched samples for QA
"""
import argparse, json, re, time
import pandas as pd
from pathlib import Path

DATA = Path("data/polymarket")
TML = Path("data/tml_history_10y.parquet")

# Patterns to extract two player names from PM market question
VS_PATTERNS = [
    re.compile(r"^(?P<a>[A-Z][a-zA-Z\-' ]+?)\s+vs\.?\s+(?P<b>[A-Z][a-zA-Z\-' ]+?)(?:\?|$|\s*(?:match|winner))", re.I),
    re.compile(r"will\s+(?P<a>[A-Z][a-zA-Z\-' ]+?)\s+(?:beat|defeat|win against)\s+(?P<b>[A-Z][a-zA-Z\-' ]+)", re.I),
    re.compile(r"(?P<a>[A-Z][a-zA-Z\-' ]+?)\s+(?:vs|v\.)\s+(?P<b>[A-Z][a-zA-Z\-' ]+)", re.I),
    re.compile(r"^(?P<a>[A-Z][a-zA-Z\-' ]+?)\s*[-–—]\s*(?P<b>[A-Z][a-zA-Z\-' ]+?)(?:\?|$)", re.I),
]
# Outright pattern — ignored for H2H join (won't have 2 players in 1 match)
# Covers: "win the French Open", "win Gold in Men's Tennis", "win a gold medal",
#         "win the championship/masters/cup/slam/trophy", "reach the Final"
OUTRIGHT_RE = re.compile(
    r"\b(win (the|a|gold|bronze|silver)|win\b.*\b(gold|bronze|silver|medal|open|championship|masters|slam|cup|trophy|title|final)\b|reach the (final|semifinal|quarterfinal))",
    re.I,
)

def extract_players(question: str) -> tuple[str, str] | None:
    q = (question or "").strip()
    if not q: return None
    if OUTRIGHT_RE.search(q): return None   # outright market, not H2H
    for pat in VS_PATTERNS:
        m = pat.search(q)
        if m:
            a, b = m.group("a").strip(), m.group("b").strip()
            # quick sanity: not too short, not single words like "yes"/"no"
            if len(a) >= 3 and len(b) >= 3 and a.lower() != b.lower():
                return a, b
    return None

def last_name(full: str) -> str:
    """Get last name for matching — handles 'De Minaur', 'van de Zandschulp', 'Tsitsipas' etc."""
    if not full: return ""
    parts = full.strip().split()
    if not parts: return ""
    # Sackmann format is 'First Last' — just take last token. Handles 'Alcaraz', 'De Minaur'.
    # For 'De Minaur' we'd want 'Minaur' as last-token anyway, which is unique enough.
    return parts[-1].lower()

def load_pm_markets() -> pd.DataFrame:
    raw = json.load(open(DATA / "pm_tennis_markets_hist.json"))
    rows = []
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
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        win_token = None; win_outcome = None
        if tokens and prices and len(tokens) == 2 and len(prices) == 2:
            try:
                p0, p1 = float(prices[0]), float(prices[1])
                if p0 == 1.0: win_token, win_outcome = str(tokens[0]), (outcomes[0] if outcomes else "")
                elif p1 == 1.0: win_token, win_outcome = str(tokens[1]), (outcomes[1] if outcomes else "")
            except: pass
        rows.append({
            "conditionId": cid,
            "question": m.get("question",""),
            "slug": m.get("slug",""),
            "event_slug": (m.get("events") or [{}])[0].get("slug","") if isinstance(m.get("events"), list) else "",
            "endDate": m.get("endDate",""),
            "outcomes": outcomes,
            "winning_token": win_token,
            "winning_outcome": win_outcome,
            "resolved": win_token is not None,
        })
    df = pd.DataFrame(rows)
    df["end_ts"] = pd.to_datetime(df["endDate"], errors="coerce", utc=True)
    return df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--show-unmatched", type=int, default=0)
    args = ap.parse_args()

    t0 = time.time()
    print("[1/4] loading PM markets ...")
    pm = load_pm_markets()
    print(f"    {len(pm):,} markets  resolved={pm.resolved.sum():,}")
    if args.limit:
        pm = pm.head(args.limit)

    print("[2/4] loading TML matches ...")
    tml = pd.read_parquet(TML)
    # normalize
    tml["winner_last"] = tml["winner_name"].fillna("").apply(last_name)
    tml["loser_last"]  = tml["loser_name"].fillna("").apply(last_name)
    tml["match_dt"] = pd.to_datetime(tml["match_date"], errors="coerce", utc=True)
    # index by last name for fast lookup
    print(f"    {len(tml):,} matches  {tml['winner_last'].nunique():,} unique last-name winners")

    # build a dict: last_name -> df slice (for each side)
    print("    indexing TML by last-name pairs ...")
    by_winner = dict(tuple(tml.groupby("winner_last")))
    by_loser  = dict(tuple(tml.groupby("loser_last")))

    print("[3/4] matching markets ...")
    results = []
    stats = {"total": 0, "outright_skip": 0, "no_players": 0,
             "no_candidate": 0, "matched": 0, "name_swap_check": 0,
             "date_mismatch": 0}
    unmatched_samples = []

    for i, m in enumerate(pm.itertuples()):
        stats["total"] += 1
        players = extract_players(m.question)
        if not players:
            if OUTRIGHT_RE.search(m.question or ""):
                stats["outright_skip"] += 1
            else:
                stats["no_players"] += 1
                if len(unmatched_samples) < args.show_unmatched:
                    unmatched_samples.append(("no_players", m.question))
            continue

        pa, pb = players
        la, lb = last_name(pa), last_name(pb)
        if not la or not lb or la == lb:
            stats["no_candidate"] += 1; continue

        # candidate pool: matches where la is winner + lb is loser, or vice versa
        cands = []
        if la in by_winner:
            df_a = by_winner[la]
            cands.append(df_a[df_a["loser_last"] == lb])
        if lb in by_winner:
            df_b = by_winner[lb]
            cands.append(df_b[df_b["loser_last"] == la])
        cand = pd.concat(cands) if cands else pd.DataFrame()
        if cand.empty:
            stats["no_candidate"] += 1
            if len(unmatched_samples) < args.show_unmatched:
                unmatched_samples.append((f"no_tml[{la}_{lb}]", m.question))
            continue

        # closest by date
        if pd.notna(m.end_ts):
            cand = cand.copy()
            cand["dt_gap_days"] = (cand["match_dt"] - m.end_ts).dt.total_seconds().abs() / 86400
            cand = cand[cand.dt_gap_days <= 3].sort_values("dt_gap_days")
        else:
            cand["dt_gap_days"] = -1
            cand = cand.head(1)

        if cand.empty:
            stats["date_mismatch"] += 1; continue

        best = cand.iloc[0]

        # confidence score
        confidence = 1  # name match
        if pd.notna(m.end_ts) and best.dt_gap_days <= 2:
            confidence = 2  # name + date match
        # If PM is resolved, check if PM winning_outcome matches TML winner
        result_validated = False
        if m.resolved and m.winning_outcome:
            pm_win_lc = m.winning_outcome.lower()
            tml_winner = (best.winner_name or "").lower()
            if last_name(tml_winner) in pm_win_lc or pm_win_lc in tml_winner:
                result_validated = True
                confidence = 3

        results.append({
            "conditionId": m.conditionId,
            "question": m.question,
            "market_end_ts": m.end_ts,
            "pm_winning_outcome": m.winning_outcome,
            "pm_resolved": m.resolved,
            "player_a_pm": pa, "player_b_pm": pb,
            "tml_winner": best.winner_name,
            "tml_loser": best.loser_name,
            "tml_winner_last": best.winner_last,
            "tml_loser_last": best.loser_last,
            "surface": best.get("surface"),
            "round": best.get("round"),
            "tourney_name": best.get("tourney_name"),
            "tourney_level": best.get("tourney_level"),  # G/M/A/C/F
            "match_date": best.match_dt,
            "dt_gap_days": best.dt_gap_days if "dt_gap_days" in best else None,
            "join_confidence": confidence,
            "result_validated": result_validated,
        })
        stats["matched"] += 1

        if (i + 1) % 5000 == 0:
            print(f"    {i+1:,}/{len(pm):,}  matched={stats['matched']:,}  "
                  f"{stats['matched']/(i+1)*100:.1f}%  {(i+1)/(time.time()-t0):.0f} mkt/sec")

    print(f"\n[4/4] writing pm_settled.parquet ...")
    out = pd.DataFrame(results)
    if not out.empty:
        out.to_parquet(DATA / "pm_settled.parquet", index=False, compression="zstd")

    # Report
    total = stats["total"]
    print(f"\n— JOIN RESULTS ({time.time()-t0:.0f}s) —")
    print(f"  total PM markets examined:       {total:,}")
    print(f"    outright (skipped):             {stats['outright_skip']:,}")
    print(f"    unparseable question:           {stats['no_players']:,}")
    print(f"    no TML candidate / date miss:   {stats['no_candidate']+stats['date_mismatch']:,}")
    print(f"    ✓ matched:                      {stats['matched']:,}  "
          f"({stats['matched']/total*100:.1f}%)")
    if not out.empty:
        print(f"\n  match confidence breakdown:")
        for c in [1,2,3]:
            n = (out.join_confidence == c).sum()
            label = {1:"name only", 2:"name+date", 3:"name+date+result"}[c]
            print(f"    {c} ({label}): {n:,}")
        print(f"\n  joined surface mix (confidence≥2):")
        sf = out[out.join_confidence >= 2].surface.value_counts().head(6)
        for k, v in sf.items(): print(f"    {k or 'NaN':<10} {v:>6,}")
        print(f"\n  tournament level (G/M/A/C/F, confidence≥2):")
        tl = out[out.join_confidence >= 2].tourney_level.value_counts().head(10)
        for k, v in tl.items(): print(f"    {k or 'NaN':<5} {v:>6,}")
        print(f"\n  sample matches (confidence=3, first 10):")
        top = out[out.join_confidence == 3].head(10)
        for r in top.itertuples():
            print(f"    [{r.surface}/{r.round}/{r.tourney_name}] PM:{r.question[:50]:<50} → TML:{r.tml_winner} def {r.tml_loser}")

    if args.show_unmatched and unmatched_samples:
        print(f"\n  unmatched samples:")
        for reason, q in unmatched_samples[:args.show_unmatched]:
            print(f"    [{reason}] {q[:80]}")

    print(f"\n✓ wrote {DATA / 'pm_settled.parquet'}  ({len(out):,} rows)")

if __name__ == "__main__":
    main()
