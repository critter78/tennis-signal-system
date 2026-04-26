"""
AUTO-TRADER ENGINE — Paper / Semi-Auto / Full-Auto
====================================================

Reads fresh signals from picks.jsonl, applies user-defined entry rules
from auto_trader_config.json, and either:
  - PAPER mode:  logs what it WOULD do (no real trades)
  - SEMI mode:   writes pending orders for user confirmation via dashboard
  - LIVE mode:   executes orders via Polymarket CLOB API (future)

Also monitors open positions and applies exit rules (stop-loss, take-profit,
trailing stop) on a polling loop.

Usage:
  # Run after pipeline refresh (entry scan):
  python3 20_auto_trader.py --scan

  # Run position monitor (exit loop):
  python3 20_auto_trader.py --monitor --interval 60

  # One-shot monitor:
  python3 20_auto_trader.py --monitor --once

  # Dry-run (ignores mode, always paper):
  python3 20_auto_trader.py --scan --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import os

# ─── Paths ───────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "auto_trader_config.json"

# Render persistent storage
PERSIST = Path("/data")
if PERSIST.exists() and PERSIST.is_dir():
    LOGS_DIR = PERSIST / "logs"
    DATA_DIR = PERSIST / "data"
else:
    LOGS_DIR = BASE_DIR / "logs"
    DATA_DIR = BASE_DIR / "data"

PICKS_FILE = LOGS_DIR / "picks.jsonl"
BETS_FILE = LOGS_DIR / "my_bets.json"
PENDING_FILE = LOGS_DIR / "pending_trades.json"  # Semi-auto: trades awaiting user confirmation

# ─── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    print("[config] auto_trader_config.json not found — using defaults", file=sys.stderr)
    return {
        "mode": "paper",
        "entry_rules": {
            "min_edge": 10.0, "min_model_prob": 55.0, "min_gap": 5,
            "min_consensus": 3, "edge_conf": ["STRONG"],
            "market_types": ["h2h"], "max_stake_per_bet": 50.0,
            "kelly_fraction": 0.25, "max_daily_bets": 10,
            "bet_type": "edge", "min_volume": 1000,
            "exclude_outrights": True,
        },
        "exit_rules": {
            "stop_loss_pct": 15.0,
            "take_profit_1_price": 85, "take_profit_1_sell_pct": 50,
            "take_profit_2_price": 95, "take_profit_2_sell_pct": 100,
            "trailing_stop_pct": 8.0, "time_exit_hours": 4,
            "poll_interval_seconds": 60,
        },
        "safety": {
            "kill_switch": False, "daily_loss_limit": 200.0,
            "max_total_exposure": 500.0, "require_confirmation": True,
            "paper_trade_log": "data/logs/paper_trades.jsonl",
            "live_trade_log": "data/logs/live_trades.jsonl",
        },
        "polymarket": {
            "clob_url": "https://clob.polymarket.com",
            "gamma_url": "https://gamma-api.polymarket.com",
        },
    }


# ─── Signal Loading ─────────────────────────────────────────────────────────

def load_todays_signals() -> list[dict]:
    """Load today's signals from picks.jsonl."""
    if not PICKS_FILE.exists():
        print("[signals] picks.jsonl not found", file=sys.stderr)
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals = []
    with open(PICKS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                logged = (p.get("logged_at") or "")[:10]
                if logged == today and not p.get("outcome"):
                    signals.append(p)
            except json.JSONDecodeError:
                continue
    return signals


def load_existing_bets() -> list[dict]:
    """Load already-placed bets to avoid duplicates."""
    if not BETS_FILE.exists():
        return []
    try:
        data = json.loads(BETS_FILE.read_text())
        bets = data.get("bets", data) if isinstance(data, dict) else data
        if isinstance(bets, dict):
            bets = list(bets.values())
        return [b for b in bets if isinstance(b, dict) and b.get("match")]
    except Exception:
        return []


# ─── Entry Filter ────────────────────────────────────────────────────────────

def passes_entry_rules(signal: dict, rules: dict, existing_bets: list[dict]) -> tuple[bool, str]:
    """
    Check if a signal passes all entry rules.
    Returns (pass: bool, reason: str).
    """
    match = signal.get("match", "")
    bet_on = signal.get("bet_on", "")

    # Already bet on this match?
    for b in existing_bets:
        if b.get("match") == match:
            return False, "already_bet"

    # Market type filter
    mt = signal.get("market_type", "h2h")
    if rules.get("exclude_outrights") and mt != "h2h":
        return False, f"market_type={mt}"
    if mt not in rules.get("market_types", ["h2h"]):
        return False, f"market_type={mt}"

    # Edge threshold
    edge = abs(signal.get("edge", 0))
    if edge < rules.get("min_edge", 10):
        return False, f"edge={edge:.1f} < {rules['min_edge']}"

    # Model probability
    model_prob = signal.get("model_prob", 0)
    if model_prob < rules.get("min_model_prob", 55):
        return False, f"model_prob={model_prob:.1f} < {rules['min_model_prob']}"

    # Gap (model_prob - poly_price)
    poly_price = signal.get("poly_price", 0)
    gap = model_prob - poly_price
    if gap < rules.get("min_gap", 5):
        return False, f"gap={gap:.1f} < {rules['min_gap']}"

    # Consensus
    consensus_count = 0
    if signal.get("base_agrees"):
        consensus_count += 1
    if signal.get("lstm_agrees"):
        consensus_count += 1
    if signal.get("elo_agrees"):
        consensus_count += 1
    if signal.get("surface_agrees"):
        consensus_count += 1
    # Fallback: use has_edge or confidence if individual flags not available
    if consensus_count == 0 and signal.get("confidence", 0) >= 60:
        consensus_count = 3  # approximate
    if consensus_count < rules.get("min_consensus", 3):
        return False, f"consensus={consensus_count}/4 < {rules['min_consensus']}"

    # Edge confidence level (skip if field not present in data)
    allowed_conf = rules.get("edge_conf", ["STRONG"])
    ec = signal.get("edge_confidence")
    if ec and allowed_conf and allowed_conf != ["ALL"]:
        if ec not in allowed_conf:
            return False, f"edge_conf={ec} not in {allowed_conf}"

    # Volume (skip filter if volume data not available)
    vol = signal.get("volume") or signal.get("liquidity") or 0
    min_vol = rules.get("min_volume", 1000)
    if min_vol > 0 and vol > 0 and vol < min_vol:
        return False, f"volume={vol} < {min_vol}"

    return True, "PASS"


def calculate_stake(signal: dict, rules: dict) -> float:
    """Calculate stake using Kelly fraction, capped by max_stake."""
    kelly_fraction = rules.get("kelly_fraction", 0.25)
    max_stake = rules.get("max_stake_per_bet", 50.0)

    # Try pre-computed kelly_stake first
    kelly_pct = signal.get("kelly_stake", 0)
    if kelly_pct and kelly_pct > 0:
        stake = max_stake * (kelly_pct / 100) * kelly_fraction
    else:
        # Compute from edge: Kelly % = (p*b - q) / b where p = model_prob, b = decimal odds from poly_price
        model_prob = signal.get("model_prob", 50) / 100
        poly_price = signal.get("poly_price", 50) / 100
        if poly_price > 0 and poly_price < 1:
            b = (1 / poly_price) - 1  # decimal odds minus 1
            q = 1 - model_prob
            kelly_raw = (model_prob * b - q) / b if b > 0 else 0
            kelly_raw = max(0, kelly_raw)
            stake = max_stake * kelly_raw * kelly_fraction
        else:
            stake = max_stake * 0.1  # fallback 10% of max

    # Floor at $5, cap at max
    stake = max(5.0, min(stake, max_stake))
    return round(stake, 2)


# ─── Pending Trades (Semi-Auto) ─────────────────────────────────────────────

def load_pending_trades() -> list[dict]:
    """Load pending trades awaiting user confirmation."""
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return []


def save_pending_trades(trades: list[dict]):
    """Save pending trades list."""
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(trades, indent=2))


