#!/usr/bin/env python3
"""
Local CLOB Executor — runs on YOUR machine (not Render).

Polymarket geo-blocks US server IPs, so CLOB orders can't execute from Render.
This script runs locally, polls for Telegram-approved trades, executes them
via CLOB, and reports results back to the server (which updates Telegram).

Flow:
  1. You get Telegram alert → tap Approve
  2. Server marks trade as "confirmed, awaiting local executor"
  3. This script picks it up, places the CLOB order, reports back
  4. Server updates your Telegram message with the result

Usage:
    python3 21_local_executor.py              # Watch mode (default): poll & auto-execute
    python3 21_local_executor.py --once       # One-shot: check once, execute, exit
    python3 21_local_executor.py --test       # Dry-run: resolve token, build order, don't send
    python3 21_local_executor.py --interval 15  # Poll every 15 seconds (default: 30)

Requires: pip install py-clob-client python-dotenv requests
"""

import os, sys, json, time, argparse, hashlib
from pathlib import Path
from datetime import datetime

# ─── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ─── Config ────────────────────────────────────────────────────────────────────
SERVER_URL = os.environ.get("TENNIS_SERVER_URL", "https://tennis.critterlabs.io")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

LOG_FILE = Path(__file__).parent / "executor.log"


def log(msg: str):
    """Print and log to file."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


def get_admin_cookie() -> str:
    if not ADMIN_PASSWORD or not SECRET_KEY:
        log("ERROR: ADMIN_PASSWORD and SECRET_KEY must be set in .env")
        return ""
    return hashlib.sha256(f"{ADMIN_PASSWORD}:{SECRET_KEY}".encode()).hexdigest()


# ─── CLOB Client ───────────────────────────────────────────────────────────────
def build_clob_client():
    """Initialize Polymarket CLOB client from .env credentials."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE")

    if not all([pk, api_key, api_secret, api_passphrase]):
        log("ERROR: Missing POLYMARKET_* credentials in .env")
        return None

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )

    # Signature type: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    proxy_addr = os.environ.get("POLYMARKET_PROXY_ADDRESS", "")

    log(f"CLOB init: sig_type={sig_type}, proxy={proxy_addr[:10]}..." if proxy_addr else f"CLOB init: sig_type={sig_type}, no proxy")

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        creds=creds,
        signature_type=sig_type,
        funder=proxy_addr if proxy_addr else None,
    )
    return client


# ─── Token ID Resolution ──────────────────────────────────────────────────────
def resolve_token_id(trade: dict) -> str:
    """Resolve clobTokenId from Gamma API (runs locally, not geo-blocked)."""
    import requests

    bet_on = (trade.get("bet_on") or "").strip()
    slug = trade.get("slug", "")
    if not bet_on:
        return ""

    url = "https://gamma-api.polymarket.com/markets"
    markets = []

    if slug:
        try:
            r = requests.get("https://gamma-api.polymarket.com/events",
                             params={"slug": slug}, timeout=15)
            r.raise_for_status()
            events = r.json()
            if events:
                ev = events[0] if isinstance(events, list) else events
                for m in ev.get("markets", [ev]):
                    markets.append(m)
            log(f"  Slug '{slug}': {len(markets)} market(s)")
        except Exception as e:
            log(f"  Slug search failed: {e}")

    if not markets:
        for tag in ["tennis", "atp-tennis", "wta-tennis"]:
            try:
                r = requests.get(url, params={"tag_slug": tag, "active": "true",
                                              "closed": "false", "limit": 200}, timeout=15)
                r.raise_for_status()
                markets.extend(r.json())
            except:
                pass

    last_name = bet_on.split()[-1].lower()

    for m in markets:
        q = m.get("question", "")
        if last_name not in q.lower():
            continue
        outcomes = m.get("outcomes", [])
        clob_tids = m.get("clobTokenIds", [])
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        if isinstance(clob_tids, str):
            try: clob_tids = json.loads(clob_tids)
            except: clob_tids = []
        if not clob_tids or len(clob_tids) != len(outcomes):
            continue
        for i, outcome in enumerate(outcomes):
            if last_name in outcome.lower():
                log(f"  Resolved token for {bet_on}: {clob_tids[i][:30]}...")
                return clob_tids[i]

    return ""


