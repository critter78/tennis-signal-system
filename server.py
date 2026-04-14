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
    response.set_cookie("tennis_admin", token, max_age=30*24*3600, httponly=True, samesite="Lax")
    return response


# ─── HELPER FUNCTIONS ─────────────────────────────────────────────────────────

def enable_cors(f):
    """Decorator to enable CORS headers on a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = f(*args, **kwargs)
        if isinstance(response, dict):
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
        if DASHBOARD_TEMPLATE.exists():
            picks_data = load_picks_jsonl()
            with open(DASHBOARD_TEMPLATE, 'r') as f:
                content = f.read()

            picks_json = json.dumps(picks_data, indent=2)
            content = content.replace("window.PICKS_DATA = []", f"window.PICKS_DATA = {picks_json}")

            # For shared views: hide bet buttons and admin features, show expiry banner
            if is_shared and share_expires:
                expiry_banner = f"""
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
                }})();
                </script>
                """
                # Inject banner after <body>
                content = content.replace("<body>", f"<body>\n{expiry_banner}\n<div style='margin-top:40px'>", 1)
                content = content.replace("</body>", "</div>\n</body>", 1)

                # Hide Place Bet buttons in shared view
                content = content.replace(
                    ".sig-bet-btn {",
                    ".shared-hide { display: none !important; }\n        .sig-bet-btn {"
                )

            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
    except Exception as e:
        logger.warning(f"Error generating dashboard: {e}. Falling back to latest card.")

    latest_card = get_latest_betting_card()
    if latest_card:
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

<h1>SHARE MANAGER</h1>
<div class="sub"><a href="/" class="back-link">&larr; Back to Dashboard</a></div>

<div class="section">
    <h2>GENERATE TIME-LIMITED SHARE LINK</h2>
    <div class="duration-grid" id="durGrid">
        <div class="dur-btn" data-min="10" onclick="selectDur(this)">10 min</div>
        <div class="dur-btn" data-min="15" onclick="selectDur(this)">15 min</div>
        <div class="dur-btn" data-min="30" onclick="selectDur(this)">30 min</div>
        <div class="dur-btn" data-min="45" onclick="selectDur(this)">45 min</div>
        <div class="dur-btn" data-min="60" onclick="selectDur(this)">60 min</div>
        <div class="dur-btn" data-min="75" onclick="selectDur(this)">75 min</div>
        <div class="dur-btn" data-min="90" onclick="selectDur(this)">90 min</div>
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
            '<td>' + (isActive ? '<button class="revoke-btn" onclick="revokeShare(\\''+token+'\\')">REVOKE</button>' : '') + '</td>' +
            '</tr>';
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

loadShares();
setInterval(loadShares, 30000);
</script>
</body></html>
"""


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
    allowed = [10, 15, 30, 45, 60, 75, 90]
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
    """Get or save user bets."""
    try:
        if request.method == "GET":
            bets = load_bets()
            return jsonify({"bets": bets})

        elif request.method == "POST":
            data = request.get_json() or {}
            bets = load_bets()
            bets.update(data)

            if save_bets(bets):
                return jsonify({"status": "success", "bets": bets}), 201
            else:
                return jsonify({"status": "error", "message": "Failed to save"}), 500

    except Exception as e:
        logger.error(f"Error in /api/bets: {e}")
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
