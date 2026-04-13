#!/usr/bin/env python3
"""
Daily Signal Generation for Render Cron Job
Runs at 8am ET (12:00 UTC) every day.
1. Pulls fresh tennis markets from Polymarket
2. Loads trained model (if available)
3. Generates betting card + logs picks
4. Dashboard auto-serves latest data via server.py
"""

import subprocess
import sys
import os
import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
os.chdir(str(BASE_DIR))

LOG_FILE = BASE_DIR / "logs" / "cron.log"
LOG_FILE.parent.mkdir(exist_ok=True)


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_step(name, cmd):
    """Run a shell command, log output, return success bool."""
    log(f"Starting: {name}")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n")[-10:]:
                log(f"  {line}")
        if result.returncode != 0:
            log(f"  ERROR (exit {result.returncode}): {result.stderr[-300:]}")
            return False
        log(f"  Completed: {name}")
        return True
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT: {name}")
        return False
    except Exception as e:
        log(f"  EXCEPTION: {e}")
        return False


def main():
    log("=" * 60)
    log("DAILY SIGNAL GENERATION - RENDER CRON")
    log("=" * 60)

    # Step 1: Generate betting card (pulls fresh Polymarket data)
    success = run_step(
        "Generate betting card",
        [sys.executable, "04_betting_card.py", "--min-volume", "500"],
    )

    if not success:
        log("Betting card generation failed. Trying data-only mode...")
        run_step(
            "Generate betting card (data-only fallback)",
            [sys.executable, "04_betting_card.py", "--min-volume", "300"],
        )

    # Step 2: LSTM learner — auto-retrain if enough resolved picks
    run_step(
        "LSTM learner check/train",
        [sys.executable, "06_lstm_learner.py", "train"],
    )

    # Step 3: Generate dashboard with latest picks
    run_step(
        "Generate dashboard",
        [sys.executable, "07_dashboard.py"],
    )

    # Step 4: Log summary
    picks_file = BASE_DIR / "logs" / "picks.jsonl"
    if picks_file.exists():
        with open(picks_file) as f:
            total = sum(1 for line in f if line.strip())
        log(f"Total picks in log: {total}")

    cards = sorted((BASE_DIR / "cards").glob("betting_card_*.html"))
    if cards:
        log(f"Latest card: {cards[-1].name}")

    log("Daily run complete.")
    log("=" * 60)


if __name__ == "__main__":
    main()