# ─── Gamma neg_risk cross-check ───────────────────────────────────────────────
def _check_gamma_neg_risk(token_id: str):
    """Check neg_risk for a token via Gamma API (more reliable than CLOB for sports)."""
    import requests
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"clob_token_ids": token_id, "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        if markets and len(markets) > 0:
            m = markets[0]
            neg_risk = m.get("negRisk")
            if neg_risk is not None:
                # Gamma returns string "true"/"false" or bool
                if isinstance(neg_risk, str):
                    neg_risk = neg_risk.lower() == "true"
                log(f"  Gamma API neg_risk={neg_risk} for token {token_id[:20]}...")
                return bool(neg_risk)
    except Exception as e:
        log(f"  Gamma neg_risk check failed: {e}")
    return None


# ─── Execute Order ─────────────────────────────────────────────────────────────
def execute_order(client, trade: dict, dry_run: bool = False) -> dict:
    """Build and send a CLOB limit BUY order."""
    token_id = trade.get("token_id") or ""
    if not token_id:
        log("  No token_id in trade, resolving from Gamma API...")
        token_id = resolve_token_id(trade)
    if not token_id:
        return {"success": False, "error": "Could not resolve token_id"}

    stake = trade.get("stake", 5.0)
    entry_price = trade.get("entry_price", trade.get("poly_price", 50))

    # Convert: entry_price is cents (66), CLOB wants decimal (0.66)
    price = entry_price / 100 if entry_price > 1 else entry_price
    size = round(stake / price, 2) if price > 0 else 0

    if size <= 0:
        return {"success": False, "error": f"Invalid size: stake=${stake}, price={price}"}

    log(f"  Order: price={price:.2f} size={size} stake=${stake:.2f}")

    if dry_run:
        log("  [DRY RUN] Order not sent")
        return {"success": True, "dry_run": True, "price": price, "size": size}

    try:
        from py_clob_client.order_builder.constants import BUY
        from py_clob_client.clob_types import OrderArgs, CreateOrderOptions

        # Query the market's actual tick_size and neg_risk
        tick_size = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        log(f"  Market params (CLOB): tick_size={tick_size}, neg_risk={neg_risk}")

        # Cross-check neg_risk via Gamma API (CLOB get_neg_risk returns wrong value for sports)
        gamma_neg_risk = _check_gamma_neg_risk(token_id)
        if gamma_neg_risk is not None:
            if gamma_neg_risk != neg_risk:
                log(f"  ⚠ Gamma says neg_risk={gamma_neg_risk}, overriding CLOB value!")
            neg_risk = gamma_neg_risk  # Trust Gamma over CLOB for sports markets

        # Round price to match tick_size precision
        tick_float = float(tick_size)
        price = round(round(price / tick_float) * tick_float, 4)

        # Resolve fee rate
        fee_rate_bps = client.get_fee_rate_bps(token_id)
        log(f"  fee_rate_bps={fee_rate_bps}")

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY,
            fee_rate_bps=fee_rate_bps,
        )

        # IMPORTANT: Bypass client.create_and_post_order() because it has a bug:
        #   neg_risk = options.neg_risk if options and options.neg_risk else ...
        # When neg_risk is False, Python treats False as falsy and ignores our value.
        # Instead, call builder.create_order directly with CreateOrderOptions (not Partial).
        # This guarantees our neg_risk value is used for contract selection.

        # Try with detected neg_risk first; if order_version_mismatch, flip and retry
        for attempt, nr in enumerate([neg_risk, not neg_risk]):
            try:
                log(f"  Attempt {attempt+1}: neg_risk={nr}")
                opts = CreateOrderOptions(tick_size=tick_size, neg_risk=nr)
                signed_order = client.builder.create_order(order_args, opts)

                # Dump the exact payload for debugging
                import json as _json
                from py_clob_client.config import get_contract_config
                cc = get_contract_config(137, nr)
                payload = signed_order.dict()
                log(f"  Exchange contract: {cc.exchange}")
                log(f"  Order payload: {_json.dumps(payload, indent=None)}")

                order = client.post_order(signed_order)
                log(f"  ✓ CLOB order placed: {order}")
                return {"success": True, "order": order, "price": price, "size": size, "stake": stake}
            except Exception as ex:
                log(f"  Attempt {attempt+1} error: {ex}")
                if "order_version_mismatch" in str(ex) and attempt == 0:
                    log(f"  Retrying with neg_risk={not nr}...")
                    continue
                raise

        return {"success": False, "error": "Exhausted neg_risk attempts"}
    except Exception as e:
        log(f"  CLOB order failed: {e}")
        return {"success": False, "error": str(e)}