def load_trade_history() -> list[dict]:
    """Load confirmed/rejected trades from paper_trades.jsonl to prevent re-queuing."""
    log_path = LOGS_DIR / "paper_trades.jsonl"
    if PERSIST.exists():
        log_path = PERSIST / "logs" / "paper_trades.jsonl"
    if not log_path.exists():
        return []
    trades = []
    with open(log_path) as f:
        for line in f:
            try:
                trades.append(json.loads(line.strip()))
            except Exception:
                continue
    return trades


def add_pending_trade(trade: dict):
    """Add a trade to the pending queue for user confirmation."""
    import uuid
    trade["pending_id"] = str(uuid.uuid4())[:8]
    trade["status"] = "pending"
    trade["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    match_key = trade.get("match", "")
    bet_key = trade.get("bet_on", "")

    # Don't add duplicates — check pending queue
    pending = load_pending_trades()
    for p in pending:
        if p.get("match") == match_key and p.get("bet_on") == bet_key:
            return  # already queued

    # Don't re-queue trades that were already confirmed or rejected
    history = load_trade_history()
    for h in history:
        if h.get("match") == match_key and h.get("bet_on") == bet_key:
            return  # already acted on

    pending.append(trade)
    save_pending_trades(pending)


def confirm_pending_trade(pending_id: str) -> dict | None:
    """Confirm a pending trade — mark as confirmed, return the trade."""
    pending = load_pending_trades()
    confirmed = None
    remaining = []
    for p in pending:
        if p.get("pending_id") == pending_id:
            p["status"] = "confirmed"
            p["confirmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            confirmed = p
        else:
            remaining.append(p)
    save_pending_trades(remaining)
    return confirmed


def reject_pending_trade(pending_id: str) -> dict | None:
    """Reject a pending trade — remove from queue."""
    pending = load_pending_trades()
    rejected = None
    remaining = []
    for p in pending:
        if p.get("pending_id") == pending_id:
            rejected = p
        else:
            remaining.append(p)
    save_pending_trades(remaining)
    return rejected


# ─── Paper Trade Logging ────────────────────────────────────────────────────

def log_trade(trade: dict, config: dict):
    """Append a trade record to the appropriate log file."""
    mode = config.get("mode", "paper")
    safety = config.get("safety", {})

    if mode == "paper" or mode == "semi":
        log_path = BASE_DIR / safety.get("paper_trade_log", "data/logs/paper_trades.jsonl")
    else:
        log_path = BASE_DIR / safety.get("live_trade_log", "data/logs/live_trades.jsonl")

    # Use persistent storage on Render
    if PERSIST.exists():
        log_path = PERSIST / "logs" / log_path.name

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(trade) + "\n")


def load_todays_trades(config: dict) -> list[dict]:
    """Load today's paper/live trades to check daily limits."""
    safety = config.get("safety", {})
    mode = config.get("mode", "paper")

    if mode in ("paper", "semi"):
        log_path = BASE_DIR / safety.get("paper_trade_log", "data/logs/paper_trades.jsonl")
    else:
        log_path = BASE_DIR / safety.get("live_trade_log", "data/logs/live_trades.jsonl")

    if PERSIST.exists():
        log_path = PERSIST / "logs" / log_path.name

    if not log_path.exists():
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trades = []
    with open(log_path) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                if (t.get("timestamp", "")[:10] == today
                        and t.get("action") == "entry"):
                    trades.append(t)
            except Exception:
                continue
    return trades


# ─── CLOB Price Fetch ────────────────────────────────────────────────────────

def fetch_clob_price(token_id: str, clob_url: str = "https://clob.polymarket.com") -> float | None:
    """Fetch current BUY price from Polymarket CLOB."""
    try:
        r = requests.get(f"{clob_url}/price",
                         params={"token_id": token_id, "side": "BUY"},
                         timeout=10)
        if r.status_code == 200:
            return float(r.json().get("price", 0) or 0)
    except Exception as e:
        print(f"[clob] price fetch failed: {e}", file=sys.stderr)
    return None


# ─── CLOB Order Execution ──────────────────────────────────────────────────

def get_clob_client():
    """Initialize Polymarket CLOB client from .env credentials."""
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass  # dotenv not installed, rely on environment vars

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE")
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    proxy_addr = os.environ.get("POLYMARKET_PROXY_ADDRESS", "")

    if not all([pk, api_key, api_secret, api_passphrase]):
        print("[clob] Missing credentials in .env — cannot execute trades", file=sys.stderr)
        return None

    try:
        from py_clob_client.client import ClobClient

        creds = {
            "apiKey": api_key,
            "secret": api_secret,
            "passphrase": api_passphrase,
        }

        if sig_type == 0:
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                creds=creds,
            )
        else:
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                creds=creds,
                signature_type=sig_type,
                funder=proxy_addr,
            )
        return client
    except Exception as e:
        print(f"[clob] Client init failed: {e}", file=sys.stderr)
        return None


