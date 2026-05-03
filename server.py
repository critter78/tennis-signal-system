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
import requests as http_requests  # renamed to avoid Flask conflict

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

# Telegram bot — for trade signal alerts with approve/reject buttons
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def telegram_send(text: str, reply_markup: dict = None) -> bool:
    """Send a Telegram message via urllib (avoids requests recursion on Render)."""
    import urllib.request
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        logger.warning(f"[TELEGRAM] Skipped — missing token={bool(TELEGRAM_BOT_TOKEN)} chat_id={bool(TELEGRAM_CHAT_ID)}")
        return False
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            if resp.status == 200:
                logger.info(f"[TELEGRAM] Message sent OK")
                return True
            else:
                logger.warning(f"[TELEGRAM] Send failed {resp.status}: {body[:300]}")
                return False
    except Exception as e:
        logger.warning(f"[TELEGRAM] Send exception: {e}")
        return False


def telegram_edit(message_id: int, text: str) -> bool:
    """Edit a previously sent Telegram message via urllib."""
    import urllib.request
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        return False
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API}/editMessageText",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"[TELEGRAM] Edit failed: {e}")
        return False


def telegram_answer_callback(callback_query_id: str, text: str = "") -> bool:
    """Answer a callback query via urllib."""
    import urllib.request
    if not TELEGRAM_API:
        return False
    try:
        data = json.dumps({"callback_query_id": callback_query_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API}/answerCallbackQuery",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except:
        return False


def telegram_notify_pending(trades: list):
    """Send Telegram alerts for new pending trades with approve/reject buttons."""
    for t in trades:
        pid = t.get("pending_id", "?")
        bet_on = t.get("bet_on", "?")
        match = t.get("match", "?")
        model = t.get("model_prob", 0)
        poly = t.get("poly_price", t.get("entry_price", 0))
        edge = t.get("edge", 0)
        stake = t.get("stake", 5)
        surface = t.get("surface", "")
        tournament = t.get("tournament", "")

        text = (
            f"\U0001f3be <b>POLYMARKET TENNIS BETTING SIGNAL</b> \U0001f3be\n\n"
            f"\U0001f3af <b>{bet_on}</b>\n"
            f"{match}\n"
        )
        if tournament:
            text += f"\U0001f3c6 {tournament}"
            if surface:
                text += f" ({surface})"
            text += "\n"
        text += (
            f"\n"
            f"\U0001f4ca Model: <b>{model}%</b> | Poly: <b>{poly}c</b>\n"
            f"\U0001f4b0 Edge: <b>{edge}%</b> | Stake: <b>${stake}</b>\n"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{pid}"},
                {"text": "❌ Reject", "callback_data": f"reject:{pid}"},
            ]]
        }
        telegram_send(text, reply_markup=keyboard)


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
    """Remove share tokens that expired more than 4 hours ago.
    Keeps recently-expired tokens visible in admin so the user can review them,
    then auto-purges after 4 hours."""
    shares = load_shares()
    now = datetime.utcnow()
    grace_period = timedelta(hours=4)
    # Keep tokens that are either still active OR expired less than 4 hours ago
    kept = {k: v for k, v in shares.items()
            if datetime.fromisoformat(v["expires_at"]) + grace_period > now}
    if len(kept) != len(shares):
        purged = len(shares) - len(kept)
        logger.info(f"[SHARE-CLEANUP] Purged {purged} expired share links (>4h past expiry)")
        save_shares(kept)
    return kept


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
    response.set_cookie("tennis_admin", token, max_age=86400, httponly=True, samesite="Lax")
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


def load_picks_jsonl(enrich=True):
    """Load all picks from picks.jsonl as a list of dicts.

    Args:
        enrich: If True, backfill serve data from parquet (requires pandas ~100MB).
                Set False for lightweight callers (background threads, APIs, comeback radar)
                to avoid OOM on Render 512MB free tier.
    """
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
    # Only when explicitly requested (dashboard render) — pandas import = ~100MB
    if enrich and picks:
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

        picks_count = len(load_picks_jsonl(enrich=False))
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
                    /* Hide mode switcher — shared view is signals only */
                    .mode-switcher { display: none !important; }
                    /* Hide auto-trader app entirely */
                    #autotrader-app { display: none !important; }
                    /* Hide admin/logout links */
                    a[href*="admin"], a[href*="logout"] { display: none !important; }
                    /* Hide Place Bet / I BET THIS buttons (keep v5-bet-selector visible — it shows EDGE PICK/BET info) */
                    .sig-bet-btn, .v5-bet-btn { display: none !important; }
                    /* Make bet-option divs non-clickable and hide personal bet highlighting */
                    .v5-bet-selector .bet-option { pointer-events: none; cursor: default; }
                    .v5-bet-selector .bet-option.active-bet { border-color: inherit; background: inherit; }
                    /* Hide LSTM sections */
                    #lstmProgress, #lstmInsights { display: none !important; }
                    /* Hide header subtitle (picks logged, bets placed, resolved counts) */
                    #headerSub { display: none !important; }
                    /* Hide auto-trade and active bet badges */
                    .active-tag { display: none !important; }
                    /* Only show Today's Signals + Players tabs */
                    .tab[onclick*="mybets"], .tab[onclick*="performance"], .tab[onclick*="overview"], .tab[onclick*="accuracy"] { display: none !important; }
                    #panel-mybets, #panel-performance, #panel-overview, #panel-accuracy { display: none !important; }
                    /* Hide Comeback Radar mode tab */
                    .mode-tab[onclick*="comeback"] { display: none !important; }
                    #comeback-mode { display: none !important; }
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
                        // Only allow Today's Signals + Players
                        var allowed = ['signals', 'players'];
                        window._origSwitchTab = window.switchTab;
                        window.switchTab = function(name) {{
                            if (allowed.indexOf(name) === -1) return;
                            if (window._origSwitchTab) window._origSwitchTab(name);
                        }};
                    }});
                }})();
                </script>
                """
                # Inject banner after <body>
                content = content.replace("<body>", f"<body>\n{expiry_banner}\n<div style='margin-top:40px'>", 1)
                content = content.replace("</body>", "</div>\n</body>", 1)

                # For shared view: include ALL picks (same as main dashboard)
                # but strip personal bet/trade data
                sensitive_keys = ("myOutcome", "myStake", "myOdds", "actual_shares",
                                  "entry_price", "exit_price", "pnl", "shares",
                                  "trade_id", "clob_order_id")
                shared_picks = []
                for p in picks_data:
                    clean = {k: v for k, v in p.items() if k not in sensitive_keys}
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
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <h2 style="margin:0">ACTIVE SHARE LINKS</h2>
        <button class="revoke-btn" style="font-size:10px;padding:6px 14px" onclick="clearExpiredShares()">CLEAR ALL EXPIRED</button>
    </div>
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

async function deleteShare(token) {
    await fetch('/admin/revoke-share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token })
    });
    loadShares();
}

async function clearExpiredShares() {
    await fetch('/admin/clear-expired-shares', { method: 'POST' });
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
            '<td style="display:flex;gap:6px">' + (isActive ? '<button class="copy-btn" style="margin:0;font-size:9px;padding:4px 8px" onclick="copyShareLink(\\''+token+'\\')">COPY LINK</button><button class="revoke-btn" onclick="revokeShare(\\''+token+'\\')">REVOKE</button>' : '<button class="revoke-btn" style="opacity:0.7;font-size:9px;padding:4px 8px" onclick="deleteShare(\\''+token+'\\')">DELETE</button>') + '</td>' +
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
    """List all share tokens (auto-purges links >4h past expiry)."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    # Auto-purge links expired more than 4 hours ago
    cleanup_expired_shares()
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