# ─── Server Communication ─────────────────────────────────────────────────────
def fetch_approved_trades() -> list:
    """Fetch confirmed-but-unexecuted trades from the server."""
    import requests
    cookie = get_admin_cookie()
    if not cookie:
        return []
    try:
        r = requests.get(
            f"{SERVER_URL}/api/auto-trader/approved",
            cookies={"tennis_admin": cookie},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("trades", [])
    except Exception as e:
        log(f"Server fetch failed: {e}")
        return []


def report_execution(pending_id: str, success: bool, order=None, error: str = ""):
    """Report CLOB execution result back to the server."""
    import requests
    cookie = get_admin_cookie()
    if not cookie:
        return
    try:
        r = requests.post(
            f"{SERVER_URL}/api/auto-trader/execution-report",
            json={
                "pending_id": pending_id,
                "success": success,
                "order": order,
                "error": error,
            },
            cookies={"tennis_admin": cookie},
            timeout=30,
        )
        if r.status_code == 200:
            log(f"  Reported to server: {'OK' if success else 'FAILED'}")
        else:
            log(f"  Server report failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log(f"  Server report failed: {e}")


# ─── Watch Mode ────────────────────────────────────────────────────────────────
def watch_mode(interval: int = 30, once: bool = False, dry_run: bool = False):
    """Poll server for approved trades, execute them, report back."""
    print()
    print("=" * 60)
    print("  CRITTERLABS Local CLOB Executor")
    print(f"  Server: {SERVER_URL}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Poll: {'once' if once else f'every {interval}s'}")
    print("=" * 60)
    print()

    if not dry_run:
        client = build_clob_client()
        if not client:
            return
        log("CLOB client ready")
    else:
        client = None
        log("DRY RUN mode — no orders will be sent")

    log("Watching for approved trades... (Ctrl+C to stop)\n")

    executed_ids = set()  # Track what we've already processed this session

    while True:
        try:
            trades = fetch_approved_trades()

            # Filter out trades we've already processed this session
            new_trades = [t for t in trades if t.get("pending_id") not in executed_ids]

            if new_trades:
                log(f"Found {len(new_trades)} approved trade(s) to execute")
                for trade in new_trades:
                    pid = trade.get("pending_id", "?")
                    bet_on = trade.get("bet_on", "?")
                    match_name = trade.get("match", "?")
                    stake = trade.get("stake", 0)

                    log(f"\n{'─' * 50}")
                    log(f"EXECUTING: {bet_on}")
                    log(f"  Match: {match_name}")
                    log(f"  Stake: ${stake:.2f} | Price: {trade.get('entry_price', '?')}c")

                    result = execute_order(client, trade, dry_run=dry_run)

                    if result.get("success"):
                        log(f"  SUCCESS — placed at {result.get('price')}, size={result.get('size')}")
                        if not dry_run:
                            report_execution(pid, True, order=result.get("order"))
                    else:
                        log(f"  FAILED — {result.get('error')}")
                        if not dry_run:
                            report_execution(pid, False, error=result.get("error", ""))

                    executed_ids.add(pid)

                log(f"\n{'─' * 50}\n")
            else:
                # Inline status (no newline spam)
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{ts}] No new trades — waiting...", end="\r")

            if once:
                if not new_trades:
                    log("No approved trades found.")
                break

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\n")
            log("Stopped. Bye!")
            break


# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Local CLOB Executor — polls for Telegram-approved trades and executes them")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit (don't poll)")
    parser.add_argument("--test", action="store_true",
                        help="Dry run — resolve tokens and build orders but don't send")
    parser.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    args = parser.parse_args()

    watch_mode(interval=args.interval, once=args.once, dry_run=args.test)