def _resolve_token_id(trade: dict) -> str:
    """
    Last-resort lookup: fetch the market from Gamma API and extract
    the clobTokenId for the player we're betting on.
    Gamma API returns outcomes/clobTokenIds as JSON strings — must parse them.
    """
    import requests as _req
    import json as _json
    bet_on = (trade.get("bet_on") or "").strip()
    slug = trade.get("slug", "")
    if not bet_on:
        print("[resolve_tid] No bet_on in trade", file=sys.stderr)
        return ""

    url = "https://gamma-api.polymarket.com/markets"
    markets = []

    # Strategy 1: search by slug (most precise)
    if slug:
        try:
            r = _req.get(url, params={"slug": slug}, timeout=15)
            r.raise_for_status()
            markets = r.json()
            print(f"[resolve_tid] Slug search '{slug}': {len(markets)} results", file=sys.stderr)
        except Exception as e:
            print(f"[resolve_tid] Slug search failed: {e}", file=sys.stderr)

    # Strategy 2: search by tag if slug didn't work
    if not markets:
        for tag in ["tennis", "atp-tennis", "wta-tennis"]:
            try:
                r = _req.get(url, params={"tag_slug": tag, "active": "true",
                                           "closed": "false", "limit": 200}, timeout=15)
                r.raise_for_status()
                batch = r.json()
                markets.extend(batch)
            except Exception as e:
                print(f"[resolve_tid] Tag {tag} failed: {e}", file=sys.stderr)

    last_name = bet_on.split()[-1].lower() if bet_on else ""

    for m in markets:
        q = m.get("question", "")
        if last_name and last_name not in q.lower():
            continue

        # Parse JSON strings — Gamma API returns these as strings, not arrays
        outcomes = m.get("outcomes", [])
        clob_tids = m.get("clobTokenIds", [])
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except: outcomes = []
        if isinstance(clob_tids, str):
            try: clob_tids = _json.loads(clob_tids)
            except: clob_tids = []

        if not clob_tids or len(clob_tids) != len(outcomes):
            continue

        # Find which outcome matches our bet_on player
        for i, outcome in enumerate(outcomes):
            if last_name in outcome.lower():
                tid = clob_tids[i]
                print(f"[resolve_tid] Found token_id for {bet_on}: {tid[:20]}...", file=sys.stderr)
                return tid

    print(f"[resolve_tid] No matching market found for {bet_on}", file=sys.stderr)
    return ""

    print(f"[resolve_tid] No matching market found for {bet_on} in {match_str}", file=sys.stderr)
    return ""


