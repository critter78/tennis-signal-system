#!/usr/bin/env python3
"""
Cron Job for Render — calls the web server's /api/refresh endpoint.

Instead of running the pipeline locally (which writes to the cron container's
ephemeral disk and gets thrown away), this calls the live web server so the
data actually gets updated where users can see it.

Schedule: every 6 hours (0 2,8,14,20 * * * UTC)
"""

import os
import json
import sys
from datetime import datetime

import requests


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def main():
    log("=" * 60)
    log("CRON: Triggering web server refresh")
    log("=" * 60)

    # Get the web server URL — Render sets this, or use the internal service URL
    # For Render internal networking, the web service is accessible via its name
    web_url = os.environ.get("WEB_SERVICE_URL", "https://tennis-betting-server.onrender.com")
    cron_secret = os.environ.get("CRON_SECRET", "tennis-cron-2026")

    refresh_url = f"{web_url}/api/refresh"
    log(f"Calling: {refresh_url}")

    try:
        resp = requests.post(
            refresh_url,
            headers={
                "X-Cron-Secret": cron_secret,
                "Content-Type": "application/json",
            },
            timeout=600,  # 10 min — the pipeline can take a while
        )

        log(f"Response: HTTP {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            log(f"Status: {data.get('status')}")
            log(f"Elapsed: {data.get('elapsed_seconds', '?')}s")
            log(f"Steps: {data.get('succeeded', '?')}/{data.get('total', '?')} succeeded")

            for step_name, step_result in data.get("steps", {}).items():
                status = step_result.get("status", "?")
                icon = "OK" if status == "ok" else "FAIL"
                log(f"  [{icon}] {step_name}")
                if step_result.get("output"):
                    # Show last 2 lines of output
                    for line in step_result["output"].strip().split("\n")[-2:]:
                        log(f"        {line.strip()}")

            log("Refresh complete.")
        else:
            log(f"ERROR: Server returned HTTP {resp.status_code}")
            log(f"  Body: {resp.text[:500]}")
            sys.exit(1)

    except requests.exceptions.Timeout:
        log("ERROR: Request timed out after 600s")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        log(f"ERROR: Could not connect to web server: {e}")
        log("  Is the web service running? Check WEB_SERVICE_URL env var.")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

    log("=" * 60)


if __name__ == "__main__":
    main()
