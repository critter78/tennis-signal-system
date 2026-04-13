"""
TENNIS POLYMARKET SIGNAL SYSTEM
Script 5: Automated Monthly Retraining

Runs on a schedule (cron or Windows Task Scheduler) to:
  1. Download latest match data from Sackmann GitHub
  2. Retrain the XGBoost model on fresh data
  3. Compare new model AUC vs old — only promote if better
  4. Send a summary notification (email or Telegram)
  5. Generate a fresh betting card

Setup options:
  A) Run manually:      python 05_retrain_scheduler.py
  B) Cron (Mac/Linux):  0 6 1 * * cd /path/to/tennis_signals && python 05_retrain_scheduler.py
  C) Windows:           use Task Scheduler to run monthly (see README)
  D) Keep-alive loop:   python 05_retrain_scheduler.py --daemon (checks daily, retrains monthly)
"""

import json
import os
import pickle
import shutil
import smtplib
import subprocess
import sys
import time
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─── CONFIG ───────────────────────────────────────────────────────────────────

# Notification settings — fill in what you want to use
NOTIFY_EMAIL    = os.getenv("NOTIFY_EMAIL", "")       # your email address
NOTIFY_PASSWORD = os.getenv("NOTIFY_PASSWORD", "")    # gmail app password
NOTIFY_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
NOTIFY_TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# Retraining config
RETRAIN_INTERVAL_DAYS = 30        # retrain every N days
MIN_AUC_IMPROVEMENT   = -0.005    # promote new model even if slightly worse (within 0.5%)
DATA_DIR    = Path("data")
MODELS_DIR  = Path("models")
CARDS_DIR   = Path("cards")
LOG_DIR     = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / "retrain_log.jsonl"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(event: str, data: dict = None):
    entry = {"ts": datetime.now().isoformat(), "event": event, **(data or {})}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {event}", 
          json.dumps(data) if data else "")


def should_retrain() -> bool:
    """Check if enough time has passed since last successful retrain."""
    if not LOG_FILE.exists():
        return True
    # Read last successful retrain event
    entries = [json.loads(l) for l in LOG_FILE.read_text().strip().split("\n") if l]
    retrains = [e for e in entries if e.get("event") == "retrain_complete"]
    if not retrains:
        return True
    last = datetime.fromisoformat(retrains[-1]["ts"])
    days_since = (datetime.now() - last).days
    print(f"  Last retrain: {last.date()} ({days_since} days ago)")
    return days_since >= RETRAIN_INTERVAL_DAYS


# ─── DATA UPDATE ──────────────────────────────────────────────────────────────

def update_data() -> dict:
    """Pull latest Sackmann data for current + previous year."""
    log("data_update_start")
    current_year = datetime.now().year
    years_to_fetch = [current_year - 1, current_year]

    results = {"fetched": [], "errors": []}

    for tour in ["atp", "wta"]:
        url_base = (
            "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
            if tour == "atp" else
            "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"
        )
        for year in years_to_fetch:
            url  = url_base.format(year=year)
            dest = DATA_DIR / "raw" / f"{tour}_{year}.csv"
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                # Only overwrite if file changed (compare sizes)
                if dest.exists() and len(r.content) == dest.stat().st_size:
                    print(f"    {dest.name}: unchanged")
                else:
                    dest.write_bytes(r.content)
                    df = pd.read_csv(dest, low_memory=False)
                    print(f"    {dest.name}: updated ({len(df):,} matches)")
                    results["fetched"].append(f"{tour}_{year}")
            except Exception as e:
                results["errors"].append(f"{tour}_{year}: {e}")
                print(f"    ✗ {tour}_{year}: {e}")

    # Rebuild combined parquet
    if results["fetched"]:
        print("  Rebuilding matches_combined.parquet...")
        frames = []
        for f in sorted((DATA_DIR / "raw").glob("*.csv")):
            try:
                df = pd.read_csv(f, low_memory=False)
                tour = "wta" if "wta" in f.name else "atp"
                df["tour"] = tour
                frames.append(df)
            except Exception:
                pass

        if frames:
            combined = pd.concat(frames, ignore_index=True)
            # Standardise date column
            if "tourney_date" in combined.columns:
                combined = combined.rename(columns={"tourney_date": "date",
                                                    "winner_name": "winner",
                                                    "loser_name": "loser",
                                                    "winner_rank": "w_rank",
                                                    "loser_rank":  "l_rank"})
            combined["date"] = pd.to_datetime(
                combined["date"].astype(str), format="%Y%m%d", errors="coerce"
            )
            combined.to_parquet(DATA_DIR / "raw" / "matches_combined.parquet", index=False)
            print(f"  ✓ {len(combined):,} total matches in dataset")
            results["total_matches"] = len(combined)

    log("data_update_complete", results)
    return results