def execute_clob_order(trade: dict) -> dict:
    """
    Place a limit BUY order on Polymarket CLOB.
    Returns dict with order_id and status, or error info.
    """
    client = get_clob_client()
    if not client:
        return {"success": False, "error": "CLOB client not configured"}

    token_id = trade.get("token_id")
    if not token_id:
        # Auto-resolve token_id from Gamma API using match/bet_on info
        token_id = _resolve_token_id(trade)
    if not token_id:
        return {"success": False, "error": "No token_id in trade and Gamma API lookup failed"}

    stake = trade.get("stake", 5.0)
    entry_price = trade.get("entry_price", trade.get("poly_price", 50))

    # Convert price: entry_price is in cents (e.g. 66), CLOB wants decimal (0.66)
    price = entry_price / 100 if entry_price > 1 else entry_price

    # Size = how many shares to buy (stake / price)
    size = round(stake / price, 2) if price > 0 else 0

    if size <= 0:
        return {"success": False, "error": f"Invalid size: stake=${stake}, price={price}"}

    try:
        from py_clob_client.order_builder.constants import BUY
        from py_clob_client.clob_types import OrderArgs

        # Create and post a limit order using OrderArgs
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=size,
            side=BUY,
        )
        order = client.create_and_post_order(order_args
        )

        print(f"[CLOB] Order placed: {order}", file=sys.stderr)
        return {
            "success": True,
            "order": order,
            "price": price,
            "size": size,
            "stake": stake,
        }
    except Exception as e:
        print(f"[CLOB] Order failed: {e}", file=sys.stderr)
        return {"success": False, "error": str(e)}


