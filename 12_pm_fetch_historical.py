"""
Polymarket Historical Fetcher — Tennis Markets + All Trades
Run from any env with egress to *.polymarket.com (local Mac or Render shell).

Phase 1: pulls all SETTLED tennis markets via gamma /events?tag_slug=tennis
         (events reliably carry the tennis tag; markets are child rows).
Phase 2: for each settled market, pulls every trade via data-api /trades
         (no `user` filter = returns all wallets), paginated.

Outputs (written under data/polymarket/):
    pm_tennis_markets_hist.json        — full market list, one row per market
    pm_trades/{conditionId}.json       — trades per market (list of dicts)
    pm_fetch_manifest.json             — run summary + per-market row counts

Resumable: skips markets whose trades file already exists. Safe to kill + rerun.

Usage:
    python3 12_pm_fetch_historical.py                 # full history, all tennis
    python3 12_pm_fetch_historical.py --since 2023-01 # only markets ending >= date
    python3 12_pm_fetch_historical.py --limit 50      # first N markets (smoke test)
    python3 12_pm_fetch_historical.py --phase 2       # re-run just trade pull
"""
import os
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

OUT = Path("data/polymarket")
(OUT / "pm_trades").mkdir(parents=True, exist_ok=True)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def fetch_events(since: str | None, limit_cap: int | None) -> list[dict]:
    """Use gamma /events — tag_slug filter actually works here."""
    print("[phase 1] fetching tennis events (closed) ...")
    events, offset, step = [], 0, 200
    while True:
        params = {"tag_slug": "tennis", "closed": "true", "limit": step, "offset": offset}
        r = requests.get(f"{GAMMA}/events", params=params, timeout=30)
        r.raise_for_status()
        batch = r.json() or []
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        print(f"  events offset={offset:>5} got={len(batch):>4} running={len(events)}")
        if len(batch) < step:
            break
        offset += step
        if limit_cap and len(events) * 4 >= limit_cap:  # ~4 markets per event rough est
            break
        time.sleep(0.2)
    if since:
        events = [e for e in events if (e.get("endDate") or e.get("end_date") or "") >= since]
        print(f"  after since={since}: {len(events)} events")
    return events

def markets_from_events(events: list[dict], limit_cap: int | None) -> list[dict]:
    """Flatten event.markets[] into market list, dedup on conditionId."""
    seen, out = set(), []
    for ev in events:
        for m in (ev.get("markets") or []):
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            # keep event slug/title for join context
            m["_event_slug"] = ev.get("slug", "")
            m["_event_title"] = ev.get("title", "")
            out.append(m)
            if limit_cap and len(out) >= limit_cap:
                return out
    return out

def fetch_trades_for_market(condition_id: str) -> list[dict]:
    """Hit /trades endpoint — no `user` param = all wallets on this market."""
    trades, offset, step = [], 0, 500
    while True:
        params = {"market": condition_id, "limit": step, "offset": offset}
        r = requests.get(f"{DATA_API}/trades", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2); continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        batch = r.json() or []
        if not isinstance(batch, list) or not batch:
            break
        trades.extend(batch)
        if len(batch) < step:
            break
        offset += step
        time.sleep(0.15)
    return trades

def phase1(since: str | None, limit_cap: int | None) -> list[dict]:
    events = fetch_events(since, limit_cap)
    markets = markets_from_events(events, limit_cap)
    out = OUT / "pm_tennis_markets_hist.json"
    out.write_text(json.dumps(markets, indent=2))
    print(f"  ✓ {len(markets)} markets → {out}  (from {len(events)} events)")
    return markets

def phase2(markets: list[dict]) -> None:
    print("\n[phase 2] fetching trades per market ...")
    manifest = {"generated_at": now_iso(),
                "market_count": len(markets), "markets": {}}
    for i, m in enumerate(markets, 1):
        cid = m.get("conditionId") or m.get("condition_id") or m.get("id")
        if not cid:
            continue
        dest = OUT / "pm_trades" / f"{cid}.json"
        if dest.exists():
            try:
                n = len(json.loads(dest.read_text()))
                manifest["markets"][cid] = {"trades": n, "cached": True,
                                             "question": m.get("question", "")}
                print(f"  [{i:>4}/{len(markets)}] {cid[:12]}… cached ({n} trades)")
                continue
            except Exception:
                pass
        try:
            trades = fetch_trades_for_market(cid)
            dest.write_text(json.dumps(trades))
            manifest["markets"][cid] = {"trades": len(trades), "cached": False,
                                         "question": m.get("question", "")}
            print(f"  [{i:>4}/{len(markets)}] {cid[:12]}… {len(trades):>5} trades  "
                  f"{(m.get('question') or '')[:60]}")
        except Exception as e:
            manifest["markets"][cid] = {"error": str(e), "question": m.get("question", "")}
            print(f"  [{i:>4}/{len(markets)}] {cid[:12]}… ERROR: {e}")

    mpath = OUT / "pm_fetch_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    total = sum(v.get("trades", 0) for v in manifest["markets"].values())
    print(f"\n✓ done: {len(markets)} markets, {total:,} trades total → {mpath}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date (YYYY-MM or YYYY-MM-DD). Filter markets ending after.")
    ap.add_argument("--limit", type=int, help="Cap markets (smoke test)")
    ap.add_argument("--phase", choices=["1", "2", "both"], default="both",
                    help="Run only phase 1 (markets) or phase 2 (trades). Default: both.")
    args = ap.parse_args()

    if args.phase in ("1", "both"):
        markets = phase1(args.since, args.limit)
    else:
        # reuse existing markets file
        mpath = OUT / "pm_tennis_markets_hist.json"
        if not mpath.exists():
            raise SystemExit(f"{mpath} not found — run phase 1 first")
        markets = json.loads(mpath.read_text())
        print(f"[phase 2] using existing {mpath}  ({len(markets)} markets)")

    if args.phase in ("2", "both"):
        phase2(markets)

if __name__ == "__main__":
    main()