# ─── RETRAIN ──────────────────────────────────────────────────────────────────

def retrain_model() -> dict:
    """Retrain by calling 02_features_and_train.py as subprocess."""
    log("retrain_start")
    print("\n  Running feature engineering + training...")

    start = time.time()
    result = subprocess.run(
        [sys.executable, "02_features_and_train.py",
         "--load-features" if (DATA_DIR / "features.parquet").exists() else ""],
        capture_output=True, text=True
    )
    elapsed = round(time.time() - start, 1)

    if result.returncode != 0:
        error_msg = result.stderr[-500:] if result.stderr else "unknown error"
        log("retrain_failed", {"error": error_msg, "elapsed_s": elapsed})
        print(f"  ✗ Training failed:\n{error_msg}")
        return {"success": False, "error": error_msg}

    # Parse AUC from stdout
    auc = None
    for line in result.stdout.split("\n"):
        if "Mean AUC" in line:
            try:
                auc = float(line.split(":")[-1].strip())
            except ValueError:
                pass

    print(result.stdout[-800:])  # Show last bit of training output
    result_data = {"success": True, "auc": auc, "elapsed_s": elapsed}
    log("retrain_complete", result_data)
    return result_data


def compare_and_promote(new_auc: float) -> bool:
    """
    Compare new model AUC against previous version.
    Promote (keep) new model if it's not worse by more than threshold.
    """
    meta_path = MODELS_DIR / "latest_model.json"
    if not meta_path.exists():
        return True  # No previous model — always promote

    with open(meta_path) as f:
        meta = json.load(f)
    old_auc = meta.get("mean_auc", 0)

    improvement = new_auc - old_auc
    print(f"\n  Model comparison: {old_auc:.4f} → {new_auc:.4f} ({improvement:+.4f})")

    if improvement >= MIN_AUC_IMPROVEMENT:
        print("  ✓ New model promoted")
        return True
    else:
        print(f"  ✗ New model NOT promoted (degraded by {abs(improvement):.4f})")
        # Archive the rejected model
        model_files = sorted(MODELS_DIR.glob("tennis_xgb_v*.pkl"))
        if model_files:
            rejected = model_files[-1]
            rejected.rename(rejected.with_suffix(".pkl.rejected"))
        return False


# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────