# ─── Entry Scan ──────────────────────────────────────────────────────────────

def scan_entries(config: dict, dry_run: bool = False) -> list[dict]:
    """
    Scan today's signals, filter by entry rules, log paper trades.
    Returns list of trade decisions.
    """
    mode = config.get("mode", "paper")
    if dry_run:
        mode = "paper"

    rules = config.get("entry_rules", {})
    safety = config.get("safety", {})

    # Kill switch
    if safety.get("kill_switch"):
        print("[KILL SWITCH] Auto-trader is disabled.")
        return []

    # Load signals and existing bets
    signals = load_todays_signals()
    existing_bets = load_existing_bets()
    todays_trades = load_todays_trades(config)

    print(f"\n{'='*70}")
    print(f"  AUTO-TRADER SCAN  |  Mode: {mode.upper()}  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")
    print(f"  Signals today: {len(signals)}")
    print(f"  Existing bets: {len(existing_bets)}")
    print(f"  Trades today:  {len(todays_trades)}")
    print(f"{'='*70}\n")

    # Daily bet limit
    max_daily = rules.get("max_daily_bets", 10)
    if len(todays_trades) >= max_daily:
        print(f"[limit] Daily bet limit reached ({max_daily})")
        return []

    # Daily loss check
    daily_pnl = sum(t.get("unrealized_pnl", 0) for t in todays_trades)
    if daily_pnl <= -safety.get("daily_loss_limit", 200):
        print(f"[limit] Daily loss limit reached (${daily_pnl:.2f})")
        return []

    # Total exposure check
    total_exposure = sum(t.get("stake", 0) for t in todays_trades if not t.get("exited"))
    max_exposure = safety.get("max_total_exposure", 500)

    decisions = []
    passed = 0
    filtered = 0

    for sig in signals:
        passes, reason = passes_entry_rules(sig, rules, existing_bets)

        if not passes:
            filtered += 1
            continue

        # Check exposure limit
        stake = calculate_stake(sig, rules)
        if total_exposure + stake > max_exposure:
            print(f"  [skip] {sig.get('match', '?')[:40]} — exposure limit (${total_exposure:.0f}+${stake:.0f} > ${max_exposure:.0f})")
            continue

        # Check daily bet limit
        if passed + len(todays_trades) >= max_daily:
            print(f"  [skip] Daily limit reached")
            break

        passed += 1
        total_exposure += stake

        # Build trade record
        trade = {
            "action": "entry",
            "mode": mode,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "match": sig.get("match"),
            "bet_on": sig.get("bet_on"),
            "player_a": sig.get("player_a"),
            "player_b": sig.get("player_b"),
            "predicted_winner": sig.get("predicted_winner"),
            "bet_type": "edge",
            "tournament": sig.get("tournament"),
            "surface": sig.get("surface"),
            "market_type": sig.get("market_type"),
            "model_prob": sig.get("model_prob"),
            "poly_price": sig.get("poly_price"),
            "edge": sig.get("edge"),
            "gap": round(sig.get("model_prob", 0) - sig.get("poly_price", 0), 1),
            "consensus": sig.get("confidence"),
            "edge_confidence": sig.get("edge_confidence"),
            "kelly_stake": sig.get("kelly_stake"),
            "stake": stake,
            "entry_price": sig.get("poly_price"),
            "token_id": sig.get("token_id"),
            "poly_link": sig.get("poly_link"),
            "slug": sig.get("slug"),
            "volume": sig.get("volume"),
            "run_id": sig.get("run_id"),
            "exited": False,
        }

        # Log decision
        edge_str = f"+{sig.get('edge', 0):.1f}%" if sig.get('edge', 0) > 0 else f"{sig.get('edge', 0):.1f}%"
        gap_val = trade["gap"]
        gap_str = f"+{gap_val:.0f}" if gap_val > 0 else f"{gap_val:.0f}"

        status = "PAPER" if mode == "paper" else ("PENDING" if mode == "semi" else "EXECUTE")
        print(f"  [{status}] {sig.get('match', '?')[:45]}")
        print(f"          Pick: {sig.get('bet_on', '?')} | Model: {sig.get('model_prob', 0):.1f}% | Poly: {sig.get('poly_price', 0):.0f}c | Gap: {gap_str} | Edge: {edge_str}")
        print(f"          Stake: ${stake:.2f} | Kelly: {sig.get('kelly_stake', 0):.1f}% | Conf: {sig.get('edge_confidence', '?')}")
        print()

        # In semi mode: queue for confirmation instead of just logging
        if mode == "semi":
            add_pending_trade(trade)
        else:
            log_trade(trade, config)

        decisions.append(trade)

    print(f"{'─'*70}")
    print(f"  Summary: {passed} trades | {filtered} filtered | Mode: {mode.upper()}")
    if passed > 0:
        print(f"  Total stake: ${sum(d['stake'] for d in decisions):.2f}")
    if mode == "semi" and passed > 0:
        print(f"  → {passed} trade(s) queued for confirmation in dashboard")
    print(f"{'─'*70}\n")

    return decisions


