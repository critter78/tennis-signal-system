#!/usr/bin/env python3
"""
Flask web server for Tennis Betting Signal System
Serves dashboard, betting cards, API endpoints, health checks,
and time-limited share links.
Works with gunicorn for production deployment on Render.
"""

import os
import json
import glob
import secrets
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_file, render_template_string, redirect, make_response
import subprocess
import logging

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Base paths
BASE_DIR = Path(__file__).parent.resolve()
CARDS_DIR = BASE_DIR / "cards"
LOGS_DIR = BASE_DIR / "logs"
MODELS_DIR = BASE_DIR / "models"
PICKS_FILE = LOGS_DIR / "picks.jsonl"
BETS_FILE = LOGS_DIR / "my_bets.json"
SHARES_FILE = LOGS_DIR / "shares.json"
DASHBOARD_TEMPLATE = BASE_DIR / "dashboard.html"

# Admin password — set via environment variable on Render
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "tennis2026")

# Cron secret — the cron job sends this to authenticate refresh requests
CRON_SECRET = os.environ.get("CRON_SECRET", "tennis-cron-2026")

# Polymarket wallet address — set via environment variable on Render
POLY_WALLET = os.environ.get("POLY_WALLET", "0x0D2ad18A44ac2D4A001aEdd0EF9a7B016DAA031d")
POLY_DATA_API = "https://data-api.polymarket.com"

# Create necessary directories
CARDS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)


# ─── SHARE LINK MANAGEMENT ───────────────────────────────────────────────────