@app.route("/admin/clear-expired-shares", methods=["POST"])
def admin_clear_expired_shares():
    """Delete all expired share tokens at once."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    shares = load_shares()
    now = datetime.utcnow()
    active = {k: v for k, v in shares.items()
              if datetime.fromisoformat(v["expires_at"]) > now}
    removed = len(shares) - len(active)
    save_shares(active)
    return jsonify({"status": "cleared", "removed": removed})


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
    """Return all picks as JSON array (no serve enrichment — saves ~100MB)."""
    try:
        picks = load_picks_jsonl(enrich=False)
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
    """Fetch user's actual trades from Polymarket Data API.
    Uses requests library (works on Render's proxy — urllib gets 403)."""
    trades = []
    try:
        params = {
            "user": POLY_WALLET,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
            "limit": "200",
        }
        if since_ts:
            params["start"] = str(int(since_ts))

        resp = http_requests.get(
            f"{POLY_DATA_API}/activity",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "close",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

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
    picks = load_picks_jsonl(enrich=False)
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

    _GAMMA_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }
    def _gamma_price_get(url, params=None, timeout=3):
        resp = http_requests.get(url, params=params, headers=_GAMMA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    try:
        # Read current picks to get slugs
        picks_path = LOGS_DIR / "picks.jsonl"
        if not picks_path.exists():
            picks_path = BASE_DIR / "logs" / "picks.jsonl"

        slugs = set()
        # Include picks from last 7 days so visible signals always get fresh prices
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        if picks_path.exists():
            with open(picks_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pick = json.loads(line)
                        logged = pick.get("logged_at", "")
                        if logged and logged[:10] < cutoff:
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
                events = _gamma_price_get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": slug},
                    timeout=3,
                )
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

    _GAMMA_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }
    def _gamma_whale_get(url, params=None, timeout=8):
        resp = http_requests.get(url, params=params, headers=_GAMMA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

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
                events = _gamma_whale_get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=8)
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


def _run_inline_resolver(prefetched_markets=None):
    """
    Core outcome resolution logic — reusable by both /resolve route and cron pipeline.
    prefetched_markets: optional list of raw Gamma API event objects fetched by the browser
                       (bypasses Cloudflare WAF that blocks datacenter IPs).
    Returns dict with keys: status, message, output.
    """
    import time

    _GAMMA_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }

    def _gamma_get(url, params=None, timeout=10):
        """Fetch JSON from Polymarket Gamma API using requests (works on Render's proxy)."""
        resp = http_requests.get(url, params=params, headers=_GAMMA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    t0 = time.time()

    logger.info("Running inline outcome resolution...")
    output_lines = []

    # Load picks (no serve enrichment needed for resolution)
    picks = load_picks_jsonl(enrich=False)
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

        # ── Build resolved markets from pre-fetched data OR server-side API calls ──
        resolved_markets = []

        if prefetched_markets:
            # Browser already fetched from Gamma API (bypasses Cloudflare WAF)
            output_lines.append(f"Using {len(prefetched_markets)} pre-fetched events from browser")
            for ev in prefetched_markets:
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

        # Fallback: try server-side fetch (may fail with 403 if Cloudflare blocks)
        if not prefetched_markets:
            for tag in ["tennis", "atp", "wta"]:
                try:
                    data = _gamma_get(
                        "https://gamma-api.polymarket.com/events",
                        params={"tag_slug": tag, "closed": "true", "limit": "100",
                                "order": "endDate", "ascending": "false"},
                        timeout=10,
                    )
                    for ev in data:
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

        # Build slug → raw event lookup from prefetched data for individual slug phases
        _prefetch_slug_cache = {}
        if prefetched_markets:
            for ev in prefetched_markets:
                s = ev.get("slug", "")
                if s:
                    _prefetch_slug_cache[s] = ev

        def _get_event_by_slug(slug):
            """Get event by slug: use prefetch cache if available, else server-side API."""
            if slug in _prefetch_slug_cache:
                return [_prefetch_slug_cache[slug]]
            # Fallback to API (may 403 on Render)
            return _gamma_get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=8)

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
            # BUT preserve voids that came from actual trade data (trade_result)
            n_bet_void_reverted = 0
            for bet in bet_list:
                if bet.get("outcome") != "void":
                    continue
                # Don't revert if trade_result confirms the void — that's wallet data
                if bet.get("trade_result") in ("void", "refund"):
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
                        events = _get_event_by_slug(slug)
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
                        events = _get_event_by_slug(bslug)
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

                # ── Phase 0d: Propagate trade_result → outcome for unresolved bets ──
                # If wallet monitoring already determined the result but the resolver
                # couldn't match it, trust the wallet data.
                trade_propagated = 0
                for bet in bet_list:
                    if bet.get("outcome") is not None:
                        continue
                    tr = bet.get("trade_result", "")
                    if tr and tr != "open":
                        if tr == "win":
                            bet["outcome"] = "win"
                            if bet.get("trade_pnl") is not None:
                                bet["pnl"] = bet["trade_pnl"]
                        elif tr == "loss":
                            bet["outcome"] = "loss"
                            if bet.get("trade_pnl") is not None:
                                bet["pnl"] = bet["trade_pnl"]
                        elif tr in ("void", "refund", "partial"):
                            bet["outcome"] = "void"
                            bet["pnl"] = 0.0
                        trade_propagated += 1
                if trade_propagated:
                    output_lines.append(f"Phase 0d: Propagated {trade_propagated} trade_result → outcome")

                if bet_resolutions > 0 or n_bet_void_reverted > 0 or trade_propagated > 0:
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
                    events = _get_event_by_slug(pslug)
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
                events = _get_event_by_slug(slug)
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

        # ── Phase 2b: Resolve AUTO-TRADER trades (paper_trades.jsonl) ──
        at_resolutions = 0
        try:
            at_log_path = LOGS_DIR / "paper_trades.jsonl"
            if at_log_path.exists():
                at_trades = []
                with open(at_log_path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            at_trades.append(json.loads(line))

                at_unresolved = [t for t in at_trades if t.get("status") == "confirmed"
                                 and t.get("outcome") is None]
                if at_unresolved:
                    output_lines.append(f"\nPhase 2b: {len(at_unresolved)} unresolved auto-trader trades")

                    for trade in at_unresolved:
                        # Try slug-based resolution first
                        tslug = trade.get("slug", "")
                        if not tslug:
                            tpl = trade.get("poly_link", "")
                            if "/event/" in tpl:
                                tslug = tpl.split("/event/")[-1].split("?")[0].split("/")[0]

                        resolved_parsed = None

                        # Check slug cache from Phase 0
                        if tslug and tslug in slug_resolved_cache:
                            resolved_parsed = slug_resolved_cache[tslug]

                        # Check against bulk-fetched resolved markets by player names
                        if not resolved_parsed:
                            match_str = trade.get("match", "")
                            parts = re.split(r'\s+vs\.?\s+', match_str)
                            if len(parts) == 2:
                                t_a_last = parts[0].strip().split()[-1].lower()
                                t_b_last = parts[1].strip().split()[-1].lower()
                                if len(t_a_last) > 2 and len(t_b_last) > 2:
                                    for rm in unique_resolved:
                                        rm_words = rm.get("_words", set())
                                        if t_a_last in rm_words and t_b_last in rm_words:
                                            resolved_parsed = rm
                                            break

                        # Individual slug lookup if we have a slug but no cache hit
                        if not resolved_parsed and tslug:
                            try:
                                events = _get_event_by_slug(tslug)
                                if events:
                                    ev = events[0] if isinstance(events, list) else events
                                    markets = ev.get("markets", [])
                                    for m in (markets if markets else [ev]):
                                        parsed = _parse_resolved_market(m)
                                        if parsed:
                                            resolved_parsed = parsed
                                            break
                            except Exception:
                                pass

                        if resolved_parsed:
                            # Use _resolve_bet helper — same logic as bets
                            if _resolve_bet(trade, resolved_parsed):
                                # Recalculate PnL based on actual stake, not $100 notional
                                stake = trade.get("actual_stake", trade.get("stake", 0))
                                entry_price = trade.get("actual_buy_price",
                                              trade.get("entry_price",
                                              trade.get("poly_price", 50)))
                                price_dec = entry_price / 100 if entry_price > 1 else entry_price
                                if trade["outcome"] == "win":
                                    trade["pnl"] = round(stake * (1 - price_dec) / price_dec, 2)
                                elif trade["outcome"] == "loss":
                                    trade["pnl"] = round(-stake, 2)
                                # else void: pnl = 0 (already set by _resolve_bet)
                                at_resolutions += 1

                if at_resolutions > 0:
                    with open(at_log_path, "w") as f:
                        for t in at_trades:
                            row = {k: v for k, v in t.items() if k != "_words"}
                            f.write(json.dumps(row) + "\n")
                    at_resolved_all = [t for t in at_trades if t.get("outcome") in ("win", "loss")]
                    at_wins = sum(1 for t in at_resolved_all if t["outcome"] == "win")
                    at_pnl = sum(t.get("pnl", 0) for t in at_resolved_all)
                    output_lines.append(f"Auto-trader: resolved {at_resolutions} trades | "
                                        f"{at_wins}W-{len(at_resolved_all)-at_wins}L | PnL: ${at_pnl:+,.2f}")

        except Exception as e:
            output_lines.append(f"[warn] Auto-trader resolution: {e}")

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


@app.route("/api/resolve-slugs", methods=["GET"])
@enable_cors
def api_resolve_slugs():
    """Return all slugs needed for resolution so the browser can fetch them."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        slugs = set()
        # From picks (no serve enrichment needed for slug resolution)
        picks = load_picks_jsonl(enrich=False)
        for p in picks:
            if p.get("outcome") is not None:
                continue
            s = p.get("slug", "")
            if not s:
                pl = p.get("poly_link", "")
                if "/event/" in pl:
                    s = pl.split("/event/")[-1].split("?")[0].split("/")[0]
            if s:
                slugs.add(s)
        # From bets
        bets_data = load_bets()
        bet_list = bets_data.get("bets", []) if isinstance(bets_data, dict) else []
        for b in bet_list:
            if b.get("outcome") is not None:
                continue
            pl = b.get("poly_link", "")
            if "/event/" in pl:
                s = pl.split("/event/")[-1].split("?")[0].split("/")[0]
                if s:
                    slugs.add(s)
        return jsonify({"slugs": sorted(slugs), "count": len(slugs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/resolve", methods=["POST"])
@enable_cors
def resolve_outcomes_route():
    """Manually trigger outcome resolution — fast last-name matching."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    # Accept pre-fetched market data from browser (bypasses Cloudflare WAF)
    prefetched = None
    try:
        body = request.get_json(silent=True) or {}
        if "markets" in body:
            prefetched = body["markets"]
            logger.info(f"[RESOLVE] Received {len(prefetched)} pre-fetched markets from browser")
    except Exception:
        pass

    result = _run_inline_resolver(prefetched_markets=prefetched)
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

    # Auto-trader scan (semi-auto: queues pending trades for user confirmation)
    try:
        auto_cfg = json.loads(AUTO_TRADER_CONFIG.read_text()) if AUTO_TRADER_CONFIG.exists() else {}
        if auto_cfg.get("mode") in ("semi", "live") and not auto_cfg.get("safety", {}).get("kill_switch"):
            logger.info("  Running auto-trader scan...")
            r = subprocess.run(
                ["python3", str(BASE_DIR / "20_auto_trader.py"), "--scan"],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60
            )
            results["auto_trader"] = {
                "status": "ok" if r.returncode == 0 else "error",
                "output": (r.stdout or "")[-500:],
            }
            logger.info(f"  Auto-trader: {(r.stdout or '')[-100:]}")
        else:
            results["auto_trader"] = {"status": "skipped", "output": "Mode is paper or kill switch on"}
    except Exception as e:
        results["auto_trader"] = {"status": "error", "error": str(e)}
        logger.error(f"  Auto-trader scan exception: {e}")

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
    """Background thread that auto-resolves bets every 1 hour."""
    import time as _time
    AUTO_RESOLVE_INTERVAL = 1 * 3600  # 1 hour (was 4h — too slow for match resolution)
    _time.sleep(120)  # Wait 2 min after startup before first run
    while True:
        try:
            logger.info("[AUTO-RESOLVE] Running scheduled outcome resolution...")
            result = _run_inline_resolver()
            logger.info(f"[AUTO-RESOLVE] Done: {result.get('message', result.get('output', '')[:200])}")
        except Exception as e:
            logger.warning(f"[AUTO-RESOLVE] Error: {e}")
        _time.sleep(AUTO_RESOLVE_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════
# POSITION MONITOR — background thread, checks every 10 minutes
# ═══════════════════════════════════════════════════════════════════════

POSITION_MONITOR_INTERVAL = 10 * 60  # 10 minutes


def _position_monitor_loop():
    """Background thread: monitors open auto-trader positions every 10 minutes.
    Fetches current prices via Gamma API, checks stop-loss/take-profit/trailing-stop,
    queues exits in pending_trades.json, and sends Telegram alerts."""
    import time as _time

    _time.sleep(90)  # Wait 90s after startup before first run

    _GAMMA_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }

    def _gamma_get(url, params=None, timeout=5):
        """Fetch JSON from Polymarket Gamma API using requests (works on Render's proxy)."""
        resp = http_requests.get(url, params=params, headers=_GAMMA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    # Track peak prices across cycles (persists in memory)
    peak_tracker = {}  # key: match|bet_on → peak price in cents

    while True:
        try:
            # Load auto-trader config
            cfg_path = AUTO_TRADER_CONFIG if AUTO_TRADER_CONFIG.exists() else AUTO_TRADER_CONFIG_LOCAL
            if not cfg_path.exists():
                _time.sleep(POSITION_MONITOR_INTERVAL)
                continue
            config = json.loads(cfg_path.read_text())

            mode = config.get("mode", "paper")
            if config.get("safety", {}).get("kill_switch"):
                logger.debug("[POS-MONITOR] Kill switch is on, skipping")
                _time.sleep(POSITION_MONITOR_INTERVAL)
                continue

            # Load open positions from paper_trades.jsonl
            log_path = LOGS_DIR / "paper_trades.jsonl"
            persist_log = Path("/data/logs/paper_trades.jsonl")
            if persist_log.exists():
                log_path = persist_log

            if not log_path.exists():
                _time.sleep(POSITION_MONITOR_INTERVAL)
                continue

            entries = []
            exit_keys = set()
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                        if t.get("action") == "entry":
                            entries.append(t)
                        elif t.get("action") in ("exit", "stop_loss", "take_profit",
                                                  "take_profit_partial", "trailing_stop",
                                                  "market_resolved"):
                            key = t.get("match", "") + "|" + t.get("bet_on", "")
                            exit_keys.add(key)
                    except Exception:
                        continue

            open_positions = [
                e for e in entries
                if (e.get("match", "") + "|" + e.get("bet_on", "")) not in exit_keys
                # Also skip entries that were already exited inline (by exit-report API)
                and not e.get("exit_action")
                and not e.get("exit_at")
                and not e.get("outcome")
            ]

            if not open_positions:
                logger.debug("[POS-MONITOR] No open positions")
                _time.sleep(POSITION_MONITOR_INTERVAL)
                continue

            logger.info(f"[POS-MONITOR] Checking {len(open_positions)} open positions...")

            exit_rules = config.get("exit_rules", {})
            stop_loss_pct = max(exit_rules.get("stop_loss_pct", 15), 5)  # Min 5%
            tp1_price = exit_rules.get("take_profit_1_price", 85)
            tp2_price = exit_rules.get("take_profit_2_price", 95)
            trailing_stop_pct = max(exit_rules.get("trailing_stop_pct", 8), 3)  # Min 3%
            exits_triggered = 0

            for pos in open_positions:
                slug = pos.get("slug", "")
                bet_on = pos.get("bet_on", "")
                match_name = pos.get("match", "?")
                entry_price = pos.get("entry_price", 50)
                stake = pos.get("stake", 100)
                pending_id = pos.get("pending_id", "")
                pos_key = match_name + "|" + bet_on

                if not slug:
                    continue

                # Fetch current price from Gamma API
                try:
                    events = _gamma_get(
                        "https://gamma-api.polymarket.com/events",
                        params={"slug": slug},
                        timeout=5,
                    )
                    if not events:
                        continue
                    ev = events[0] if isinstance(events, list) else events
                    markets = ev.get("markets", [ev])

                    current_price = None
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
                            for name, price in zip(outcomes, prices_list):
                                if str(name).lower().strip() == bet_on.lower().strip():
                                    current_price = round(float(price) * 100, 1)
                                    break

                    if current_price is None:
                        continue

                except Exception as e:
                    logger.debug(f"[POS-MONITOR] Price fetch failed for {slug}: {e}")
                    continue

                # Skip already-resolved markets (price is 0c or 100c)
                # These matches are over — log as resolved, don't treat as stop-loss
                if current_price <= 0.5 or current_price >= 99.5:
                    resolved_outcome = "win" if current_price >= 99.5 else "loss"
                    pnl_pct_r = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                    pnl_usd_r = (current_price - entry_price) / 100 * (stake / (entry_price / 100)) if entry_price > 0 else 0
                    logger.info(f"[POS-MONITOR]   {bet_on[:20]:>20} | RESOLVED ({resolved_outcome}) | {entry_price:.0f}c → {current_price:.0f}c")

                    # Log resolution (not stop-loss)
                    exit_record = {
                        "action": "market_resolved",
                        "mode": mode,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "match": match_name,
                        "bet_on": bet_on,
                        "entry_price": entry_price,
                        "exit_price": current_price,
                        "stake": stake,
                        "pnl_pct": round(pnl_pct_r, 2),
                        "pnl_usd": round(pnl_usd_r, 2),
                        "reason": f"Market resolved: {resolved_outcome}",
                        "pending_id": pending_id,
                        "slug": slug,
                        "source": "render_monitor",
                    }
                    with open(log_path, "a") as f:
                        f.write(json.dumps(exit_record) + "\n")

                    pnl_emoji = "✅" if pnl_usd_r >= 0 else "❌"
                    pnl_sign = "+" if pnl_usd_r >= 0 else ""
                    try:
                        telegram_send(
                            f"{pnl_emoji} MATCH RESOLVED\n\n"
                            f"{bet_on}\n"
                            f"{match_name}\n\n"
                            f"Result: {resolved_outcome.upper()}\n"
                            f"Entry: {entry_price:.0f}c → Final: {current_price:.0f}c\n"
                            f"P&L: {pnl_sign}${abs(pnl_usd_r):.2f} ({pnl_sign}{pnl_pct_r:.1f}%)"
                        )
                    except Exception:
                        pass
                    peak_tracker.pop(pos_key, None)
                    continue

                # Track peak price
                peak = max(current_price, peak_tracker.get(pos_key, entry_price))
                peak_tracker[pos_key] = peak

                # Calculate P&L
                pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                pnl_usd = (current_price - entry_price) / 100 * (stake / (entry_price / 100)) if entry_price > 0 else 0

                # ── Exit Rule Checks ──
                exit_action = None
                exit_reason = None

                # 1. Stop-loss (only for in-play price drops, not resolved markets)
                if pnl_pct <= -stop_loss_pct:
                    exit_action = "stop_loss"
                    exit_reason = f"Stop loss: {pnl_pct:.1f}% (limit: -{stop_loss_pct}%)"

                # 2. Take-profit
                if not exit_action and current_price >= tp1_price:
                    if current_price >= tp2_price:
                        exit_action = "take_profit"
                        exit_reason = f"Take profit T2: {current_price:.0f}c >= {tp2_price}c"
                    else:
                        exit_action = "take_profit"
                        exit_reason = f"Take profit T1: {current_price:.0f}c >= {tp1_price}c"

                # 3. Trailing stop
                if not exit_action and peak > entry_price:
                    drop_from_peak = ((peak - current_price) / peak * 100) if peak > 0 else 0
                    if drop_from_peak >= trailing_stop_pct:
                        exit_action = "trailing_stop"
                        exit_reason = f"Trailing stop: dropped {drop_from_peak:.1f}% from peak {peak:.0f}c"

                pnl_sign = "+" if pnl_pct >= 0 else ""
                logger.info(f"[POS-MONITOR]   {bet_on[:20]:>20} | {current_price:.0f}c (entry {entry_price:.0f}c) | P&L: {pnl_sign}{pnl_pct:.1f}% | Peak: {peak:.0f}c" +
                            (f" | EXIT: {exit_reason}" if exit_action else ""))

                if exit_action:
                    exits_triggered += 1

                    # Log the exit to paper_trades.jsonl
                    exit_record = {
                        "action": exit_action,
                        "mode": mode,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "match": match_name,
                        "bet_on": bet_on,
                        "entry_price": entry_price,
                        "exit_price": current_price,
                        "peak": peak,
                        "stake": stake,
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_usd": round(pnl_usd, 2),
                        "reason": exit_reason,
                        "pending_id": pending_id,
                        "slug": slug,
                        "source": "render_monitor",
                    }
                    with open(log_path, "a") as f:
                        f.write(json.dumps(exit_record) + "\n")

                    # Send Telegram alert
                    pnl_emoji = "✅" if pnl_usd >= 0 else "❌"
                    try:
                        telegram_send(
                            f"{pnl_emoji} POSITION EXIT\n\n"
                            f"{bet_on}\n"
                            f"{match_name}\n\n"
                            f"Status: {exit_action.upper().replace('_', ' ')}\n"
                            f"Entry: {entry_price:.0f}c → Exit: {current_price:.0f}c\n"
                            f"P&L: {pnl_sign}${abs(pnl_usd):.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
                            f"Reason: {exit_reason}\n\n"
                            f"⏱ Detected by 10-min monitor"
                        )
                    except Exception as e:
                        logger.warning(f"[POS-MONITOR] Telegram failed: {e}")

                    # Remove from peak tracker
                    peak_tracker.pop(pos_key, None)

                _time.sleep(0.5)  # Rate limit between API calls

            if exits_triggered:
                logger.info(f"[POS-MONITOR] Triggered {exits_triggered} exit(s)")
            else:
                logger.info(f"[POS-MONITOR] All {len(open_positions)} positions OK")

        except Exception as e:
            logger.warning(f"[POS-MONITOR] Error: {e}")

        _time.sleep(POSITION_MONITOR_INTERVAL)


# ─── Auto-Trader API Endpoints ─────────���────────────────────────────────────

AUTO_TRADER_CONFIG_LOCAL = BASE_DIR / "auto_trader_config.json"
# On Render, persist config to /data so it survives deploys
PERSIST_DIR = Path("/data")
if PERSIST_DIR.exists() and PERSIST_DIR.is_dir():
    AUTO_TRADER_CONFIG = PERSIST_DIR / "auto_trader_config.json"
else:
    AUTO_TRADER_CONFIG = AUTO_TRADER_CONFIG_LOCAL
PENDING_TRADES_FILE = LOGS_DIR / "pending_trades.json"


def _load_auto_config():
    # On first run on Render: copy from repo to persistent storage
    if AUTO_TRADER_CONFIG != AUTO_TRADER_CONFIG_LOCAL and not AUTO_TRADER_CONFIG.exists():
        if AUTO_TRADER_CONFIG_LOCAL.exists():
            AUTO_TRADER_CONFIG.write_text(AUTO_TRADER_CONFIG_LOCAL.read_text())
    if AUTO_TRADER_CONFIG.exists():
        return json.loads(AUTO_TRADER_CONFIG.read_text())
    return {"mode": "semi"}


def _save_auto_config(cfg):
    AUTO_TRADER_CONFIG.write_text(json.dumps(cfg, indent=2))


@app.route("/api/auto-trader/config", methods=["GET", "POST"])
@enable_cors
def auto_trader_config_api():
    """Get or update auto-trader configuration."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "GET":
        cfg = _load_auto_config()
        return jsonify(cfg)

    # POST — update config
    try:
        updates = request.get_json(force=True)
        cfg = _load_auto_config()
        # Deep merge entry_rules, exit_rules, safety
        for section in ("entry_rules", "exit_rules", "safety"):
            if section in updates:
                cfg.setdefault(section, {}).update(updates[section])
                del updates[section]
        cfg.update(updates)
        _save_auto_config(cfg)
        return jsonify({"status": "ok", "config": cfg})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/auto-trader/pending", methods=["GET"])
@enable_cors
def auto_trader_pending():
    """Get pending trades awaiting confirmation."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    if PENDING_TRADES_FILE.exists():
        try:
            trades = json.loads(PENDING_TRADES_FILE.read_text())
            return jsonify({"trades": trades})
        except Exception:
            pass
    return jsonify({"trades": []})


@app.route("/api/auto-trader/confirm", methods=["POST"])
@enable_cors
def auto_trader_confirm():
    """Confirm a pending trade — logs it and removes from queue."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        pending_id = data.get("pending_id")
        if not pending_id:
            return jsonify({"error": "pending_id required"}), 400

        # Load pending
        pending = json.loads(PENDING_TRADES_FILE.read_text()) if PENDING_TRADES_FILE.exists() else []
        confirmed = None
        remaining = []
        for p in pending:
            if p.get("pending_id") == pending_id:
                p["status"] = "confirmed"
                p["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
                confirmed = p
            else:
                remaining.append(p)

        PENDING_TRADES_FILE.write_text(json.dumps(remaining, indent=2))

        if confirmed:
            # Queue for local executor (Render is geo-blocked by Polymarket)
            confirmed["executed"] = False
            confirmed["exec_error"] = "awaiting_local_executor"

            # Log to trade log — local executor will pick this up
            log_path = LOGS_DIR / "paper_trades.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                f.write(json.dumps(confirmed) + "\n")

            logger.info(f"[AUTO-TRADER] Confirmed trade: {confirmed.get('match')} — {confirmed.get('bet_on')} (queued for local executor)")
            return jsonify({"status": "confirmed", "trade": confirmed, "execution": {"awaiting": "local_executor"}})

        return jsonify({"error": "pending_id not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auto-trader/reject", methods=["POST"])
@enable_cors
def auto_trader_reject():
    """Reject a pending trade — remove from queue."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        pending_id = data.get("pending_id")
        if not pending_id:
            return jsonify({"error": "pending_id required"}), 400

        pending = json.loads(PENDING_TRADES_FILE.read_text()) if PENDING_TRADES_FILE.exists() else []
        rejected = None
        remaining = []
        for p in pending:
            if p.get("pending_id") == pending_id:
                rejected = p
            else:
                remaining.append(p)

        PENDING_TRADES_FILE.write_text(json.dumps(remaining, indent=2))

        if rejected:
            logger.info(f"[AUTO-TRADER] Rejected trade: {rejected.get('match')} — {rejected.get('bet_on')}")
            return jsonify({"status": "rejected", "trade": rejected})

        return jsonify({"error": "pending_id not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auto-trader/requeue", methods=["POST"])
@enable_cors
def auto_trader_requeue():
    """Re-queue a trade at a new price (spread was too wide).
    Creates a new pending trade at the adjusted price and sends Telegram notification."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        if not data or not data.get("match"):
            return jsonify({"error": "Trade data required"}), 400

        import uuid
        new_pid = str(uuid.uuid4())[:8]

        # Build the new pending trade at the adjusted price
        trade = {
            "pending_id": new_pid,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "requeued": True,
            "original_price": data.get("original_price"),
            "match": data.get("match"),
            "bet_on": data.get("bet_on"),
            "player_a": data.get("player_a"),
            "player_b": data.get("player_b"),
            "model_prob": data.get("model_prob"),
            "poly_price": data.get("new_price"),  # Updated price
            "entry_price": data.get("new_price"),  # Updated price
            "edge": data.get("edge"),
            "stake": data.get("stake"),
            "token_id": data.get("token_id"),
            "poly_link": data.get("poly_link"),
            "slug": data.get("slug"),
            "tournament": data.get("tournament"),
            "surface": data.get("surface"),
            "market_type": data.get("market_type"),
            "action": "entry",
            "mode": "semi",
            "bet_type": data.get("bet_type", "edge"),
        }

        # Add to pending queue
        pending = json.loads(PENDING_TRADES_FILE.read_text()) if PENDING_TRADES_FILE.exists() else []
        pending.append(trade)
        PENDING_TRADES_FILE.write_text(json.dumps(pending, indent=2))

        # Send Telegram with price change context
        original_p = data.get("original_price", "?")
        new_p = data.get("new_price", "?")
        gap = abs(float(new_p or 0) - float(original_p or 0))

        text = (
            f"⚠️ <b>PRICE MOVED — RE-QUEUED</b>\n\n"
            f"\U0001f3be <b>{trade['bet_on']}</b>\n"
            f"{trade['match']}\n"
        )
        if trade.get("tournament"):
            text += f"\U0001f3c6 {trade['tournament']}"
            if trade.get("surface"):
                text += f" ({trade['surface']})"
            text += "\n"
        text += (
            f"\n"
            f"\U0001f4ca Model: <b>{trade.get('model_prob', '?')}%</b>\n"
            f"\U0001f4b0 Original: <b>{original_p}c</b> → Now: <b>{new_p}c</b> (moved {gap:.0f}c)\n"
            f"Stake: <b>${trade.get('stake', 5)}</b>\n"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve at " + str(new_p) + "c", "callback_data": f"approve:{new_pid}"},
                {"text": "❌ Reject", "callback_data": f"reject:{new_pid}"},
            ]]
        }
        telegram_send(text, reply_markup=keyboard)

        logger.info(f"[AUTO-TRADER] Re-queued trade at new price: {trade['match']} — {trade['bet_on']} @ {new_p}c (was {original_p}c)")
        return jsonify({"status": "requeued", "pending_id": new_pid, "trade": trade})

    except Exception as e:
        logger.error(f"Error in requeue: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Handle Telegram bot webhook — inline button callbacks for approve/reject."""
    try:
        data = request.get_json(force=True)
        callback = data.get("callback_query")
        if not callback:
            return jsonify({"ok": True})  # Ignore non-callback updates

        cb_id = callback.get("id", "")
        cb_data = callback.get("data", "")
        message = callback.get("message", {})
        msg_id = message.get("message_id")
        from_user = callback.get("from", {}).get("first_name", "?")

        logger.info(f"[TELEGRAM] Callback: {cb_data} from {from_user}")

        if ":" not in cb_data:
            telegram_answer_callback(cb_id, "Unknown action")
            return jsonify({"ok": True})

        action, pending_id = cb_data.split(":", 1)

        # Load pending trades
        pending = json.loads(PENDING_TRADES_FILE.read_text()) if PENDING_TRADES_FILE.exists() else []
        trade = None
        for p in pending:
            if p.get("pending_id") == pending_id:
                trade = p
                break

        if not trade:
            telegram_answer_callback(cb_id, "Trade not found (expired?)")
            if msg_id:
                original_text = message.get("text", "")
                telegram_edit(msg_id, original_text + "\n\n(Trade no longer pending)")
            return jsonify({"ok": True})

        bet_on = trade.get("bet_on", "?")
        match_name = trade.get("match", "?")

        if action == "approve":
            telegram_answer_callback(cb_id, "Approving...")

            # Remove from pending queue
            remaining = [p for p in pending if p.get("pending_id") != pending_id]
            trade["status"] = "confirmed"
            trade["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
            trade["executed"] = False  # Awaiting local executor (Render is geo-blocked)
            trade["telegram_msg_id"] = msg_id  # So local executor can update the message
            PENDING_TRADES_FILE.write_text(json.dumps(remaining, indent=2))

            # Log to trade history (local executor will pick this up)
            log_path = LOGS_DIR / "paper_trades.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                f.write(json.dumps(trade) + "\n")

            # Update Telegram message — tell user local executor will handle it
            status_text = f"APPROVED by {from_user}\nAwaiting local executor..."
            if msg_id:
                telegram_edit(msg_id,
                    f"POLYMARKET TENNIS BETTING SIGNAL\n\n"
                    f"{bet_on}\n{match_name}\n\n"
                    f"Status: {status_text}")

            logger.info(f"[TELEGRAM] Approved: {bet_on} ({match_name}) — queued for local executor")

        elif action == "reject":
            telegram_answer_callback(cb_id, "Rejected")

            remaining = [p for p in pending if p.get("pending_id") != pending_id]
            PENDING_TRADES_FILE.write_text(json.dumps(remaining, indent=2))

            if msg_id:
                telegram_edit(msg_id,
                    f"POLYMARKET TENNIS BETTING SIGNAL\n\n"
                    f"{bet_on}\n{match_name}\n\n"
                    f"Status: REJECTED by {from_user}")

            logger.info(f"[TELEGRAM] Rejected: {bet_on} ({match_name})")

        else:
            telegram_answer_callback(cb_id, "Unknown action")

        return jsonify({"ok": True})

    except Exception as e:
        logger.warning(f"[TELEGRAM] Webhook error: {e}")
        return jsonify({"ok": True})  # Always return 200 to Telegram


@app.route("/api/auto-trader/reset", methods=["POST"])
@enable_cors
def auto_trader_reset():
    """Clear all pending trades and trade history for a fresh start."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        # Clear pending trades
        if PENDING_TRADES_FILE.exists():
            PENDING_TRADES_FILE.write_text("[]")

        # Clear trade history
        paper_log = LOGS_DIR / "paper_trades.jsonl"
        if paper_log.exists():
            paper_log.write_text("")

        logger.info("[AUTO-TRADER] Reset: cleared pending trades and trade history")
        return jsonify({"status": "reset", "message": "All pending trades and history cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auto-trader/approved", methods=["GET"])
@enable_cors
def auto_trader_approved():
    """Return confirmed-but-unexecuted trades for the local executor to pick up."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    log_path = LOGS_DIR / "paper_trades.jsonl"
    if not log_path.exists():
        return jsonify({"trades": []})

    trades = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                if t.get("status") == "confirmed" and not t.get("executed"):
                    trades.append(t)
        return jsonify({"trades": trades})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auto-trader/execution-report", methods=["POST"])
@enable_cors
def auto_trader_execution_report():
    """Local executor reports CLOB execution result. Updates trade log + Telegram."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    pending_id = body.get("pending_id")
    success = body.get("success", False)
    order_data = body.get("order")
    error_msg = body.get("error", "")

    if not pending_id:
        return jsonify({"error": "pending_id required"}), 400

    # Update the trade in paper_trades.jsonl
    log_path = LOGS_DIR / "paper_trades.jsonl"
    if not log_path.exists():
        return jsonify({"error": "No trade log found"}), 404

    trades = []
    updated = False
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            trades.append(json.loads(line))

    for t in trades:
        if t.get("pending_id") == pending_id:
            t["executed"] = success
            t["executed_at"] = datetime.utcnow().isoformat() + "Z"
            t["executed_by"] = "local_executor"
            if success:
                t["clob_order"] = order_data
                t.pop("exec_error", None)
            else:
                t["exec_error"] = error_msg
            updated = True

            # Update Telegram message if we have the msg_id
            tg_msg_id = t.get("telegram_msg_id")
            bet_on = t.get("bet_on", "?")
            match_name = t.get("match", "?")
            if tg_msg_id:
                if success:
                    status_text = f"APPROVED\nCLOB order placed by local executor"
                else:
                    status_text = f"APPROVED\nLocal executor failed: {error_msg[:80]}"
                try:
                    telegram_edit(tg_msg_id,
                        f"POLYMARKET TENNIS BETTING SIGNAL\n\n"
                        f"{bet_on}\n{match_name}\n\n"
                        f"Status: {status_text}")
                except Exception as e:
                    logger.warning(f"[EXEC-REPORT] Telegram edit failed: {e}")
            break

    if not updated:
        return jsonify({"error": f"Trade {pending_id} not found"}), 404

    # Rewrite the log
    with open(log_path, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")

    logger.info(f"[EXEC-REPORT] {pending_id}: {'SUCCESS' if success else 'FAILED'}")
    return jsonify({"ok": True, "updated": True})


@app.route("/api/auto-trader/exit-report", methods=["POST"])
@enable_cors
def auto_trader_exit_report():
    """Local executor reports a position exit (stop loss / take profit / trailing stop)."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    pending_id = body.get("pending_id")
    exit_action = body.get("exit_action", "exit")
    exit_price = body.get("exit_price", 0)
    pnl_usd = body.get("pnl_usd", 0)
    pnl_pct = body.get("pnl_pct", 0)
    success = body.get("success", False)

    if not pending_id:
        return jsonify({"error": "pending_id required"}), 400

    log_path = LOGS_DIR / "paper_trades.jsonl"
    if not log_path.exists():
        return jsonify({"error": "No trade log found"}), 404

    trades = []
    updated = False
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))

    for t in trades:
        if t.get("pending_id") == pending_id:
            t["exit_action"] = exit_action
            t["exit_price"] = exit_price
            t["exit_at"] = datetime.utcnow().isoformat() + "Z"
            t["pnl"] = pnl_usd
            t["pnl_pct"] = pnl_pct
            if success:
                t["outcome"] = "win" if pnl_usd >= 0 else "loss"
                t["sell_order"] = body.get("sell_order")
            else:
                t["exit_error"] = "sell_failed"
            updated = True

            # Update Telegram if we have msg_id
            tg_msg_id = t.get("telegram_msg_id")
            if tg_msg_id:
                bet_on = t.get("bet_on", "?")
                match_name = t.get("match", "?")
                pnl_sign = "+" if pnl_usd >= 0 else ""
                try:
                    telegram_edit(tg_msg_id,
                        f"POLYMARKET TENNIS BETTING SIGNAL\n\n"
                        f"{bet_on}\n{match_name}\n\n"
                        f"Status: EXITED ({exit_action})\n"
                        f"Exit price: {exit_price:.0f}c | P&L: {pnl_sign}${pnl_usd:.2f} ({pnl_sign}{pnl_pct:.1f}%)")
                except Exception as e:
                    logger.warning(f"[EXIT-REPORT] Telegram edit failed: {e}")
            break

    if not updated:
        return jsonify({"error": f"Trade {pending_id} not found"}), 404

    with open(log_path, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")

    logger.info(f"[EXIT-REPORT] {pending_id}: {exit_action} | P&L: ${pnl_usd:+.2f}")
    return jsonify({"ok": True, "updated": True})


@app.route("/api/auto-trader/cron-scan", methods=["POST"])
@enable_cors
def auto_trader_cron_scan():
    """Cron-triggered auto-trader scan. Authenticated via CRON_SECRET.
    Runs entry rules, queues pending trades, sends Telegram alerts."""
    secret = request.headers.get("X-Cron-Secret") or (request.get_json(silent=True) or {}).get("secret")
    if secret != CRON_SECRET and not check_admin_cookie():
        logger.warning("Unauthorized /api/auto-trader/cron-scan attempt")
        return jsonify({"error": "Unauthorized"}), 401
    # Delegate to the same logic as the manual scan
    return _do_auto_trader_scan()


@app.route("/api/auto-trader/scan", methods=["POST"])
@enable_cors
def auto_trader_scan():
    """Trigger an auto-trader scan (runs entry rules against today's signals)."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    # Accept browser-fetched token_id map (bypasses Cloudflare WAF on Render)
    body = request.get_json(silent=True) or {}
    token_id_map = body.get("token_id_map")
    if token_id_map and isinstance(token_id_map, dict):
        cache_path = LOGS_DIR / "token_id_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(token_id_map))
        logger.info(f"[AUTO-TRADER] Wrote token_id cache: {len(token_id_map)} markets")

    return _do_auto_trader_scan()


def _do_auto_trader_scan():
    """Shared scan logic: run 20_auto_trader.py, load pending, send Telegram alerts."""
    try:
        import subprocess

        logger.info(f"[AUTO-TRADER] Starting scan subprocess...")
        result = subprocess.run(
            ["python3", str(BASE_DIR / "20_auto_trader.py"), "--scan"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=60
        )
        logger.info(f"[AUTO-TRADER] Scan exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"[AUTO-TRADER] Scan stdout (last 500): {result.stdout[-500:]}")
        if result.stderr:
            logger.warning(f"[AUTO-TRADER] Scan stderr: {result.stderr[-500:]}")

        # Load pending trades to return
        pending = json.loads(PENDING_TRADES_FILE.read_text()) if PENDING_TRADES_FILE.exists() else []
        logger.info(f"[AUTO-TRADER] Pending trades in file: {len(pending)}")

        # Send Telegram alerts only for NEW pending trades (not yet alerted)
        new_trades = [t for t in pending if not t.get("telegram_sent")]
        if new_trades:
            try:
                telegram_notify_pending(new_trades)
                # Mark as alerted so we don't re-send
                for t in pending:
                    if not t.get("telegram_sent"):
                        t["telegram_sent"] = True
                PENDING_TRADES_FILE.write_text(json.dumps(pending, indent=2))
                logger.info(f"[AUTO-TRADER] Sent {len(new_trades)} NEW Telegram alert(s) ({len(pending)} total pending)")
            except Exception as e:
                logger.warning(f"[AUTO-TRADER] Telegram notify failed: {e}")
        else:
            logger.info(f"[AUTO-TRADER] No new pending trades to alert ({len(pending)} already alerted)")

        return jsonify({
            "status": "ok",
            "output": result.stdout[-2000:] if result.stdout else "",
            "pending_count": len(pending),
            "pending": pending,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Scan timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auto-trader/history", methods=["GET"])
@enable_cors
def auto_trader_history():
    """Get recent auto-trader trade history."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    log_path = LOGS_DIR / "paper_trades.jsonl"
    if not log_path.exists():
        return jsonify({"trades": []})

    trades = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
        # Return last 50
        trades = trades[-50:]
        trades.reverse()
        return jsonify({"trades": trades})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════
# COMEBACK RADAR — live in-play edge detection via Poly price drops
# ═══════════════════════════════════════════════════════════════════════

# Fair comeback probabilities by rank tier and set state (from 21,260 ATP matches 2018-2026)
COMEBACK_FAIR_VALUE = {
    # (rank_tier, set_state) → fair probability (0-1)
    # Bo5 states
    ("top10", "0-1"): 0.50, ("top10", "1-2"): 0.35, ("top10", "0-2"): 0.21,
    ("11-30", "0-1"): 0.33, ("11-30", "1-2"): 0.25, ("11-30", "0-2"): 0.09,
    ("31-50", "0-1"): 0.24, ("31-50", "1-2"): 0.21, ("31-50", "0-2"): 0.08,
    ("51-100","0-1"): 0.18, ("51-100","1-2"): 0.18, ("51-100","0-2"): 0.05,
    ("100+",  "0-1"): 0.15, ("100+",  "1-2"): 0.16, ("100+",  "0-2"): 0.04,
    # Bo3 states (only 0-1 possible)
    ("top10", "0-1b3"): 0.32, ("11-30", "0-1b3"): 0.24,
    ("31-50", "0-1b3"): 0.20, ("51-100","0-1b3"): 0.18, ("100+","0-1b3"): 0.14,
}

# Known comeback kings: personal override rates when down 0-1 in Bo5
COMEBACK_KINGS = {
    "novak djokovic": 0.49, "jannik sinner": 0.43, "carlos alcaraz": 0.44,
    "alexander zverev": 0.38, "daniil medvedev": 0.35, "rafael nadal": 0.42,
    "stefanos tsitsipas": 0.31, "andrey rublev": 0.30, "ben shelton": 0.32,
    "matteo berrettini": 0.33, "holger rune": 0.44, "felix auger-aliassime": 0.41,
    "nick kyrgios": 0.35, "dominic thiem": 0.32, "grigor dimitrov": 0.29,
    "lorenzo musetti": 0.46, "nuno borges": 0.36, "cameron norrie": 0.33,
}

# Price snapshot file for tracking drops
_COMEBACK_SNAPSHOT_FILE = LOGS_DIR / "comeback_snapshots.json"
_COMEBACK_SIGNALS_FILE = LOGS_DIR / "comeback_signals.json"

def _rank_tier(rank_val):
    """Convert numeric rank to tier string."""
    try:
        r = int(float(rank_val))
    except (ValueError, TypeError):
        return "100+"
    if r <= 10: return "top10"
    if r <= 30: return "11-30"
    if r <= 50: return "31-50"
    if r <= 100: return "51-100"
    return "100+"

def _get_fair_value(player_name, rank, set_state):
    """Look up fair comeback probability for a player at a given set state."""
    tier = _rank_tier(rank)
    # Check if player is a known comeback king (boost for 0-1 state)
    name_lower = player_name.lower().strip() if player_name else ""
    if name_lower in COMEBACK_KINGS and set_state == "0-1":
        return COMEBACK_KINGS[name_lower]
    return COMEBACK_FAIR_VALUE.get((tier, set_state), 0.15)

def _infer_set_state(prev_price, curr_price, prev_state=None):
    """Infer the trailing player's set state from the magnitude of the price drop.
    Returns (set_state, confidence) or (None, 0) if no significant drop."""
    if prev_price is None or curr_price is None:
        return None, 0
    drop = prev_price - curr_price
    drop_pct = (drop / prev_price * 100) if prev_price > 0 else 0

    # Significant drop thresholds (calibrated to typical set-loss price movements)
    if drop_pct >= 55:
        # Massive drop: likely went from competitive to 0-2 down
        return "0-2", 0.7
    elif drop_pct >= 25:
        # Big drop: lost a set. Check if previously already down
        if prev_state == "0-1":
            return "1-2", 0.8  # was 0-1, now dropped more → 1-2 or 0-2
        elif prev_state == "1-1":
            return "1-2", 0.85
        else:
            return "0-1", 0.8
    elif drop_pct >= 12:
        # Moderate drop: could be losing a close set or break in current set
        if prev_state is None:
            return "0-1", 0.5  # lower confidence
    return None, 0

def _extract_player_names(question):
    """Extract two player names from a Polymarket question string."""
    q = question.strip()
    # Pattern: "Tournament: Player A vs Player B"
    if ":" in q:
        q = q.split(":", 1)[1].strip()
    q = q.replace("?", "").strip()
    parts = q.split(" vs ")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    parts = q.split(" v ")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None, None


@app.route("/api/comeback/signals", methods=["GET"])
@enable_cors
def comeback_signals():
    """Get current comeback radar signals. Called by dashboard every 60s.
    Fetches active tennis markets, detects price drops, calculates edge."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    _GAMMA_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }
    def _gamma_get(url, params=None, timeout=8):
        resp = http_requests.get(url, params=params, headers=_GAMMA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    try:
        # 1. Load previous price snapshots
        snapshots = {}
        if _COMEBACK_SNAPSHOT_FILE.exists():
            try:
                snapshots = json.loads(_COMEBACK_SNAPSHOT_FILE.read_text())
            except Exception:
                snapshots = {}

        # 2. Fetch active tennis markets from Gamma
        active_markets = []
        for tag in ["tennis", "atp", "wta"]:
            try:
                events = _gamma_get(
                    "https://gamma-api.polymarket.com/events",
                    params={"tag_slug": tag, "closed": "false", "active": "true",
                            "limit": "50", "order": "volume", "ascending": "false"},
                    timeout=8,
                )
                for ev in (events if isinstance(events, list) else [events]):
                    markets = ev.get("markets", [ev])
                    for m in markets:
                        active_markets.append({
                            "slug": ev.get("slug", m.get("slug", "")),
                            "question": m.get("question", ""),
                            "market_id": m.get("id", m.get("conditionId", "")),
                            "outcomes": m.get("outcomes", ""),
                            "outcome_prices": m.get("outcomePrices", ""),
                            "volume": float(m.get("volume", 0) or 0),
                            "end_date": m.get("endDate", ev.get("endDate", "")),
                        })
            except Exception as e:
                logger.debug(f"Comeback radar: tag {tag} fetch error: {e}")
                continue

        # 2b. Build a lookup of pre-match prices from picks data (signal cards)
        # This seeds the peak/initial prices so we can detect drops even on first scan
        picks_price_map = {}  # slug → {price_a, price_b, player_a, player_b}
        try:
            picks = load_picks_jsonl(enrich=False)
            for p in picks:
                s = p.get("slug", "")
                if s and p.get("poly_price_a") and p.get("poly_price_b"):
                    picks_price_map[s] = {
                        "price_a": p["poly_price_a"],
                        "price_b": p["poly_price_b"],
                        "player_a": p.get("player_a", ""),
                        "player_b": p.get("player_b", ""),
                    }
        except Exception as e:
            logger.debug(f"Comeback radar: picks lookup failed: {e}")

        # 3. Parse prices and detect drops
        signals = []
        new_snapshots = {}
        now_iso = datetime.now().isoformat()

        for mkt in active_markets:
            slug = mkt["slug"]
            if not slug:
                continue

            # Parse outcomes and prices
            outcomes_raw = mkt["outcomes"]
            prices_raw = mkt["outcome_prices"]
            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
            except (json.JSONDecodeError, TypeError):
                outcomes = []
            try:
                prices_list = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
            except (json.JSONDecodeError, TypeError):
                prices_list = []

            if len(outcomes) != 2 or len(prices_list) != 2:
                continue

            player_a, player_b = outcomes[0], outcomes[1]
            question = mkt["question"]

            # ── Filter: only keep head-to-head match markets ──
            # H2H markets have "vs" or "v" in the question AND player names as outcomes
            # Futures/outrights have "Will X win..." with Yes/No outcomes — skip those
            is_h2h = " vs " in question or " v " in question
            is_yes_no = player_a.lower() in ("yes", "no") and player_b.lower() in ("yes", "no")

            if not is_h2h:
                continue  # Skip futures, outrights, "Will X win tournament?" markets

            # If outcomes are Yes/No but question has "vs", extract player names from question
            if is_yes_no:
                extracted_a, extracted_b = _extract_player_names(question)
                if extracted_a and extracted_b:
                    player_a, player_b = extracted_a, extracted_b
                else:
                    continue  # Can't determine player names

            try:
                price_a = round(float(prices_list[0]) * 100, 1)
                price_b = round(float(prices_list[1]) * 100, 1)
            except (ValueError, TypeError):
                continue

            # ── Filter: skip completed / effectively resolved markets ──
            # If either side is >= 95c or <= 2c, the match is decided
            if price_a >= 95 or price_b >= 95 or price_a <= 2 or price_b <= 2:
                continue

            # Skip markets whose end_date is in the past (already finished)
            end_date_str = mkt.get("end_date", "")
            if end_date_str:
                try:
                    # Gamma returns ISO format like "2026-04-29T23:00:00Z"
                    clean = end_date_str.replace("Z", "+00:00")
                    end_dt = datetime.fromisoformat(clean)
                    from datetime import timezone
                    now_utc = datetime.now(timezone.utc)
                    if end_dt < now_utc:
                        continue
                except Exception:
                    pass  # If we can't parse it, keep the market

            # Skip very low volume markets
            if mkt["volume"] < 5000:
                continue

            # Store current snapshot
            snap_key = slug
            prev_snap = snapshots.get(snap_key, {})

            # Seed initial/peak prices from picks data if no prior snapshot exists
            # This lets us detect drops even on first scan — picks have pre-match prices
            picks_seed = picks_price_map.get(slug, {})
            seed_a = picks_seed.get("price_a", price_a)
            seed_b = picks_seed.get("price_b", price_b)

            initial_a = prev_snap.get("initial_price_a", seed_a)
            initial_b = prev_snap.get("initial_price_b", seed_b)
            peak_a = max(price_a, prev_snap.get("peak_a", seed_a))
            peak_b = max(price_b, prev_snap.get("peak_b", seed_b))

            new_snapshots[snap_key] = {
                "price_a": price_a, "price_b": price_b,
                "player_a": player_a, "player_b": player_b,
                "question": mkt["question"],
                "volume": mkt["volume"],
                "market_id": mkt["market_id"],
                "updated": now_iso,
                "prev_state_a": prev_snap.get("state_a"),
                "prev_state_b": prev_snap.get("state_b"),
                "initial_price_a": initial_a,
                "initial_price_b": initial_b,
                "peak_a": peak_a,
                "peak_b": peak_b,
            }

            # Check each player for a significant drop from their peak/initial price
            for player, curr_price, initial_price, peak_price, prev_state, opp, opp_price in [
                (player_a, price_a, initial_a,
                 peak_a, prev_snap.get("state_a"), player_b, price_b),
                (player_b, price_b, initial_b,
                 peak_b, prev_snap.get("state_b"), player_a, price_a),
            ]:
                # Compare current price to the peak (highest seen) — most reliable drop signal
                set_state, confidence = _infer_set_state(peak_price, curr_price, prev_state)
                if not set_state:
                    continue

                # Look up player rank from picks data
                player_rank = None
                try:
                    picks = load_picks_jsonl(enrich=False)
                    name_lower = player.lower().strip()
                    for p in reversed(picks):
                        pa = (p.get("player_a", "") or "").lower()
                        pb = (p.get("player_b", "") or "").lower()
                        if name_lower in pa or pa in name_lower:
                            player_rank = p.get("rank")
                            break
                        if name_lower in pb or pb in name_lower:
                            player_rank = p.get("rank")
                            break
                except Exception:
                    pass

                # Calculate fair value
                fair_value = _get_fair_value(player, player_rank, set_state)
                fair_price_c = round(fair_value * 100)
                edge_c = fair_price_c - curr_price

                # Only signal if meaningful edge (>= 5c)
                if edge_c < 5:
                    continue

                # Determine signal strength
                if edge_c >= 15:
                    strength = "strong"
                elif edge_c >= 8:
                    strength = "moderate"
                else:
                    strength = "weak"

                # Store inferred state for next scan
                state_key = "state_a" if player == player_a else "state_b"
                new_snapshots[snap_key][state_key] = set_state

                tier = _rank_tier(player_rank)
                is_comeback_king = player.lower().strip() in COMEBACK_KINGS
                king_rate = COMEBACK_KINGS.get(player.lower().strip())

                poly_link = f"https://polymarket.com/event/{slug}" if slug else ""

                signals.append({
                    "player": player,
                    "opponent": opp,
                    "slug": slug,
                    "question": mkt["question"],
                    "poly_link": poly_link,
                    "set_state": set_state,
                    "poly_price": curr_price,
                    "fair_price": fair_price_c,
                    "edge": edge_c,
                    "strength": strength,
                    "confidence": confidence,
                    "rank": player_rank,
                    "rank_tier": tier,
                    "is_comeback_king": is_comeback_king,
                    "king_comeback_rate": round(king_rate * 100, 1) if king_rate else None,
                    "initial_price": round(initial_price, 1),
                    "peak_price": round(peak_price, 1),
                    "volume": mkt["volume"],
                    "market_id": mkt["market_id"],
                    "scanned_at": now_iso,
                })

        # 4. Save updated snapshots
        try:
            _COMEBACK_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _COMEBACK_SNAPSHOT_FILE.write_text(json.dumps(new_snapshots, indent=2))
        except Exception as e:
            logger.warning(f"Comeback: snapshot save failed: {e}")

        # 5. Save active signals
        try:
            _COMEBACK_SIGNALS_FILE.write_text(json.dumps(signals, indent=2))
        except Exception:
            pass

        # Sort: strongest edge first
        signals.sort(key=lambda s: s["edge"], reverse=True)

        return jsonify({
            "signals": signals,
            "active_markets": len(active_markets),
            "scanned_at": now_iso,
        })

    except Exception as e:
        logger.error(f"Comeback radar error: {e}")
        return jsonify({"error": str(e), "signals": []}), 500


@app.route("/api/comeback/model", methods=["GET"])
@enable_cors
def comeback_model():
    """Return the comeback fair value lookup table for reference."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    table = {}
    for (tier, state), prob in COMEBACK_FAIR_VALUE.items():
        table[f"{tier}_{state}"] = {"tier": tier, "state": state, "fair_prob": prob,
                                     "fair_price_c": round(prob * 100)}
    kings = {name: {"rate": round(r*100,1)} for name, r in COMEBACK_KINGS.items()}
    return jsonify({"model": table, "comeback_kings": kings, "total_matches": 21260})


# ── COMEBACK BET TRACKING ──
_COMEBACK_BETS_FILE = LOGS_DIR / "comeback_bets.jsonl"


def _load_comeback_bets():
    """Load all comeback bets from JSONL file."""
    bets = []
    if _COMEBACK_BETS_FILE.exists():
        for line in _COMEBACK_BETS_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    bets.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return bets


def _append_comeback_bet(bet):
    """Append a single comeback bet to the JSONL file."""
    _COMEBACK_BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_COMEBACK_BETS_FILE, "a") as f:
        f.write(json.dumps(bet) + "\n")


def _save_comeback_bets(bets):
    """Rewrite the entire comeback bets file (for updates)."""
    _COMEBACK_BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_COMEBACK_BETS_FILE, "w") as f:
        for b in bets:
            f.write(json.dumps(b) + "\n")


@app.route("/api/comeback/bets", methods=["GET"])
@enable_cors
def get_comeback_bets():
    """Return all logged comeback bets with performance stats."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    bets = _load_comeback_bets()

    # Calculate performance stats
    total = len(bets)
    resolved = [b for b in bets if b.get("outcome") in ("win", "loss")]
    wins = [b for b in resolved if b["outcome"] == "win"]
    losses = [b for b in resolved if b["outcome"] == "loss"]
    pending = [b for b in bets if b.get("outcome") not in ("win", "loss")]

    total_pnl = sum(b.get("pnl_usd", 0) for b in resolved)
    win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else 0

    return jsonify({
        "bets": bets,
        "stats": {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "pending": len(pending),
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 2),
        },
    })


@app.route("/api/comeback/bets", methods=["POST"])
@enable_cors
def log_comeback_bet():
    """Log a new comeback bet from a signal card."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) if request.is_json else {}
    if not data:
        try:
            data = json.loads(request.data.decode()) if request.data else {}
        except Exception:
            data = {}

    required = ["player", "opponent", "slug", "entry_price", "fair_price", "edge", "set_state"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    bet = {
        "bet_id": f"cb_{int(datetime.now().timestamp()*1000)}",
        "player": data["player"],
        "opponent": data["opponent"],
        "slug": data["slug"],
        "question": data.get("question", ""),
        "poly_link": data.get("poly_link", ""),
        "entry_price": data["entry_price"],
        "fair_price": data["fair_price"],
        "edge": data["edge"],
        "set_state": data["set_state"],
        "strength": data.get("strength", ""),
        "rank": data.get("rank"),
        "rank_tier": data.get("rank_tier", ""),
        "is_comeback_king": data.get("is_comeback_king", False),
        "stake_usd": data.get("stake_usd", 5.0),
        "outcome": None,
        "exit_price": None,
        "pnl_usd": None,
        "logged_at": datetime.now().isoformat(),
        "resolved_at": None,
        "notes": data.get("notes", ""),
    }

    _append_comeback_bet(bet)
    logger.info(f"Comeback bet logged: {bet['player']} @ {bet['entry_price']}c (edge +{bet['edge']}c)")

    return jsonify({"ok": True, "bet": bet})


@app.route("/api/comeback/bets/<bet_id>", methods=["PUT"])
@enable_cors
def update_comeback_bet(bet_id):
    """Update a comeback bet outcome (win/loss/cancel)."""
    if not check_admin_cookie():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) if request.is_json else {}
    if not data:
        try:
            data = json.loads(request.data.decode()) if request.data else {}
        except Exception:
            data = {}

    bets = _load_comeback_bets()
    found = False
    for b in bets:
        if b.get("bet_id") == bet_id:
            if "outcome" in data:
                b["outcome"] = data["outcome"]  # "win", "loss", "cancel"
            if "exit_price" in data:
                b["exit_price"] = data["exit_price"]
            if "pnl_usd" in data:
                b["pnl_usd"] = data["pnl_usd"]
            elif b.get("exit_price") and b.get("entry_price") and b.get("stake_usd"):
                # Auto-calculate P&L
                entry_p = b["entry_price"] / 100
                exit_p = b["exit_price"] / 100
                shares = b["stake_usd"] / entry_p if entry_p > 0 else 0
                b["pnl_usd"] = round(shares * (exit_p - entry_p), 2)
            if "notes" in data:
                b["notes"] = data["notes"]
            b["resolved_at"] = datetime.now().isoformat()
            found = True
            break

    if not found:
        return jsonify({"error": "Bet not found"}), 404

    _save_comeback_bets(bets)
    return jsonify({"ok": True, "bet": b})


# ═══════════════════════════════════════════════════════════════════════
# BACKGROUND THREAD STARTUP — works with both Gunicorn and direct run
# ═══════════════════════════════════════════════════════════════════════

_bg_threads_started = False


def _start_background_threads():
    """Start background threads (auto-resolver + position monitor).
    Safe to call multiple times — only starts once."""
    global _bg_threads_started
    if _bg_threads_started:
        return
    _bg_threads_started = True

    import threading

    resolver_thread = threading.Thread(target=_auto_resolve_loop, daemon=True)
    resolver_thread.start()
    logger.info("[AUTO-RESOLVE] Background resolver started (every 1 hour)")

    monitor_thread = threading.Thread(target=_position_monitor_loop, daemon=True)
    monitor_thread.start()
    logger.info("[POS-MONITOR] Background position monitor started (every 10 minutes)")


# Start threads on module import (Gunicorn imports the module, doesn't run __main__)
# Only start if not in Flask debug reloader child process
import os as _os
if _os.environ.get("WERKZEUG_RUN_MAIN") != "true" or _os.environ.get("FLASK_ENV") != "development":
    _start_background_threads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"

    logger.info(f"Starting Tennis Betting Signal Server on port {port}")
    logger.info(f"Base directory: {BASE_DIR}")
    logger.info(f"Debug mode: {debug}")

    # Ensure background threads are running (redundant safety)
    _start_background_threads()

    app.run(host="0.0.0.0", port=port, debug=debug)
