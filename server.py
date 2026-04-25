#!/usr/bin/env python3
"""
Flask web server for Tennis Betting Signal System
Serves dashboard, betting cards, API endpoints, health checks,
and time-limited share links.
Works with gunicorn for production deployment on Render.
"""

import os
import sys
import json
import glob
import re
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
MODELS_DIR = BASE_DIR / "models"
DASHBOARD_TEMPLATE = BASE_DIR / "dashboard.html"

# ── PERSISTENT STORAGE ──
# Use /data on Render (persistent disk), fall back to repo-relative for local dev
PERSISTENT_DIR = Path("/data")
if PERSISTENT_DIR.exists() and PERSISTENT_DIR.is_dir():
    LOGS_DIR = PERSISTENT_DIR / "logs"
    DATA_DIR = PERSISTENT_DIR / "data"
    _USING_PERSISTENT_DISK = True
else:
    LOGS_DIR = BASE_DIR / "logs"
    DATA_DIR = BASE_DIR / "data"
    _USING_PERSISTENT_DISK = False

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

PICKS_FILE = LOGS_DIR / "picks.jsonl"
BETS_FILE = LOGS_DIR / "my_bets.json"
SHARES_FILE = LOGS_DIR / "shares.json"

def _seed_persistent_files():
    """On first boot with persistent disk, copy data files from repo if they don't exist yet."""
    if not _USING_PERSISTENT_DISK:
        return
    seed_map = {
        BASE_DIR / "logs" / "picks.jsonl": PICKS_FILE,
        BASE_DIR / "logs" / "my_bets.json": BETS_FILE,
        BASE_DIR / "logs" / "shares.json": SHARES_FILE,
        BASE_DIR / "data" / "live_rankings.json": DATA_DIR / "live_rankings.json",
        BASE_DIR / "data" / "player_profiles.json": DATA_DIR / "player_profiles.json",
    }
    import shutil
    for src, dst in seed_map.items():
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  [persistent] Seeded {dst} from {src}", file=sys.stderr)
    print(f"  [persistent] Using persistent disk at /data", file=sys.stderr)

_seed_persistent_files()

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
    # Enrich picks missing serve data from parquet files
    if picks:
        picks = _enrich_serve_data(picks)
    return picks


# ── Serve data enrichment cache ──
_serve_cache = {}  # player_name -> serve stats dict
_serve_df = None   # lazy-loaded historical dataframe

def _load_hist_df():
    """Lazy-load and merge historical data for serve stats."""
    global _serve_df
    if _serve_df is not None:
        return _serve_df
    try:
        import pandas as pd
        frames = []
        sack_path = BASE_DIR / "data" / "raw" / "matches_combined.parquet"
        tml_path = BASE_DIR / "data" / "tml_history_10y.parquet"
        if sack_path.exists():
            df_s = pd.read_parquet(sack_path)
            df_s["date"] = pd.to_datetime(df_s["date"], errors="coerce")
            frames.append(df_s)
        if tml_path.exists():
            df_t = pd.read_parquet(tml_path)
            rename = {"winner_name": "winner", "loser_name": "loser", "tourney_name": "tournament"}
            df_t = df_t.rename(columns={k: v for k, v in rename.items() if k in df_t.columns})
            if "match_date" in df_t.columns:
                df_t["date"] = pd.to_datetime(df_t["match_date"], errors="coerce")
            elif "tourney_date" in df_t.columns:
                df_t["date"] = pd.to_datetime(df_t["tourney_date"].astype(str), format="%Y%m%d", errors="coerce")
            frames.append(df_t)
        if frames:
            _serve_df = pd.concat(frames, ignore_index=True)
            _serve_df["date"] = pd.to_datetime(_serve_df["date"], errors="coerce")
            logger.info(f"[SERVE] Loaded {len(_serve_df):,} matches for serve enrichment")
        else:
            _serve_df = pd.DataFrame()
            logger.warning("[SERVE] No historical data found for serve enrichment")
    except Exception as e:
        logger.error(f"[SERVE] Error loading hist data: {e}")
        _serve_df = pd.DataFrame()
    return _serve_df


def _compute_serve_for_player(df, player):
    """Compute serve stats for a player from historical data."""
    global _serve_cache
    if player in _serve_cache:
        return _serve_cache[player]

    from datetime import timedelta
    import pandas as pd

    if df is None or df.empty or "w_ace" not in df.columns:
        _serve_cache[player] = {}
        return {}

    wins = df[df["winner"] == player]
    losses = df[df["loser"] == player]

    # Use player's most recent 365 days of data
    all_dates = pd.concat([wins["date"], losses["date"]]).dropna()
    if len(all_dates) == 0:
        _serve_cache[player] = {}
        return {}

    max_date = all_dates.max()
    since = max_date - timedelta(days=365)
    wins = wins[wins["date"] >= since]
    losses = losses[losses["date"] >= since]

    if len(wins) + len(losses) == 0:
        _serve_cache[player] = {}
        return {}

    def _avg(series):
        s = series.dropna()
        return round(float(s.mean()), 1) if len(s) > 0 else None

    def _ratio(num, denom):
        n, d = num.dropna(), denom.dropna()
        if len(n) == 0 or len(d) == 0:
            return None
        return round(float(n.sum() / d.sum() * 100), 1) if d.sum() > 0 else None

    # Aces/match
    vals = [v for v in [_avg(wins["w_ace"]) if "w_ace" in wins.columns else None,
                         _avg(losses["l_ace"]) if "l_ace" in losses.columns else None] if v is not None]
    aces = round(sum(vals) / len(vals), 1) if vals else None

    # DFs/match
    vals = [v for v in [_avg(wins["w_df"]) if "w_df" in wins.columns else None,
                         _avg(losses["l_df"]) if "l_df" in losses.columns else None] if v is not None]
    dfs = round(sum(vals) / len(vals), 1) if vals else None

    # 1st serve %
    first_in = None
    if "w_1stIn" in df.columns and "w_svpt" in df.columns:
        first_in = _ratio(pd.concat([wins.get("w_1stIn", pd.Series()), losses.get("l_1stIn", pd.Series())]),
                          pd.concat([wins.get("w_svpt", pd.Series()), losses.get("l_svpt", pd.Series())]))

    # 1st serve win %
    first_won = None
    if "w_1stWon" in df.columns and "w_1stIn" in df.columns:
        first_won = _ratio(pd.concat([wins.get("w_1stWon", pd.Series()), losses.get("l_1stWon", pd.Series())]),
                           pd.concat([wins.get("w_1stIn", pd.Series()), losses.get("l_1stIn", pd.Series())]))

    # 2nd serve win %
    second_won = None
    if "w_2ndWon" in df.columns and "w_svpt" in df.columns and "w_1stIn" in df.columns:
        all_2nd = pd.concat([wins.get("w_2ndWon", pd.Series()), losses.get("l_2ndWon", pd.Series())])
        all_2nd_att = pd.concat([wins.get("w_svpt", pd.Series()) - wins.get("w_1stIn", pd.Series()),
                                  losses.get("l_svpt", pd.Series()) - losses.get("l_1stIn", pd.Series())])
        second_won = _ratio(all_2nd, all_2nd_att)

    # BP saved %
    bp_saved = None
    if "w_bpSaved" in df.columns and "w_bpFaced" in df.columns:
        bp_saved = _ratio(pd.concat([wins.get("w_bpSaved", pd.Series()), losses.get("l_bpSaved", pd.Series())]),
                          pd.concat([wins.get("w_bpFaced", pd.Series()), losses.get("l_bpFaced", pd.Series())]))

    # BP convert % (opponent's bpSaved / bpFaced, inverted)
    bp_convert = None
    if "w_bpFaced" in df.columns and "w_bpSaved" in df.columns:
        # When this player wins: opponent is loser → l_bpFaced, l_bpSaved
        # When this player loses: opponent is winner → w_bpFaced, w_bpSaved
        opp_faced = pd.concat([wins.get("l_bpFaced", pd.Series()), losses.get("w_bpFaced", pd.Series())])
        opp_saved = pd.concat([wins.get("l_bpSaved", pd.Series()), losses.get("w_bpSaved", pd.Series())])
        opp_faced_clean = opp_faced.dropna()
        opp_saved_clean = opp_saved.dropna()
        if len(opp_faced_clean) > 0 and opp_faced_clean.sum() > 0:
            bp_convert = round(float((opp_faced_clean.sum() - opp_saved_clean.sum()) / opp_faced_clean.sum() * 100), 1)

    result = {
        "aces": aces, "dfs": dfs, "first_in": first_in,
        "first_won": first_won, "second_won": second_won,
        "bp_saved": bp_saved, "bp_convert": bp_convert,
    }
    _serve_cache[player] = result
    return result


