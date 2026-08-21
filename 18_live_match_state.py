#!/usr/bin/env python3
"""
18_live_match_state.py — Live Match-State Feed (Live Tennis API)

Pulls in-progress tennis match state — current score, who is serving, a
three-valued break-point flag, and the retirement / walkover / completed
lifecycle status — from the Live Tennis API and caches it to disk so we hit
the endpoint at most once per polling window.

This is a *live-state* companion to 09_rankings_fetcher.py: the rankings
fetcher answers "how good is this player", this answers "what is happening in
this match right now". The signal / auto-trader layer can use it to gate a
Polymarket edge — e.g. never open (or immediately exit) a position on a match
whose feed already reports a retirement, walkover, or completion, and read the
live score / break-point context alongside the model probability.

Disclosure: the Live Tennis API is a vendor data feed (livetennisapi.com); this
module is a thin read-only client for it. Everything it calls
(GET /matches?status=live) is on the free keyed tier (30 req/min, 100 req/day),
so keep the polling cadence slow. This is a DATA feed only — it never places,
prices, or executes anything.

Set an API key (free keys: https://livetennisapi.com/subscribe/free):
    export LIVETENNIS_API_KEY=sk_...

Usage:
    from importlib import import_module
    live = import_module("18_live_match_state")

    cache = live.load_cached_state()                       # disk or fresh pull
    state = live.lookup_live_state("Sinner", "Alcaraz", cache)
    # → {"found": True, "status": "in_progress", "server": 1,
    #    "break_point": False, "score_line": "6-4 3-2 (40-15)",
    #    "retired": False, "walkover": False, "completed": False, ...}

CLI:
    python3 18_live_match_state.py --refresh
    python3 18_live_match_state.py --live
    python3 18_live_match_state.py --lookup "Sinner" "Alcaraz"
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.resolve()
# Use /data on Render (persistent disk), fall back to repo-relative for local dev
_PERSISTENT = Path("/data")
if _PERSISTENT.exists() and _PERSISTENT.is_dir():
    DATA_DIR = _PERSISTENT / "data"
else:
    DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "live_match_state.json"

# Live data goes stale fast — re-fetch anything older than this many seconds.
CACHE_MAX_AGE_SECONDS = 60

# ── Live Tennis API (free keyed tier) ──
API_BASE = os.environ.get(
    "LIVETENNIS_BASE_URL", "https://api.livetennisapi.com/api/public/v1"
).rstrip("/")
API_KEY = (
    os.environ.get("LIVETENNIS_API_KEY")
    or os.environ.get("LIVE_TENNIS_API_KEY")
    or ""
)
# Name-match acceptance: both sides of a market must clear this to be a hit.
MATCH_THRESHOLD = 0.60

_HEADERS = {
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "tennis-signal-system/18_live_match_state (+livetennisapi.com)",
}

# Retirement / walkover wording that can appear in a status/event_status string.
_RETIRED_RE = re.compile(r"\b(retir|ret\.?|abandon)\w*\b", re.IGNORECASE)
_WALKOVER_RE = re.compile(r"\b(walk[\s-]?over|walkover|w/o|withdr)\w*\b", re.IGNORECASE)
_COMPLETED_RE = re.compile(r"\b(complet|finish|ended|final|result|won)\w*\b", re.IGNORECASE)


# ════════════════════════════════════════════════════════════════════════════
# SCORE HELPERS (Live Tennis API score object)
# ════════════════════════════════════════════════════════════════════════════

def derive_break_point(score):
    """Three-valued break-point flag for the current point.

    Returns:
        True   — the receiver is one point from breaking serve
        False  — a normal point (or a tiebreak, where breaks don't apply)
        None   — UNDEFINED: no server or the points are null (e.g. between
                 games, or a completed match that carries null points)

    Rule (documented Live Tennis API behaviour): a break point is on when the
    RECEIVER is at AD, or at 40 while the SERVER is at 0/15/30. Never on in a
    tiebreak. UNDEF whenever the server or points are missing/null.
    """
    if not score:
        return None
    server = score.get("server")
    points = score.get("points") or []
    if server not in (1, 2):
        return None
    if len(points) != 2 or points[0] is None or points[1] is None:
        return None
    if score.get("is_tiebreak"):
        return False
    receiver_points = str(points[1] if server == 1 else points[0])
    server_points = str(points[0] if server == 1 else points[1])
    if receiver_points == "AD":
        return True
    return receiver_points == "40" and server_points in ("0", "15", "30")


def score_line(score):
    """Render "6-4 3-2 (40-15)" from a Live Tennis API score object."""
    if not score:
        return ""
    games = score.get("games") or []
    parts = []
    if len(games) == 2 and games[0] and len(games[0]) == len(games[1]):
        parts = [f"{a}-{b}" for a, b in zip(games[0], games[1])]
    points = score.get("points") or []
    if len(points) == 2 and points[0] is not None and points[1] is not None:
        parts.append(f"({points[0]}-{points[1]})")
    return " ".join(parts)


def classify_status(match):
    """Return (retired, walkover, completed) booleans from a match object.

    Reads both ``status`` and ``event_status`` — the feed reports retirements /
    walkovers in the event-status wording. A live/in-progress match is all
    False.
    """
    blob = " ".join(
        str(match.get(k) or "")
        for k in ("status", "event_status", "result", "note")
    )
    retired = bool(_RETIRED_RE.search(blob))
    walkover = bool(_WALKOVER_RE.search(blob))
    # "completed" only when the match isn't specifically a retirement/walkover
    completed = bool(_COMPLETED_RE.search(blob)) or retired or walkover
    return retired, walkover, completed


def _match_view(match):
    """Flatten one raw Live Tennis API match object into a plain-dict view."""
    players = match.get("players") or {}
    score = match.get("score") or {}
    server = score.get("server") if score.get("server") in (1, 2) else None
    retired, walkover, completed = classify_status(match)
    return {
        "found": True,
        "match_id": match.get("id"),
        "player1": str((players.get("p1") or {}).get("name") or "?"),
        "player2": str((players.get("p2") or {}).get("name") or "?"),
        "status": match.get("status"),
        "event_status": match.get("event_status"),
        "score_line": score_line(score),
        "sets": [int(s) for s in (score.get("sets") or []) if str(s).lstrip("-").isdigit()],
        "server": server,
        "is_tiebreak": bool(score.get("is_tiebreak")),
        "break_point": derive_break_point(score),
        "retired": retired,
        "walkover": walkover,
        "completed": completed,
        "live_as_of": score.get("timestamp"),
    }


# ════════════════════════════════════════════════════════════════════════════
# FETCH
# ════════════════════════════════════════════════════════════════════════════

def fetch_live_matches(status="live", tour=None, timeout=15):
    """Pull current matches from the Live Tennis API.

    Returns a cache dict: {"matches": [...raw...], "count": n, "source": ...,
    "fetched_at": iso, "errors": [...]}. Never raises on a network / auth
    problem — returns an empty result with the error recorded so callers can
    fall back to whatever they already had.
    """
    if not API_KEY:
        msg = (
            "no LIVETENNIS_API_KEY set — live match-state disabled "
            "(free keys: https://livetennisapi.com/subscribe/free)"
        )
        print(f"  [live-state] {msg}", file=sys.stderr)
        return {
            "matches": [], "count": 0, "source": API_BASE,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "errors": [msg],
        }

    params = {"status": status, "limit": 50}
    if tour:
        params["tour"] = tour
    url = f"{API_BASE}/matches"
    errors = []
    matches = []
    try:
        print(f"  [live-state] Fetching {status} matches from {url}")
        r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
        if r.status_code == 429:
            errors.append("rate limit hit (free tier: 30 req/min, 100 req/day)")
            print(f"  [live-state] {errors[-1]}", file=sys.stderr)
        else:
            r.raise_for_status()
            body = r.json()
            matches = body.get("data", []) if isinstance(body, dict) else []
            print(f"  [live-state] {len(matches)} {status} matches")
    except requests.RequestException as e:
        errors.append(str(e))
        print(f"  [live-state] fetch failed: {e}", file=sys.stderr)
    except ValueError as e:  # bad JSON
        errors.append(f"bad JSON: {e}")
        print(f"  [live-state] {errors[-1]}", file=sys.stderr)

    return {
        "matches": matches,
        "count": len(matches),
        "source": API_BASE,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }


# ════════════════════════════════════════════════════════════════════════════
# CACHE
# ════════════════════════════════════════════════════════════════════════════

def save_cache(data):
    """Save live-state cache to disk."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [live-state] Cache saved: {data.get('count', 0)} matches → {CACHE_FILE}")


