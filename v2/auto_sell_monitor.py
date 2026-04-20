"""
V2 AUTO-SELL MONITOR
=====================

Polls Polymarket CLOB prices for every open v2 position and writes a
`sell_alerts.jsonl` line whenever a pick crosses a profit threshold.

Thresholds (tunable at the top of the file — data-driven from the
edge_analysis.py 2026-04-20 finding that 96.5% of picks hit +10% peak):

    75c  → info only  (track peak)
    85c  → SELL HALF  — lock in profit; leave a runner
    95c  → SELL ALL   — convergence, take the last 5c via resolution risk

This does NOT execute trades — it only surfaces alerts. CRITTER executes
manually on polymarket.com (per memory: "do not execute trades or move money").

Run as a background process on the v2 web service, every 5 minutes:
    python3 auto_sell_monitor.py --interval 300

Or one-shot (e.g. from a cron):
    python3 auto_sell_monitor.py --once
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

V2_DIR = Path(__file__).parent.resolve()
LOGS   = V2_DIR / "logs"

PERSIST = Path("/data")
if PERSIST.exists() and PERSIST.is_dir():
    LOGS = PERSIST / "v2_logs"
LOGS.mkdir(parents=True, exist_ok=True)

PICKS_FILE    = LOGS / "v2_picks.jsonl"
POSITIONS     = LOGS / "v2_open_positions.json"    # CRITTER edits this when he actually takes a bet
ALERTS_FILE   = LOGS / "sell_alerts.jsonl"
STATE_FILE    = LOGS / "sell_alert_state.json"     # so we don't re-alert

CLOB = "https://clob.polymarket.com"

# Thresholds in decimal price (e.g. 0.85 = 85¢)
SELL_HALF_THR = 0.85
SELL_ALL_THR  = 0.95
INFO_THR      = 0.75


def load_open_positions() -> list[dict]:
    """
    Open positions file format (user-editable — one JSON list):
    [
        {"token_id": "0xabc…", "pick": "Sinner", "match": "Sinner vs Alcaraz",
         "entry_price": 0.62, "stake": 50, "run_id": "20260420_080000"},
        ...
    ]

    If the file doesn't exist, fall back to "all picks from the latest run"
    so CRITTER gets alerts even before he manually logs positions.
    """
    if POSITIONS.exists():
        try:
            return json.loads(POSITIONS.read_text())
        except Exception:
            pass

    latest = LOGS / "v2_picks_latest.json"
    if not latest.exists():
        return []
    try:
        data = json.loads(latest.read_text())
        out = []
        for p in data.get("picks", []):
            if not p.get("token_id"):
                continue
            out.append({
                "token_id": p["token_id"],
                "pick":     p["pick"],
                "match":    p["question"],
                "entry_price": p.get("poly_price"),
                "stake":    None,  # not yet committed
                "run_id":   p["run_id"],
                "virtual":  True,  # flag so we know this is paper-trade tracking
            })
        return out
    except Exception as e:
        print(f"[open] failed: {e}", file=sys.stderr)
        return []


def fetch_price(token_id: str) -> float | None:
    try:
        r = requests.get(f"{CLOB}/price",
                         params={"token_id": token_id, "side": "BUY"},
                         timeout=10)
        if r.status_code == 200:
            return float(r.json().get("price", 0) or 0)
    except Exception as e:
        print(f"[clob] {token_id[:10]}… failed: {e}", file=sys.stderr)
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def evaluate(pos: dict, price: float, state: dict) -> dict | None:
    """Return an alert dict if this position crosses a new threshold."""
    tid = pos["token_id"]
    prev = state.get(tid, {"peak": 0.0, "fired": []})
    peak = max(prev["peak"], price)

    fired = list(prev.get("fired", []))
    new_action = None
    if price >= SELL_ALL_THR and "sell_all" not in fired:
        new_action = "sell_all"
        fired.append("sell_all")
    elif price >= SELL_HALF_THR and "sell_half" not in fired:
        new_action = "sell_half"
        fired.append("sell_half")
    elif price >= INFO_THR and "info" not in fired:
        new_action = "info"
        fired.append("info")

    state[tid] = {"peak": peak, "fired": fired, "last_price": price,
                  "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    if new_action is None:
        return None

    entry = pos.get("entry_price")
    gain_pct = ((price - entry) / entry * 100) if entry and entry > 0 else None
    stake = pos.get("stake")
    profit_usd = None
    if stake and entry and entry > 0:
        # sell_half realizes half, sell_all realizes remaining
        size_realized = 0.5 if new_action == "sell_half" else (0.5 if "sell_half" in (prev.get("fired") or []) else 1.0)
        profit_usd = round(stake * size_realized * ((price - entry) / entry), 2)

    return {
        "ts":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action":    new_action,
        "token_id":  tid,
        "match":     pos.get("match"),
        "pick":      pos.get("pick"),
        "entry":     entry,
        "price":     round(price, 4),
        "peak":      round(peak, 4),
        "gain_pct":  round(gain_pct, 2) if gain_pct is not None else None,
        "stake":     stake,
        "profit_usd": profit_usd,
        "virtual":   pos.get("virtual", False),
        "run_id":    pos.get("run_id"),
    }


def one_pass(verbose: bool = True) -> int:
    positions = load_open_positions()
    if not positions:
        if verbose:
            print("[monitor] no open positions")
        return 0

    state = load_state()
    alerts = []
    for pos in positions:
        tid = pos.get("token_id")
        if not tid:
            continue
        price = fetch_price(tid)
        if price is None:
            continue
        alert = evaluate(pos, price, state)
        if alert:
            alerts.append(alert)

    save_state(state)

    if alerts:
        with open(ALERTS_FILE, "a") as f:
            for a in alerts:
                f.write(json.dumps(a) + "\n")
        if verbose:
            for a in alerts:
                icon = "FIRE" if a["action"] == "sell_all" else ("SELL-HALF" if a["action"] == "sell_half" else "INFO")
                print(f"  [{icon}] {a['pick']:<20} @ {a['price']:.2f}  "
                      f"(entry {a.get('entry')}, peak {a['peak']:.2f})")
    elif verbose:
        print(f"[monitor] no new alerts across {len(positions)} positions")
    return len(alerts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Single pass, then exit")
    ap.add_argument("--interval", type=int, default=300, help="Poll seconds (default 300)")
    args = ap.parse_args()

    if args.once:
        n = one_pass()
        return 0 if n >= 0 else 1

    print(f"[monitor] polling every {args.interval}s  "
          f"thresholds: info={INFO_THR}  half={SELL_HALF_THR}  all={SELL_ALL_THR}")
    while True:
        try:
            one_pass()
        except Exception as e:
            print(f"[monitor] loop error: {e}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