def load_shares():
    """Load active share tokens."""
    try:
        if SHARES_FILE.exists():
            with open(SHARES_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading shares: {e}")
    return {}


def save_shares(shares):
    """Save share tokens to disk."""
    try:
        with open(SHARES_FILE, 'w') as f:
            json.dump(shares, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving shares: {e}")


def create_share_token(duration_minutes, label=""):
    """Create a new time-limited share token."""
    token = secrets.token_urlsafe(16)
    shares = load_shares()
    now = datetime.utcnow()
    expires = now + timedelta(minutes=duration_minutes)

    shares[token] = {
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "duration_minutes": duration_minutes,
        "label": label,
        "views": 0,
    }
    save_shares(shares)
    return token


def validate_share_token(token):
    """Check if a share token is valid and not expired."""
    shares = load_shares()
    if token not in shares:
        return False

    expires = datetime.fromisoformat(shares[token]["expires_at"])
    if datetime.utcnow() > expires:
        return False

    # Increment view count
    shares[token]["views"] += 1
    save_shares(shares)
    return True


def cleanup_expired_shares():
    """Remove expired share tokens."""
    shares = load_shares()
    now = datetime.utcnow()
    active = {k: v for k, v in shares.items()
              if datetime.fromisoformat(v["expires_at"]) > now}
    if len(active) != len(shares):
        save_shares(active)
    return active


# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────

def check_admin_cookie():
    """Check if the request has a valid admin session cookie."""
    cookie = request.cookies.get("tennis_admin")
    if not cookie:
        return False
    expected = hashlib.sha256(f"{ADMIN_PASSWORD}:{app.secret_key}".encode()).hexdigest()
    return cookie == expected


def set_admin_cookie(response):
    """Set the admin session cookie."""
    token = hashlib.sha256(f"{ADMIN_PASSWORD}:{app.secret_key}".encode()).hexdigest()
    response.set_cookie("tennis_admin", token, max_age=3600, httponly=True, samesite="Lax")
    return response


# ─── HELPER FUNCTIONS ─────────────────────────────────────────────────────────

def enable_cors(f):
    """Decorator to enable CORS headers on a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = f(*args, **kwargs)
        # Handle tuple responses like (jsonify(...), 401)
        if isinstance(response, tuple):
            response = make_response(*response)
        elif isinstance(response, dict):
            response = make_response(jsonify(response))
        elif isinstance(response, str):
            response = make_response(response)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response
    return decorated_function


def get_latest_betting_card():
    """Find the most recent betting card HTML file."""
    card_files = sorted(glob.glob(str(CARDS_DIR / "betting_card_*.html")))
    if card_files:
        return card_files[-1]
    return None


def load_picks_jsonl():
    """Load all picks from picks.jsonl as a list of dicts."""
    picks = []
    try:
        if PICKS_FILE.exists():
            with open(PICKS_FILE, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            picks.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    except Exception as e:
        logger.error(f"Error loading picks: {e}")
    return picks


def load_bets():
    """Load user bets from my_bets.json."""
    try:
        if BETS_FILE.exists():
            with open(BETS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading bets: {e}")
    return {}


def save_bets(bets_data):
    """Save user bets to my_bets.json."""
    try:
        BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BETS_FILE, 'w') as f:
            json.dump(bets_data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving bets: {e}")
        return False


def get_system_status():
    """Get current system health status."""
    try:
        last_card = get_latest_betting_card()
        last_card_time = None
        if last_card:
            mtime = os.path.getmtime(last_card)
            last_card_time = datetime.fromtimestamp(mtime).isoformat()

        picks_count = len(load_picks_jsonl())
        model_exists = (MODELS_DIR / "latest_model.json").exists()

        return {
            "status": "healthy",
            "last_card_generated": last_card_time,
            "picks_count": picks_count,
            "model_available": model_exists,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


def serve_dashboard(is_shared=False, share_expires=None):
    """Serve the dashboard HTML with picks data injected."""
    try:
        logger.info(f"DASHBOARD_TEMPLATE path: {DASHBOARD_TEMPLATE}")
        logger.info(f"DASHBOARD_TEMPLATE exists: {DASHBOARD_TEMPLATE.exists()}")
        if DASHBOARD_TEMPLATE.exists():
            picks_data = load_picks_jsonl()
            logger.info(f"Serving from template: {DASHBOARD_TEMPLATE} ({DASHBOARD_TEMPLATE.stat().st_size} bytes)")
            with open(DASHBOARD_TEMPLATE, 'r') as f:
                content = f.read()
            logger.info(f"Template has myOutcome: {'myOutcome' in content}")

            picks_json = json.dumps(picks_data, indent=2)
            # Handle both with and without semicolon
            if "window.PICKS_DATA = [];" in content:
                content = content.replace("window.PICKS_DATA = [];", f"window.PICKS_DATA = {picks_json};")
            else:
                content = content.replace("window.PICKS_DATA = []", f"window.PICKS_DATA = {picks_json}")

            # For shared views: only show Today's Signals, hide everything else
            if is_shared and share_expires:
                # CSS-based hiding — works immediately, no DOM timing issues
                share_css = """
                <style id="shared-view-styles">
                    /* Hide all tabs except Today's Signals */
                    .tab:not(.active) { display: none !important; }
                    .tabs { pointer-events: none; }
                    .tab.active { pointer-events: auto; cursor: default; }
                    /* Hide non-signal panels */
                    #panel-overview, #panel-mybets, #panel-performance, #panel-accuracy { display: none !important; }
                    /* Hide admin/logout links */
                    a[href*="admin"], a[href*="logout"] { display: none !important; }
                    /* Hide Place Bet buttons */
                    .sig-bet-btn { display: none !important; }
                    /* Hide LSTM sections */
                    #lstmProgress, #lstmInsights { display: none !important; }
                    /* Hide header subtitle (picks logged, bets placed, resolved counts) */
                    #headerSub { display: none !important; }
                    /* Hide personal stat cards (Bets Placed, Win Rate, Total P&L) via nth-child */
                    .stat-card:nth-child(2), .stat-card:nth-child(4), .stat-card:nth-child(5) { display: none !important; }
                </style>
                """

                expiry_banner = f"""
                {share_css}
                <div id="shareBanner" style="position:fixed;top:0;left:0;right:0;z-index:9999;background:#e65100;color:white;text-align:center;padding:10px;font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600">
                    SHARED VIEW — Expires: <span id="shareExpiry">{share_expires}</span>
                    <span id="shareCountdown" style="margin-left:12px"></span>
                </div>
                <script>
                (function() {{
                    const exp = new Date("{share_expires}Z");
                    function update() {{
                        const diff = exp - new Date();
                        if (diff <= 0) {{
                            document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0f1117;color:#f44336;font-family:monospace;font-size:20px">This share link has expired.</div>';
                            return;
                        }}
                        const m = Math.floor(diff/60000);
                        const s = Math.floor((diff%60000)/1000);
                        document.getElementById('shareCountdown').textContent = m + 'm ' + s + 's remaining';
                    }}
                    update();
                    setInterval(update, 1000);

                    // Also handle via JS after DOM loads as extra safety
                    document.addEventListener('DOMContentLoaded', function() {{
                        // Disable tab switching to hidden panels
                        window._origSwitchTab = window.switchTab;
                        window.switchTab = function(name) {{
                            if (name !== 'signals') return;
                            if (window._origSwitchTab) window._origSwitchTab(name);
                        }};
                    }});
                }})();
                </script>
                """
                # Inject banner after <body>
                content = content.replace("<body>", f"<body>\n{expiry_banner}\n<div style='margin-top:40px'>", 1)
                content = content.replace("</body>", "</div>\n</body>", 1)

                # For shared view: only include today's unresolved signals, strip sensitive fields
                from datetime import date
                today_str = date.today().isoformat()
                shared_picks = []
                for p in picks_data:
                    logged = p.get("logged_at", "")
                    if logged.startswith(today_str):
                        clean = {k: v for k, v in p.items()
                                 if k not in ("outcome", "pnl", "actual_winner", "resolved_at", "myOutcome", "myStake", "myOdds")}
                        shared_picks.append(clean)
                shared_json = json.dumps(shared_picks, indent=2)
                content = content.replace(f"window.PICKS_DATA = {json.dumps(picks_data, indent=2)};", f"window.PICKS_DATA = {shared_json};")
                content = content.replace(f"window.PICKS_DATA = {json.dumps(picks_data, indent=2)}", f"window.PICKS_DATA = {shared_json}")

            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
    except Exception as e:
        logger.warning(f"Error generating dashboard: {e}. Falling back to latest card.", exc_info=True)

    logger.warning("FALLBACK: Serving from cards/ directory instead of template!")
    latest_card = get_latest_betting_card()
    if latest_card:
        logger.warning(f"FALLBACK: Serving {latest_card}")
        try:
            with open(latest_card, 'r') as f:
                content = f.read()
            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
        except Exception as e:
            logger.error(f"Error serving latest card: {e}")

    return jsonify({"error": "Dashboard not available"}), 503


# ─── LOGIN PAGE ───────────────────────────────────────────────────────────────

LOGIN_PAGE = """
<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tennis Signal System — Login</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='30' fill='%23c8e645' stroke='%23fff' stroke-width='2'/%3E%3Cpath d='M8 32c0-6 8-12 24-12s24 6 24 12' fill='none' stroke='%23fff' stroke-width='2.5'/%3E%3Cpath d='M8 32c0 6 8 12 24 12s24-6 24-12' fill='none' stroke='%23fff' stroke-width='2.5'/%3E%3C/svg%3E">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'IBM Plex Mono', monospace; }
body { background: #0f1117; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.login-box { background: #1a1d27; border: 1px solid #2d3139; padding: 40px; max-width: 400px; width: 90%; text-align: center; }
h1 { font-size: 16px; color: #d4740a; margin-bottom: 8px; letter-spacing: 0.06em; }
.sub { font-size: 11px; color: #6c757d; margin-bottom: 24px; }
input { width: 100%; background: #0f1117; border: 1px solid #2d3139; color: #e0e0e0; padding: 12px 16px; font-size: 14px; font-family: 'IBM Plex Mono', monospace; margin-bottom: 16px; }
input:focus { outline: none; border-color: #d4740a; }
button { width: 100%; background: #d4740a; color: white; border: none; padding: 12px; font-size: 13px; font-weight: 700; font-family: 'IBM Plex Mono', monospace; cursor: pointer; letter-spacing: 0.04em; }
button:hover { background: #e65100; }
.error { color: #f44336; font-size: 11px; margin-bottom: 12px; }
</style>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
</head><body>
<div class="login-box">
    <h1>TENNIS BETTING SIGNAL SYSTEM</h1>
    <div class="sub">Enter password to access the dashboard</div>
    {{ERROR}}
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Password" autofocus>
        <button type="submit">ACCESS DASHBOARD</button>
    </form>
</div>
</body></html>
"""

# ─── EXPIRED PAGE ─────────────────────────────────────────────────────────────

EXPIRED_PAGE = """
<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Link Expired</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'IBM Plex Mono', monospace; }
body { background: #0f1117; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.box { text-align: center; padding: 40px; }
h1 { font-size: 48px; color: #f44336; margin-bottom: 16px; }
p { font-size: 14px; color: #6c757d; }
</style>
</head><body>
<div class="box">
    <h1>EXPIRED</h1>
    <p>This share link has expired and is no longer accessible.</p>
</div>
</body></html>
"""

# ─── ADMIN PAGE ───────────────────────────────────────────────────────────────

ADMIN_PAGE = """
<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tennis Signal System — Share Manager</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='30' fill='%23c8e645' stroke='%23fff' stroke-width='2'/%3E%3Cpath d='M8 32c0-6 8-12 24-12s24 6 24 12' fill='none' stroke='%23fff' stroke-width='2.5'/%3E%3Cpath d='M8 32c0 6 8 12 24 12s24-6 24-12' fill='none' stroke='%23fff' stroke-width='2.5'/%3E%3C/svg%3E">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'IBM Plex Mono', monospace; }
body { background: #0f1117; color: #e0e0e0; min-height: 100vh; padding: 30px; }
h1 { font-size: 16px; color: #d4740a; margin-bottom: 6px; letter-spacing: 0.06em; }
.sub { font-size: 11px; color: #6c757d; margin-bottom: 24px; }
.section { background: #1a1d27; border: 1px solid #2d3139; padding: 24px; margin-bottom: 20px; }
.section h2 { font-size: 13px; color: #d4740a; margin-bottom: 16px; letter-spacing: 0.04em; }
.duration-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-bottom: 16px; }
@media (max-width: 600px) {
    body { padding: 15px; }
    .section { padding: 16px; }
    .duration-grid { grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .dur-btn { padding: 12px 6px; font-size: 12px; }
    .result .url { font-size: 11px; }
    table { font-size: 10px; }
    th, td { padding: 6px; }
    h1 { font-size: 14px; }
}
.dur-btn { background: #0f1117; border: 1px solid #2d3139; color: #e0e0e0; padding: 14px; text-align: center; cursor: pointer; font-family: 'IBM Plex Mono', monospace; font-size: 14px; font-weight: 700; transition: all 0.2s; }
.dur-btn:hover { border-color: #d4740a; color: #d4740a; }
.dur-btn.selected { border-color: #d4740a; background: #1e1200; color: #d4740a; }
.label-row { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
.label-row input { flex: 1; background: #0f1117; border: 1px solid #2d3139; color: #e0e0e0; padding: 10px; font-family: 'IBM Plex Mono', monospace; font-size: 12px; }
.label-row input:focus { outline: none; border-color: #d4740a; }
.gen-btn { background: #d4740a; color: white; border: none; padding: 12px 24px; font-size: 13px; font-weight: 700; font-family: 'IBM Plex Mono', monospace; cursor: pointer; letter-spacing: 0.04em; width: 100%; }
.gen-btn:hover { background: #e65100; }
.gen-btn:disabled { background: #2d3139; cursor: not-allowed; }
.result { display: none; background: #0f1117; border: 1px solid #1b5e20; padding: 16px; margin-top: 16px; }
.result .url { font-size: 13px; color: #4caf50; word-break: break-all; margin-bottom: 10px; }
.result .info { font-size: 10px; color: #6c757d; }
.copy-btn { background: #1b5e20; color: white; border: none; padding: 8px 16px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 700; cursor: pointer; margin-top: 8px; }
.copy-btn:hover { background: #2e7d32; }
table { width: 100%; border-collapse: collapse; font-size: 11px; }
th { text-align: left; padding: 8px; font-size: 9px; color: #6c757d; text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid #2d3139; }
td { padding: 8px; border-bottom: 1px solid #1e2130; }
.active { color: #4caf50; }
.expired { color: #f44336; }
.revoke-btn { background: #f44336; color: white; border: none; padding: 4px 10px; font-family: 'IBM Plex Mono', monospace; font-size: 10px; cursor: pointer; }
.back-link { color: #d4740a; text-decoration: none; font-size: 12px; }
.back-link:hover { text-decoration: underline; }
</style>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
</head><body>

<h1>ADMIN PANEL</h1>
<div class="sub"><a href="/" class="back-link">&larr; Back to Dashboard</a></div>

<div class="section">
    <h2>SYSTEM ACTIONS</h2>
    <div style="display:flex; gap:10px; flex-wrap:wrap">
        <button class="gen-btn" style="width:auto; padding:12px 24px" onclick="resolveNow()">RESOLVE OUTCOMES NOW</button>
        <button class="gen-btn" style="width:auto; padding:12px 24px; background:#1b5e20" onclick="generateNow()">GENERATE FRESH CARD</button>
        <button class="gen-btn" style="width:auto; padding:12px 24px; background:#1565c0" onclick="dedupNow()">CLEAN DUPLICATES</button>
        <button class="gen-btn" style="width:auto; padding:12px 24px; background:#6a1b9a" onclick="fullRefresh()">RUN FULL PIPELINE</button>
    </div>
    <div id="actionResult" style="display:none; margin-top:12px; background:#0f1117; border:1px solid #2d3139; padding:12px; font-size:11px; white-space:pre-wrap; max-height:200px; overflow-y:auto"></div>
</div>

<div class="section">
    <h2>GENERATE TIME-LIMITED SHARE LINK</h2>
    <div class="duration-grid" id="durGrid">
        <div class="dur-btn" data-min="15" onclick="selectDur(this)">15 min</div>
        <div class="dur-btn" data-min="30" onclick="selectDur(this)">30 min</div>
        <div class="dur-btn" data-min="45" onclick="selectDur(this)">45 min</div>
        <div class="dur-btn" data-min="60" onclick="selectDur(this)">1 hr</div>
        <div class="dur-btn" data-min="90" onclick="selectDur(this)">90 min</div>
        <div class="dur-btn" data-min="120" onclick="selectDur(this)">2 hr</div>
        <div class="dur-btn" data-min="240" onclick="selectDur(this)">4 hr</div>
        <div class="dur-btn" data-min="360" onclick="selectDur(this)">6 hr</div>
        <div class="dur-btn" data-min="720" onclick="selectDur(this)">12 hr</div>
    </div>
    <div class="label-row">
        <input type="text" id="shareLabel" placeholder="Label (optional) — e.g. 'For Mike'">
    </div>
    <button class="gen-btn" id="genBtn" onclick="generateLink()" disabled>SELECT A DURATION ABOVE</button>
    <div class="result" id="result">
        <div class="url" id="shareUrl"></div>
        <div class="info" id="shareInfo"></div>
        <button class="copy-btn" onclick="copyLink()">COPY LINK</button>
    </div>
</div>

<div class="section">
    <h2>ACTIVE SHARE LINKS</h2>
    <div id="sharesTable"></div>
</div>

<script>
let selectedMin = 0;

function selectDur(el) {
    document.querySelectorAll('.dur-btn').forEach(b => b.classList.remove('selected'));
    el.classList.add('selected');
    selectedMin = parseInt(el.dataset.min);
    const btn = document.getElementById('genBtn');
    btn.disabled = false;
    btn.textContent = 'GENERATE ' + selectedMin + ' MIN SHARE LINK';
}

async function generateLink() {
    const label = document.getElementById('shareLabel').value;
    const res = await fetch('/admin/create-share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: selectedMin, label })
    });
    const data = await res.json();
    if (data.url) {
        document.getElementById('result').style.display = 'block';
        document.getElementById('shareUrl').textContent = data.url;
        document.getElementById('shareInfo').textContent =
            'Expires at ' + new Date(data.expires_at + 'Z').toLocaleString() + ' (' + selectedMin + ' minutes)';
        loadShares();
    }
}

function copyLink() {
    const url = document.getElementById('shareUrl').textContent;
    navigator.clipboard.writeText(url).then(() => {
        const btn = event.target;
        btn.textContent = 'COPIED!';
        setTimeout(() => btn.textContent = 'COPY LINK', 2000);
    });
}

function copyShareLink(token) {
    const url = window.location.origin + '/s/' + token;
    navigator.clipboard.writeText(url).then(() => {
        const btn = event.target;
        const orig = btn.textContent;
        btn.textContent = 'COPIED!';
        setTimeout(() => btn.textContent = orig, 2000);
    });
}

async function revokeShare(token) {
    await fetch('/admin/revoke-share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token })
    });
    loadShares();
}

async function loadShares() {
    const res = await fetch('/admin/shares');
    const data = await res.json();
    const wrap = document.getElementById('sharesTable');

    if (!data.shares || Object.keys(data.shares).length === 0) {
        wrap.innerHTML = '<div style="color:#6c757d; font-size:12px; padding:10px">No share links created yet.</div>';
        return;
    }

    let html = '<table><thead><tr><th>Token</th><th>Label</th><th>Duration</th><th>Expires</th><th>Views</th><th>Status</th><th></th></tr></thead><tbody>';
    const now = new Date();

    for (const [token, info] of Object.entries(data.shares)) {
        const exp = new Date(info.expires_at + 'Z');
        const isActive = exp > now;
        const status = isActive ? '<span class="active">ACTIVE</span>' : '<span class="expired">EXPIRED</span>';
        const remaining = isActive ? Math.ceil((exp - now) / 60000) + 'm left' : 'expired';
        html += '<tr>' +
            '<td style="font-size:10px;color:#6c757d">' + token.slice(0, 8) + '...</td>' +
            '<td>' + (info.label || '—') + '</td>' +
            '<td>' + info.duration_minutes + ' min</td>' +
            '<td style="font-size:10px">' + remaining + '</td>' +
            '<td>' + info.views + '</td>' +
            '<td>' + status + '</td>' +
            '<td style="display:flex;gap:6px">' + (isActive ? '<button class="copy-btn" style="margin:0;font-size:9px;padding:4px 8px" onclick="copyShareLink(\\''+token+'\\')">COPY LINK</button><button class="revoke-btn" onclick="revokeShare(\\''+token+'\\')">REVOKE</button>' : '') + '</td>' +
            '</tr>';
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

async function resolveNow() {
    const box = document.getElementById('actionResult');
    box.style.display = 'block';
    box.style.borderColor = '#d4740a';
    box.textContent = 'Running outcome resolver...';
    try {
        const res = await fetch('/resolve', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            box.style.borderColor = '#1b5e20';
            box.textContent = 'Done!\\n\\n' + (data.output || 'No output');
        } else {
            box.style.borderColor = '#f44336';
            box.textContent = 'Error: ' + (data.error || data.message || 'Unknown') + '\\n\\n' + (data.output || '');
        }
    } catch(e) {
        box.style.borderColor = '#f44336';
        box.textContent = 'Request failed: ' + e.message;
    }
}

async function generateNow() {
    const box = document.getElementById('actionResult');
    box.style.display = 'block';
    box.style.borderColor = '#d4740a';
    box.textContent = 'Generating fresh betting card... (this may take 1-2 minutes)';
    try {
        const res = await fetch('/generate', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            box.style.borderColor = '#1b5e20';
            box.textContent = 'Done! Fresh card generated. Reload the dashboard to see updates.';
        } else {
            box.style.borderColor = '#f44336';
            box.textContent = 'Error: ' + (data.error || data.message || 'Unknown');
        }
    } catch(e) {
        box.style.borderColor = '#f44336';
        box.textContent = 'Request failed: ' + e.message;
    }
}

async function dedupNow() {
    const box = document.getElementById('actionResult');
    box.style.display = 'block';
    box.style.borderColor = '#1565c0';
    box.textContent = 'Cleaning duplicate picks...';
    try {
        const res = await fetch('/admin/dedup', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            box.style.borderColor = '#1b5e20';
            box.textContent = 'Done!\\n\\n' + (data.output || 'Dedup complete');
        } else {
            box.style.borderColor = '#f44336';
            box.textContent = 'Error: ' + (data.error || data.output || 'Unknown');
        }
    } catch(e) {
        box.style.borderColor = '#f44336';
        box.textContent = 'Request failed: ' + e.message;
    }
}

async function fullRefresh() {
    const box = document.getElementById('actionResult');
    box.style.display = 'block';
    box.style.borderColor = '#6a1b9a';
    box.textContent = 'Running full pipeline (rankings > card > dedup > dashboard)... This may take 2-3 minutes.';
    try {
        const res = await fetch('/admin/full-refresh', { method: 'POST' });
        const data = await res.json();
        box.style.borderColor = data.status === 'ok' ? '#1b5e20' : '#d4740a';
        let output = 'Pipeline ' + data.status + ' (' + (data.elapsed_seconds || '?') + 's)\\n\\n';
        if (data.steps) {
            for (const [step, info] of Object.entries(data.steps)) {
                output += step.toUpperCase() + ': ' + (info.status || '?') + '\\n';
                if (info.output) output += info.output.trim() + '\\n';
                output += '\\n';
            }
        }
        box.textContent = output;
    } catch(e) {
        box.style.borderColor = '#f44336';
        box.textContent = 'Request failed: ' + e.message;
    }
}

loadShares();
setInterval(loadShares, 30000);
</script>
</body></html>
"""


# ─── POLYMARKET RESOLUTION HELPERS ────────────────────────────────────────────

def _parse_resolved_market(m):
    """Parse a resolved market to extract the winner."""
    market_id = m.get("id") or m.get("condition_id") or m.get("conditionId", "")
    question = m.get("question", "")
    slug = m.get("slug", "")

    if not question:
        return None

    outcomes_raw = m.get("outcomes", "")
    prices_raw = m.get("outcomePrices", "")

    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
    except (json.JSONDecodeError, TypeError):
        prices = []

    winner = None
    if outcomes and prices and len(outcomes) == len(prices):
        for name, price in zip(outcomes, prices):
            try:
                if float(price) >= 0.95:
                    winner = str(name)
                    break
            except (ValueError, TypeError):
                continue

    if not winner:
        resolved_at = m.get("resolvedAt") or m.get("resolved_at")
        if resolved_at:
            winner = m.get("resolution") or m.get("resolvedOutcome")

    if not winner:
        return None

    return {
        "market_id": market_id,
        "question": question,
        "slug": slug,
        "winner": winner,
        "resolved_at": m.get("resolvedAt") or m.get("resolved_at") or datetime.utcnow().isoformat(),
    }


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    """Serve dashboard — requires admin login."""
    if not check_admin_cookie():
        return make_response(LOGIN_PAGE.replace("{{ERROR}}", "")), 200

    return serve_dashboard()


@app.route("/login", methods=["POST"])
def login():
    """Handle admin login."""
    password = request.form.get("password", "")
    if password == ADMIN_PASSWORD:
        response = make_response(redirect("/"))
        set_admin_cookie(response)
        return response
    return make_response(LOGIN_PAGE.replace("{{ERROR}}", '<div class="error">Incorrect password</div>')), 401


@app.route("/logout", methods=["GET"])
def logout():
    """Clear admin session."""
    response = make_response(redirect("/"))
    response.delete_cookie("tennis_admin")
    return response


# ─── SHARE ROUTES ─────────────────────────────────────────────────────────────

@app.route("/s/<token>", methods=["GET"])
def shared_view(token):
    """Serve dashboard via time-limited share link."""
    shares = load_shares()

    if token not in shares:
        return make_response(EXPIRED_PAGE), 404

    expires = datetime.fromisoformat(shares[token]["expires_at"])
    if datetime.utcnow() > expires:
        return make_response(EXPIRED_PAGE), 403

    # Increment views
    shares[token]["views"] += 1
    save_shares(shares)

    return serve_dashboard(is_shared=True, share_expires=shares[token]["expires_at"])


# ─── ADMIN ROUTES ────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET"])
def admin_page():
    """Share management admin page."""
    if not check_admin_cookie():
        return make_response(redirect("/")), 302
    return make_response(ADMIN_PAGE), 200


@app.route("/admin/create-share", methods=["POST"])
def admin_create_share():
    """Create a new time-limited share link."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    duration = data.get("duration", 30)
    label = data.get("label", "")

    # Validate duration
    allowed = [15, 30, 45, 60, 90, 120, 240, 360, 720]
    if duration not in allowed:
        return jsonify({"error": f"Duration must be one of: {allowed}"}), 400

    token = create_share_token(duration, label)
    shares = load_shares()

    # Build full URL
    host = request.host_url.rstrip("/")
    url = f"{host}/s/{token}"

    return jsonify({
        "token": token,
        "url": url,
        "expires_at": shares[token]["expires_at"],
        "duration_minutes": duration,
    })


@app.route("/admin/shares", methods=["GET"])
def admin_list_shares():
    """List all share tokens."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    shares = load_shares()
    return jsonify({"shares": shares})


@app.route("/admin/revoke-share", methods=["POST"])
def admin_revoke_share():
    """Revoke (delete) a share token."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    token = data.get("token")

    shares = load_shares()
    if token in shares:
        del shares[token]
        save_shares(shares)
        return jsonify({"status": "revoked"})

    return jsonify({"error": "Token not found"}), 404


# ─── EXISTING API ROUTES ─────────────────────────────────────────────────────

@app.route("/card", methods=["GET"])
@enable_cors
def latest_card():
    """Serve the latest betting card."""
    latest = get_latest_betting_card()
    if latest:
        try:
            with open(latest, 'r') as f:
                content = f.read()
            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
        except Exception as e:
            logger.error(f"Error serving card: {e}")
            return jsonify({"error": "Could not read card"}), 500

    return jsonify({"error": "No betting card available"}), 404


@app.route("/api/picks", methods=["GET"])
@enable_cors
def api_picks():
    """Return all picks as JSON array."""
    try:
        picks = load_picks_jsonl()
        return jsonify({"picks": picks, "count": len(picks)})
    except Exception as e:
        logger.error(f"Error in /api/picks: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bets", methods=["GET", "POST"])
@enable_cors
def api_bets():
    """Get or save user bets. Stores as {"bets": [array], ...}."""
    try:
        if request.method == "GET":
            bets = load_bets()
            # Normalize: always return bets as an array
            if isinstance(bets, dict):
                bet_list = bets.get("bets", [])
                if isinstance(bet_list, dict):
                    # Old format: object keyed by "match|bet_on" — convert to array
                    bet_list = [v for v in bet_list.values() if isinstance(v, dict) and "match" in v]
                return jsonify({"bets": bet_list})
            return jsonify({"bets": []})

        elif request.method == "POST":
            data = request.get_json() or {}
            # Accept both {"bets": [...]} and bare object format
            if isinstance(data, dict) and "bets" in data:
                new_bets = data["bets"]
            elif isinstance(data, list):
                new_bets = data
            else:
                # Old format: object keyed by "match|bet_on"
                new_bets = [v for v in data.values() if isinstance(v, dict) and "match" in v]

            # Deduplicate by match + bet_on
            seen = set()
            unique = []
            for b in new_bets:
                key = (b.get("match", ""), b.get("bet_on", ""))
                if key not in seen:
                    seen.add(key)
                    unique.append(b)

            # Preserve trade sync metadata
            existing = load_bets()
            save_data = {"bets": unique}
            if isinstance(existing, dict):
                if "last_synced" in existing:
                    save_data["last_synced"] = existing["last_synced"]
                if "last_trade_sync" in existing:
                    save_data["last_trade_sync"] = existing["last_trade_sync"]

            if save_bets(save_data):
                return jsonify({"status": "success", "bets": unique}), 201
            else:
                return jsonify({"status": "error", "message": "Failed to save"}), 500

    except Exception as e:
        logger.error(f"Error in /api/bets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bets/raw", methods=["GET"])
@enable_cors
def api_bets_raw():
    """Debug endpoint: return raw my_bets.json contents."""
    try:
        bets = load_bets()
        return jsonify({"raw": bets, "type": str(type(bets).__name__)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Polymarket Trade Data Integration ───

def fetch_poly_trades(since_ts=None):
    """Fetch user's actual trades from Polymarket Data API."""
    import requests as req
    trades = []
    try:
        params = {
            "user": POLY_WALLET,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
            "limit": 200,
        }
        if since_ts:
            params["start"] = str(int(since_ts))

        r = req.get(f"{POLY_DATA_API}/activity", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            trades = data
        elif isinstance(data, dict):
            trades = data.get("data", data.get("activity", []))

        logger.info(f"Fetched {len(trades)} trades from Polymarket Data API")
    except Exception as e:
        logger.error(f"Error fetching Polymarket trades: {e}")
    return trades


def match_trade_to_bet(trade, bet):
    """Check if a Polymarket trade matches a user's bet selection."""
    # Compare by market title/slug containing player names
    trade_title = (trade.get("title", "") or trade.get("name", "")).lower()
    trade_slug = (trade.get("eventSlug", "") or trade.get("slug", "")).lower()
    trade_outcome = (trade.get("outcome", "") or "").lower()

    bet_match = bet.get("match", "").lower()
    bet_on = bet.get("bet_on", "").lower()

    # Extract last names from the bet
    players = bet_match.split(" vs ")
    if len(players) != 2:
        return False

    a_last = players[0].strip().split()[-1].lower()
    b_last = players[1].strip().split()[-1].lower()
    bet_on_last = bet_on.strip().split()[-1].lower()

    # Check if the trade is for this match (both last names in title or slug)
    title_match = (a_last in trade_title and b_last in trade_title)
    slug_match = (a_last in trade_slug and b_last in trade_slug)
    if not title_match and not slug_match:
        return False

    # Check if the trade is for the same player we bet on
    # Polymarket trades have an "outcome" field (player name) and "side" (BUY/SELL)
    trade_side = trade.get("side", "BUY")
    if trade_side == "BUY":
        # Buying YES on this outcome = betting on this player
        outcome_last = trade_outcome.strip().split()[-1].lower() if trade_outcome else ""
        if outcome_last == bet_on_last or bet_on_last in trade_outcome:
            return True
    elif trade_side == "SELL":
        # Selling YES on the other player = also effectively betting on our player
        outcome_last = trade_outcome.strip().split()[-1].lower() if trade_outcome else ""
        if outcome_last != bet_on_last and (outcome_last == a_last or outcome_last == b_last):
            return True

    return False


def enrich_bets_with_trades(bets, trades):
    """Match user bets with actual Polymarket trade data.

    For each bet, find matching trades and aggregate:
    - actual_buy_price (weighted avg price in cents)
    - actual_stake (total USDC spent)
    - actual_shares (total shares bought)
    """
    enriched = []
    for bet in bets:
        matching_trades = []
        for trade in trades:
            if match_trade_to_bet(trade, bet):
                matching_trades.append(trade)

        if matching_trades:
            # Aggregate: sum up all matching BUY trades
            total_usdc = 0
            total_shares = 0
            for t in matching_trades:
                usdc = float(t.get("usdcSize", 0) or t.get("cash", 0) or 0)
                shares = float(t.get("size", 0) or t.get("tokens", 0) or 0)
                total_usdc += usdc
                total_shares += shares

            avg_price = (total_usdc / total_shares * 100) if total_shares > 0 else 0

            bet["actual_buy_price"] = round(avg_price, 1)   # cents
            bet["actual_stake"] = round(total_usdc, 2)       # USDC
            bet["actual_shares"] = round(total_shares, 2)
            bet["trade_count"] = len(matching_trades)
            bet["trade_ids"] = [t.get("transactionHash", "")[:12] for t in matching_trades[:5]]
        enriched.append(bet)

    return enriched


@app.route("/api/poly-trades", methods=["GET"])
@enable_cors
def api_poly_trades():
    """Fetch and match Polymarket trades against user's tracked bets."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        # Fetch trades from Polymarket
        trades = fetch_poly_trades()

        # Load user's tracked bets
        bets = load_bets()
        bet_list = bets.get("bets", []) if isinstance(bets, dict) else bets

        # If bets come from the POST body (client sends myBets array)
        client_bets = request.args.get("from_client")
        if client_bets:
            # Client will send bets as query param or we read from saved
            pass

        # Enrich bets with trade data
        enriched = enrich_bets_with_trades(bet_list, trades)

        # Save enriched bets back
        if isinstance(bets, dict):
            bets["bets"] = enriched
        save_bets(bets)

        return jsonify({
            "status": "success",
            "trades_fetched": len(trades),
            "bets_matched": sum(1 for b in enriched if b.get("actual_stake")),
            "bets": enriched,
        })

    except Exception as e:
        logger.error(f"Error in /api/poly-trades: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync-bets", methods=["POST"])
@enable_cors
def api_sync_bets():
    """Sync bets from client localStorage, then enrich with Polymarket trade data."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        client_bets = request.get_json() or []
        if isinstance(client_bets, dict):
            client_bets = client_bets.get("bets", [])

        # Fetch real trade data from Polymarket
        trades = fetch_poly_trades()

        # Enrich each bet
        enriched = enrich_bets_with_trades(client_bets, trades)

        # Save to server
        save_bets({"bets": enriched, "last_synced": datetime.utcnow().isoformat()})

        matched = sum(1 for b in enriched if b.get("actual_stake"))
        logger.info(f"Synced {len(enriched)} bets, {matched} matched to Polymarket trades")

        return jsonify({
            "status": "success",
            "trades_fetched": len(trades),
            "bets_matched": matched,
            "bets": enriched,
        })

    except Exception as e:
        logger.error(f"Error in /api/sync-bets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
@enable_cors
def api_status():
    """Return system health status."""
    try:
        status = get_system_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error in /api/status: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


_player_cache = {"data": None, "ts": None}

@app.route("/api/players", methods=["GET"])
@enable_cors
def api_players():
    """Compute comprehensive player profiles from match history.
    Returns profiles for all unique players in today's signals.
    Query param ?all=1 returns top 100 by ELO instead.
    Results are cached for 30 minutes."""
    import pandas as pd, numpy as np
    from datetime import timedelta

    # Return cached result if fresh (< 30 min)
    if _player_cache["data"] and _player_cache["ts"]:
        age = (datetime.now() - _player_cache["ts"]).total_seconds()
        if age < 1800 and not request.args.get("refresh"):
            return jsonify(_player_cache["data"])

    try:
        hist_path = BASE_DIR / "data" / "raw" / "matches_combined.parquet"
        if not hist_path.exists():
            return jsonify({"error": "No match history data found"}), 404

        df = pd.read_parquet(hist_path)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        cutoff = pd.Timestamp.now()

        # Determine which players to profile
        target_players = set()
        picks_data = load_picks_jsonl()
        for p in picks_data:
            match_str = p.get("match", "")
            if " vs " in match_str:
                parts = match_str.split(" vs ")
                target_players.add(parts[0].strip())
                target_players.add(parts[1].strip())

        # Also accept ?player=Name query param
        q_player = request.args.get("player", "").strip()
        if q_player:
            target_players.add(q_player)

        # If ?all=1, get top 100 by match count (active players)
        if request.args.get("all") == "1":
            since = cutoff - timedelta(days=365)
            recent = df[df["date"] >= since]
            all_players = pd.concat([recent["winner"], recent["loser"]]).value_counts()
            target_players = set(all_players.head(100).index)

        if not target_players:
            return jsonify({"players": []})

        # ── HELPER: ELO computation (numpy-optimized) ──
        def compute_elo_fast(matches_df):
            """Compute ELO from sorted matches using numpy arrays for speed."""
            winners = matches_df["winner"].values
            losers = matches_df["loser"].values
            ratings = {}
            for i in range(len(winners)):
                w, l = winners[i], losers[i]
                if w not in ratings: ratings[w] = 1500.0
                if l not in ratings: ratings[l] = 1500.0
                ra, rb = ratings[w], ratings[l]
                ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))
                k = 32 * (1.5 if ea < 0.3 else 0.7 if ea > 0.8 else 1.0)
                ratings[w] = ra + k * (1 - ea)
                ratings[l] = rb + k * (ea - 1)
            return ratings

        sorted_df = df.sort_values("date")
        elo = compute_elo_fast(sorted_df)

        # Surface ELOs
        surf_elo = {}
        for surf in ["Hard", "Clay", "Grass"]:
            sdf = sorted_df[sorted_df["surface"] == surf]
            surf_elo[surf] = compute_elo_fast(sdf)

        # ELO percentiles (active players only)
        since = cutoff - timedelta(days=365)
        active = set(df[df["date"] >= since]["winner"].unique()) | set(df[df["date"] >= since]["loser"].unique())
        active_elos = sorted([elo.get(p, 1500) for p in active])

        def pct(val):
            if not active_elos: return 0
            below = sum(1 for e in active_elos if e < val)
            return round(below / len(active_elos) * 100)

        # ── BUILD PROFILES ──
        profiles = []
        for player in target_players:
            # All matches for this player
            pw = df[df["winner"] == player].copy()
            pl = df[df["loser"] == player].copy()
            pw["result"] = "W"
            pw["opponent"] = pw["loser"]
            pw["opp_rank"] = pw["l_rank"]
            pw["player_rank"] = pw["w_rank"]
            pl["result"] = "L"
            pl["opponent"] = pl["winner"]
            pl["opp_rank"] = pl["w_rank"]
            pl["player_rank"] = pl["l_rank"]
            pm = pd.concat([pw, pl]).sort_values("date")

            if len(pm) < 5:
                continue  # Skip players with too few matches

            # Current rank
            latest = pm.dropna(subset=["player_rank"]).tail(1)
            current_rank = int(latest["player_rank"].iloc[0]) if len(latest) > 0 else None

            # Rank 30 days ago
            d30 = cutoff - timedelta(days=30)
            pm30 = pm[pm["date"] <= d30].dropna(subset=["player_rank"]).tail(1)
            rank_30d = int(pm30["player_rank"].iloc[0]) if len(pm30) > 0 else current_rank
            rank_delta = (rank_30d - current_rank) if current_rank and rank_30d else 0

            # Player info
            info_row = pw.tail(1) if len(pw) > 0 else pl.tail(1)
            country = info_row["winner_ioc"].iloc[0] if len(pw) > 0 and "winner_ioc" in info_row.columns else (info_row["loser_ioc"].iloc[0] if len(pl) > 0 and "loser_ioc" in info_row.columns else "")
            hand = info_row["winner_hand"].iloc[0] if len(pw) > 0 and "winner_hand" in info_row.columns else (info_row["loser_hand"].iloc[0] if len(pl) > 0 and "loser_hand" in info_row.columns else "")
            age = info_row["winner_age"].iloc[0] if len(pw) > 0 and "winner_age" in info_row.columns else (info_row["loser_age"].iloc[0] if len(pl) > 0 and "loser_age" in info_row.columns else None)

            # ── FORM ──
            last10 = pm.tail(10)
            form_results = list(last10["result"].values)
            wins_10 = form_results.count("W")
            losses_10 = form_results.count("L")

            # Streak
            streak = 0
            streak_type = ""
            for r in reversed(form_results):
                if not streak_type:
                    streak_type = r
                if r == streak_type:
                    streak += 1
                else:
                    break
            streak_str = f"{streak_type}{streak}"

            # Win rates: 90d and 365d
            d90 = cutoff - timedelta(days=90)
            d365 = cutoff - timedelta(days=365)
            pm90 = pm[pm["date"] >= d90]
            pm365 = pm[pm["date"] >= d365]
            wr90 = round(len(pm90[pm90["result"] == "W"]) / max(len(pm90), 1) * 100, 1)
            wr365 = round(len(pm365[pm365["result"] == "W"]) / max(len(pm365), 1) * 100, 1)
            wr_trend = round(wr90 - wr365, 1)

            # Avg tournament depth
            depth_map = {"F": 7, "SF": 6, "QF": 5, "R16": 4, "R32": 3, "R64": 2, "R128": 1, "RR": 4, "BR": 6, "ER": 1}
            if "round" in pm.columns:
                depths = pm365["round"].map(depth_map).dropna()
                avg_depth_num = depths.mean() if len(depths) > 0 else 3
                depth_labels = {7: "F", 6: "SF", 5: "QF", 4: "R16", 3: "R32", 2: "R64", 1: "R128"}
                avg_depth = depth_labels.get(round(avg_depth_num), "R32")
            else:
                avg_depth = "—"

            # ── SURFACE PERFORMANCE ──
            surfaces = {}
            for surf in ["Hard", "Clay", "Grass"]:
                spm = pm365[pm365["surface"] == surf]
                sw = len(spm[spm["result"] == "W"])
                sl = len(spm[spm["result"] == "L"])
                total = sw + sl
                wr = round(sw / max(total, 1) * 100, 1)
                raw_se = surf_elo.get(surf, {}).get(player)
                gen_e = elo.get(player, 1500)
                blended = round((gen_e + raw_se) / 2) if raw_se else None
                surfaces[surf] = {"wr": wr, "w": sw, "l": sl, "elo": blended}

            # ── OPPONENT TIER PERFORMANCE ──
            tiers = {}
            for label, lo, hi in [("Top 10", 1, 10), ("Top 20", 1, 20), ("Top 50", 1, 50), ("Top 100", 1, 100), ("100+", 101, 9999)]:
                tier_m = pm[pm["opp_rank"].between(lo, hi)]
                tw = len(tier_m[tier_m["result"] == "W"])
                tl = len(tier_m[tier_m["result"] == "L"])
                tt = tw + tl
                tiers[label] = {"wr": round(tw / max(tt, 1) * 100, 1), "w": tw, "l": tl}

            # ── MENTAL TOUGHNESS ──
            def parse_sets(score):
                """Parse score string to count sets won by each player."""
                if not isinstance(score, str):
                    return None, None, False, False
                sets = score.replace("(w/o)", "").replace("RET", "").strip().split()
                p1_sets = p2_sets = 0
                has_tb = False
                for s in sets:
                    s_clean = s.split("(")[0].split("[")[0]
                    parts = s_clean.split("-")
                    if len(parts) == 2:
                        try:
                            a, b = int(parts[0]), int(parts[1])
                            if a > b: p1_sets += 1
                            else: p2_sets += 1
                            if a == 7 or b == 7:
                                has_tb = True
                        except ValueError:
                            pass
                total_sets = p1_sets + p2_sets
                is_deciding = total_sets >= 3 and abs(p1_sets - p2_sets) <= 1
                return p1_sets, p2_sets, is_deciding, has_tb

            deciding_w = deciding_l = tb_w = tb_l = comeback_w = comeback_l = 0
            final_w = final_l = 0
            scores = pm["score"].values if "score" in pm.columns else []
            results = pm["result"].values
            rounds = pm["round"].values if "round" in pm.columns else [None] * len(pm)
            for idx in range(len(results)):
                is_win = results[idx] == "W"
                score_val = scores[idx] if idx < len(scores) else None
                p1s, p2s, is_deciding, has_tb = parse_sets(score_val)
                if p1s is None:
                    continue

                if is_deciding:
                    if is_win: deciding_w += 1
                    else: deciding_l += 1
                if has_tb:
                    if is_win: tb_w += 1
                    else: tb_l += 1
                # Comeback: lost first set but won match
                score_str = str(score_val or "")
                first_set = score_str.split()[0] if score_str else ""
                first_parts = first_set.split("(")[0].split("-")
                if len(first_parts) == 2:
                    try:
                        fs1, fs2 = int(first_parts[0]), int(first_parts[1])
                        player_is_winner_in_source = is_win
                        lost_first = (fs1 < fs2) if player_is_winner_in_source else (fs1 > fs2)
                        if lost_first and is_win: comeback_w += 1
                        elif lost_first and not is_win: comeback_l += 1
                    except ValueError:
                        pass

                # Finals
                rd = rounds[idx] if idx < len(rounds) else None
                if rd == "F":
                    if is_win: final_w += 1
                    else: final_l += 1

            mental = {
                "deciding": {"w": deciding_w, "l": deciding_l, "pct": round(deciding_w / max(deciding_w + deciding_l, 1) * 100)},
                "tiebreak": {"w": tb_w, "l": tb_l, "pct": round(tb_w / max(tb_w + tb_l, 1) * 100)},
                "comeback": {"w": comeback_w, "l": comeback_l, "pct": round(comeback_w / max(comeback_w + comeback_l, 1) * 100)},
                "finals":   {"w": final_w, "l": final_l, "pct": round(final_w / max(final_w + final_l, 1) * 100)},
            }

            # ── FATIGUE ──
            d30_matches = pm[pm["date"] >= d90]  # use 90d for better sample
            d30_actual = pm[pm["date"] >= (cutoff - timedelta(days=30))]
            matches_30d = len(d30_actual)
            avg_mins = round(pm365["minutes"].dropna().mean()) if "minutes" in pm365.columns and len(pm365["minutes"].dropna()) > 0 else None
            last_match_date = pm["date"].max()
            days_since = (cutoff - last_match_date).days if pd.notna(last_match_date) else None

            fatigue = {
                "matches_30d": matches_30d,
                "avg_duration": f"{avg_mins // 60}h {avg_mins % 60:02d}m" if avg_mins and avg_mins > 0 else "—",
                "days_since": days_since,
                "load": "Heavy" if matches_30d >= 8 else "Moderate" if matches_30d >= 5 else "Light",
                "freshness": "Fresh" if days_since and days_since >= 4 else "Short rest" if days_since and days_since >= 2 else "Back-to-back" if days_since is not None else "—",
            }

            # ── GRAND SLAM PROFILE ──
            slam_map = {
                "Australian Open": "AUS", "Roland Garros": "RG",
                "Wimbledon": "WIM", "US Open": "USO"
            }
            slams = {}
            best_round_order = {"F": 7, "SF": 6, "QF": 5, "R16": 4, "R32": 3, "R64": 2, "R128": 1, "W": 8}
            for slam_full, slam_short in slam_map.items():
                slam_matches = pm[pm["tournament"].str.contains(slam_full, case=False, na=False)]
                sw_s = len(slam_matches[slam_matches["result"] == "W"])
                sl_s = len(slam_matches[slam_matches["result"] == "L"])
                # Best result
                best = "—"
                # Check if they won the final
                slam_finals_won = slam_matches[(slam_matches["result"] == "W") & (slam_matches["round"] == "F")]
                if len(slam_finals_won) > 0:
                    best = "W"
                elif len(slam_matches) > 0:
                    best_val = 0
                    for _, r in slam_matches.iterrows():
                        rd = r.get("round", "")
                        rv = best_round_order.get(rd, 0)
                        if r["result"] == "L" and rv > best_val:
                            best_val = rv
                            # Lost in this round = reached this round
                            best = rd
                        elif r["result"] == "W" and rv > best_val:
                            best_val = rv
                            best = rd
                slams[slam_short] = {"best": best, "w": sw_s, "l": sl_s}

            # ── ELO TREND (monthly, last 6 months) ──
            elo_trend = []
            player_elo_track = 1500
            for _, row in df.sort_values("date").iterrows():
                w, l = row["winner"], row["loser"]
                if w == player or l == player:
                    # Recalculate to track this player's ELO over time
                    pass  # We'd need full tracking — skip for v1, use current
            # Simplified: just provide current ELO
            gen_elo = round(elo.get(player, 1500))
            gen_pct = pct(gen_elo)

            # Surface ELO percentiles
            surf_profiles = {}
            for surf in ["Hard", "Clay", "Grass"]:
                se = surfaces[surf]["elo"]
                surf_active_elos = sorted([surf_elo.get(surf, {}).get(p, 1500) for p in active if p in surf_elo.get(surf, {})])
                sp = 0
                if se and surf_active_elos:
                    sp = round(sum(1 for e in surf_active_elos if e < se) / len(surf_active_elos) * 100)
                surf_profiles[surf] = {"elo": se, "pct": sp}

            # Tour (ATP/WTA)
            tour = pm.tail(1)["tour"].iloc[0] if "tour" in pm.columns and len(pm) > 0 else ""

            profiles.append({
                "name": player,
                "country": str(country) if pd.notna(country) else "",
                "hand": str(hand) if pd.notna(hand) else "",
                "age": round(float(age), 1) if pd.notna(age) else None,
                "tour": str(tour),
                "rank": current_rank,
                "rank_delta": rank_delta,
                "elo": gen_elo,
                "elo_pct": gen_pct,
                "surf_elo": surf_profiles,
                "form": form_results,
                "wins_10": wins_10,
                "losses_10": losses_10,
                "streak": streak_str,
                "wr_90d": wr90,
                "wr_365d": wr365,
                "wr_trend": wr_trend,
                "avg_depth": avg_depth,
                "surfaces": surfaces,
                "tiers": tiers,
                "mental": mental,
                "fatigue": fatigue,
                "slams": slams,
                "total_matches": len(pm),
            })

        # Sort by ELO descending
        profiles.sort(key=lambda x: x["elo"], reverse=True)
        result = {"players": profiles}
        _player_cache["data"] = result
        _player_cache["ts"] = datetime.now()
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error in /api/players: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
@enable_cors
def generate_card():
    """Trigger a new betting card generation."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        script_path = BASE_DIR / "04_betting_card.py"
        if not script_path.exists():
            return jsonify({"error": "04_betting_card.py not found"}), 404

        logger.info("Triggering betting card generation...")
        result = subprocess.run(
            ["python3", str(script_path)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode == 0:
            latest_card_path = get_latest_betting_card()
            return jsonify({
                "status": "success",
                "message": "Card generation completed",
                "latest_card": latest_card_path
            }), 200
        else:
            logger.error(f"Card generation failed: {result.stderr}")
            return jsonify({
                "status": "error",
                "message": "Card generation failed",
                "error": result.stderr[:500]
            }), 500

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Generation timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/resolve", methods=["POST"])
@enable_cors
def resolve_outcomes_route():
    """Manually trigger outcome resolution — fast last-name matching."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        import requests as req
        import time
        t0 = time.time()

        logger.info("Running inline outcome resolution...")
        output_lines = []

        # Load picks
        picks = load_picks_jsonl()
        if not picks:
            return jsonify({"status": "success", "output": "No picks found."})

        unresolved = [p for p in picks if p.get("outcome") is None]
        output_lines.append(f"Total picks: {len(picks)}, Unresolved: {len(unresolved)}")

        if not unresolved:
            return jsonify({"status": "success", "output": "All picks already resolved!"})

        # Only check picks older than 24 hours
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=24)

        eligible_indices = set()
        for i, p in enumerate(picks):
            if p.get("outcome") is not None:
                continue
            logged_at = p.get("logged_at", "")
            if logged_at:
                try:
                    pt = datetime.fromisoformat(logged_at.replace("Z", ""))
                    if pt > cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            eligible_indices.add(i)

        output_lines.append(f"Eligible (>24h old): {len(eligible_indices)}")

        if not eligible_indices:
            return jsonify({"status": "success", "output": "\n".join(output_lines) + "\nNo eligible picks to resolve."})

        # Bulk fetch resolved markets from Polymarket (3 API calls)
        resolved_markets = []
        for tag in ["tennis", "atp", "wta"]:
            try:
                r = req.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"tag_slug": tag, "closed": "true", "limit": 100,
                            "order": "endDate", "ascending": "false"},
                    timeout=10,
                )
                r.raise_for_status()
                for ev in r.json():
                    markets = ev.get("markets", [])
                    if markets:
                        for m in markets:
                            parsed = _parse_resolved_market(m)
                            if parsed:
                                if not parsed.get("slug"):
                                    parsed["slug"] = ev.get("slug", "")
                                resolved_markets.append(parsed)
                    else:
                        parsed = _parse_resolved_market(ev)
                        if parsed:
                            resolved_markets.append(parsed)
            except Exception as e:
                output_lines.append(f"[warn] events/{tag}: {e}")

        # Deduplicate
        seen = set()
        unique_resolved = []
        for rm in resolved_markets:
            mid = rm.get("market_id", "")
            if mid and mid not in seen:
                seen.add(mid)
                unique_resolved.append(rm)

        output_lines.append(f"Found {len(unique_resolved)} resolved markets ({time.time()-t0:.1f}s)")

        # Build fast lookup index: map last-name pairs to resolved markets
        # This replaces the O(n*m) SequenceMatcher loop with O(1) dict lookups
        lastname_index = {}  # (last_a, last_b) -> resolved market
        for rm in unique_resolved:
            q = rm.get("question", "").lower()
            # Extract all words > 2 chars as potential last names
            words = set(w for w in q.replace("?", "").replace(",", "").split() if len(w) > 2)
            rm["_words"] = words

        # ── REVERT incorrectly resolved futures/outright picks ──
        FUTURES_KW = ["french open", "us open", "australian open", "wimbledon",
                       "roland garros", "grand slam", "men's", "women's"]
        n_reverted = 0
        for pick in picks:
            if pick.get("outcome") is None:
                continue
            # Check if this is a futures pick that was wrongly resolved
            mt = pick.get("market_type", "")
            pb = pick.get("player_b", "").lower()
            match_name = pick.get("match", "").lower()
            is_futures = (mt == "outright"
                          or any(kw in pb for kw in FUTURES_KW)
                          or any(kw in match_name for kw in FUTURES_KW)
                          or " — " in pick.get("match", ""))
            if is_futures:
                pick.pop("outcome", None)
                pick.pop("pnl", None)
                pick.pop("actual_winner", None)
                pick.pop("resolved_at", None)
                n_reverted += 1

        if n_reverted:
            output_lines.append(f"Reverted {n_reverted} incorrectly resolved futures picks")

        # ── Phase 0: Resolve USER'S TRACKED BETS first (individual slug lookups) ──
        # The user only has a handful of bets — prioritize resolving them
        new_resolutions = 0
        try:
            bets_data = load_bets()
            bet_list = bets_data.get("bets", []) if isinstance(bets_data, dict) else []
            bet_slugs = set()
            for bet in bet_list:
                poly_link = bet.get("poly_link", "")
                if "/event/" in poly_link:
                    bet_slugs.add(poly_link.split("/event/")[-1].split("?")[0].split("/")[0])

            if bet_slugs:
                output_lines.append(f"Phase 0: Resolving {len(bet_slugs)} tracked bet markets...")
                slug_resolved_cache = {}  # slug -> parsed resolved market
                for slug in bet_slugs:
                    try:
                        r = req.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8)
                        r.raise_for_status()
                        events = r.json()
                        if not events:
                            continue
                        ev = events[0] if isinstance(events, list) else events
                        markets = ev.get("markets", [])
                        for m in (markets if markets else [ev]):
                            parsed = _parse_resolved_market(m)
                            if parsed:
                                slug_resolved_cache[slug] = parsed
                                # Also add to unique_resolved for Phase 1 matching
                                unique_resolved.append(parsed)
                                break
                    except Exception as e:
                        output_lines.append(f"  [warn] bet slug {slug}: {e}")

                # Now resolve ALL picks matching these slugs
                for pick in picks:
                    if pick.get("outcome") is not None:
                        continue
                    pslug = pick.get("slug", "")
                    if not pslug:
                        pl = pick.get("poly_link", "")
                        if "/event/" in pl:
                            pslug = pl.split("/event/")[-1].split("?")[0].split("/")[0]
                    if pslug and pslug in slug_resolved_cache:
                        parsed = slug_resolved_cache[pslug]
                        winner_raw = parsed.get("winner", "")
                        pa = pick.get("player_a", "")
                        pbb = pick.get("player_b", "")
                        a_last = pa.split()[-1].lower() if pa else ""
                        b_last = pbb.split()[-1].lower() if pbb else ""
                        winner_lower = winner_raw.lower()
                        wl = winner_lower.split()[-1] if winner_lower else ""

                        if winner_lower in ["yes", "true", "1"]:
                            winner_name = pa
                        elif winner_lower in ["no", "false", "0"]:
                            winner_name = pbb
                        elif a_last == wl or a_last in winner_lower:
                            winner_name = pa
                        elif b_last == wl or b_last in winner_lower:
                            winner_name = pbb
                        else:
                            continue

                        bet_on = pick.get("bet_on", "")
                        bl = bet_on.split()[-1].lower() if bet_on else ""
                        wnl = winner_name.split()[-1].lower() if winner_name else ""
                        bet_won = (bl == wnl) or (bet_on.lower() == winner_name.lower())

                        poly_price = pick.get("poly_price", 50)
                        price_dec = poly_price / 100
                        if bet_won:
                            pnl = round(100 * (1 - price_dec), 2)
                            outcome = "win"
                        else:
                            pnl = round(-100 * price_dec, 2)
                            outcome = "loss"

                        pick["outcome"] = outcome
                        pick["pnl"] = pnl
                        pick["actual_winner"] = winner_name
                        pick["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                        new_resolutions += 1

                output_lines.append(f"Phase 0: Resolved {new_resolutions} picks from tracked bets")
        except Exception as e:
            output_lines.append(f"  [warn] Phase 0: {e}")

        # Match H2H picks using fast last-name lookups (no SequenceMatcher!)
        phase1_start = new_resolutions
        for i, pick in enumerate(picks):
            if i not in eligible_indices:
                continue
            # Also skip newly-eligible picks that were just reverted
            if pick.get("outcome") is not None:
                continue

            # Skip futures/outright picks — they resolve on a different timeline
            mt = pick.get("market_type", "")
            pb = pick.get("player_b", "").lower()
            match_name = pick.get("match", "").lower()
            if (mt == "outright"
                or any(kw in pb for kw in FUTURES_KW)
                or any(kw in match_name for kw in FUTURES_KW)
                or " — " in pick.get("match", "")):
                continue

            pick_player_a = pick.get("player_a", "")
            pick_player_b = pick.get("player_b", "")

            if not pick_player_a or not pick_player_b:
                continue

            a_last = pick_player_a.split()[-1].lower()
            b_last = pick_player_b.split()[-1].lower()

            if len(a_last) <= 2 or len(b_last) <= 2:
                continue

            # Fast match: find resolved market containing both last names
            rm = None
            for candidate in unique_resolved:
                if a_last in candidate["_words"] and b_last in candidate["_words"]:
                    rm = candidate
                    break

            if not rm:
                continue

            # Determine winner using simple last-name matching (no SequenceMatcher)
            winner_raw = rm.get("winner", "")
            winner_name = winner_raw

            if winner_raw.lower() in ["yes", "true", "1"]:
                winner_name = pick_player_a
            elif winner_raw.lower() in ["no", "false", "0"]:
                winner_name = pick_player_b
            else:
                # Check if winner matches player A or B by last name
                winner_lower = winner_raw.lower()
                winner_last = winner_lower.split()[-1] if winner_lower else ""
                if a_last == winner_last or a_last in winner_lower:
                    winner_name = pick_player_a
                elif b_last == winner_last or b_last in winner_lower:
                    winner_name = pick_player_b

            # Check if our bet won (simple last-name comparison)
            bet_on = pick.get("bet_on", "")
            bet_last = bet_on.split()[-1].lower() if bet_on else ""
            winner_last = winner_name.split()[-1].lower() if winner_name else ""
            bet_won = (bet_last == winner_last) or (bet_on.lower() == winner_name.lower())

            poly_price = pick.get("poly_price", 50)
            price_dec = poly_price / 100
            if bet_won:
                pnl = round(100 * (1 - price_dec), 2)
                outcome = "win"
            else:
                pnl = round(-100 * price_dec, 2)
                outcome = "loss"

            pick["outcome"] = outcome
            pick["pnl"] = pnl
            pick["actual_winner"] = winner_name
            pick["resolved_at"] = rm.get("resolved_at", datetime.utcnow().isoformat())
            new_resolutions += 1
            output_lines.append(
                f"{'WIN' if outcome == 'win' else 'LOSS'}: {pick.get('match', '?')} | "
                f"Bet: {bet_on} | Winner: {winner_name} | PnL: ${pnl:+.0f}"
            )

        # Phase 2: For still-unresolved picks, collect UNIQUE slugs and do batch lookups
        # (Deduplicated: each slug looked up once, result applied to all matching picks)
        max_slug_lookups = 30
        unmatched_slugs = {}  # slug -> list of pick indices
        for i, pick in enumerate(picks):
            if pick.get("outcome") is not None:
                continue
            mt = pick.get("market_type", "")
            pb = pick.get("player_b", "").lower()
            match_name = pick.get("match", "").lower()
            if (mt == "outright" or any(kw in pb for kw in FUTURES_KW)
                or any(kw in match_name for kw in FUTURES_KW)
                or " — " in pick.get("match", "")):
                continue

            slug = pick.get("slug", "")
            if not slug:
                poly_link = pick.get("poly_link", "")
                if "/event/" in poly_link:
                    slug = poly_link.split("/event/")[-1].split("?")[0].split("/")[0]
            if slug:
                if slug not in unmatched_slugs:
                    unmatched_slugs[slug] = []
                unmatched_slugs[slug].append(i)

        slug_lookups = 0
        slug_cache = {}  # slug -> parsed resolved market
        for slug in list(unmatched_slugs.keys())[:max_slug_lookups]:
            try:
                slug_lookups += 1
                r = req.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8)
                r.raise_for_status()
                events = r.json()
                if not events:
                    continue
                ev = events[0] if isinstance(events, list) else events
                markets = ev.get("markets", [])
                for m in (markets if markets else [ev]):
                    parsed = _parse_resolved_market(m)
                    if parsed:
                        slug_cache[slug] = parsed
                        break
            except Exception as e:
                output_lines.append(f"[warn] slug lookup {slug}: {e}")

        # Apply slug results to ALL matching picks
        for slug, parsed in slug_cache.items():
            for pick_idx in unmatched_slugs.get(slug, []):
                pick = picks[pick_idx]
                if pick.get("outcome") is not None:
                    continue

                winner_raw = parsed.get("winner", "")
                pa = pick.get("player_a", "")
                pbb = pick.get("player_b", "")
                a_last = pa.split()[-1].lower() if pa else ""
                b_last = pbb.split()[-1].lower() if pbb else ""
                winner_lower = winner_raw.lower()
                wl = winner_lower.split()[-1] if winner_lower else ""

                if winner_lower in ["yes", "true", "1"]:
                    winner_name = pa
                elif winner_lower in ["no", "false", "0"]:
                    winner_name = pbb
                elif a_last == wl or a_last in winner_lower:
                    winner_name = pa
                elif b_last == wl or b_last in winner_lower:
                    winner_name = pbb
                else:
                    continue

                bet_on = pick.get("bet_on", "")
                bl = bet_on.split()[-1].lower() if bet_on else ""
                wnl = winner_name.split()[-1].lower() if winner_name else ""
                bet_won = (bl == wnl) or (bet_on.lower() == winner_name.lower())

                poly_price = pick.get("poly_price", 50)
                price_dec = poly_price / 100
                if bet_won:
                    pnl = round(100 * (1 - price_dec), 2)
                    outcome = "win"
                else:
                    pnl = round(-100 * price_dec, 2)
                    outcome = "loss"

                pick["outcome"] = outcome
                pick["pnl"] = pnl
                pick["actual_winner"] = winner_name
                pick["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                new_resolutions += 1

        if slug_lookups:
            output_lines.append(f"Phase 2: {slug_lookups} unique slug lookups, {len(slug_cache)} resolved")

        output_lines.append(f"\nNew resolutions: {new_resolutions} ({time.time()-t0:.1f}s total)")

        # Save if anything changed (resolutions or reverts)
        if new_resolutions > 0 or n_reverted > 0:
            PICKS_FILE.parent.mkdir(exist_ok=True)
            with open(PICKS_FILE, 'w') as f:
                for p in picks:
                    row = {k: v for k, v in p.items() if k != "_line_idx" and k != "_words"}
                    f.write(json.dumps(row) + "\n")

            all_resolved = [p for p in picks if p.get("outcome") is not None]
            wins = sum(1 for p in all_resolved if p["outcome"] == "win")
            losses = len(all_resolved) - wins
            total_pnl = sum(p.get("pnl", 0) for p in all_resolved)
            output_lines.append(f"Record: {wins}W-{losses}L | PnL: ${total_pnl:+,.0f}")

        # ── Phase 3: Auto-enrich tracked bets with Polymarket trade data ──
        try:
            bets_data = load_bets()
            bet_list = bets_data.get("bets", []) if isinstance(bets_data, dict) else []
            if bet_list:
                trades = fetch_poly_trades()
                if trades:
                    enriched = enrich_bets_with_trades(bet_list, trades)
                    matched = sum(1 for b in enriched if b.get("actual_stake"))
                    if isinstance(bets_data, dict):
                        bets_data["bets"] = enriched
                        bets_data["last_trade_sync"] = datetime.utcnow().isoformat()
                    save_bets(bets_data)
                    output_lines.append(f"Trade sync: {matched}/{len(bet_list)} bets matched to Polymarket trades")
        except Exception as e:
            output_lines.append(f"[warn] Trade sync: {e}")

        return jsonify({
            "status": "success",
            "message": f"Resolved {new_resolutions} picks",
            "output": "\n".join(output_lines)
        })

    except Exception as e:
        logger.error(f"Resolution error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/admin/dedup", methods=["POST"])
@enable_cors
def admin_dedup():
    """Run deduplication on picks — accessible from admin panel."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        logger.info("Admin triggered dedup...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "05_bet_logger.py"), "dedup"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30
        )
        output = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            return jsonify({"status": "success", "output": output})
        else:
            return jsonify({"status": "error", "output": output}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/admin/debug-rank/<player_name>", methods=["GET"])
@enable_cors
def admin_debug_rank(player_name):
    """Debug ranking lookup for a player — shows cache entries and lookup result."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401
    import sys
    sys.path.insert(0, str(BASE_DIR))
    try:
        from importlib import import_module
        fetcher = import_module("09_rankings_fetcher")
        cache = fetcher.load_cached_rankings()
        rankings = cache.get("rankings", {})
        lastname_index = cache.get("lastname_index", {})

        # Find all matching keys
        search = player_name.lower().strip()
        matching_keys = {k: v for k, v in rankings.items() if search in k}

        # Try the actual lookup
        rank, tour = fetcher.lookup_player_rank(player_name, cache)

        # Check name variants
        name_keys = list(fetcher._all_name_keys(player_name))
        last = fetcher._last_name(player_name)
        lastname_matches = lastname_index.get(last, [])

        return jsonify({
            "player": player_name,
            "lookup_result": {"rank": rank, "tour": tour},
            "name_keys_tried": name_keys,
            "matching_cache_keys": matching_keys,
            "lastname": last,
            "lastname_index_matches": lastname_matches,
            "cache_count": cache.get("count", 0),
            "cache_fetched_at": cache.get("fetched_at"),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()})


@app.route("/admin/full-refresh", methods=["POST"])
@enable_cors
def admin_full_refresh():
    """Full pipeline refresh triggered from admin panel (uses admin cookie, no CRON_SECRET needed)."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    # Delegate to the main refresh logic by setting is_admin flag
    # We reuse the same code path as /api/refresh
    import time
    t0 = time.time()
    results = {}
    logger.info("=" * 50)
    logger.info("ADMIN FULL REFRESH: Pipeline starting")
    logger.info("=" * 50)

    steps = [
        ("rankings", ["python3", str(BASE_DIR / "09_rankings_fetcher.py"), "--refresh"], 60),
        ("card", ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "300"], 300),
        ("dedup", ["python3", str(BASE_DIR / "05_bet_logger.py"), "dedup"], 30),
    ]

    for name, cmd, timeout in steps:
        try:
            logger.info(f"  Running {name}...")
            r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout)
            # Include BOTH stdout and stderr in results for debugging
            out = (r.stdout or "")[-500:]
            err = (r.stderr or "")[-500:]
            combined = out
            if err:
                combined += "\n--- STDERR ---\n" + err
            results[name] = {"status": "ok" if r.returncode == 0 else "error", "output": combined[-800:]}
            if r.returncode != 0:
                logger.warning(f"  {name} failed: {r.stderr[-200:]}")
                # Retry card gen at lower volume
                if name == "card":
                    r2 = subprocess.run(
                        ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "100"],
                        cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
                    )
                    out2 = (r2.stdout or "")[-500:]
                    err2 = (r2.stderr or "")[-500:]
                    combined2 = out2
                    if err2:
                        combined2 += "\n--- STDERR ---\n" + err2
                    results[name] = {"status": "ok" if r2.returncode == 0 else "error", "output": combined2[-800:]}
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}

    # Dashboard rebuild
    try:
        dash_script = BASE_DIR / "07_dashboard.py"
        if dash_script.exists():
            r = subprocess.run(["python3", str(dash_script)], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60)
            results["dashboard"] = {"status": "ok" if r.returncode == 0 else "error", "output": (r.stdout or "")[-200:]}
    except Exception as e:
        results["dashboard"] = {"status": "error", "error": str(e)}

    elapsed = time.time() - t0
    ok_count = sum(1 for v in results.values() if v.get("status") == "ok")

    return jsonify({
        "status": "ok" if ok_count == len(results) else "partial",
        "steps": results,
        "elapsed_seconds": round(elapsed, 1),
    }), 200


@app.route("/api/refresh", methods=["POST"])
@enable_cors
def api_refresh():
    """
    Full pipeline refresh — called by the cron job every 6 hours.
    Runs: rankings fetch → card generation → outcome resolution → dashboard rebuild.
    Secured with CRON_SECRET to prevent unauthorized triggers.
    """
    # Authenticate: accept via header or JSON body
    secret = request.headers.get("X-Cron-Secret") or (request.json or {}).get("secret")
    is_admin = check_admin_cookie()

    if secret != CRON_SECRET and not is_admin:
        logger.warning("Unauthorized /api/refresh attempt")
        return jsonify({"error": "Unauthorized"}), 401

    import time
    t0 = time.time()
    results = {}
    logger.info("=" * 50)
    logger.info("API REFRESH: Full pipeline starting")
    logger.info("=" * 50)

    # Step 1: Fetch live rankings
    try:
        logger.info("[1/4] Fetching live rankings...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "09_rankings_fetcher.py"), "--refresh"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60
        )
        results["rankings"] = {
            "status": "ok" if r.returncode == 0 else "error",
            "output": (r.stdout or "")[-200:],
        }
        if r.returncode != 0:
            logger.warning(f"Rankings fetch failed: {r.stderr[-200:]}")
    except Exception as e:
        results["rankings"] = {"status": "error", "error": str(e)}
        logger.warning(f"Rankings fetch exception: {e}")

    # Step 2: Generate fresh betting card
    try:
        logger.info("[2/5] Generating betting card...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "300"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            logger.info("  Card gen failed at $300, trying $100...")
            r = subprocess.run(
                ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "100"],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
            )
        results["card"] = {
            "status": "ok" if r.returncode == 0 else "error",
            "output": (r.stdout or "")[-300:],
        }
        if r.returncode != 0:
            logger.error(f"Card generation failed: {r.stderr[-300:]}")
    except Exception as e:
        results["card"] = {"status": "error", "error": str(e)}
        logger.error(f"Card generation exception: {e}")

    # Step 3a: Deduplicate picks (remove duplicates from previous cron runs)
    try:
        logger.info("[3a/5] Deduplicating picks...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "05_bet_logger.py"), "dedup"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30
        )
        results["dedup"] = {
            "status": "ok" if r.returncode == 0 else "error",
            "output": (r.stdout or "")[-200:],
        }
        if r.returncode != 0:
            logger.warning(f"Dedup failed: {r.stderr[-200:]}")
    except Exception as e:
        results["dedup"] = {"status": "error", "error": str(e)}
        logger.warning(f"Dedup exception: {e}")

    # Step 3b: Resolve outcomes
    try:
        logger.info("[3b/5] Resolving outcomes...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "08_outcome_resolver.py")],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=180
        )
        results["resolve"] = {
            "status": "ok" if r.returncode == 0 else "error",
            "output": (r.stdout or "")[-300:],
        }
        if r.returncode != 0:
            logger.error(f"Outcome resolution failed: {r.stderr[-300:]}")
    except Exception as e:
        results["resolve"] = {"status": "error", "error": str(e)}
        logger.error(f"Outcome resolution exception: {e}")

    # Step 4: Rebuild dashboard
    try:
        logger.info("[4/4] Rebuilding dashboard...")
        dash_script = BASE_DIR / "07_dashboard.py"
        if dash_script.exists():
            r = subprocess.run(
                ["python3", str(dash_script)],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60
            )
            results["dashboard"] = {
                "status": "ok" if r.returncode == 0 else "error",
                "output": (r.stdout or "")[-200:],
            }
        else:
            results["dashboard"] = {"status": "skipped", "reason": "07_dashboard.py not found"}
    except Exception as e:
        results["dashboard"] = {"status": "error", "error": str(e)}

    elapsed = time.time() - t0
    ok_count = sum(1 for v in results.values() if v.get("status") == "ok")
    total = len(results)

    logger.info(f"API REFRESH complete: {ok_count}/{total} steps succeeded in {elapsed:.1f}s")
    logger.info("=" * 50)

    return jsonify({
        "status": "ok" if ok_count == total else "partial",
        "steps": results,
        "elapsed_seconds": round(elapsed, 1),
        "succeeded": ok_count,
        "total": total,
    }), 200


@app.route("/health", methods=["GET"])
def health():
    """Simple health check for Render."""
    return jsonify({"status": "ok"}), 200


# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({"error": "Internal server error"}), 500


@app.after_request
def after_request(response):
    """Add CORS headers to all responses."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"

    logger.info(f"Starting Tennis Betting Signal Server on port {port}")
    logger.info(f"Base directory: {BASE_DIR}")
    logger.info(f"Debug mode: {debug}")

    app.run(host="0.0.0.0", port=port, debug=debug)