def _enrich_serve_data(picks):
    """Backfill serve stats into picks that are missing them."""
    # Quick check: if most picks already have serve data, skip
    missing_count = sum(1 for p in picks if p.get("sa_aces") is None and p.get("sb_aces") is None
                        and p.get("market_type") == "h2h")
    if missing_count == 0:
        return picks

    df = _load_hist_df()
    if df is None or df.empty:
        return picks

    enriched = 0
    for p in picks:
        if p.get("sa_aces") is not None or p.get("sb_aces") is not None:
            continue
        pa = p.get("player_a", "")
        pb = p.get("player_b", "")
        if not pa or not pb or p.get("market_type") != "h2h":
            continue

        sa = _compute_serve_for_player(df, pa)
        sb = _compute_serve_for_player(df, pb)
        if sa or sb:
            p["sa_aces"] = sa.get("aces")
            p["sb_aces"] = sb.get("aces")
            p["sa_dfs"] = sa.get("dfs")
            p["sb_dfs"] = sb.get("dfs")
            p["sa_first_in"] = sa.get("first_in")
            p["sb_first_in"] = sb.get("first_in")
            p["sa_first_won"] = sa.get("first_won")
            p["sb_first_won"] = sb.get("first_won")
            p["sa_second_won"] = sa.get("second_won")
            p["sb_second_won"] = sb.get("second_won")
            p["sa_bp_saved"] = sa.get("bp_saved")
            p["sb_bp_saved"] = sb.get("bp_saved")
            p["sa_bp_convert"] = sa.get("bp_convert")
            p["sb_bp_convert"] = sb.get("bp_convert")
            enriched += 1

    if enriched > 0:
        logger.info(f"[SERVE] Enriched {enriched} picks with serve data from parquet")
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


_dashboard_cache = {"html": None, "ts": 0, "picks_ts": 0}

def _get_cached_dashboard():
    """Return cached dashboard HTML, rebuilding if stale (>30s) or picks changed."""
    import time
    now = time.time()
    picks_mtime = PICKS_FILE.stat().st_mtime if PICKS_FILE.exists() else 0
    tmpl_mtime = DASHBOARD_TEMPLATE.stat().st_mtime if DASHBOARD_TEMPLATE.exists() else 0

    if (_dashboard_cache["html"]
        and now - _dashboard_cache["ts"] < 30
        and picks_mtime == _dashboard_cache["picks_ts"]
        and tmpl_mtime == _dashboard_cache.get("tmpl_ts", 0)):
        return _dashboard_cache["html"], load_picks_jsonl()

    # Rebuild
    with open(DASHBOARD_TEMPLATE, 'r') as f:
        content = f.read()
    picks_data = load_picks_jsonl()
    picks_json = json.dumps(picks_data, separators=(',', ':'))  # compact JSON
    if "window.PICKS_DATA = [];" in content:
        content = content.replace("window.PICKS_DATA = [];", f"window.PICKS_DATA = {picks_json};")
    else:
        content = content.replace("window.PICKS_DATA = []", f"window.PICKS_DATA = {picks_json}")

    _dashboard_cache["html"] = content
    _dashboard_cache["ts"] = now
    _dashboard_cache["picks_ts"] = picks_mtime
    _dashboard_cache["tmpl_ts"] = tmpl_mtime
    return content, picks_data