# ─── Position Monitor ────────────────────────────────────────────────────────

def load_open_positions(config: dict) -> list[dict]:
    """Load open (not exited) paper/live trades."""
    safety = config.get("safety", {})
    mode = config.get("mode", "paper")

    if mode in ("paper", "semi"):
        log_path = BASE_DIR / safety.get("paper_trade_log", "data/logs/paper_trades.jsonl")
    else:
        log_path = BASE_DIR / safety.get("live_trade_log", "data/logs/live_trades.jsonl")

    if PERSIST.exists():
        log_path = PERSIST / "logs" / log_path.name

    if not log_path.exists():
        return []

    positions = []
    with open(log_path) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                if t.get("action") == "entry" and not t.get("exited"):
                    positions.append(t)
            except Exception:
                continue

    # Remove positions that have been exited in later log entries
    exit_keys = set()
    with open(log_path) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                if t.get("action") in ("exit", "stop_loss", "take_profit", "trailing_stop"):
                    key = t.get("match", "") + "|" + t.get("bet_on", "")
                    exit_keys.add(key)
            except Exception:
                continue

    return [p for p in positions if (p.get("match", "") + "|" + p.get("bet_on", "")) not in exit_keys]


def monitor_positions(config: dict, once: bool = False):
    """Poll open positions and apply exit rules."""
    mode = config.get("mode", "paper")
    exit_rules = config.get("exit_rules", {})
    safety = config.get("safety", {})
    clob_url = config.get("polymarket", {}).get("clob_url", "https://clob.polymarket.com")
    interval = exit_rules.get("poll_interval_seconds", 60)

    print(f"\n{'='*70}")
    print(f"  POSITION MONITOR  |  Mode: {mode.upper()}  |  Interval: {interval}s")
    print(f"{'='*70}\n")

    while True:
        if safety.get("kill_switch"):
            print("[KILL SWITCH] Monitor paused.")
            if once:
                return
            time.sleep(interval)
            continue

        positions = load_open_positions(config)
        if not positions:
            print(f"  [{datetime.now(timezone.utc).strftime('%H:%M')}] No open positions")
            if once:
                return
            time.sleep(interval)
            continue

        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M')}] Monitoring {len(positions)} open positions...")

        for pos in positions:
            token_id = pos.get("token_id")
            if not token_id:
                continue

            price = fetch_clob_price(token_id, clob_url)
            if price is None:
                continue

            price_cents = price * 100  # Convert to cents
            entry_price = pos.get("entry_price", 50)
            stake = pos.get("stake", 100)
            match_name = pos.get("match", "?")[:40]
            bet_on = pos.get("bet_on", "?")

            # Track peak
            peak = max(pos.get("_peak", entry_price), price_cents)
            pos["_peak"] = peak

            # Current P&L
            pnl_pct = ((price_cents - entry_price) / entry_price * 100) if entry_price > 0 else 0
            pnl_usd = stake * (price_cents - entry_price) / entry_price if entry_price > 0 else 0

            # ── Exit Rule Checks ──

            exit_action = None
            exit_reason = None

            # 1. Stop-loss
            stop_loss_pct = exit_rules.get("stop_loss_pct", 15)
            if pnl_pct <= -stop_loss_pct:
                exit_action = "stop_loss"
                exit_reason = f"Stop loss triggered: {pnl_pct:.1f}% (limit: -{stop_loss_pct}%)"

            # 2. Take-profit tier 1
            tp1_price = exit_rules.get("take_profit_1_price", 85)
            if not exit_action and price_cents >= tp1_price:
                tp2_price = exit_rules.get("take_profit_2_price", 95)
                if price_cents >= tp2_price:
                    exit_action = "take_profit"
                    exit_reason = f"Take profit T2: price {price_cents:.1f}c >= {tp2_price}c"
                else:
                    exit_action = "take_profit_partial"
                    exit_reason = f"Take profit T1: price {price_cents:.1f}c >= {tp1_price}c (sell {exit_rules.get('take_profit_1_sell_pct', 50)}%)"

            # 3. Trailing stop (only if we've been in profit)
            if not exit_action and peak > entry_price:
                trail_pct = exit_rules.get("trailing_stop_pct", 8)
                drop_from_peak = ((peak - price_cents) / peak * 100) if peak > 0 else 0
                if drop_from_peak >= trail_pct:
                    exit_action = "trailing_stop"
                    exit_reason = f"Trailing stop: dropped {drop_from_peak:.1f}% from peak {peak:.0f}c"

            # Log status
            pnl_color = "+" if pnl_pct >= 0 else ""
            print(f"    {bet_on:>15} | {price_cents:.0f}c (entry {entry_price:.0f}c) | P&L: {pnl_color}{pnl_pct:.1f}% (${pnl_color}{pnl_usd:.2f}) | Peak: {peak:.0f}c", end="")

            if exit_action:
                print(f" | EXIT: {exit_reason}")

                exit_record = {
                    "action": exit_action,
                    "mode": mode,
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "match": pos.get("match"),
                    "bet_on": pos.get("bet_on"),
                    "entry_price": entry_price,
                    "exit_price": price_cents,
                    "peak": peak,
                    "stake": stake,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(pnl_usd, 2),
                    "reason": exit_reason,
                    "token_id": token_id,
                }
                log_trade(exit_record, config)
            else:
                print()

        if once:
            return
        time.sleep(interval)


