#!/usr/bin/env python3
"""
Auto-Trader Cron — Render Cron Job
Runs every 2 hours. Calls the web server's cron-scan endpoint,
which runs entry rules, queues pending trades, and sends Telegram alerts.

Lightweight HTTP caller — all logic lives in server.py.
Authenticated via CRON_SECRET (same as the pipeline cron).
"""

import os
import sys
import json
import urllib.request
from datetime import datetime

WEB_SERVICE_URL = os.environ.get("WEB_SERVICE_URL", "https://tennis.critterlabs.io")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def main():
    log("=" * 60)
    log("AUTO-TRADER CRON SCAN")
    log("=" * 60)

    if not CRON_SECRET:
        log("ERROR: CRON_SECRET not set. Cannot authenticate.")
        sys.exit(1)

    scan_url = f"{WEB_SERVICE_URL}/api/auto-trader/cron-scan"
    log(f"Calling: {scan_url}")

    try:
        data = json.dumps({"secret": CRON_SECRET}).encode("utf-8")
        req = urllib.request.Request(
            scan_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Cron-Secret": CRON_SECRET,
                "User-Agent": "auto-trader-cron/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode()
            log(f"Response: {resp.status}")

            try:
                result = json.loads(body)
                log(f"Status: {result.get('status', '?')}")
                pending_count = result.get("pending_count", 0)
                log(f"Pending trades: {pending_count}")
                for t in result.get("pending", []):
                    log(f"  → {t.get('bet_on', '?')} | {t.get('match', '?')} | TG:{t.get('telegram_sent', False)}")
                if result.get("output"):
                    for line in result["output"].strip().split("\n")[-3:]:
                        log(f"  {line}")
            except json.JSONDecodeError:
                log(f"Raw: {body[:500]}")

    except urllib.error.HTTPError as e:
        log(f"HTTP Error {e.code}: {e.reason}")
        try:
            log(f"Body: {e.read().decode()[:300]}")
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        log(f"Request failed: {e}")
        sys.exit(1)

    log("Done.")
    log("=" * 60)


if __name__ == "__main__":
    main()