def serve_dashboard(is_shared=False, share_expires=None):
    """Serve the dashboard HTML with picks data injected."""
    try:
        if DASHBOARD_TEMPLATE.exists():
            if not is_shared:
                # Fast path: serve cached dashboard
                content, _ = _get_cached_dashboard()
                response = make_response(content)
                response.headers["Content-Type"] = "text/html"
                return response

            # Shared view: build from RAW template (not cached version with injected data)
            # This avoids the regex-on-huge-JSON bug that caused fallback to old card HTML
            with open(DASHBOARD_TEMPLATE, 'r') as f:
                content = f.read()
            picks_data = load_picks_jsonl()

            # For shared views: only show Today's Signals, hide everything else
            if is_shared and share_expires:
                # CSS-based hiding — works immediately, no DOM timing issues
                share_css = """
                <style id="shared-view-styles">
                    /* Hide all tabs except Today's Signals and Players */
                    .tab { display: none !important; }
                    .tab.active, .tab[onclick*="signals"], .tab[onclick*="players"] { display: inline-block !important; cursor: pointer; pointer-events: auto; }
                    /* Hide non-signal/non-player panels */
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
                            if (name !== 'signals' && name !== 'players') return;
                            if (window._origSwitchTab) window._origSwitchTab(name);
                        }};
                    }});
                }})();
                </script>
                """
                # Inject banner after <body>
                content = content.replace("<body>", f"<body>\n{expiry_banner}\n<div style='margin-top:40px'>", 1)
                content = content.replace("</body>", "</div>\n</body>", 1)

                # For shared view: include today's signals + resolved history (for SYS WR buckets)
                from datetime import date
                today_str = date.today().isoformat()
                sensitive_keys = ("myOutcome", "myStake", "myOdds", "actual_shares")
                shared_picks = []
                for p in picks_data:
                    logged = p.get("logged_at", "")
                    is_today = logged.startswith(today_str)
                    is_resolved = p.get("outcome") is not None
                    if is_today or is_resolved:
                        clean = {k: v for k, v in p.items() if k not in sensitive_keys}
                        # For resolved historical (not today): only keep fields needed for SYS WR calc
                        if not is_today and is_resolved:
                            clean = {"model_prob": p.get("model_prob"), "outcome": p.get("outcome"),
                                     "logged_at": logged}
                        shared_picks.append(clean)
                shared_json = json.dumps(shared_picks, separators=(',', ':'))
                # Simple string replace on the raw template (has empty PICKS_DATA)
                content = content.replace(
                    "window.PICKS_DATA = [];",
                    f"window.PICKS_DATA = {shared_json};"
                )
                # Fallback if template uses no-semicolon form
                content = content.replace(
                    "window.PICKS_DATA = []",
                    f"window.PICKS_DATA = {shared_json}"
                )

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
body { background: #0f1117; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; overflow: hidden; }
.login-box { background: #1a1d27; border: 1px solid #2d3139; padding: 40px; max-width: 400px; width: 90%; text-align: center; }
h1 { font-size: 16px; color: #d4740a; margin-bottom: 8px; letter-spacing: 0.06em; }
.sub { font-size: 11px; color: #6c757d; margin-bottom: 24px; }
input { width: 100%; background: #0f1117; border: 1px solid #2d3139; color: #e0e0e0; padding: 12px 16px; font-size: 14px; font-family: 'IBM Plex Mono', monospace; margin-bottom: 16px; }
input:focus { outline: none; border-color: #d4740a; }
button { width: 100%; background: #d4740a; color: white; border: none; padding: 12px; font-size: 13px; font-weight: 700; font-family: 'IBM Plex Mono', monospace; cursor: pointer; letter-spacing: 0.04em; }
button:hover { background: #e65100; }
.error { color: #f44336; font-size: 11px; margin-bottom: 12px; }
.powered-by { font-size: 9px; color: #c4a44e; margin-top: 16px; letter-spacing: 0.04em; }
.login-logo { margin-top: 18px; display: flex; flex-direction: column; align-items: center; gap: 6px; }
.login-logo .dog-img { height: 68px; display: block; }
.login-logo .text-img { height: 40px; display: block; }
footer { position: fixed; bottom: 0; left: 0; right: 0; background: #13151d; border-top: 1px solid #2d3139; padding: 8px 20px; display: flex; align-items: center; justify-content: center; gap: 0; }
footer .brand-critter { font-size: 10px; font-weight: 700; letter-spacing: 0.10em; background: linear-gradient(180deg, #e2b968 0%, #d4a04a 30%, #c4873a 60%, #b87333 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; filter: drop-shadow(0 1px 1px rgba(0,0,0,0.5)); }
footer .brand-labs { font-size: 11px; font-weight: 700; letter-spacing: 0.12em; -webkit-text-fill-color: transparent; -webkit-text-stroke: 0.8px #c4873a; filter: drop-shadow(0 1px 1px rgba(0,0,0,0.5)); margin-left: 1px; }
footer .rights { font-size: 9px; color: #5a5f6a; letter-spacing: 0.03em; margin-left: 6px; }
</style>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
</head><body>
<div class="login-box">
    <h1>ATP/WTA TENNIS BETTING SIGNAL SYSTEM</h1>
    <div class="sub">Enter Password for Access!</div>
    {{ERROR}}
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Password" autofocus>
        <button type="submit">ACCESS DASHBOARD</button>
    </form>
    <div class="powered-by">POWERED BY AMORA EDGE</div>
    <div class="login-logo">
        <img class="dog-img" src="/static/critterlabs_dog.png" alt="CritterLabs Dog">
        <img class="text-img" src="/static/critterlabs_text.png" alt="Critter Labs">
    </div>
</div>
<footer>
    <span class="brand-critter">CRITTER</span><span class="brand-labs">LABS</span>
    <span class="rights">&mdash; All Rights Reserved, 2026</span>
</footer>
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
        <button class="gen-btn" style="width:auto; padding:12px 24px; background:#00695c" onclick="trackPeaks()">TRACK PEAK PRICES</button>
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

async function trackPeaks() {
    const box = document.getElementById('actionResult');
    box.style.display = 'block';
    box.style.borderColor = '#00695c';
    box.textContent = 'Tracking peak prices from Polymarket...';
    try {
        const res = await fetch('/api/track-peaks', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            box.style.borderColor = '#1b5e20';
            box.textContent = 'Done!\\n\\n' + (data.output || 'No output');
        } else {
            box.style.borderColor = '#f44336';
            box.textContent = 'Error: ' + (data.error || 'Unknown') + '\\n\\n' + (data.output || '');
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
    box.textContent = 'Starting pipeline (tml_fetch > rankings > card > dedup)...';
    try {
        const res = await fetch('/admin/full-refresh', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'started' || data.status === 'already_running') {
            box.textContent = 'Pipeline running in background... polling for status every 5s';
            // Poll for completion
            const poll = setInterval(async () => {
                try {
                    const sr = await fetch('/admin/pipeline-status');
                    const sd = await sr.json();
                    if (sd.status === 'running') {
                        box.textContent = 'Pipeline still running... (polling every 5s)';
                    } else {
                        clearInterval(poll);
                        box.style.borderColor = sd.status === 'ok' ? '#1b5e20' : '#d4740a';
                        let output = 'Pipeline ' + sd.status + ' (' + (sd.elapsed_seconds || '?') + 's)\\n\\n';
                        if (sd.steps) {
                            for (const [step, info] of Object.entries(sd.steps)) {
                                output += step.toUpperCase() + ': ' + (info.status || '?') + '\\n';
                                if (info.output) output += info.output.trim() + '\\n';
                                output += '\\n';
                            }
                        }
                        box.textContent = output;
                    }
                } catch(pe) {
                    box.textContent = 'Polling error: ' + pe.message + ' (will retry...)';
                }
            }, 5000);
        } else {
            box.style.borderColor = '#d4740a';
            box.textContent = 'Unexpected: ' + JSON.stringify(data);
        }
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
    """Parse a resolved market to extract the winner (or detect void/push)."""
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
    is_void = False
    resolved_at = m.get("resolvedAt") or m.get("resolved_at")
    is_closed = m.get("closed") is True or str(m.get("closed", "")).lower() == "true"

    if outcomes and prices and len(outcomes) == len(prices):
        float_prices = []
        for price in prices:
            try:
                float_prices.append(float(price))
            except (ValueError, TypeError):
                float_prices.append(None)

        for name, p in zip(outcomes, float_prices):
            if p is not None and p >= 0.95:
                winner = str(name)
                break

        # If prices didn't give a clear winner, try the lower threshold (0.80)
        # for markets that are closed/resolved but prices haven't fully settled
        if not winner and (is_closed or resolved_at):
            for name, p in zip(outcomes, float_prices):
                if p is not None and p >= 0.80:
                    winner = str(name)
                    break

    # Fallback: check resolution/resolvedOutcome fields BEFORE void detection
    if not winner and resolved_at:
        winner = m.get("resolution") or m.get("resolvedOutcome")

    # Detect voided/cancelled market: all prices very close to equal (0.50/0.50)
    # Only mark void if market is closed AND no winner found by any method
    # Use tight threshold (< 0.03) to avoid false voids on slow-settling markets
    if not winner and is_closed and outcomes and prices and len(outcomes) == len(prices):
        float_prices = []
        for price in prices:
            try:
                float_prices.append(float(price))
            except (ValueError, TypeError):
                float_prices.append(None)
        if len(float_prices) >= 2 and all(p is not None for p in float_prices):
            max_p = max(float_prices)
            min_p = min(float_prices)
            if max_p - min_p < 0.01:  # 1% — only true voids (0.50/0.50)
                is_void = True

    if not winner and not is_void:
        return None

    return {
        "market_id": market_id,
        "question": question,
        "slug": slug,
        "winner": winner,  # None if voided
        "is_void": is_void,
        "resolved_at": resolved_at or datetime.utcnow().isoformat(),
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


# ─── STATIC ASSETS ────────────────────────────────────────────────────────────

@app.route("/static/<path:filename>", methods=["GET"])
def serve_static(filename):
    """Serve static assets (logo, images, etc.)."""
    static_dir = Path(__file__).parent / "static"
    return send_file(static_dir / filename)


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
    """Check if a Polymarket trade matches a user's bet match.

    Returns which side the trade is on:
    - "same" = trade is on the player the user bet on
    - "opponent" = trade is on the other player in the match
    - False = not this match at all

    This allows tracking both-side trading for full P&L calculation.
    """
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

    # Determine opponent last name
    opp_last = b_last if bet_on_last == a_last else a_last

    # Check if the trade is for this match (both last names in title or slug)
    title_match = (a_last in trade_title and b_last in trade_title)
    slug_match = (a_last in trade_slug and b_last in trade_slug)
    if not title_match and not slug_match:
        return False

    # Determine which side this trade is on
    outcome_last = trade_outcome.strip().split()[-1].lower() if trade_outcome else ""
    if outcome_last == bet_on_last or bet_on_last in trade_outcome:
        return "same"
    if outcome_last == opp_last or opp_last in trade_outcome:
        return "opponent"

    return False


def enrich_bets_with_trades(bets, trades):
    """Match user bets with actual Polymarket trade data.

    Tracks BOTH buys and sells to compute real trade P&L:
    - actual_buy_price (weighted avg buy price in cents)
    - actual_sell_price (weighted avg sell price in cents, 0 if no sells)
    - actual_stake (total USDC spent buying)
    - actual_sell_total (total USDC received from sells)
    - actual_shares_bought / actual_shares_sold / shares_held
    - trade_pnl (realized: sell proceeds - cost basis of sold shares)
    - trade_return (% return on cost basis)
    - trade_result: "win" if return >= 10%, "loss" if < 10%, "open" if still holding
    """
    TRADE_WIN_THRESHOLD = 0.10  # 10% profit = trade win

    enriched = []
    for bet in bets:
        same_trades = []
        opp_trades = []
        for trade in trades:
            side = match_trade_to_bet(trade, bet)
            if side == "same":
                same_trades.append(trade)
            elif side == "opponent":
                opp_trades.append(trade)

        all_trades = same_trades + opp_trades

        if all_trades:
            # --- SAME-SIDE position (the player we bet on) ---
            s_buys = [t for t in same_trades if t.get("side", "BUY") == "BUY"]
            s_sells = [t for t in same_trades if t.get("side") == "SELL"]
            s_buy_usdc = sum(float(t.get("usdcSize", 0) or 0) for t in s_buys)
            s_buy_shares = sum(float(t.get("size", 0) or 0) for t in s_buys)
            s_sell_usdc = sum(float(t.get("usdcSize", 0) or 0) for t in s_sells)
            s_sell_shares = sum(float(t.get("size", 0) or 0) for t in s_sells)
            s_avg_buy = (s_buy_usdc / s_buy_shares * 100) if s_buy_shares > 0 else 0
            s_avg_sell = (s_sell_usdc / s_sell_shares * 100) if s_sell_shares > 0 else 0
            s_held = round(s_buy_shares - s_sell_shares, 4)

            # --- OPPONENT-SIDE position ---
            o_buys = [t for t in opp_trades if t.get("side", "BUY") == "BUY"]
            o_sells = [t for t in opp_trades if t.get("side") == "SELL"]
            o_buy_usdc = sum(float(t.get("usdcSize", 0) or 0) for t in o_buys)
            o_buy_shares = sum(float(t.get("size", 0) or 0) for t in o_buys)
            o_sell_usdc = sum(float(t.get("usdcSize", 0) or 0) for t in o_sells)
            o_sell_shares = sum(float(t.get("size", 0) or 0) for t in o_sells)
            o_held = round(o_buy_shares - o_sell_shares, 4)

            # Total cost across both sides
            total_buy_usdc = s_buy_usdc + o_buy_usdc
            total_sell_usdc = s_sell_usdc + o_sell_usdc
            total_buy_shares = s_buy_shares + o_buy_shares
            total_sell_shares = s_sell_shares + o_sell_shares

            # Display values: show same-side prices as primary, flag if both-side
            bet["actual_buy_price"] = round(s_avg_buy, 1) if s_buy_shares > 0 else round((o_buy_usdc / o_buy_shares * 100) if o_buy_shares > 0 else 0, 1)
            bet["actual_sell_price"] = round(s_avg_sell, 1) if s_sell_shares > 0 else round((o_sell_usdc / o_sell_shares * 100) if o_sell_shares > 0 else 0, 1)
            bet["actual_stake"] = round(total_buy_usdc, 2)
            bet["actual_sell_total"] = round(total_sell_usdc, 2)
            bet["actual_shares"] = round(total_buy_shares, 2)
            bet["actual_shares_sold"] = round(total_sell_shares, 2)
            bet["shares_held"] = round(s_held + o_held, 2)
            bet["trade_count"] = len(all_trades)
            bet["buy_count"] = len(s_buys) + len(o_buys)
            bet["sell_count"] = len(s_sells) + len(o_sells)
            bet["trade_ids"] = [t.get("transactionHash", "")[:12] for t in all_trades[:5]]
            bet["both_sides"] = len(opp_trades) > 0

            # --- Calculate COMBINED P&L across both positions ---
            outcome = bet.get("outcome")

            def _position_pnl(buy_usdc, buy_shares, sell_usdc, sell_shares, held, is_winner):
                """Calculate P&L for a single position (same-side or opponent-side)."""
                if buy_shares <= 0:
                    return 0.0
                if held <= 0.01:
                    # Fully closed out
                    cost = (buy_usdc / buy_shares * sell_shares)
                    return sell_usdc - cost
                elif outcome:
                    # Market resolved — settle remaining shares
                    if is_winner:
                        settle = held * 1.0
                    elif outcome == "void":
                        settle = (buy_usdc / buy_shares) * held
                    else:
                        settle = 0.0
                    return sell_usdc + settle - buy_usdc
                else:
                    # Market still open — realized P&L only
                    if sell_shares > 0:
                        cost = (buy_usdc / buy_shares * sell_shares)
                        return sell_usdc - cost
                    return 0.0

            # Determine if each side is the winner
            s_is_winner = outcome == "win"
            o_is_winner = outcome == "loss"  # if our pick lost, opponent won

            s_pnl = _position_pnl(s_buy_usdc, s_buy_shares, s_sell_usdc, s_sell_shares, s_held, s_is_winner)
            o_pnl = _position_pnl(o_buy_usdc, o_buy_shares, o_sell_usdc, o_sell_shares, o_held, o_is_winner)
            total_pnl = s_pnl + o_pnl

            # Determine trade result
            has_any_sells = (s_sell_shares + o_sell_shares) > 0.01
            has_any_held = (s_held + o_held) > 0.01

            if outcome == "void":
                bet["trade_pnl"] = 0.0
                bet["trade_return"] = 0.0
                bet["trade_result"] = "void"
            elif outcome and has_any_sells:
                # Market resolved and we have sells — full P&L
                trade_return = (total_pnl / total_buy_usdc) if total_buy_usdc > 0 else 0
                bet["trade_pnl"] = round(total_pnl, 2)
                bet["trade_return"] = round(trade_return * 100, 1)
                bet["trade_result"] = "win" if trade_return >= TRADE_WIN_THRESHOLD else "loss"
            elif outcome and not has_any_sells and has_any_held:
                # Market resolved, never sold — settlement only
                trade_return = (total_pnl / total_buy_usdc) if total_buy_usdc > 0 else 0
                bet["trade_pnl"] = round(total_pnl, 2)
                bet["trade_return"] = round(trade_return * 100, 1)
                bet["trade_result"] = "win" if trade_return >= TRADE_WIN_THRESHOLD else "loss"
            elif has_any_sells and not has_any_held:
                # Fully closed out both sides, market may or may not be resolved
                trade_return = (total_pnl / total_buy_usdc) if total_buy_usdc > 0 else 0
                bet["trade_pnl"] = round(total_pnl, 2)
                bet["trade_return"] = round(trade_return * 100, 1)
                bet["trade_result"] = "win" if trade_return >= TRADE_WIN_THRESHOLD else "loss"
            elif has_any_sells and has_any_held and not outcome:
                # Partially sold, market still open
                trade_return = (total_pnl / total_buy_usdc) if total_buy_usdc > 0 else 0
                bet["trade_pnl"] = round(total_pnl, 2)
                bet["trade_return"] = round(trade_return * 100, 1)
                bet["trade_result"] = "partial"
            else:
                bet["trade_result"] = "open"
                bet["trade_pnl"] = None
                bet["trade_return"] = None

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

        # Resolve outcomes from picks data (needed for P&L calc)
        _resolve_bet_outcomes(bet_list)

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


def _resolve_bet_outcomes(bets):
    """Resolve outcome for each bet by cross-referencing with picks data.

    Same logic as the frontend renderMyBets() but done server-side so that
    enrich_bets_with_trades has outcome info for P&L calculations.
    """
    picks = load_picks_jsonl()
    if not picks:
        return bets

    for bet in bets:
        if bet.get("outcome"):
            continue  # Already has outcome, skip

        bet_match = bet.get("match", "")
        bet_on = bet.get("bet_on", "")

        # Strategy 1: exact match name + same bet_on
        match = None
        for p in picks:
            if p.get("match") == bet_match and p.get("bet_on") == bet_on and p.get("outcome"):
                match = p
                break

        # Strategy 2: same match name, any resolved pick
        if not match:
            for p in picks:
                if p.get("match") == bet_match and p.get("outcome"):
                    match = p
                    break

        # Strategy 3: fuzzy match by player last names
        if not match and bet_match:
            parts = re.split(r'\s+vs\.?\s+', bet_match.lower())
            if len(parts) == 2:
                last_a = parts[0].strip().split()[-1]
                last_b = parts[1].strip().split()[-1]
                for p in picks:
                    pm = (p.get("match") or "").lower()
                    if last_a in pm and last_b in pm and p.get("outcome"):
                        match = p
                        break

        if match and match.get("outcome") and match.get("actual_winner"):
            if match["outcome"] == "void":
                bet["outcome"] = "void"
            else:
                bet_on_last = bet_on.split()[-1].lower() if bet_on else ""
                winner_last = match["actual_winner"].split()[-1].lower() if match.get("actual_winner") else ""
                i_won = (bet_on_last == winner_last) or (bet_on.lower() == match["actual_winner"].lower())
                bet["outcome"] = "win" if i_won else "loss"
                bet["actual_winner"] = match.get("actual_winner")

    return bets


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

        # Step 0: Save client bets first, then run full inline resolver
        # (resolves picks via Polymarket API + direct bet resolution for bets without picks)
        save_bets({"bets": client_bets, "last_synced": datetime.utcnow().isoformat()})
        try:
            _run_inline_resolver()
        except Exception as re:
            logger.warning(f"Inline resolver during sync: {re}")

        # Step 1: Re-load bets (now with outcomes from inline resolver)
        resolved_bets = load_bets()
        resolved_list = resolved_bets.get("bets", []) if isinstance(resolved_bets, dict) else resolved_bets

        # Step 1b: Also resolve via picks cross-reference (catches anything resolver missed)
        _resolve_bet_outcomes(resolved_list)

        # Step 2: Fetch real trade data from Polymarket
        trades = fetch_poly_trades()

        # Step 3: Enrich each bet with trade data + P&L
        enriched = enrich_bets_with_trades(resolved_list, trades)

        # Save final enriched bets
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


@app.route("/api/players", methods=["GET"])
@enable_cors
def api_players():
    """Serve pre-computed player profiles from JSON file.
    Profiles are generated by the pipeline (04_betting_card.py)."""
    try:
        profiles_path = DATA_DIR / "player_profiles.json"
        if not profiles_path.exists():
            # Fallback: check repo-relative path
            profiles_path = BASE_DIR / "data" / "player_profiles.json"
        if not profiles_path.exists():
            return jsonify({"players": [], "message": "Player profiles not yet generated. Run the pipeline first."})

        with open(profiles_path, "r") as f:
            data = json.load(f)

        return jsonify(data)

    except Exception as e:
        logger.error(f"Error in /api/players: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-prices", methods=["GET"])
@enable_cors
def api_live_prices():
    """Fetch fresh Polymarket prices for all active signal markets.
    Returns a map of slug → {prices: {outcome: price_cents}, volume}."""
    import requests as req

    try:
        # Read current picks to get slugs
        picks_path = LOGS_DIR / "picks.jsonl"
        if not picks_path.exists():
            picks_path = BASE_DIR / "logs" / "picks.jsonl"

        slugs = set()
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        if picks_path.exists():
            with open(picks_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pick = json.loads(line)
                        # Only fetch prices for today's picks, not all 500+
                        logged = pick.get("logged_at", "")
                        if not logged.startswith(today_str):
                            continue
                        slug = pick.get("slug", "")
                        if slug:
                            slugs.add(slug)
                    except json.JSONDecodeError:
                        continue

        if not slugs:
            return jsonify({"prices": {}, "message": "No active markets"})

        # Limit to 20 slugs max to prevent blocking the single worker for minutes
        slug_list = list(slugs)[:20]
        price_map = {}
        for slug in slug_list:
            try:
                r = req.get(
                    f"https://gamma-api.polymarket.com/events?slug={slug}",
                    timeout=3,
                )
                r.raise_for_status()
                events = r.json()
                if not events:
                    continue
                ev = events[0] if isinstance(events, list) else events
                markets = ev.get("markets", [ev])
                for m in markets:
                    outcomes_raw = m.get("outcomes", "")
                    prices_raw = m.get("outcomePrices", "")
                    try:
                        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
                    except (json.JSONDecodeError, TypeError):
                        outcomes = []
                    try:
                        prices_list = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
                    except (json.JSONDecodeError, TypeError):
                        prices_list = []

                    if outcomes and prices_list and len(outcomes) == len(prices_list):
                        prices = {}
                        for name, price in zip(outcomes, prices_list):
                            try:
                                prices[str(name)] = round(float(price) * 100, 1)
                            except (ValueError, TypeError):
                                pass
                        vol = float(m.get("volume", 0) or 0)
                        mid = m.get("id") or m.get("conditionId", "")
                        q = m.get("question", "")
                        price_map[slug] = {
                            "prices": prices,
                            "volume": vol,
                            "market_id": mid,
                            "question": q,
                        }
            except Exception as e:
                logger.debug(f"Live price fetch failed for {slug}: {e}")
                continue

        return jsonify({"prices": price_map, "count": len(price_map)})

    except Exception as e:
        logger.error(f"Error in /api/live-prices: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/whales", methods=["GET"])
@enable_cors
def api_whales():
    """Fetch whale data for a specific market on-demand.
    Query params: slug (market slug), token_ids (comma-sep CLOB token IDs),
    outcomes (comma-sep outcome names), volume (total market volume)."""
    import requests as req

    try:
        slug = request.args.get("slug", "")
        token_ids_raw = request.args.get("token_ids", "")
        outcomes_raw = request.args.get("outcomes", "")
        volume = float(request.args.get("volume", 0))

        clob_token_ids = [t.strip() for t in token_ids_raw.split(",") if t.strip()]
        outcomes = [o.strip() for o in outcomes_raw.split(",") if o.strip()]

        # If no token IDs provided, try to look them up from the slug
        if not clob_token_ids and slug:
            try:
                r = req.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8)
                r.raise_for_status()
                events = r.json()
                if events:
                    ev = events[0] if isinstance(events, list) else events
                    markets = ev.get("markets", [ev])
                    for m in markets:
                        ids_raw = m.get("clobTokenIds", "")
                        if isinstance(ids_raw, str):
                            try:
                                clob_token_ids = json.loads(ids_raw) if ids_raw else []
                            except (json.JSONDecodeError, TypeError):
                                clob_token_ids = []
                        else:
                            clob_token_ids = ids_raw or []

                        out_raw = m.get("outcomes", "")
                        if isinstance(out_raw, str):
                            try:
                                outcomes = json.loads(out_raw) if out_raw else []
                            except (json.JSONDecodeError, TypeError):
                                outcomes = []
                        else:
                            outcomes = out_raw or []

                        if not volume:
                            volume = float(m.get("volume", 0) or 0)
                        break
            except Exception as e:
                logger.debug(f"Whale slug lookup failed for {slug}: {e}")

        if not clob_token_ids or volume <= 0:
            return jsonify({"whale_data": None, "message": "Missing token IDs or volume"})

        # Import whale function from betting card module
        from importlib import import_module
        bc = import_module("04_betting_card")
        # Temporarily enable whale fetching
        old_skip = bc._SKIP_WHALES
        bc._SKIP_WHALES = False
        try:
            whale_data = bc._fetch_whale_data(clob_token_ids, outcomes, volume)
        finally:
            bc._SKIP_WHALES = old_skip

        return jsonify({"whale_data": whale_data})

    except Exception as e:
        logger.error(f"Error in /api/whales: {e}")
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


def _run_inline_resolver():
    """
    Core outcome resolution logic — reusable by both /resolve route and cron pipeline.
    Returns dict with keys: status, message, output.
    """
    import requests as req
    import time
    t0 = time.time()

    logger.info("Running inline outcome resolution...")
    output_lines = []

    # Load picks
    picks = load_picks_jsonl()
    if not picks:
        return {"status": "success", "message": "No picks found.", "output": "No picks found."}

    try:
        unresolved = [p for p in picks if p.get("outcome") is None]
        output_lines.append(f"Total picks: {len(picks)}, Unresolved: {len(unresolved)}")

        # Only check picks older than 3 hours (most Polymarket tennis markets
        # resolve within 1-2 hours of match completion)
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=3)

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

        output_lines.append(f"Eligible (>3h old): {len(eligible_indices)}")

        # NOTE: Do NOT return early here — even if no picks are eligible,
        # Phase 0 still needs to run to resolve BETS (which are independent of picks)

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

        # ── REVERT incorrectly voided H2H picks (non-futures) ──
        # Previous void detection was too aggressive; re-check these
        n_void_reverted = 0
        for pick in picks:
            if pick.get("outcome") != "void":
                continue
            match_name = pick.get("match", "")
            # Only revert H2H picks (contain " vs "), not futures
            if " vs " in match_name and " — " not in match_name:
                mt = pick.get("market_type", "")
                if mt != "outright":
                    pick.pop("outcome", None)
                    pick.pop("pnl", None)
                    pick.pop("actual_winner", None)
                    pick.pop("resolved_at", None)
                    n_void_reverted += 1
        if n_void_reverted:
            output_lines.append(f"Reverted {n_void_reverted} incorrectly voided H2H picks for re-resolution")

        # ── Phase 0: Resolve USER'S TRACKED BETS first (individual slug lookups) ──
        # The user only has a handful of bets — prioritize resolving them
        new_resolutions = 0
        try:
            bets_data = load_bets()
            bet_list = bets_data.get("bets", []) if isinstance(bets_data, dict) else []

            # Revert incorrectly voided H2H bets for re-resolution
            n_bet_void_reverted = 0
            for bet in bet_list:
                if bet.get("outcome") != "void":
                    continue
                match_name = bet.get("match", "")
                if " vs " in match_name and " — " not in match_name:
                    bet.pop("outcome", None)
                    bet.pop("pnl", None)
                    bet.pop("actual_winner", None)
                    bet.pop("resolved_at", None)
                    n_bet_void_reverted += 1
            if n_bet_void_reverted:
                output_lines.append(f"Reverted {n_bet_void_reverted} incorrectly voided bets for re-resolution")

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
                                q = parsed.get("question", "").lower()
                                parsed["_words"] = set(w for w in q.replace("?", "").replace(",", "").split() if len(w) > 2)
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

                        # Handle voided/cancelled markets
                        if parsed.get("is_void"):
                            pick["outcome"] = "void"
                            pick["pnl"] = 0.0
                            pick["actual_winner"] = "VOID"
                            pick["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                            new_resolutions += 1
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

                # ── Phase 0b: Directly resolve BETS that have no matching pick ──
                # User may have bet on matches the signal generator didn't pick
                # Strategy 1: Match bets by slug from slug_resolved_cache
                # Strategy 2: Match bets against bulk-fetched resolved markets by player names
                # Strategy 3: For bets with poly_link but no cache hit, do individual slug lookups
                bet_resolutions = 0

                def _resolve_bet(bet, parsed):
                    """Apply resolution to a bet. Returns True if resolved."""
                    if parsed.get("is_void"):
                        bet["outcome"] = "void"
                        bet["pnl"] = 0.0
                        bet["actual_winner"] = "VOID"
                        bet["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                        return True

                    winner_raw = parsed.get("winner", "")
                    # Parse player names from bet match string
                    match_str = bet.get("match", "")
                    parts = re.split(r'\s+vs\.?\s+', match_str)
                    if len(parts) == 2:
                        pa = parts[0].strip()
                        pbb = parts[1].strip()
                    else:
                        pa = bet.get("player_a", "")
                        pbb = bet.get("player_b", "")

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
                        return False

                    bet_on = bet.get("bet_on", "")
                    bl = bet_on.split()[-1].lower() if bet_on else ""
                    wnl = winner_name.split()[-1].lower() if winner_name else ""
                    bet_won = (bl == wnl) or (bet_on.lower() == winner_name.lower())

                    poly_price = bet.get("poly_price", 50)
                    price_dec = poly_price / 100
                    if bet_won:
                        bet["outcome"] = "win"
                        bet["pnl"] = round(100 * (1 - price_dec), 2)
                    else:
                        bet["outcome"] = "loss"
                        bet["pnl"] = round(-100 * price_dec, 2)
                    bet["actual_winner"] = winner_name
                    bet["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                    return True

                unresolved_bet_slugs = []  # For Strategy 3
                for bet in bet_list:
                    if bet.get("outcome") is not None:
                        continue

                    # Strategy 1: slug from poly_link
                    bslug = ""
                    bpl = bet.get("poly_link", "")
                    if "/event/" in bpl:
                        bslug = bpl.split("/event/")[-1].split("?")[0].split("/")[0]
                    if bslug and bslug in slug_resolved_cache:
                        if _resolve_bet(bet, slug_resolved_cache[bslug]):
                            bet_resolutions += 1
                            continue

                    # Strategy 2: Match against bulk-fetched resolved markets by player names
                    match_str = bet.get("match", "")
                    parts = re.split(r'\s+vs\.?\s+', match_str)
                    if len(parts) == 2:
                        a_last = parts[0].strip().split()[-1].lower()
                        b_last = parts[1].strip().split()[-1].lower()
                        if len(a_last) > 2 and len(b_last) > 2:
                            for rm in unique_resolved:
                                rm_words = rm.get("_words", set())
                                if a_last in rm_words and b_last in rm_words:
                                    if _resolve_bet(bet, rm):
                                        bet_resolutions += 1
                                        break

                    if bet.get("outcome") is not None:
                        continue

                    # Collect slug for Strategy 3 (individual lookups)
                    if bslug and bslug not in slug_resolved_cache:
                        unresolved_bet_slugs.append((bet, bslug))

                # Strategy 3: Individual slug lookups for remaining bets
                for bet, bslug in unresolved_bet_slugs[:20]:
                    if bet.get("outcome") is not None:
                        continue
                    try:
                        r = req.get(f"https://gamma-api.polymarket.com/events?slug={bslug}", timeout=8)
                        r.raise_for_status()
                        events = r.json()
                        if not events:
                            continue
                        ev = events[0] if isinstance(events, list) else events
                        markets = ev.get("markets", [])
                        for m in (markets if markets else [ev]):
                            parsed = _parse_resolved_market(m)
                            if parsed:
                                if _resolve_bet(bet, parsed):
                                    bet_resolutions += 1
                                break
                    except Exception:
                        pass

                if bet_resolutions > 0 or n_bet_void_reverted > 0:
                    if isinstance(bets_data, dict):
                        bets_data["bets"] = bet_list
                    save_bets(bets_data)

                output_lines.append(f"Phase 0: Resolved {new_resolutions} picks + {bet_resolutions} bets from tracked bet markets")
        except Exception as e:
            output_lines.append(f"  [warn] Phase 0: {e}")

        # ── Phase 0c: Resolve picks that have Polymarket slugs via direct API lookup ──
        # Picks generated by the signal system have slugs, but Phase 0 only used bet slugs.
        # This phase does individual slug lookups for unresolved picks with slugs.
        phase0c_resolved = 0
        try:
            slugs_already_fetched = set()  # avoid duplicate API calls
            for i, pick in enumerate(picks):
                if i not in eligible_indices or pick.get("outcome") is not None:
                    continue
                pslug = pick.get("slug", "")
                if not pslug or pslug in slugs_already_fetched:
                    continue
                # Skip futures/outright picks
                mt = pick.get("market_type", "")
                match_name = pick.get("match", "")
                if (mt == "outright" or " — " in match_name
                    or any(kw in match_name.lower() for kw in FUTURES_KW)):
                    continue
                slugs_already_fetched.add(pslug)
                try:
                    r = req.get(f"https://gamma-api.polymarket.com/events?slug={pslug}", timeout=8)
                    r.raise_for_status()
                    events = r.json()
                    if not events:
                        continue
                    ev = events[0] if isinstance(events, list) else events
                    markets = ev.get("markets", [])
                    parsed = None
                    for m in (markets if markets else [ev]):
                        parsed = _parse_resolved_market(m)
                        if parsed:
                            break
                    if not parsed:
                        continue
                    # Also add to unique_resolved for Phase 1 matching
                    q = parsed.get("question", "").lower()
                    parsed["_words"] = set(w for w in q.replace("?", "").replace(",", "").split() if len(w) > 2)
                    unique_resolved.append(parsed)
                    # Resolve ALL picks with this slug
                    for pick2 in picks:
                        if pick2.get("outcome") is not None:
                            continue
                        if pick2.get("slug", "") != pslug:
                            continue
                        # Handle voided
                        if parsed.get("is_void"):
                            pick2["outcome"] = "void"
                            pick2["pnl"] = 0.0
                            pick2["actual_winner"] = "VOID"
                            pick2["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                            phase0c_resolved += 1
                            continue
                        winner_raw = parsed.get("winner", "")
                        pa = pick2.get("player_a", "")
                        pbb = pick2.get("player_b", "")
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
                        bet_on = pick2.get("bet_on", "")
                        bl = bet_on.split()[-1].lower() if bet_on else ""
                        wnl = winner_name.split()[-1].lower() if winner_name else ""
                        bet_won = (bl == wnl) or (bet_on.lower() == winner_name.lower())
                        poly_price = pick2.get("poly_price", 50)
                        price_dec = poly_price / 100
                        if bet_won:
                            pick2["outcome"] = "win"
                            pick2["pnl"] = round(100 * (1 - price_dec), 2)
                        else:
                            pick2["outcome"] = "loss"
                            pick2["pnl"] = round(-100 * price_dec, 2)
                        pick2["actual_winner"] = winner_name
                        pick2["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                        phase0c_resolved += 1
                except Exception:
                    pass
            new_resolutions += phase0c_resolved
            if phase0c_resolved:
                output_lines.append(f"Phase 0c: Resolved {phase0c_resolved} picks via direct slug lookup")
        except Exception as e:
            output_lines.append(f"  [warn] Phase 0c: {e}")

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
                if a_last in candidate.get("_words", set()) and b_last in candidate.get("_words", set()):
                    rm = candidate
                    break

            if not rm:
                continue

            # Handle voided/cancelled markets
            if rm.get("is_void"):
                pick["outcome"] = "void"
                pick["pnl"] = 0.0
                pick["actual_winner"] = "VOID"
                pick["resolved_at"] = rm.get("resolved_at", datetime.utcnow().isoformat())
                new_resolutions += 1
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

                # Handle voided/cancelled markets
                if parsed.get("is_void"):
                    pick["outcome"] = "void"
                    pick["pnl"] = 0.0
                    pick["actual_winner"] = "VOID"
                    pick["resolved_at"] = parsed.get("resolved_at", datetime.utcnow().isoformat())
                    new_resolutions += 1
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
        if new_resolutions > 0 or n_reverted > 0 or n_void_reverted > 0:
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

        return {
            "status": "success",
            "message": f"Resolved {new_resolutions} picks",
            "output": "\n".join(output_lines)
        }

    except Exception as e:
        logger.error(f"Resolution error: {e}")
        return {"status": "error", "error": str(e)}


@app.route("/resolve", methods=["POST"])
@enable_cors
def resolve_outcomes_route():
    """Manually trigger outcome resolution — fast last-name matching."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    result = _run_inline_resolver()
    status_code = 200 if result.get("status") == "success" else 500
    return jsonify(result), status_code


@app.route("/api/serve-check", methods=["GET"])
@enable_cors
def api_serve_check():
    """Debug endpoint: check how many picks have serve data."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401
    picks = load_picks_jsonl()
    total = len(picks)
    with_serve = [p for p in picks if p.get("sa_aces") is not None or p.get("sb_aces") is not None]
    without_serve = total - len(with_serve)
    # Sample a few picks with/without serve data
    sample_with = []
    for p in with_serve[:3]:
        sample_with.append({
            "match": p.get("match", ""),
            "sa_aces": p.get("sa_aces"),
            "sb_aces": p.get("sb_aces"),
            "sa_first_in": p.get("sa_first_in"),
        })
    sample_without = []
    for p in picks:
        if p.get("sa_aces") is None and p.get("sb_aces") is None:
            sample_without.append({
                "match": p.get("match", ""),
                "logged_at": p.get("logged_at", ""),
                "outcome": p.get("outcome"),
            })
            if len(sample_without) >= 3:
                break
    # Check TML parquet
    tml_path = BASE_DIR / "data" / "tml_history_10y.parquet"
    tml_info = {"exists": tml_path.exists()}
    if tml_path.exists():
        import os
        tml_info["size_mb"] = round(os.path.getsize(tml_path) / 1024 / 1024, 1)
    return jsonify({
        "total_picks": total,
        "with_serve_data": len(with_serve),
        "without_serve_data": without_serve,
        "sample_with": sample_with,
        "sample_without": sample_without,
        "tml_parquet": tml_info,
    })


@app.route("/api/track-peaks", methods=["POST"])
@enable_cors
def api_track_peaks():
    """Trigger peak price tracking from Polymarket price history."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        logger.info("Admin triggered peak price tracking...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "10_peak_tracker.py")],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
        )
        output = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            return jsonify({"status": "success", "output": output})
        else:
            return jsonify({"status": "error", "output": output}), 500
    except Exception as e:
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


_pipeline_status = {"running": False, "result": None}

def _run_pipeline_background():
    """Run the full pipeline in a background thread."""
    import time
    global _pipeline_status
    _pipeline_status["running"] = True
    _pipeline_status["result"] = None
    # Clear serve cache so fresh data is computed after pipeline
    global _serve_cache, _serve_df
    _serve_cache = {}
    _serve_df = None
    t0 = time.time()
    results = {}
    logger.info("=" * 50)
    logger.info("ADMIN FULL REFRESH: Pipeline starting (background)")
    logger.info("=" * 50)

    steps = [
        ("tml_fetch", ["python3", str(BASE_DIR / "12_tml_live_fetch.py"), "--recent"], 120),
        ("rankings", ["python3", str(BASE_DIR / "09_rankings_fetcher.py"), "--refresh"], 60),
        ("card", ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "300", "--skip-whales"], 300),
        ("dedup", ["python3", str(BASE_DIR / "05_bet_logger.py"), "dedup"], 30),
    ]

    for name, cmd, timeout in steps:
        try:
            logger.info(f"  Running {name}...")
            r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout)
            # Capture more output for card step (serve data diagnostics appear at end)
            cap = 4000 if name == "card" else 500
            out = (r.stdout or "")[-cap:]
            err = (r.stderr or "")[-500:]
            combined = out
            if err:
                combined += "\n--- STDERR ---\n" + err
            if r.returncode != 0 and not combined.strip():
                combined = f"[Process exited with code {r.returncode}, no output captured — possible segfault or OOM]"
            results[name] = {"status": "ok" if r.returncode == 0 else "error", "output": combined[-4500:]}
            if r.returncode != 0:
                logger.warning(f"  {name} failed: {r.stderr[-200:]}")
                if name == "card":
                    r2 = subprocess.run(
                        ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "100", "--skip-whales"],
                        cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
                    )
                    out2 = (r2.stdout or "")[-4000:]
                    err2 = (r2.stderr or "")[-500:]
                    combined2 = out2
                    if err2:
                        combined2 += "\n--- STDERR ---\n" + err2
                    results[name] = {"status": "ok" if r2.returncode == 0 else "error", "output": combined2[-800:]}
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}

    # ── Resolve outcomes (inline — same logic as RESOLVE OUTCOMES NOW button) ──
    try:
        logger.info("  Running outcome resolution...")
        resolve_result = _run_inline_resolver()
        results["resolve"] = {
            "status": "ok" if resolve_result.get("status") == "success" else "error",
            "output": resolve_result.get("output", "")[-400:],
        }
        logger.info(f"  Resolver: {resolve_result.get('message', '')[:100]}")
    except Exception as e:
        results["resolve"] = {"status": "error", "error": str(e)}
        logger.error(f"  Outcome resolution exception: {e}")

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
    _pipeline_status["result"] = {
        "status": "ok" if ok_count == len(results) else "partial",
        "steps": results,
        "elapsed_seconds": round(elapsed, 1),
        "succeeded": ok_count,
        "total": len(results),
    }
    _pipeline_status["running"] = False
    logger.info(f"ADMIN FULL REFRESH: Done in {elapsed:.1f}s — {ok_count}/{len(results)} ok")


@app.route("/admin/full-refresh", methods=["POST"])
@enable_cors
def admin_full_refresh():
    """Full pipeline refresh — runs in background thread, returns immediately."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    if _pipeline_status["running"]:
        return jsonify({"status": "already_running", "message": "Pipeline is already running. Check /admin/pipeline-status for progress."}), 200

    import threading
    t = threading.Thread(target=_run_pipeline_background, daemon=True)
    t.start()

    return jsonify({"status": "started", "message": "Pipeline started in background. Poll /admin/pipeline-status for results."}), 200


@app.route("/admin/pipeline-status", methods=["GET"])
@enable_cors
def admin_pipeline_status():
    """Check background pipeline status."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401
    if _pipeline_status["running"]:
        return jsonify({"status": "running", "message": "Pipeline is still running..."}), 200
    if _pipeline_status["result"]:
        return jsonify(_pipeline_status["result"]), 200
    return jsonify({"status": "idle", "message": "No pipeline has been run yet."}), 200


@app.route("/admin/diag-card", methods=["POST"])
@enable_cors
def admin_diag_card():
    """Diagnostic: try running 04_betting_card.py and capture full output."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = subprocess.run(
            ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "500", "--skip-whales"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120
        )
        return jsonify({
            "returncode": r.returncode,
            "stdout_last_2000": (r.stdout or "")[-2000:],
            "stderr_last_2000": (r.stderr or "")[-2000:],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout after 120s"})
    except Exception as e:
        return jsonify({"error": str(e)})


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

    # Step 0: Fetch fresh TML match data (2016-2026 ATP with serve stats)
    try:
        logger.info("[0/7] Fetching TML live data...")
        tml_script = BASE_DIR / "12_tml_live_fetch.py"
        if tml_script.exists():
            r = subprocess.run(
                ["python3", str(tml_script), "--recent"],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120
            )
            results["tml_fetch"] = {
                "status": "ok" if r.returncode == 0 else "error",
                "output": (r.stdout or "")[-300:],
            }
            if r.returncode != 0:
                logger.warning(f"TML fetch failed: {r.stderr[-200:]}")
        else:
            results["tml_fetch"] = {"status": "skipped", "reason": "12_tml_live_fetch.py not found"}
    except Exception as e:
        results["tml_fetch"] = {"status": "error", "error": str(e)}
        logger.warning(f"TML fetch exception: {e}")

    # Step 1: Fetch live rankings
    try:
        logger.info("[1/7] Fetching live rankings...")
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
            ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "300", "--skip-whales"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            logger.info("  Card gen failed at $300, trying $100...")
            r = subprocess.run(
                ["python3", str(BASE_DIR / "04_betting_card.py"), "--min-volume", "100", "--skip-whales"],
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

    # Step 3b: Resolve outcomes (inline — uses same logic as /resolve admin button)
    try:
        logger.info("[3b/5] Resolving outcomes (inline)...")
        resolve_result = _run_inline_resolver()
        results["resolve"] = {
            "status": "ok" if resolve_result.get("status") == "success" else "error",
            "output": resolve_result.get("output", "")[-300:],
        }
        logger.info(f"  Resolver: {resolve_result.get('message', resolve_result.get('output', '')[:100])}")
    except Exception as e:
        results["resolve"] = {"status": "error", "error": str(e)}
        logger.error(f"Outcome resolution exception: {e}")

    # Step 3c: Track peak prices (Polymarket price history for trade outcomes)
    try:
        logger.info("[3c/6] Tracking peak prices...")
        r = subprocess.run(
            ["python3", str(BASE_DIR / "10_peak_tracker.py")],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
        )
        results["peak_tracker"] = {
            "status": "ok" if r.returncode == 0 else "error",
            "output": (r.stdout or "")[-300:],
        }
        if r.returncode != 0:
            logger.warning(f"Peak tracker failed: {r.stderr[-200:]}")
    except Exception as e:
        results["peak_tracker"] = {"status": "error", "error": str(e)}
        logger.warning(f"Peak tracker exception: {e}")

    # Step 4: Rebuild dashboard
    try:
        logger.info("[4/6] Rebuilding dashboard...")
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


def _auto_resolve_loop():
    """Background thread that auto-resolves bets every 4 hours."""
    import time as _time
    AUTO_RESOLVE_INTERVAL = 4 * 3600  # 4 hours
    _time.sleep(120)  # Wait 2 min after startup before first run
    while True:
        try:
            logger.info("[AUTO-RESOLVE] Running scheduled outcome resolution...")
            result = _run_inline_resolver()
            logger.info(f"[AUTO-RESOLVE] Done: {result.get('message', result.get('output', '')[:200])}")
        except Exception as e:
            logger.warning(f"[AUTO-RESOLVE] Error: {e}")
        _time.sleep(AUTO_RESOLVE_INTERVAL)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"

    logger.info(f"Starting Tennis Betting Signal Server on port {port}")
    logger.info(f"Base directory: {BASE_DIR}")
    logger.info(f"Debug mode: {debug}")

    # Start background auto-resolver (runs every 4 hours)
    import threading
    resolver_thread = threading.Thread(target=_auto_resolve_loop, daemon=True)
    resolver_thread.start()
    logger.info("[AUTO-RESOLVE] Background resolver started (every 4 hours)")

    app.run(host="0.0.0.0", port=port, debug=debug)
