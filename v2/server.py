#!/usr/bin/env python3
"""
V2 Flask Web Server — tennisv2.critterlabs.io

Serves:
  /                       — v2 dashboard
  /api/picks/latest       — latest v2 pick run, ranked by model_prob
  /api/picks/history      — tail of v2_picks.jsonl
  /api/alerts             — recent sell alerts
  /api/pnl                — cumulative P&L on resolved v2 picks
  /api/status             — health check (Render)
  /api/refresh            — triggered by cron; regenerates picks (auth: X-Cron-Secret)

This runs parallel to and isolated from the v1 service at tennis.critterlabs.io.
"""

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, render_template_string

BASE_DIR = Path(__file__).parent.resolve()
DASH_HTML = BASE_DIR / "dashboard.html"

# Persistent disk on Render
PERSIST = Path("/data")
if PERSIST.exists() and PERSIST.is_dir():
    LOGS = PERSIST / "v2_logs"
    DATA = PERSIST / "v2_data"
    _PERSIST = True
else:
    LOGS = BASE_DIR / "logs"
    DATA = BASE_DIR / "data"
    _PERSIST = False
LOGS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

PICKS_FILE      = LOGS / "v2_picks.jsonl"
PICKS_LATEST    = LOGS / "v2_picks_latest.json"
ALERTS_FILE     = LOGS / "sell_alerts.jsonl"
POSITIONS_FILE  = LOGS / "v2_open_positions.json"
RESOLVED_FILE   = LOGS / "v2_resolved.jsonl"

CRON_SECRET = os.environ.get("CRON_SECRET", "tennis-v2-cron-2026")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))


def _seed_persistent():
    if not _PERSIST:
        return
    import shutil
    seed = {
        BASE_DIR / "logs"   / "v2_picks.jsonl":      PICKS_FILE,
        BASE_DIR / "logs"   / "v2_picks_latest.json": PICKS_LATEST,
        BASE_DIR / "data"   / "matches_combined_v2.parquet": DATA / "matches_combined_v2.parquet",
        BASE_DIR / "models" / "tennis_xgb_v2.pkl":   BASE_DIR / "models" / "tennis_xgb_v2.pkl",  # stays in repo
    }
    for src, dst in seed.items():
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"[seed] {dst}", file=sys.stderr)
_seed_persistent()


def require_cron(f):
    @wraps(f)
    def wrapper(*a, **kw):
        sent = request.headers.get("X-Cron-Secret", "")
        if not secrets.compare_digest(sent, CRON_SECRET):
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **kw)
    return wrapper


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    picks_age = None
    if PICKS_LATEST.exists():
        mtime = datetime.fromtimestamp(PICKS_LATEST.stat().st_mtime, tz=timezone.utc)
        picks_age = int((datetime.now(timezone.utc) - mtime).total_seconds() / 60)
    return jsonify({
        "status": "ok",
        "service": "tennis-v2-signal",
        "persistent_disk": _PERSIST,
        "picks_latest_age_min": picks_age,
        "picks_file_exists": PICKS_FILE.exists(),
        "alerts_file_exists": ALERTS_FILE.exists(),
    })


@app.get("/api/picks/latest")
def picks_latest():
    if not PICKS_LATEST.exists():
        return jsonify({"picks": [], "generated_at": None})
    return jsonify(json.loads(PICKS_LATEST.read_text()))


@app.get("/api/picks/history")
def picks_history():
    if not PICKS_FILE.exists():
        return jsonify([])
    limit = int(request.args.get("limit", 200))
    lines = PICKS_FILE.read_text().splitlines()
    out = [json.loads(l) for l in lines[-limit:]]
    return jsonify(out)


@app.get("/api/alerts")
def alerts():
    if not ALERTS_FILE.exists():
        return jsonify([])
    limit = int(request.args.get("limit", 100))
    lines = ALERTS_FILE.read_text().splitlines()
    out = [json.loads(l) for l in lines[-limit:]]
    return jsonify(out)


@app.get("/api/pnl")
def pnl():
    """Cumulative P&L on resolved v2 picks (flat $1 sizing for baseline)."""
    if not RESOLVED_FILE.exists():
        return jsonify({"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "wr": None})
    wins = losses = 0
    pnl_total = 0.0
    by_prob_bucket = {}
    for line in RESOLVED_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        res = r.get("result")   # "win" or "loss"
        poly = r.get("entry_price") or r.get("poly_price")
        if res not in ("win", "loss") or poly is None:
            continue
        # P&L per $1 at binary payout: win = (1 − poly)/poly, loss = -1
        if res == "win":
            wins += 1
            pnl_total += (1 - poly) / poly
        else:
            losses += 1
            pnl_total -= 1.0
        # Bucket by model_prob
        b = round(float(r.get("model_prob", 0)) * 10) / 10
        d = by_prob_bucket.setdefault(b, {"w": 0, "l": 0, "pnl": 0.0})
        d["w" if res == "win" else "l"] += 1
        d["pnl"] += ((1 - poly) / poly) if res == "win" else -1.0
    n = wins + losses
    return jsonify({
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / n, 4) if n else None,
        "pnl_per_$1": round(pnl_total, 4),
        "by_prob_bucket": by_prob_bucket,
    })


@app.get("/api/positions")
def positions():
    if not POSITIONS_FILE.exists():
        return jsonify([])
    try:
        return jsonify(json.loads(POSITIONS_FILE.read_text()))
    except Exception:
        return jsonify([])


@app.post("/api/refresh")
@require_cron
def refresh():
    """Called by the v2 cron job — regenerates picks."""
    t0 = datetime.now(timezone.utc)
    steps = {}

    def run(cmd, name):
        try:
            p = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True,
                               text=True, timeout=600)
            steps[name] = {
                "status": "ok" if p.returncode == 0 else "fail",
                "returncode": p.returncode,
                "tail": (p.stdout[-600:] + "\n" + p.stderr[-600:]).strip(),
            }
        except Exception as e:
            steps[name] = {"status": "fail", "error": str(e)}

    # 1) refresh TML data (incremental)
    run([sys.executable, str(BASE_DIR / "fetch_tml.py"), "--rebuild"], "fetch_tml")
    # 2) regenerate v2 picks
    run([sys.executable, str(BASE_DIR / "generate_v2_picks.py")], "generate_picks")
    # 3) one pass of sell monitor
    run([sys.executable, str(BASE_DIR / "auto_sell_monitor.py"), "--once"], "sell_monitor")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    ok = sum(1 for s in steps.values() if s.get("status") == "ok")
    return jsonify({
        "status": "ok" if ok == len(steps) else "partial",
        "elapsed_seconds": round(elapsed, 1),
        "succeeded": ok,
        "total": len(steps),
        "steps": steps,
    })


@app.get("/")
def dashboard():
    if DASH_HTML.exists():
        return render_template_string(DASH_HTML.read_text())
    return "v2 dashboard template missing", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