def load_cached_state(max_age_seconds=CACHE_MAX_AGE_SECONDS):
    """Load live match state from cache. Re-fetches if stale or missing."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            fetched_at = data.get("fetched_at", "")
            if fetched_at:
                try:
                    fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                    if fetched_dt.tzinfo is None:
                        fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - fetched_dt
                    if age < timedelta(seconds=max_age_seconds):
                        print(
                            f"  [live-state] Using cached state "
                            f"({data.get('count', 0)} matches, {int(age.total_seconds())}s old)"
                        )
                        return data
                    print(f"  [live-state] Cache stale ({int(age.total_seconds())}s old), refreshing...")
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [live-state] Cache read error: {e}", file=sys.stderr)

    data = fetch_live_matches()
    if data.get("count", 0) > 0:
        save_cache(data)
    return data


# ════════════════════════════════════════════════════════════════════════════
# LOOKUP
# ════════════════════════════════════════════════════════════════════════════

def _name_similarity(a, b):
    """Conservative similarity between two player names (0..1)."""
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_last = a.split()[-1]
    b_last = b.split()[-1]
    if a_last and b_last and a_last == b_last:
        return max(0.75, SequenceMatcher(None, a, b).ratio())
    return SequenceMatcher(None, a, b).ratio()


def lookup_live_state(player_a, player_b, cache=None, threshold=MATCH_THRESHOLD):
    """Find the live match between two players (a Polymarket market's two sides).

    Both names must agree with the same match's two players (either order).
    Returns a flat state dict (see ``_match_view``) with ``found=True`` on a
    hit, or ``{"found": False, ...}`` when no live match clears ``threshold``.
    Returns ``None`` never — always a dict — so callers can branch on
    ``["found"]``.
    """
    if cache is None:
        cache = load_cached_state()
    matches = cache.get("matches", [])
    if not matches:
        return {"found": False, "reason": "no live matches in feed"}

    best = None
    best_score = 0.0
    for m in matches:
        players = m.get("players") or {}
        n1 = str((players.get("p1") or {}).get("name") or "")
        n2 = str((players.get("p2") or {}).get("name") or "")
        if not n1 or not n2:
            continue
        direct = min(_name_similarity(player_a, n1), _name_similarity(player_b, n2))
        reversed_ = min(_name_similarity(player_a, n2), _name_similarity(player_b, n1))
        score = max(direct, reversed_)
        if score > best_score:
            best_score = score
            best = m

    if best is None or best_score < threshold:
        return {"found": False, "reason": "no live match above threshold",
                "best_score": round(best_score, 3)}

    view = _match_view(best)
    view["match_confidence"] = round(best_score, 3)
    return view


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch/lookup live tennis match state")
    parser.add_argument("--refresh", action="store_true", help="Force refresh from API")
    parser.add_argument("--live", action="store_true", help="Print all live matches")
    parser.add_argument("--lookup", nargs=2, metavar=("PLAYER_A", "PLAYER_B"),
                        help="Look up the live match between two players")
    args = parser.parse_args()

    if args.refresh:
        data = fetch_live_matches()
        if data.get("count", 0) > 0:
            save_cache(data)
        print(f"\nFetched {data['count']} live matches (source: {data.get('source')})")
        if data.get("errors"):
            print(f"Errors: {data['errors']}")
    else:
        data = load_cached_state()

    if args.live:
        matches = data.get("matches", [])
        if not matches:
            print("  (no live matches)")
        for m in matches:
            v = _match_view(m)
            bp = {True: "BREAK POINT", False: "", None: ""}[v["break_point"]]
            srv = "" if v["server"] not in (1, 2) else (
                f"serving: {v['player1'] if v['server'] == 1 else v['player2']}"
            )
            flags = " ".join(f for f in (
                bp,
                "tiebreak" if v["is_tiebreak"] else "",
                "RETIRED" if v["retired"] else "",
                "WALKOVER" if v["walkover"] else "",
                "COMPLETED" if v["completed"] and not (v["retired"] or v["walkover"]) else "",
            ) if f)
            print(f"  {v['player1']} vs {v['player2']}  {v['score_line']}"
                  f"  {srv}  {flags}".rstrip())

    if args.lookup:
        state = lookup_live_state(args.lookup[0], args.lookup[1], data)
        print(json.dumps(state, indent=2))