# ─── Backtest Against Historical Picks ───────────────────────────────────────

def backtest(config: dict, days: int = 30):
    """
    Run entry rules against historical picks to see what WOULD have been
    traded, and what the outcomes were. Useful for tuning parameters.
    """
    rules = config.get("entry_rules", {})

    if not PICKS_FILE.exists():
        print("[backtest] picks.jsonl not found")
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    signals = []
    with open(PICKS_FILE) as f:
        for line in f:
            try:
                p = json.loads(line.strip())
                logged = (p.get("logged_at") or "")[:10]
                if logged >= cutoff:
                    signals.append(p)
            except Exception:
                continue

    print(f"\n{'='*70}")
    print(f"  BACKTEST  |  Last {days} days  |  {len(signals)} signals")
    print(f"{'='*70}\n")

    would_trade = []
    for sig in signals:
        passes, reason = passes_entry_rules(sig, rules, [])
        if passes:
            would_trade.append(sig)

    resolved = [s for s in would_trade if s.get("outcome") in ("win", "loss")]
    wins = [s for s in resolved if s.get("outcome") == "win"]
    losses = [s for s in resolved if s.get("outcome") == "loss"]
    pending = [s for s in would_trade if not s.get("outcome")]

    total_pnl = sum(s.get("pnl", 0) for s in resolved)

    print(f"  Would have traded: {len(would_trade)} signals")
    print(f"  Resolved:          {len(resolved)} ({len(wins)}W - {len(losses)}L)")
    print(f"  Pending:           {len(pending)}")
    if resolved:
        wr = len(wins) / len(resolved) * 100
        print(f"  Win Rate:          {wr:.1f}%")
        print(f"  P&L (notional):    ${total_pnl:+.0f}")

        # Breakdown by edge bucket
        print(f"\n  {'Edge Bucket':<15} {'Trades':<8} {'W-L':<10} {'WR':<8} {'Avg PnL':<10}")
        print(f"  {'─'*55}")
        for lo, hi, label in [(3, 10, "3-10%"), (10, 20, "10-20%"), (20, 50, "20%+")]:
            bucket = [s for s in resolved if lo <= abs(s.get("edge", 0)) < hi]
            if bucket:
                bw = len([s for s in bucket if s["outcome"] == "win"])
                bl = len(bucket) - bw
                bwr = bw / len(bucket) * 100
                bpnl = sum(s.get("pnl", 0) for s in bucket) / len(bucket)
                print(f"  {label:<15} {len(bucket):<8} {bw}W-{bl}L    {bwr:>5.1f}%  ${bpnl:>+7.0f}")

        # Breakdown by gap bucket
        print(f"\n  {'Gap Bucket':<15} {'Trades':<8} {'W-L':<10} {'WR':<8}")
        print(f"  {'─'*45}")
        for lo, hi, label in [(0, 5, "0-5"), (5, 10, "5-10"), (10, 20, "10-20"), (20, 100, "20+")]:
            bucket = [s for s in resolved if lo <= (s.get("model_prob", 0) - s.get("poly_price", 0)) < hi]
            if bucket:
                bw = len([s for s in bucket if s["outcome"] == "win"])
                bl = len(bucket) - bw
                bwr = bw / len(bucket) * 100
                print(f"  {label:<15} {len(bucket):<8} {bw}W-{bl}L    {bwr:>5.1f}%")

    print(f"\n{'='*70}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auto-trader engine")
    parser.add_argument("--scan", action="store_true", help="Scan signals and generate trade decisions")
    parser.add_argument("--monitor", action="store_true", help="Monitor open positions for exits")
    parser.add_argument("--backtest", action="store_true", help="Backtest entry rules against historical data")
    parser.add_argument("--days", type=int, default=30, help="Backtest lookback days (default: 30)")
    parser.add_argument("--once", action="store_true", help="Run monitor once then exit")
    parser.add_argument("--interval", type=int, help="Override poll interval (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Force paper mode regardless of config")
    args = parser.parse_args()

    config = load_config()

    if args.dry_run:
        config["mode"] = "paper"

    if args.interval:
        config.setdefault("exit_rules", {})["poll_interval_seconds"] = args.interval

    if args.backtest:
        backtest(config, args.days)
    elif args.scan:
        scan_entries(config, dry_run=args.dry_run)
    elif args.monitor:
        monitor_positions(config, once=args.once)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
