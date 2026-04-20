#!/usr/bin/env python3
"""
V2 cron — calls the v2 web service's /api/refresh endpoint.

Mirrors the v1 cron pattern: data lives on the web service's persistent disk,
the cron just triggers a refresh via authenticated HTTP. Avoids the cron
container's ephemeral disk problem.
"""
import os, sys, json
from datetime import datetime
import requests


def log(m):
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] {m}")


def main():
    log("=" * 60)
    log("V2 CRON: triggering refresh on tennisv2 service")
    log("=" * 60)

    web = os.environ.get("WEB_SERVICE_URL", "https://tennis-v2-signal.onrender.com")
    secret = os.environ.get("CRON_SECRET", "tennis-v2-cron-2026")
    url = f"{web}/api/refresh"
    log(f"POST {url}")
    try:
        r = requests.post(url,
                          headers={"X-Cron-Secret": secret,
                                   "Content-Type": "application/json"},
                          timeout=900)
        log(f"HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            log(f"status={d.get('status')}  succeeded={d.get('succeeded')}/{d.get('total')}  "
                f"elapsed={d.get('elapsed_seconds')}s")
            for name, step in d.get("steps", {}).items():
                log(f"  [{step.get('status')}] {name}")
        else:
            log(f"body: {r.text[:500]}")
            sys.exit(1)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