def notify_telegram(message: str):
    """Send a Telegram message via bot. Set up: create a bot with @BotFather, get token + chat_id."""
    if not NOTIFY_TELEGRAM_TOKEN or not NOTIFY_TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{NOTIFY_TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": NOTIFY_TELEGRAM_CHAT,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        print("  ✓ Telegram notification sent")
    except Exception as e:
        print(f"  ✗ Telegram failed: {e}")


def notify_email(subject: str, body: str):
    """Send email via Gmail SMTP. Use an App Password (not your real password)."""
    if not NOTIFY_EMAIL or not NOTIFY_PASSWORD:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = NOTIFY_EMAIL
        msg["To"]      = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(NOTIFY_EMAIL, NOTIFY_PASSWORD)
            server.sendmail(NOTIFY_EMAIL, NOTIFY_EMAIL, msg.as_string())
        print("  ✓ Email notification sent")
    except Exception as e:
        print(f"  ✗ Email failed: {e}")


def send_notification(retrain_result: dict, data_result: dict, promoted: bool):
    status  = "✅ SUCCESS" if retrain_result["success"] and promoted else "⚠️ WARNING"
    auc_str = f"{retrain_result.get('auc', 'N/A'):.4f}" if retrain_result.get("auc") else "N/A"
    new_data = ", ".join(retrain_result.get("fetched", [])) or "none"

    msg = f"""
🎾 *Tennis Signal System — Monthly Retrain*

Status:    {status}
Date:      {datetime.now().strftime('%Y-%m-%d %H:%M')}
New AUC:   {auc_str}
Promoted:  {"Yes" if promoted else "No — kept previous model"}
New data:  {new_data}
Matches:   {data_result.get('total_matches', 'N/A'):,}
Train time: {retrain_result.get('elapsed_s', 'N/A')}s
"""
    notify_telegram(msg)
    notify_email(
        subject=f"Tennis Signals — Retrain {status} ({datetime.now().strftime('%Y-%m-%d')})",
        body=msg
    )


# ─── GENERATE FRESH CARD ──────────────────────────────────────────────────────

def generate_card():
    """Generate a fresh betting card after retraining."""
    print("\n  Generating post-retrain betting card...")
    result = subprocess.run(
        [sys.executable, "04_betting_card.py", "--min-edge", "0.05"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        # Find latest card
        cards = sorted(CARDS_DIR.glob("betting_card_*.html"))
        if cards:
            print(f"  ✓ Card: {cards[-1].name}")
    else:
        print(f"  ✗ Card generation failed: {result.stderr[-200:]}")


# ─── DAEMON MODE ──────────────────────────────────────────────────────────────

def run_daemon(check_interval_hours: int = 24):
    """
    Keep-alive loop: checks daily if retraining is due.
    Run this on a server or leave a terminal window open.
    Kill with Ctrl+C.
    """
    print(f"\n  🔄 Daemon mode — checking every {check_interval_hours}h")
    print(f"  Retraining interval: every {RETRAIN_INTERVAL_DAYS} days")
    print("  Press Ctrl+C to stop\n")

    while True:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Checking retrain schedule...")
        if should_retrain():
            run_retrain()
        else:
            print("  Not due yet. Sleeping...")

        # Sleep until next check
        sleep_secs = check_interval_hours * 3600
        next_check = datetime.now() + timedelta(seconds=sleep_secs)
        print(f"  Next check: {next_check.strftime('%Y-%m-%d %H:%M')}")
        time.sleep(sleep_secs)


# ─── FULL RETRAIN PIPELINE ────────────────────────────────────────────────────

def run_retrain():
    print("\n" + "=" * 60)
    print(f"  MONTHLY RETRAIN — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. Update data
    print("\n[1/4] Updating match data...")
    data_result = update_data()

    # 2. Invalidate cached features so they're recomputed on fresh data
    feat_path = DATA_DIR / "features.parquet"
    if feat_path.exists():
        feat_path.rename(feat_path.with_suffix(".parquet.old"))
        print("  Cached features invalidated — will recompute from fresh data")

    # 3. Retrain
    print("\n[2/4] Retraining model...")
    retrain_result = retrain_model()

    promoted = False
    if retrain_result["success"] and retrain_result.get("auc"):
        print("\n[3/4] Comparing models...")
        promoted = compare_and_promote(retrain_result["auc"])
        if not promoted:
            # Restore old features cache
            old_feat = feat_path.with_suffix(".parquet.old")
            if old_feat.exists():
                old_feat.rename(feat_path)

    # 4. Notify + generate card
    print("\n[4/4] Sending notification...")
    send_notification(retrain_result, data_result, promoted)
    generate_card()

    print("\n" + "=" * 60)
    print("  ✓ RETRAIN PIPELINE COMPLETE")
    print("=" * 60)
    return retrain_result


# ─── MAIN ─────────────────────────────────────────────────────────────────────

import argparse

def main():
    parser = argparse.ArgumentParser(description="Monthly retrain automation")
    parser.add_argument("--force",  action="store_true",
                        help="Force retrain even if not due")
    parser.add_argument("--daemon", action="store_true",
                        help="Run as keep-alive daemon (checks daily)")
    parser.add_argument("--check-interval", type=int, default=24,
                        help="Hours between checks in daemon mode (default 24)")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(check_interval_hours=args.check_interval)
        return

    if args.force or should_retrain():
        run_retrain()
    else:
        print(f"\n  Retrain not due yet (interval: {RETRAIN_INTERVAL_DAYS} days).")
        print("  Use --force to retrain anyway.")


if __name__ == "__main__":
    main()
