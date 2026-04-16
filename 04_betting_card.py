"""
TENNIS POLYMARKET SIGNAL SYSTEM
Script 4: Betting Card Generator

Runs the full signal pipeline and outputs a clean HTML betting card
you can open in the browser while manually placing bets on Polymarket.

Usage:
    python 04_betting_card.py
    python 04_betting_card.py --min-edge 0.05 --min-volume 500 --open
"""

import argparse
import json
import pickle
import subprocess
import sys
import webbrowser
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

DATA_DIR   = Path("data")
MODELS_DIR = Path("models")

# Live rankings support
_live_rankings_cache = None

def _get_live_rankings():
    """Load live rankings cache (lazy, once per run)."""
    import sys
    global _live_rankings_cache
    if _live_rankings_cache is not None:
        return _live_rankings_cache
    try:
        from importlib import import_module
        fetcher = import_module("09_rankings_fetcher")
        _live_rankings_cache = fetcher.load_cached_rankings()
        count = _live_rankings_cache.get("count", 0)
        rankings = _live_rankings_cache.get("rankings", {})
        if count > 0:
            # Print sample keys so we can debug name format issues
            sample_keys = list(rankings.keys())[:5]
            print(f"  [rankings] Loaded {count} live rankings. Sample keys: {sample_keys}", file=sys.stderr)
        else:
            print(f"  [rankings] Cache empty, falling back to historical data", file=sys.stderr)
            _live_rankings_cache = {}
    except Exception as e:
        print(f"  [rankings] Live rankings unavailable ({e}), using historical data", file=sys.stderr)
        _live_rankings_cache = {}
    return _live_rankings_cache


_rank_debug_count = {"miss": 0, "hit": 0}

def get_live_rank(player_name):
    """Get a player's live world ranking. Returns (rank, tour) or (None, None)."""
    import sys
    cache = _get_live_rankings()
    if not cache or not cache.get("rankings"):
        return None, None
    try:
        from importlib import import_module
        fetcher = import_module("09_rankings_fetcher")
        rank, tour = fetcher.lookup_player_rank(player_name, cache)
        if rank:
            _rank_debug_count["hit"] += 1
        else:
            _rank_debug_count["miss"] += 1
            # Debug: print first 5 misses to stderr so they appear in Render logs
            if _rank_debug_count["miss"] <= 5:
                # Show what keys exist that are close
                rankings = cache.get("rankings", {})
                last = player_name.strip().split()[-1].lower() if player_name.strip() else ""
                close_keys = [k for k in rankings if last in k or k in last][:3]
                print(f"  [rankings] MISS: '{player_name}' → tried keys, no match. Close: {close_keys}", file=sys.stderr)
        return rank, tour
    except Exception as e:
        print(f"  [rankings] ERROR looking up '{player_name}': {e}", file=sys.stderr)
        return None, None
CARDS_DIR  = Path("cards")
CARDS_DIR.mkdir(exist_ok=True)

# ─── REUSE SIGNAL LOGIC (inline so script is self-contained) ─────────────────

def _parse_market(m, min_volume=0):
    """Parse a single market object from the Gamma API into a standardised row."""
    vol = float(m.get("volume", 0) or m.get("volume24hr", 0) or 0)
    if vol < min_volume:
        return None

    # Parse prices — Gamma API stores these as stringified JSON arrays
    prices = {}
    outcomes_raw = m.get("outcomes", "")
    prices_raw   = m.get("outcomePrices", "")

    # Try JSON-stringified arrays first (e.g. '["Yes","No"]' and '[0.55, 0.45]')
    try:
        if isinstance(outcomes_raw, str):
            outcomes_list = json.loads(outcomes_raw)
        else:
            outcomes_list = outcomes_raw or []
    except (json.JSONDecodeError, TypeError):
        outcomes_list = []

    try:
        if isinstance(prices_raw, str):
            prices_list = json.loads(prices_raw)
        else:
            prices_list = prices_raw or []
    except (json.JSONDecodeError, TypeError):
        prices_list = []

    if outcomes_list and prices_list and len(outcomes_list) == len(prices_list):
        for name, price in zip(outcomes_list, prices_list):
            try:
                prices[str(name)] = float(price) * 100
            except (ValueError, TypeError):
                pass

    # Fallback: try tokens array (older API format)
    if not prices:
        tokens = m.get("tokens", [])
        if isinstance(tokens, list):
            for t in tokens:
                if isinstance(t, dict):
                    name  = t.get("outcome", t.get("name", ""))
                    price = t.get("price")
                    if name and price is not None:
                        prices[str(name)] = float(price) * 100

    if not prices:
        return None

    return {
        "market_id": m.get("id") or m.get("condition_id") or m.get("conditionId", ""),
        "slug":      m.get("slug", ""),
        "question":  m.get("question", ""),
        "end_date":  m.get("end_date_iso") or m.get("endDateIso", "") or m.get("endDate", ""),
        "volume":    vol,
        "liquidity": float(m.get("liquidity", 0) or 0),
        "prices":    prices,
    }


def fetch_live_markets(min_volume=0):
    rows = []

    # Strategy 1: Events endpoint with tag filtering (most reliable for sports)
    for tag in ["tennis", "atp-tennis", "wta-tennis", "sports-tennis", "atp", "wta"]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/events",
                params={"tag_slug": tag, "active": "true", "closed": "false",
                        "limit": 100, "order": "volume24hr", "ascending": "false"},
                timeout=15
            )
            r.raise_for_status()
            events = r.json()
            for ev in events:
                # Events contain nested markets
                markets_list = ev.get("markets", [])
                if markets_list:
                    for m in markets_list:
                        row = _parse_market(m, min_volume)
                        if row:
                            # Use event slug if market slug is missing
                            if not row["slug"]:
                                row["slug"] = ev.get("slug", "")
                            rows.append(row)
                else:
                    # Sometimes the event IS the market
                    row = _parse_market(ev, min_volume)
                    if row:
                        rows.append(row)
        except Exception as e:
            print(f"  [warn] events/{tag}: {e}")

    # Strategy 2: Markets endpoint with tag filtering
    for tag in ["tennis", "atp-tennis", "wta-tennis", "atp", "wta"]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"tag_slug": tag, "active": "true", "closed": "false", "limit": 500},
                timeout=15
            )
            r.raise_for_status()
            for m in r.json():
                row = _parse_market(m, min_volume)
                if row:
                    rows.append(row)
        except Exception as e:
            print(f"  [warn] markets/{tag}: {e}")

    # Strategy 3: Search-based fallback — keyword match
    for keyword in ["tennis", "ATP", "WTA"]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 200,
                        "order": "volume24hr", "ascending": "false"},
                timeout=15
            )
            r.raise_for_status()
            for m in r.json():
                q = str(m.get("question", "")).lower()
                tags = str(m.get("tags", "")).lower()
                if keyword.lower() in q or keyword.lower() in tags:
                    row = _parse_market(m, min_volume)
                    if row:
                        rows.append(row)
        except Exception as e:
            print(f"  [warn] search/{keyword}: {e}")
            break  # Don't retry if API is down

    # Deduplicate by market_id
    seen, out = set(), []
    for r in rows:
        mid = r["market_id"]
        if mid and mid not in seen:
            seen.add(mid)
            out.append(r)

    # Filter to tennis-only using STRICT criteria
    # Must mention a known tennis tournament OR a known tennis term AND be a Yes/No or player market
    tennis_tournaments = [
        "french open", "roland garros", "australian open", "wimbledon", "us open",
        "indian wells", "miami open", "monte carlo", "monte-carlo", "rolex monte",
        "madrid open", "madrid masters", "rome open", "italian open", "canadian open",
        "cincinnati", "shanghai", "paris masters", "atp finals", "wta finals",
        "dubai", "qatar open", "doha", "bnp paribas", "mutua madrid",
        "internazionali", "rogers cup", "adelaide", "brisbane", "auckland",
        "hobart", "beijing", "porsche tennis", "stuttgart", "barcelona open",
        "halle open", "queen's club", "s-hertogenbosch", "eastbourne",
        "acapulco", "rio open", "lyon open", "geneva open",
    ]
    tennis_terms = ["atp", "wta", "grand slam", "tennis"]

    # Blacklist: terms that indicate NON-tennis markets
    blacklist = [
        "nba", "nfl", "nhl", "mlb", "stanley cup", "super bowl", "world series",
        "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
        "champions league", "europa league", "epl",
        "president", "democrat", "republican", "nominee", "election", "governor",
        "senate", "congress", "cabinet", "secretary",
        "nba rookie", "mvp award", "draft pick",
        "pga", "masters tournament", "the open championship", "ryder cup",
        "world cup", "copa america", "euro 2",
        "stanley cup", "playoffs",
        "convicted", "indicted", "arrested", "sentenced",
        "gta vi", "album", "bitcoin", "crypto",
    ]

    filtered = []
    for r in out:
        q = r["question"].lower()
        # Skip if blacklisted
        if any(bl in q for bl in blacklist):
            continue
        # Must match a tennis tournament, tennis term, or come from a tennis tag
        tags = str(r.get("slug", "") or "").lower()
        is_tennis = (
            any(t in q for t in tennis_tournaments) or
            any(t in q for t in tennis_terms) or
            any(t in tags for t in ["tennis", "atp", "wta"])
        )
        if is_tennis and len(r["prices"]) == 2:
            filtered.append(r)

    # Remove resolved markets (price at 0¢ or 100¢ means already decided)
    live = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for r in filtered:
        prices = r["prices"]
        vals = list(prices.values())
        # Skip if any price is 0 or 100 (market already resolved)
        if any(v <= 0.5 or v >= 99.5 for v in vals):
            continue
        # Skip if end_date is in the past
        end = str(r.get("end_date", "") or "")[:10]
        if end and end < today_str:
            continue
        live.append(r)

    if live:
        print(f"  → {len(live)} live tennis markets after filtering ({len(filtered) - len(live)} resolved/expired removed)")
        return pd.DataFrame(live)

    # If nothing matched with strict filter, return empty
    print("  → 0 tennis markets found (strict filter)")
    return pd.DataFrame()


def parse_players(question):
    """Parse player names from head-to-head WINNER market questions.
    Handles formats like:
      - 'Rolex Monte Carlo Masters: Carlos Alcaraz vs Jannik Sinner'
      - 'Porsche Tennis Grand Prix: Ostapenko vs Andreeva'
      - 'Carlos Alcaraz vs Jannik Sinner'
      - 'Will Alcaraz beat Sinner?'
    Filters out game props (O/U, handicaps, set winners, game spreads)."""
    import re
    q = str(question).strip()

    # Skip game props — these are NOT winner markets
    q_lower = q.lower()
    prop_indicators = [
        "o/u", "over/under", "handicap", "spread", "total games",
        "set 1 ", "set 2 ", "set 3 ", "match o/u", "games o/u",
        "set winner", "set handicap", "game handicap",
        "correct score", "tiebreak", "aces", "total sets",
    ]
    if any(pi in q_lower for pi in prop_indicators):
        return "", ""

    # Skip "Will X win the TOURNAMENT" outright markets — handled by parse_outright()
    if q_lower.startswith("will ") and " win " in q_lower:
        return "", ""

    # Strip tournament prefix if present (e.g., "Rolex Monte Carlo Masters: " or "BNP Paribas Open: ")
    q_clean = q
    if ":" in q_clean:
        after_colon = q_clean.split(":", 1)[1].strip()
        # Only use the part after colon if it looks like "Player vs Player"
        if " vs" in after_colon.lower() or " beat " in after_colon.lower():
            q_clean = after_colon

    for pat in [
        r"Will (.+?) beat (.+?)\??$",
        r"(.+?)\s+vs\.?\s+(.+)",
        r"([A-Z][a-zA-Z\s\-\.\']+?) vs\.? ([A-Z][a-zA-Z\s\-\.\']+)",
    ]:
        m = re.search(pat, q_clean)
        if m:
            p1 = m.group(1).strip().rstrip("?").strip()
            p2 = m.group(2).strip().rstrip("?").strip()
            if p1 and p2 and len(p1) > 1 and len(p2) > 1:
                # Skip if names look like prop descriptions
                if any(x in p1.lower() for x in ["match o", "set ", "games", "handicap"]):
                    return "", ""
                if any(x in p2.lower() for x in ["match o", "set ", "games", "handicap"]):
                    return "", ""
                return p1, p2
    return "", ""


def parse_outright(question):
    """Parse player + tournament from outright winner markets like 'Will X win the 2026 French Open?'
    Only matches tennis tournaments, not NHL/NBA/soccer/politics/golf etc."""
    import re
    q = str(question).strip()

    # Known tennis tournament patterns
    tennis_tourney_patterns = [
        "french open", "roland garros", "australian open", "wimbledon", "us open",
        "women's french open", "men's french open", "women's australian open",
        "men's australian open", "women's wimbledon", "men's wimbledon",
        "women's us open", "men's us open",
        "indian wells", "miami open", "monte carlo", "monte-carlo", "rolex monte",
        "madrid open", "madrid masters", "rome open", "italian open",
        "canadian open", "cincinnati", "shanghai", "paris masters",
        "atp finals", "wta finals", "dubai", "qatar", "doha", "bnp paribas",
        "calendar grand slam", "more grand slam", "most grand slam",
        "porsche tennis", "stuttgart", "barcelona open", "halle",
        "queen's club", "eastbourne", "acapulco", "rio open",
    ]

    # Skip non-player names (multi-player comparisons, generic terms)
    skip_patterns = [
        "no player", "no male", "no female", "no woman", "no man",
        "another player", "any other", "alcaraz or sinner",
        "the field", "someone else", "a player", "any player",
        "calendar grand slam", "more grand slam", "most grand slam",
    ]

    for pat in [
        r"Will (.+?) win (?:the )?(\d{4} .+?)[\?\.]?$",
        r"Will (.+?) win (.+?)[\?\.]?$",
    ]:
        m = re.search(pat, q)
        if m:
            player = m.group(1).strip()
            tournament = m.group(2).strip()
            if player and len(player) > 2:
                # Skip non-player entries
                if any(sp in player.lower() for sp in skip_patterns):
                    return "", ""
                # Skip if player name contains "or" (comparison markets)
                if " or " in player.lower():
                    return "", ""
                # Verify this is a tennis tournament
                t_lower = tournament.lower()
                if any(tp in t_lower for tp in tennis_tourney_patterns):
                    return player, tournament
    return "", ""


def _build_player_index(df):
    """Build a name→canonical mapping for fast lookup. Call once, reuse."""
    players = set(df["winner"].dropna()) | set(df["loser"].dropna())
    index = {}
    for p in players:
        # Index by full name, last name, and normalised forms
        index[p.lower()] = p
        parts = p.split()
        if len(parts) >= 2:
            index[parts[-1].lower()] = p                           # last name
            index[f"{parts[0].lower()} {parts[-1].lower()}"] = p   # first + last
    return index, players


_PLAYER_INDEX = None
_PLAYER_SET   = None


def find_player(df, name):
    global _PLAYER_INDEX, _PLAYER_SET
    if _PLAYER_INDEX is None:
        _PLAYER_INDEX, _PLAYER_SET = _build_player_index(df)

    nl = name.strip().lower()
    # Exact match
    if nl in _PLAYER_INDEX:
        return _PLAYER_INDEX[nl]
    # Try "First Last" form
    parts = nl.split()
    if len(parts) >= 2:
        key = f"{parts[0]} {parts[-1]}"
        if key in _PLAYER_INDEX:
            return _PLAYER_INDEX[key]
        # Try last-name only
        if parts[-1] in _PLAYER_INDEX:
            return _PLAYER_INDEX[parts[-1]]

    # Fuzzy fallback on full player set
    best, score = "", 0.0
    for p in _PLAYER_SET:
        s = SequenceMatcher(None, nl, p.lower()).ratio()
        if s > score:
            best, score = p, s
    return best if score >= 0.60 else ""


def get_player_ranking(df, player, as_of=None):
    """Get the player's most recent ranking and ranking points from historical data."""
    cutoff = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()

    # Check wins where they have a rank recorded
    w_matches = df[(df["winner"] == player) & (df["date"] < cutoff)].sort_values("date", ascending=False)
    l_matches = df[(df["loser"] == player) & (df["date"] < cutoff)].sort_values("date", ascending=False)

    best_rank = None
    best_pts  = None

    # Find most recent ranking from winner or loser columns
    if len(w_matches) > 0 and "w_rank" in df.columns:
        recent_w = w_matches.head(5)
        ranks = recent_w["w_rank"].dropna()
        if len(ranks) > 0:
            best_rank = int(ranks.iloc[0])
            if "w_rank_pts" in df.columns:
                pts = recent_w["w_rank_pts"].dropna()
                if len(pts) > 0:
                    best_pts = float(pts.iloc[0])

    if len(l_matches) > 0 and "l_rank" in df.columns:
        recent_l = l_matches.head(5)
        ranks = recent_l["l_rank"].dropna()
        if len(ranks) > 0:
            l_rank = int(ranks.iloc[0])
            if best_rank is None or l_rank < best_rank:
                best_rank = l_rank
            if "l_rank_pts" in df.columns:
                pts = recent_l["l_rank_pts"].dropna()
                if len(pts) > 0:
                    l_pts = float(pts.iloc[0])
                    if best_pts is None or l_pts > best_pts:
                        best_pts = l_pts

    return best_rank, best_pts


# ── ELO COMPUTATION ──
# Global cache for ELO ratings (computed once per run across all players)
_elo_cache = None
_lstm_log_once = {}  # prevents spamming LSTM logs for every pick

def _build_elo_table(df, cutoff, k_base=32, initial=1500):
    """
    Compute ELO ratings for all players from match history.
    Uses variable K-factor: higher for upsets, lower for expected results.
    Optimized: uses numpy arrays extracted from DataFrame for speed.
    """
    global _elo_cache
    if _elo_cache is not None:
        return _elo_cache

    import time
    t0 = time.time()

    matches = df[df["date"] < cutoff].sort_values("date")
    winners = matches["winner"].values
    losers = matches["loser"].values

    elo = {}  # player -> rating

    for i in range(len(winners)):
        w, l = winners[i], losers[i]
        if w not in elo: elo[w] = initial
        if l not in elo: elo[l] = initial

        ra, rb = elo[w], elo[l]
        ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))

        # Variable K: bigger updates for upsets
        if ea < 0.3:
            k = k_base * 1.5
        elif ea > 0.8:
            k = k_base * 0.7
        else:
            k = k_base

        elo[w] = ra + k * (1 - ea)
        elo[l] = rb + k * (ea - 1)  # equivalent to rb + k * (0 - (1-ea))

    elapsed = time.time() - t0
    print(f"  [elo] Computed {len(elo)} player ratings in {elapsed:.1f}s")

    _elo_cache = elo
    return elo


def _compute_elo(df, player, cutoff):
    """Get a player's current ELO rating."""
    elo_table = _build_elo_table(df, cutoff)
    return round(elo_table.get(player, 1500))


def _compute_elo_percentile(df, player, cutoff):
    """Get the player's ELO percentile among all active players (played in last 365d)."""
    elo_table = _build_elo_table(df, cutoff)
    player_elo = elo_table.get(player, 1500)

    # Only consider "active" players (who played at least 1 match in last 365d)
    since = cutoff - timedelta(days=365)
    active_players = set()
    mask = (df["date"] >= since) & (df["date"] < cutoff)
    active_players.update(df.loc[mask, "winner"].unique())
    active_players.update(df.loc[mask, "loser"].unique())

    if len(active_players) < 10:
        return None

    active_elos = [elo_table.get(p, 1500) for p in active_players]
    below = sum(1 for e in active_elos if e < player_elo)
    return round(below / len(active_elos) * 100)


def _compute_momentum(df, player, cutoff, recent_matches):
    """
    Compute a psychological momentum score (0-100) based on:
    - Recent form (last 5-10 matches): 40% weight
    - Quality of wins (beating higher-ranked opponents): 25% weight
    - Rest & fatigue: 15% weight
    - Streak factor (winning/losing streak): 20% weight

    Higher = more positive momentum.
    """
    from datetime import timedelta

    if len(recent_matches) == 0:
        return 50  # neutral

    # ── RECENT FORM (last 10 matches) ── 40%
    l10 = recent_matches.tail(10)
    l5 = recent_matches.tail(5)
    if len(l10) > 0:
        form_10 = (l10["winner"] == player).mean()
        form_5 = (l5["winner"] == player).mean() if len(l5) > 0 else form_10
        # Weight recent matches more heavily
        form_score = (form_5 * 0.6 + form_10 * 0.4) * 100
    else:
        form_score = 50

    # ── QUALITY OF WINS ── 25%
    # Wins vs higher-ranked opponents in last 20 matches (vectorized)
    l20 = recent_matches.tail(20)
    quality_score = 50
    if len(l20) > 0:
        l20_wins = l20[l20["winner"] == player]
        quality_wins = 0
        if "l_rank" in df.columns and len(l20_wins) > 0:
            opp_ranks = l20_wins["l_rank"].dropna()
            quality_wins = int((opp_ranks[(opp_ranks > 0) & (opp_ranks <= 30)]).count())
        quality_total = len(l20)
        if quality_total > 0:
            quality_score = min(100, 40 + quality_wins * 15)

    # ── REST & FATIGUE ── 15%
    if len(recent_matches) > 0:
        last_date = recent_matches["date"].max()
        days_rest = (cutoff - last_date).days
        # Sweet spot: 3-7 days rest is ideal
        if 3 <= days_rest <= 7:
            rest_score = 80
        elif days_rest < 3:
            rest_score = max(30, 80 - (3 - days_rest) * 20)  # too little rest
        elif days_rest <= 14:
            rest_score = 65  # moderate break, still fine
        else:
            rest_score = max(30, 65 - (days_rest - 14) * 2)  # ring rust
    else:
        rest_score = 40

    # ── STREAK FACTOR ── 20%
    streak = 0
    if len(recent_matches) > 0:
        win_flags = (recent_matches["winner"] == player).values
        for i in range(len(win_flags) - 1, -1, -1):
            if win_flags[i]:
                if streak >= 0: streak += 1
                else: break
            else:
                if streak <= 0: streak -= 1
                else: break
    # Map streak to score: +5 streak = 95, -5 streak = 5
    streak_score = min(100, max(0, 50 + streak * 9))

    # ── COMPOSITE ──
    momentum = (
        form_score * 0.40 +
        quality_score * 0.25 +
        rest_score * 0.15 +
        streak_score * 0.20
    )
    return round(min(100, max(0, momentum)))


def get_player_stats(df, player, as_of, surface, window_days=365):
    from datetime import timedelta
    cutoff = pd.Timestamp(as_of)
    since  = cutoff - timedelta(days=window_days)

    # If the data doesn't reach into our window, use the last 365 days OF DATA instead
    max_date = df["date"].max()
    if max_date < since:
        cutoff = max_date + timedelta(days=1)
        since  = cutoff - timedelta(days=window_days)

    wins   = df[(df["winner"] == player) & (df["date"] < cutoff) & (df["date"] >= since)]
    losses = df[(df["loser"]  == player) & (df["date"] < cutoff) & (df["date"] >= since)]
    n_w, n_l = len(wins), len(losses)
    n_t = n_w + n_l
    s_w = wins[wins["surface"] == surface]
    s_l = losses[losses["surface"] == surface]
    s_t = len(s_w) + len(s_l)
    all_m = pd.concat([wins, losses]).sort_values("date")
    l10   = all_m.tail(10)
    l10_w = (l10["winner"] == player).sum()
    last_match = (cutoff - all_m["date"].max()).days if n_t > 0 else 30

    # ── SERVE & RETURN STATS ──
    # Compute per-match averages from raw serve columns
    # When player won: stats are in w_* columns; when lost: in l_* columns
    def _safe_avg(series):
        s = series.dropna()
        return float(s.mean()) if len(s) > 0 else None

    def _safe_ratio(num_series, denom_series):
        """Compute ratio (e.g. 1stWon / svpt) safely."""
        n = num_series.dropna()
        d = denom_series.dropna()
        if len(n) == 0 or len(d) == 0:
            return None
        total_n = n.sum()
        total_d = d.sum()
        return round(float(total_n / total_d * 100), 1) if total_d > 0 else None

    # Aces per match
    ace_w = _safe_avg(wins["w_ace"]) if "w_ace" in wins.columns else None
    ace_l = _safe_avg(losses["l_ace"]) if "l_ace" in losses.columns else None
    aces_per_match = None
    if ace_w is not None or ace_l is not None:
        vals = [v for v in [ace_w, ace_l] if v is not None]
        aces_per_match = round(sum(vals) / len(vals), 1)

    # Double faults per match
    df_w = _safe_avg(wins["w_df"]) if "w_df" in wins.columns else None
    df_l = _safe_avg(losses["l_df"]) if "l_df" in losses.columns else None
    dfs_per_match = None
    if df_w is not None or df_l is not None:
        vals = [v for v in [df_w, df_l] if v is not None]
        dfs_per_match = round(sum(vals) / len(vals), 1)

    # 1st serve % (1stIn / svpt)
    first_in_pct = None
    if "w_1stIn" in df.columns and "w_svpt" in df.columns:
        all_1stIn = pd.concat([wins["w_1stIn"], losses["l_1stIn"]])
        all_svpt  = pd.concat([wins["w_svpt"], losses["l_svpt"]])
        first_in_pct = _safe_ratio(all_1stIn, all_svpt)

    # 1st serve win % (1stWon / 1stIn)
    first_won_pct = None
    if "w_1stWon" in df.columns and "w_1stIn" in df.columns:
        all_1stWon = pd.concat([wins["w_1stWon"], losses["l_1stWon"]])
        all_1stIn  = pd.concat([wins["w_1stIn"], losses["l_1stIn"]])
        first_won_pct = _safe_ratio(all_1stWon, all_1stIn)

    # 2nd serve win % (2ndWon / (svpt - 1stIn))
    second_won_pct = None
    if "w_2ndWon" in df.columns and "w_svpt" in df.columns and "w_1stIn" in df.columns:
        all_2ndWon = pd.concat([wins["w_2ndWon"], losses["l_2ndWon"]])
        all_2nd_attempts = pd.concat([wins["w_svpt"] - wins["w_1stIn"], losses["l_svpt"] - losses["l_1stIn"]])
        second_won_pct = _safe_ratio(all_2ndWon, all_2nd_attempts)

    # Break points saved % (bpSaved / bpFaced)
    bp_saved_pct = None
    if "w_bpSaved" in df.columns and "w_bpFaced" in df.columns:
        all_bpSaved = pd.concat([wins["w_bpSaved"], losses["l_bpSaved"]])
        all_bpFaced = pd.concat([wins["w_bpFaced"], losses["l_bpFaced"]])
        bp_saved_pct = _safe_ratio(all_bpSaved, all_bpFaced)

    # Break points converted (opponent's bpFaced - bpSaved = breaks won by us)
    bp_convert_pct = None
    if "l_bpFaced" in df.columns and "l_bpSaved" in df.columns:
        # When we won: opponent's bp stats are in l_bp columns (they were the loser)
        # When we lost: opponent's bp stats are in w_bp columns (they were the winner)
        opp_bpFaced = pd.concat([wins["l_bpFaced"], losses["w_bpFaced"]])
        opp_bpSaved = pd.concat([wins["l_bpSaved"], losses["w_bpSaved"]])
        breaks_won = opp_bpFaced - opp_bpSaved
        bp_convert_pct = _safe_ratio(breaks_won, opp_bpFaced)

    # ── WIN/LOSS VS RANKED OPPONENTS (vectorized — no iterrows) ──
    vs_ranked = {"top10": {"w": 0, "l": 0}, "top20": {"w": 0, "l": 0}, "top50": {"w": 0, "l": 0}}
    if "l_rank" in df.columns and len(wins) > 0:
        opp_ranks_w = wins["l_rank"].dropna()
        opp_ranks_w = opp_ranks_w[opp_ranks_w > 0]
        vs_ranked["top10"]["w"] = int((opp_ranks_w <= 10).sum())
        vs_ranked["top20"]["w"] = int((opp_ranks_w <= 20).sum())
        vs_ranked["top50"]["w"] = int((opp_ranks_w <= 50).sum())
    if "w_rank" in df.columns and len(losses) > 0:
        opp_ranks_l = losses["w_rank"].dropna()
        opp_ranks_l = opp_ranks_l[opp_ranks_l > 0]
        vs_ranked["top10"]["l"] = int((opp_ranks_l <= 10).sum())
        vs_ranked["top20"]["l"] = int((opp_ranks_l <= 20).sum())
        vs_ranked["top50"]["l"] = int((opp_ranks_l <= 50).sum())

    # ── RANKING HISTORY & MOVEMENT (vectorized) ──
    def _get_rank_at(matches_w, matches_l, target_date, window_days=90):
        """Get player's rank closest to a target date within a window.
        Uses adaptive window: tries exact window first, then expands to find nearest data."""
        start = target_date - timedelta(days=window_days)
        end = target_date + timedelta(days=window_days)
        best_dist, best_rank = 9999, None
        for col, mdf in [("w_rank", matches_w), ("l_rank", matches_l)]:
            if col not in df.columns or len(mdf) == 0:
                continue
            nearby = mdf[(mdf["date"] >= start) & (mdf["date"] <= end)]
            if len(nearby) == 0:
                continue
            ranks = nearby[col].dropna()
            ranks = ranks[ranks > 0]
            if len(ranks) == 0:
                continue
            dates = nearby.loc[ranks.index, "date"]
            dists = (dates - target_date).abs().dt.days
            min_idx = dists.idxmin()
            dist = int(dists[min_idx])
            if dist < best_dist:
                best_dist = dist
                best_rank = int(ranks[min_idx])
        return best_rank

    # All matches for the player (for ranking lookups beyond the 365d window)
    all_wins = df[(df["winner"] == player) & (df["date"] < cutoff)].sort_values("date")
    all_losses = df[(df["loser"] == player) & (df["date"] < cutoff)].sort_values("date")

    rank_now = None
    if "w_rank" in df.columns and len(all_wins) > 0:
        recent = all_wins.tail(3)["w_rank"].dropna()
        if len(recent) > 0:
            rank_now = int(recent.iloc[-1])
    if "l_rank" in df.columns and len(all_losses) > 0:
        recent = all_losses.tail(3)["l_rank"].dropna()
        if len(recent) > 0:
            l_rank_now = int(recent.iloc[-1])
            if rank_now is None or l_rank_now < rank_now:
                rank_now = l_rank_now

    # Use wider windows for longer lookbacks (data may have gaps)
    rank_30d  = _get_rank_at(all_wins, all_losses, cutoff - timedelta(days=30),  window_days=45)
    rank_90d  = _get_rank_at(all_wins, all_losses, cutoff - timedelta(days=90),  window_days=90)
    rank_180d = _get_rank_at(all_wins, all_losses, cutoff - timedelta(days=180), window_days=120)
    rank_365d = _get_rank_at(all_wins, all_losses, cutoff - timedelta(days=365), window_days=180)

    # Ranking movement: positive = improved (lower rank number), negative = dropped
    rank_move_90d = None
    if rank_now and rank_90d:
        rank_move_90d = rank_90d - rank_now
    rank_move_180d = None
    if rank_now and rank_180d:
        rank_move_180d = rank_180d - rank_now
    rank_move_365d = None
    if rank_now and rank_365d:
        rank_move_365d = rank_365d - rank_now

    # ── YTD WINS / LOSSES ──
    # Use all_wins/all_losses (not 52w-filtered) and the ACTUAL as_of year
    # so YTD reflects the current calendar year even if data is stale
    actual_cutoff = pd.Timestamp(as_of)
    ytd_start = actual_cutoff.replace(month=1, day=1)
    wins_ytd = int(((all_wins["date"] >= ytd_start) & (all_wins["date"] < actual_cutoff)).sum()) if len(all_wins) > 0 else 0
    losses_ytd = int(((all_losses["date"] >= ytd_start) & (all_losses["date"] < actual_cutoff)).sum()) if len(all_losses) > 0 else 0
    # If no YTD data (data doesn't cover current year), fall back to most recent full year
    if wins_ytd == 0 and losses_ytd == 0 and len(all_wins) + len(all_losses) > 0:
        max_data_date = cutoff  # may be adjusted to data end
        fb_ytd_start = max_data_date.replace(month=1, day=1)
        wins_ytd = int(((all_wins["date"] >= fb_ytd_start) & (all_wins["date"] < max_data_date)).sum()) if len(all_wins) > 0 else 0
        losses_ytd = int(((all_losses["date"] >= fb_ytd_start) & (all_losses["date"] < max_data_date)).sum()) if len(all_losses) > 0 else 0

    # ── ELO RATING ──
    elo = _compute_elo(df, player, cutoff)
    elo_pct = _compute_elo_percentile(df, player, cutoff)

    # ── PSYCHOLOGICAL MOMENTUM ──
    momentum = _compute_momentum(df, player, cutoff, all_m)

    return {
        "win_rate":       round(n_w / n_t * 100, 1) if n_t > 5 else None,
        "surf_win_rate":  round(len(s_w) / s_t * 100, 1) if s_t > 3 else None,
        "l10_form":       f"{l10_w}/{len(l10)}",
        "matches_52w":    n_t,
        "days_rest":      last_match,
        # Serve & return stats
        "aces_per_match":   aces_per_match,
        "dfs_per_match":    dfs_per_match,
        "first_in_pct":     first_in_pct,
        "first_won_pct":    first_won_pct,
        "second_won_pct":   second_won_pct,
        "bp_saved_pct":     bp_saved_pct,
        "bp_convert_pct":   bp_convert_pct,
        # vs ranked opponents (52-week window)
        "vs_top10":         vs_ranked["top10"],
        "vs_top20":         vs_ranked["top20"],
        "vs_top50":         vs_ranked["top50"],
        # Ranking history
        "rank_now":         rank_now,
        "rank_30d":         rank_30d,
        "rank_90d":         rank_90d,
        "rank_180d":        rank_180d,
        "rank_365d":        rank_365d,
        "rank_move_90d":    rank_move_90d,
        "rank_move_180d":   rank_move_180d,
        "rank_move_365d":   rank_move_365d,
        # ELO rating
        "elo":              elo,
        "elo_pct":          elo_pct,
        # Psychological momentum (composite 0-100)
        "momentum":         momentum,
        # YTD record
        "wins_ytd":         wins_ytd,
        "losses_ytd":       losses_ytd,
    }


def get_h2h(df, p1, p2, as_of):
    cutoff = pd.Timestamp(as_of)
    mask = (
        ((df["winner"] == p1) & (df["loser"] == p2)) |
        ((df["winner"] == p2) & (df["loser"] == p1))
    ) & (df["date"] < cutoff)
    h2h = df[mask]
    total = len(h2h)
    p1w   = (h2h["winner"] == p1).sum()
    return {"total": total, "p1_wins": p1w, "p2_wins": total - p1w}


def kelly(prob, poly_price_cents, frac=0.25):
    if poly_price_cents <= 0 or poly_price_cents >= 100:
        return 0.0
    b = (100 / poly_price_cents) - 1
    k = (b * prob - (1 - prob)) / b
    return max(0.0, round(k * frac * 100, 2))


def _extract_tournament_info(question):
    """Extract tournament name and round from the market question."""
    q = str(question).strip()
    tournament = ""
    round_name = ""

    # Try to get tournament from prefix before ":"
    if ":" in q:
        prefix = q.split(":", 1)[0].strip()
        # Check if prefix looks like a tournament name (not a player name)
        if any(len(word) > 3 for word in prefix.split()) and len(prefix) > 5:
            tournament = prefix

    # If no prefix, try to extract from "Will X win the YEAR TOURNAMENT?"
    if not tournament:
        import re
        m = re.search(r"\d{4}\s+(.+?)[\?\.]?$", q)
        if m:
            tournament = m.group(1).strip().rstrip("?").strip()

    # Try to detect round from question
    q_lower = q.lower()
    round_keywords = {
        "final": "Final", "semifinal": "Semifinal", "semi-final": "Semifinal",
        "quarterfinal": "Quarterfinal", "quarter-final": "Quarterfinal",
        "round of 16": "R16", "round of 32": "R32", "round of 64": "R64",
        "round of 128": "R128", "1st round": "R1", "2nd round": "R2",
        "3rd round": "R3", "4th round": "R4",
        "r1": "R1", "r2": "R2", "r3": "R3", "r4": "R4", "r16": "R16",
        "r32": "R32", "r64": "R64", "qf": "QF", "sf": "SF",
    }
    for kw, label in round_keywords.items():
        if kw in q_lower:
            round_name = label
            break

    return tournament, round_name


def _detect_surface(question):
    """Detect surface from tournament name in the question using comprehensive mapping."""
    q = question.lower()

    # ── CLAY COURT TOURNAMENTS ──
    clay_kw = [
        # Grand Slam
        "french open", "roland garros",
        # ATP Masters 1000
        "monte carlo", "monte-carlo", "madrid", "rome", "roma", "internazionali",
        # ATP 500
        "barcelona", "rio", "rio de janeiro", "rio open", "hamburg",
        # ATP 250
        "lyon", "geneva", "buenos aires", "cordoba", "santiago", "marrakech",
        "estoril", "munich", "munchen", "bastad", "umag", "kitzbuhel",
        "gstaad", "bucharest", "houston", "sao paulo", "quito", "tiriac",
        "banja luka", "cagliari", "parma", "sardegna", "perugia", "prospera",
        "tallahassee", "sarasota", "santos",
        # WTA
        "strasbourg", "rabat", "bogota", "palermo", "lausanne", "budapest",
        "prague", "istanbul", "iasi", "makarska", "bol",
        # Challenger / ITF clay events
        "aix-en-provence", "braunschweig", "heilbronn", "poznan", "meerbusch",
        "troisdorf", "santa margherita", "francavilla", "todi",
        # Generic clay indicators
        "tierra", "terre battue",
        # French cities commonly hosting clay events
        "capfinances", "rouen",
    ]

    # ── GRASS COURT TOURNAMENTS ──
    grass_kw = [
        # Grand Slam
        "wimbledon",
        # ATP 500
        "queen", "queen's", "queens",
        "halle",
        # ATP 250
        "s-hertogenbosch", "hertogenbosch", "libema",
        "eastbourne",
        "mallorca",
        "newport",
        "stuttgart",  # WTA grass
        "berlin",     # WTA grass
        "bad homburg",
        "birmingham", "nottingham",
    ]

    # ── INDOOR HARD (technically still "Hard" but worth noting) ──
    # These are hard courts but played indoors — still return "Hard"

    if any(k in q for k in clay_kw):
        return "Clay"
    if any(k in q for k in grass_kw):
        return "Grass"

    # ── FALLBACK: check historical data for tournament surface ──
    # If we can't determine from keywords, try matching tournament name
    # against our historical match data
    return _detect_surface_from_history(q)


# Cache for historical surface lookups
_surface_history_cache = None

def _detect_surface_from_history(question_lower):
    """Try to determine surface from historical match data."""
    global _surface_history_cache

    if _surface_history_cache is None:
        try:
            hist_path = Path("data/raw")
            if hist_path.exists():
                import pandas as pd
                dfs = []
                for f in sorted(hist_path.glob("*.csv")):
                    try:
                        df = pd.read_csv(f, usecols=["tourney_name", "surface"],
                                         dtype=str, low_memory=False)
                        dfs.append(df)
                    except Exception:
                        continue
                if dfs:
                    combined = pd.concat(dfs, ignore_index=True)
                    combined = combined.dropna(subset=["tourney_name", "surface"])
                    # Build mapping: tournament name -> most common surface
                    _surface_history_cache = (
                        combined.groupby(combined["tourney_name"].str.lower())["surface"]
                        .agg(lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else "Hard")
                        .to_dict()
                    )
                else:
                    _surface_history_cache = {}
            else:
                _surface_history_cache = {}
        except Exception:
            _surface_history_cache = {}

    if not _surface_history_cache:
        return "Hard"

    # Try matching question text against known tournament names
    for tourney_name, surface in _surface_history_cache.items():
        # Check if any significant word from the tournament name appears in the question
        words = [w for w in tourney_name.split() if len(w) > 3]
        if words and all(w in question_lower for w in words):
            return surface

    return "Hard"


def generate_signals(markets_df, model, feature_cols, df_hist, min_edge=0.05, debug=False):
    today   = pd.Timestamp.now()
    signals = []

    for _, mkt in markets_df.iterrows():
        prices = mkt.get("prices", {})
        if len(prices) != 2:
            continue

        pa, pb = parse_players(mkt["question"])
        if not pa:
            continue

        vals = list(prices.values())
        keys = list(prices.keys())

        # Map prices to players — try matching last name to outcome key
        # If outcomes are "Yes"/"No", player A is the first-named player (gets "Yes")
        if pa.split()[-1].lower() in keys[0].lower():
            poly_pa = vals[0]
        elif pa.split()[-1].lower() in keys[1].lower():
            poly_pa = vals[1]
        elif keys[0].lower() in ["yes", pa.split()[0].lower()]:
            poly_pa = vals[0]
        else:
            poly_pa = vals[0]  # Default: first outcome = first player
        poly_pb = 100 - poly_pa

        # Skip effectively resolved markets (price > 95c or < 5c = match is over)
        if poly_pa >= 95 or poly_pa <= 5:
            if debug:
                print(f"  [skip] {pa} vs {pb} — market effectively resolved (price {poly_pa:.0f}c)")
            continue

        # Detect surface and tournament info from question
        surface = _detect_surface(mkt["question"])
        tournament, round_name = _extract_tournament_info(mkt["question"])

        # Build simple feature row
        pa_hist = find_player(df_hist, pa)
        pb_hist = find_player(df_hist, pb)

        try:
            sa = get_player_stats(df_hist, pa_hist, today, surface) if pa_hist else {}
        except Exception as e:
            print(f"  WARNING: Stats failed for {pa}: {e}")
            sa = {}
        try:
            sb = get_player_stats(df_hist, pb_hist, today, surface) if pb_hist else {}
        except Exception as e:
            print(f"  WARNING: Stats failed for {pb}: {e}")
            sb = {}
        h2 = get_h2h(df_hist, pa_hist, pb_hist, today) if pa_hist and pb_hist else {"total":0,"p1_wins":0,"p2_wins":0}

        # Get rankings — prefer live rankings, fall back to historical
        live_rank_a, _ = get_live_rank(pa)
        live_rank_b, _ = get_live_rank(pb)
        hist_rank_a, _ = get_player_ranking(df_hist, pa_hist, today) if pa_hist else (None, None)
        hist_rank_b, _ = get_player_ranking(df_hist, pb_hist, today) if pb_hist else (None, None)

        rank_a = live_rank_a or hist_rank_a
        rank_b = live_rank_b or hist_rank_b
        ra = rank_a or 100
        rb = rank_b or 100

        # Always log rank source to stderr for debugging (visible in pipeline output)
        src_a = "LIVE" if live_rank_a else "hist"
        src_b = "LIVE" if live_rank_b else "hist"
        print(f"  [rank] {pa}: #{rank_a} ({src_a}, live={live_rank_a}, hist={hist_rank_a}) | "
              f"{pb}: #{rank_b} ({src_b}, live={live_rank_b}, hist={hist_rank_b})", file=sys.stderr)

        if debug:
            print(f"  [h2h] {pa} (#{rank_a} {src_a}, wr={sa.get('win_rate') if pa_hist else None}) vs "
                  f"{pb} (#{rank_b} {src_b}, wr={sb.get('win_rate') if pb_hist else None}) | "
                  f"poly={poly_pa:.1f}/{poly_pb:.1f} | surface={surface}")

        def wr(s): return (s.get("win_rate") or 50) / 100
        def swr(s): return (s.get("surf_win_rate") or 50) / 100

        import math
        row = {c: 0 for c in feature_cols}
        row.update({
            "surface": 0, "tour": 0, "round": 3, "tourney_level": 2,
            "rank_diff": ra - rb,
            "rank_ratio": ra / rb if rb > 0 else 1,
            "log_rank_ratio": math.log(ra / rb) if rb > 0 and ra > 0 else 0,
            "a_win_rate_52w": wr(sa), "b_win_rate_52w": wr(sb),
            "a_win_rate_surf_52w": swr(sa), "b_win_rate_surf_52w": swr(sb),
            "a_win_rate_l10": wr(sa), "b_win_rate_l10": wr(sb),
            "a_win_rate_l20": wr(sa), "b_win_rate_l20": wr(sb),
            "form_diff_52w": wr(sa) - wr(sb),
            "form_diff_surf": swr(sa) - swr(sb),
            "a_days_since_last": sa.get("days_rest", 7),
            "b_days_since_last": sb.get("days_rest", 7),
            "a_sets_last_7d": 0, "b_sets_last_7d": 0, "fatigue_diff": 0,
            "h2h_total": h2["total"],
            "h2h_p1_wins": h2["p1_wins"] / h2["total"] if h2["total"] > 0 else 0.5,
            "h2h_surf_p1": 0.5, "h2h_advantage": 0,
            "ace_diff": (sa.get("aces_per_match") or 0) - (sb.get("aces_per_match") or 0),
            "df_diff": (sa.get("dfs_per_match") or 0) - (sb.get("dfs_per_match") or 0),
            "first_in_diff": (sa.get("first_in_pct") or 50) - (sb.get("first_in_pct") or 50),
            "win1st_diff": (sa.get("first_won_pct") or 50) - (sb.get("first_won_pct") or 50),
            "win2nd_diff": (sa.get("second_won_pct") or 50) - (sb.get("second_won_pct") or 50),
            "a_n_matches_52w": sa.get("matches_52w", 0),
            "b_n_matches_52w": sb.get("matches_52w", 0),
        })

        try:
            feat_df     = pd.DataFrame([row])[feature_cols]
            if model is None:
                raise RuntimeError("Model is None - cannot predict")
            model_prob  = float(model.predict_proba(feat_df)[0, 1])
        except Exception as e:
            # BUG FIX: Don't silently fall back to poly_price!
            # This causes model_prob == poly_price, making edge always 0
            print(f"  WARNING: Model prediction failed for {pa} vs {pb}: {type(e).__name__}: {e}")
            if debug:
                import traceback
                traceback.print_exc()
            # Instead of falling back to poly_pa/100, use a neutral 50% estimate
            model_prob = 0.5

        # Save base model probability BEFORE LSTM adjustment
        base_model_prob = model_prob

        # LSTM adjustment (if trained)
        import sys
        lstm_adj = 0.0
        try:
            from importlib import import_module
            lstm = import_module("06_lstm_learner")
            model_path = Path("models/lstm_adjuster.pkl")
            if model_path.exists():
                resolved = lstm.load_resolved_picks()
                if len(resolved) >= 20:
                    pick_stub = {
                        "model_prob": model_prob * 100,
                        "confidence": max(model_prob, 1 - model_prob) * 100,
                        "poly_price": poly_pa,
                        "edge": (model_prob - poly_pa / 100) * 100,
                        "kelly_stake": 0,
                        "volume": int(mkt.get("volume", 0)),
                        "surface": surface,
                        "tournament": tournament,
                        "market_type": "h2h",
                    }
                    lstm_adj = lstm.predict_adjustment(pick_stub, resolved)
                    model_prob = max(0.01, min(0.99, model_prob + lstm_adj))
                    if _lstm_log_once.get("first_adj") is None:
                        _lstm_log_once["first_adj"] = True
                        print(f"  [LSTM] First adjustment: {lstm_adj*100:+.1f}% (resolved={len(resolved)})", file=sys.stderr)
                elif _lstm_log_once.get("low_resolved") is None:
                    _lstm_log_once["low_resolved"] = True
                    print(f"  [LSTM] Only {len(resolved)} resolved picks (need 20+). Skipping.", file=sys.stderr)
            elif _lstm_log_once.get("no_model") is None:
                _lstm_log_once["no_model"] = True
                print(f"  [LSTM] Model not found at {model_path.resolve()}. Skipping.", file=sys.stderr)
        except Exception as e:
            if _lstm_log_once.get("error") is None:
                _lstm_log_once["error"] = True
                import traceback
                print(f"  [LSTM] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

        # Calculate edges for BOTH base model and LSTM-adjusted model
        base_edge_a = base_model_prob - poly_pa / 100
        base_edge_b = (1 - base_model_prob) - poly_pb / 100
        edge_a = model_prob - poly_pa / 100
        edge_b = (1 - model_prob) - poly_pb / 100

        # Data quality gate: if NEITHER player has meaningful historical data
        # (win_rate is None = fewer than 5 matches), the model is just guessing ~50%.
        # Skip this market entirely — no signal is better than a bad signal.
        if sa.get("win_rate") is None and sb.get("win_rate") is None:
            if debug:
                print(f"  [skip] {pa} vs {pb}: both players lack historical data")
            continue

        # Pick the side with the HIGHEST POSITIVE edge.
        # If neither side has positive edge, still include the market for display
        # but mark it as no-edge (informational only).
        if edge_a > 0 and edge_a >= edge_b:
            bet_player = pa
            edge       = edge_a
            base_edge  = base_edge_a
            poly_price = poly_pa
            prob       = model_prob
            base_prob  = base_model_prob
            poly_url_player = pa
        elif edge_b > 0 and edge_b > edge_a:
            bet_player = pb
            edge       = edge_b
            base_edge  = base_edge_b
            poly_price = poly_pb
            prob       = 1 - model_prob
            base_prob  = 1 - base_model_prob
            poly_url_player = pb
        elif edge_a >= edge_b:
            bet_player = pa
            edge       = edge_a
            base_edge  = base_edge_a
            poly_price = poly_pa
            prob       = model_prob
            base_prob  = base_model_prob
            poly_url_player = pa
        else:
            bet_player = pb
            edge       = edge_b
            base_edge  = base_edge_b
            poly_price = poly_pb
            prob       = 1 - model_prob
            base_prob  = 1 - base_model_prob
            poly_url_player = pb

        # Polymarket direct link
        slug = mkt.get("slug", "")
        poly_link = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports/tennis/games"

        signals.append({
            "market_id":    mkt.get("market_id", ""),
            "slug":         slug,
            "match":        f"{pa} vs {pb}",
            "player_a":     pa,
            "player_b":     pb,
            "bet_on":       bet_player,
            "poly_price":   round(poly_price, 1),
            "model_prob":   round(prob * 100, 1),
            "model_prob_a": round(model_prob * 100, 1),
            "model_prob_b": round((1 - model_prob) * 100, 1),
            "poly_price_a": round(poly_pa, 1),
            "poly_price_b": round(poly_pb, 1),
            "predicted_winner": pa if model_prob >= 0.5 else pb,
            "confidence":   round(max(model_prob, 1 - model_prob) * 100, 1),
            "edge":         round(edge * 100, 1),
            "base_edge":    round(base_edge * 100, 1),
            "lstm_edge":    round((edge - base_edge) * 100, 1),
            "base_prob":    round(base_prob * 100, 1),
            "lstm_adj":     round(lstm_adj * 100, 1),
            "kelly_stake":  kelly(prob, poly_price),
            "volume":       int(mkt["volume"]),
            "liquidity":    int(mkt["liquidity"]),
            "end_date":     str(mkt.get("end_date", ""))[:10],
            "poly_link":    poly_link,
            "question":     mkt["question"],
            # Stats for the card
            "sa_wr":        sa.get("win_rate"),
            "sb_wr":        sb.get("win_rate"),
            "sa_swr":       sa.get("surf_win_rate"),
            "sb_swr":       sb.get("surf_win_rate"),
            "sa_form":      sa.get("l10_form", "—"),
            "sb_form":      sb.get("l10_form", "—"),
            "sa_rest":      sa.get("days_rest"),
            "sb_rest":      sb.get("days_rest"),
            "sa_matches_52w": sa.get("matches_52w"),
            "sb_matches_52w": sb.get("matches_52w"),
            "h2h":          h2,
            "has_edge":     edge >= min_edge,  # POSITIVE edge only (not abs)
            "rank":         rank_a,
            "rank_a":       rank_a,
            "rank_b":       rank_b,
            "market_type":  "h2h",
            "tournament":   tournament,
            "round":        round_name,
            "surface":      surface,
            # ── NEW: Advanced stats ──
            "sa_vs_top10":      sa.get("vs_top10"),
            "sb_vs_top10":      sb.get("vs_top10"),
            "sa_vs_top20":      sa.get("vs_top20"),
            "sb_vs_top20":      sb.get("vs_top20"),
            "sa_vs_top50":      sa.get("vs_top50"),
            "sb_vs_top50":      sb.get("vs_top50"),
            "sa_rank_now":      live_rank_a or sa.get("rank_now"),
            "sb_rank_now":      live_rank_b or sb.get("rank_now"),
            "sa_rank_30d":      sa.get("rank_30d"),
            "sb_rank_30d":      sb.get("rank_30d"),
            "sa_rank_180d":     sa.get("rank_180d"),
            "sb_rank_180d":     sb.get("rank_180d"),
            "sa_rank_365d":     sa.get("rank_365d"),
            "sb_rank_365d":     sb.get("rank_365d"),
            "sa_rank_move":     sa.get("rank_move_180d"),
            "sb_rank_move":     sb.get("rank_move_180d"),
            "sa_rank_move_90d": sa.get("rank_move_90d"),
            "sb_rank_move_90d": sb.get("rank_move_90d"),
            "sa_rank_move_365d":sa.get("rank_move_365d"),
            "sb_rank_move_365d":sb.get("rank_move_365d"),
            "sa_elo":           sa.get("elo"),
            "sb_elo":           sb.get("elo"),
            "sa_elo_pct":       sa.get("elo_pct"),
            "sb_elo_pct":       sb.get("elo_pct"),
            "sa_momentum":      sa.get("momentum"),
            "sb_momentum":      sb.get("momentum"),
            "sa_wins_ytd":      sa.get("wins_ytd"),
            "sb_wins_ytd":      sb.get("wins_ytd"),
            "sa_losses_ytd":    sa.get("losses_ytd"),
            "sb_losses_ytd":    sb.get("losses_ytd"),
        })

    # Sort by positive edge first (true signals), then by abs edge for the rest
    signals.sort(key=lambda x: (x.get("has_edge", False), x["edge"]), reverse=True)
    return signals


def _ranking_to_tourney_prob(rank, n_rounds=7, surface_boost=1.0, form_factor=1.0):
    """
    Convert ATP/WTA ranking to tournament win probability.

    Uses a LOG RATING CURVE with TOP-RANK COMPRESSION. This solves the problem
    where rank #1 vs #2 had a huge gap despite being nearly equal in ability.

    Ranks 1-2 are compressed (treated as near-equal — both co-favorites).
    Ranks 3-5 are partially compressed (clear top tier, but not as dominant).
    Ranks 6+ use actual ranking (real talent separation kicks in).

    Surface boost and form factor then differentiate co-ranked players
    (e.g. Alcaraz overtakes Sinner on clay despite lower ranking).

    Calibrated against actual GS/Masters outcomes (2015-2024):
      GS:      R1 ~12%  R2 ~10%  R3 ~8%  R5 ~4%  R10 ~1.7%  R20 ~0.9%
      Masters: R1 ~15%  R2 ~12%  R3 ~10% R5 ~5%  R10 ~2.0%  R20 ~1.1%

    With surface + form boosts (realistic scenarios):
      Sinner (R1) US Open:  ~19%    Alcaraz (R2) US Open: ~13%
      Alcaraz (R2) French:  ~17%    Sinner (R1) French:   ~17%  ← co-favorites
    """
    import math

    if rank is None or rank <= 0:
        return 0.005  # Unknown / unranked → ~0.5%

    # Step 1: Compress top ranks so #1 and #2 are treated as near-equal
    if rank <= 2:
        effective_rank = 1.0 + (rank - 1) * 0.3     # R1 → 1.0, R2 → 1.3
    elif rank <= 5:
        effective_rank = 1.6 + (rank - 3) * 0.8     # R3 → 1.6, R4 → 2.4, R5 → 3.2
    else:
        effective_rank = rank                         # R6+ → actual rank

    # Step 2: Rating via log curve
    base_rating = 10.0
    decay = 1.3
    temperature = 1.5
    rating = base_rating - decay * math.log(max(effective_rank, 1))
    raw_prob = math.exp(rating / temperature)

    # Step 3: Normalise against the full compressed field
    if n_rounds >= 7:
        draw_size = 128   # Grand Slam
    elif n_rounds >= 6:
        draw_size = 96
    else:
        draw_size = 56    # Masters 1000

    field_sum = 0
    for r in range(1, draw_size + 1):
        if r <= 2:
            er = 1.0 + (r - 1) * 0.3
        elif r <= 5:
            er = 1.6 + (r - 3) * 0.8
        else:
            er = r
        field_sum += math.exp((base_rating - decay * math.log(max(er, 1))) / temperature)

    prob = (raw_prob / field_sum) * surface_boost * form_factor

    # Cap: even dominant player shouldn't exceed ~50% GS or ~55% Masters
    max_prob = 0.50 if n_rounds >= 7 else 0.55
    return max(0.001, min(max_prob, prob))


def _compute_slam_pedigree(df_hist, player, tourney_level="G", lookback_years=4):
    """
    Compute a pedigree factor from historical performance at Grand Slams
    (or Masters). Players who consistently go deep and win titles get a boost.
    Players who underperform at Slams relative to ranking get penalised.

    Returns a multiplier: 1.0 = neutral, >1 = Slam pedigree, <1 = Slam underperformer

    Scoring:
      Title (won final):    10 pts     Lost final:           6 pts
      Semifinal:            4 pts      Quarterfinal:         2 pts
      R16:                  1 pt       Earlier:              0 pts

    The score is normalised per-tournament to give a rate, then converted
    to a multiplier. This rewards Alcaraz (4 titles in 18 Slams) over
    Zverev (0 titles in 28 Slams) even though Zverev's raw point total
    is similar.
    """
    import numpy as np
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_years * 365)

    # Gather all matches at this tourney level
    big = df_hist[
        (df_hist["tourney_level"] == tourney_level) &
        (df_hist["date"] >= cutoff)
    ]

    wins = big[big["winner"] == player]
    losses = big[big["loser"] == player]

    if len(wins) + len(losses) < 3:
        return 1.0  # Not enough data

    ROUND_PTS_WIN  = {"F": 10, "SF": 4, "QF": 2, "R16": 1}
    ROUND_PTS_LOSS = {"F": 6,  "SF": 4, "QF": 2, "R16": 1}

    score = 0
    for _, row in wins.iterrows():
        score += ROUND_PTS_WIN.get(row.get("round", ""), 0)
    for _, row in losses.iterrows():
        score += ROUND_PTS_LOSS.get(row.get("round", ""), 0)

    # Count distinct tournaments entered (by tourney_id)
    all_matches = pd.concat([wins, losses])
    n_tourneys = all_matches["tourney_id"].nunique() if "tourney_id" in all_matches.columns else max(1, (len(wins) + len(losses)) // 5)

    # Per-tournament rate: how many pedigree points per Slam entered
    rate = score / max(n_tourneys, 1)

    # Title bonus: winning Slams is disproportionately important
    titles = len(wins[wins["round"] == "F"])
    title_bonus = titles * 0.08  # Each title adds 8% to the multiplier

    # Convert rate to multiplier:
    # Rate 0-2   → 0.85 (early-round exits = Slam underperformer)
    # Rate 2-4   → 0.95 (QF level)
    # Rate 4-6   → 1.05 (SF level, solid)
    # Rate 6-8   → 1.15 (finalist level)
    # Rate 8+    → 1.25 (champion level)
    if rate >= 8:
        factor = 1.25
    elif rate >= 6:
        factor = 1.15
    elif rate >= 4:
        factor = 1.05
    elif rate >= 2:
        factor = 0.95
    else:
        factor = 0.85

    factor += title_bonus

    return max(0.70, min(1.60, factor))


def generate_outright_signals(markets_df, df_hist, min_edge=0.01, debug=False):
    """
    Generate signals from outright tournament winner markets.
    These are 'Will X win the French Open?' style Yes/No markets.
    Uses player RANKING as the primary signal — much more predictive than win rate alone.
    Enhanced with Slam pedigree factor (how well they perform in big tournaments).
    """
    today = pd.Timestamp.now()
    signals = []
    n_matched = 0
    n_unmatched = 0

    for _, mkt in markets_df.iterrows():
        prices = mkt.get("prices", {})
        if len(prices) != 2:
            continue

        # Parse outright market
        player, tournament = parse_outright(mkt["question"])
        if not player:
            continue

        # Get Yes price (what Polymarket charges for this player to win)
        poly_yes = prices.get("Yes", prices.get("yes", None))
        if poly_yes is None:
            vals = list(prices.values())
            poly_yes = vals[0]

        # Skip already-resolved markets (price at 0¢ or 100¢)
        if poly_yes <= 0.5 or poly_yes >= 99.5:
            continue

        # Look up player in historical data
        player_hist = find_player(df_hist, player)
        if not player_hist:
            n_unmatched += 1
            if debug:
                print(f"  [miss] No match for '{player}'")
            continue

        n_matched += 1

        # Determine surface from tournament name
        surface = "Hard"
        t_lower = tournament.lower()
        if "french" in t_lower or "roland" in t_lower or "rome" in t_lower or "madrid" in t_lower or "barcelona" in t_lower or "monte carlo" in t_lower:
            surface = "Clay"
        elif "wimbledon" in t_lower or "queen" in t_lower or "halle" in t_lower:
            surface = "Grass"

        # Get player stats and ranking — prefer live rankings
        try:
            sa = get_player_stats(df_hist, player_hist, today, surface)
        except Exception as e:
            print(f"  WARNING: Stats failed for {player}: {e}")
            sa = {}
        live_rank, _ = get_live_rank(player)
        hist_rank, rank_pts = get_player_ranking(df_hist, player_hist, today)
        rank = live_rank or hist_rank

        # Tournament rounds
        is_grand_slam = any(gs in t_lower for gs in ["french", "australian", "wimbledon", "us open", "roland"])
        n_rounds = 7 if is_grand_slam else 5

        # ── MODEL PROBABILITY (ranking-based with stats adjustment) ──
        # Surface boost: players who crush on this surface get a meaningful bump
        surface_boost = 1.0
        swr = sa.get("surf_win_rate")
        wr  = sa.get("win_rate")
        if swr is not None and swr > 55:
            # e.g. 80% SWR → 1.50x, 70% SWR → 1.30x, 60% SWR → 1.10x
            surface_boost = 1.0 + (swr - 55) * 0.02
        elif swr is not None and swr < 40:
            # Poor on surface: e.g. 30% SWR → 0.70x
            surface_boost = max(0.50, 1.0 - (40 - swr) * 0.03)

        # Form factor: hot streaks and cold spells matter for tournaments
        form_factor = 1.0
        if wr is not None:
            if wr >= 75:
                form_factor = 1.30    # Dominant form (Sinner 2024 type)
            elif wr >= 65:
                form_factor = 1.15    # Strong form
            elif wr >= 55:
                form_factor = 1.05    # Solid
            elif wr < 40:
                form_factor = 0.65    # Serious slump
            elif wr < 50:
                form_factor = 0.80    # Below average

        # Slam pedigree: how well do they actually perform at big tournaments?
        # This separates proven Slam champions (Alcaraz, Sinner, Djokovic)
        # from players who underperform at Slams despite high ranking (Zverev, Rublev)
        tourney_lvl = "G" if is_grand_slam else "M"
        pedigree = _compute_slam_pedigree(df_hist, player_hist, tourney_lvl)

        model_prob = _ranking_to_tourney_prob(rank, n_rounds, surface_boost, form_factor * pedigree)

        if debug:
            print(f"  [hit]  '{player}' → '{player_hist}' | rank={rank} | wr={wr} | swr={swr} | pedigree={pedigree:.2f} | prob={model_prob:.3f} | poly={poly_yes/100:.3f}")

        poly_dec = poly_yes / 100
        edge = model_prob - poly_dec

        # Polymarket link
        slug = mkt.get("slug", "")
        poly_link = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports/tennis/games"

        signals.append({
            "market_id":       mkt.get("market_id", ""),
            "slug":            slug,
            "match":           f"{player} — {tournament}",
            "player_a":        player,
            "player_b":        tournament,
            "bet_on":          player,
            "poly_price":      round(poly_yes, 1),
            "model_prob":      round(model_prob * 100, 1),
            "model_prob_a":    round(model_prob * 100, 1),
            "model_prob_b":    round((1 - model_prob) * 100, 1),
            "poly_price_a":    round(poly_yes, 1),
            "poly_price_b":    round(100 - poly_yes, 1),
            "predicted_winner": player,
            "confidence":      round(max(model_prob, 1 - model_prob) * 100, 1),
            "edge":            round(edge * 100, 1),
            "kelly_stake":     kelly(model_prob, poly_yes) if edge > 0 else 0.0,
            "volume":          int(mkt["volume"]),
            "liquidity":       int(mkt["liquidity"]),
            "end_date":        str(mkt.get("end_date", ""))[:10],
            "poly_link":       poly_link,
            "question":        mkt["question"],
            "sa_wr":           sa.get("win_rate"),
            "sb_wr":           None,
            "sa_swr":          sa.get("surf_win_rate"),
            "sb_swr":          None,
            "sa_form":         sa.get("l10_form", "—"),
            "sb_form":         "—",
            "sa_rest":         sa.get("days_rest"),
            "sb_rest":         None,
            "h2h":             {"total": 0, "p1_wins": 0, "p2_wins": 0},
            "has_edge":        edge >= min_edge,
            "market_type":     "outright",
            "rank":            rank,
            "tournament":      tournament,
            "round":           "Outright Winner",
            "surface":         surface,
            # Advanced stats (outright — only player A has data)
            "sa_vs_top10":     sa.get("vs_top10"),
            "sb_vs_top10":     None,
            "sa_vs_top20":     sa.get("vs_top20"),
            "sb_vs_top20":     None,
            "sa_vs_top50":     sa.get("vs_top50"),
            "sb_vs_top50":     None,
            "sa_rank_now":     live_rank or sa.get("rank_now"),
            "sb_rank_now":     None,
            "sa_rank_30d":     sa.get("rank_30d"),
            "sb_rank_30d":     None,
            "sa_rank_180d":    sa.get("rank_180d"),
            "sb_rank_180d":    None,
            "sa_rank_365d":    sa.get("rank_365d"),
            "sb_rank_365d":    None,
            "sa_rank_move":    sa.get("rank_move_180d"),
            "sb_rank_move":    None,
            "sa_rank_move_90d": sa.get("rank_move_90d"),
            "sb_rank_move_90d": None,
            "sa_rank_move_365d":sa.get("rank_move_365d"),
            "sb_rank_move_365d":None,
            "sa_elo":          sa.get("elo"),
            "sb_elo":          None,
            "sa_elo_pct":      sa.get("elo_pct"),
            "sb_elo_pct":      None,
            "sa_momentum":     sa.get("momentum"),
            "sb_momentum":     None,
            "sa_wins_ytd":     sa.get("wins_ytd"),
            "sb_wins_ytd":     None,
            "sa_losses_ytd":   sa.get("losses_ytd"),
            "sb_losses_ytd":   None,
        })

    print(f"  → {n_matched} players matched, {n_unmatched} unmatched in historical data")
    # Sort: positive-edge signals first, then by edge value descending
    signals.sort(key=lambda x: (x.get("has_edge", False), x["edge"]), reverse=True)
    return signals


# ─── HTML CARD GENERATOR ──────────────────────────────────────────────────────

def edge_tier(edge):
    # Only positive edges get signal tiers
    if edge >= 10: return ("🔥 STRONG",  "#d84315", "strong")
    if edge >= 7:  return ("✅ SOLID",   "#d4740a", "solid")
    if edge >= 5:  return ("👀 WATCH",   "#495057", "watch")
    if edge >= 0:  return ("〰️ MARGINAL","#6c757d",    "marginal")
    return                 ("⛔ NO EDGE", "#f44336",    "no-edge")


def value_badge(model_prob, poly_price):
    """Flag when model says >50% win AND is significantly above the Polymarket price.
    model_prob: model's win% (e.g. 62.0)
    poly_price: Poly price in cents (e.g. 45.0)
    Returns (html_badge, is_value) tuple.
    """
    if model_prob <= 50:
        return "", False
    gap = model_prob - poly_price  # both in same units (percent / cents)
    if gap >= 15 and model_prob >= 60:
        return '<span class="value-badge value-gold" title="Model says likely winner AND Poly price way too low">💰 BEST VALUE</span>', True
    elif gap >= 8 and model_prob >= 55:
        return '<span class="value-badge value-green" title="Model says likely winner AND Poly underpriced">💎 VALUE</span>', True
    elif gap >= 3:
        return '<span class="value-badge value-blue" title="Model favors this player over Poly price">📈 EDGE+</span>', True
    return "", False


def generate_signals_data_only(markets_df, df_hist=None, debug=False):
    """Generate h2h signals using ONLY raw Polymarket prices (no model needed).
    Used when the model hasn't been trained yet."""
    today = pd.Timestamp.now()
    signals = []

    for _, mkt in markets_df.iterrows():
        prices = mkt.get("prices", {})
        if len(prices) != 2:
            continue

        pa, pb = parse_players(mkt["question"])
        if not pa:
            continue

        vals = list(prices.values())
        keys = list(prices.keys())

        # Map prices to players
        if pa.split()[-1].lower() in keys[0].lower():
            poly_pa = vals[0]
        elif pa.split()[-1].lower() in keys[1].lower():
            poly_pa = vals[1]
        elif keys[0].lower() in ["yes", pa.split()[0].lower()]:
            poly_pa = vals[0]
        else:
            poly_pa = vals[0]
        poly_pb = 100 - poly_pa

        # Skip effectively resolved markets (price > 95c or < 5c = match is over)
        if poly_pa >= 95 or poly_pa <= 5:
            if debug:
                print(f"  [skip] {pa} vs {pb} — market effectively resolved (price {poly_pa:.0f}c)")
            continue

        surface = _detect_surface(mkt["question"])
        tournament, round_name = _extract_tournament_info(mkt["question"])

        # Use Polymarket price as "model prob" in data-only mode
        model_prob = poly_pa / 100
        predicted_winner = pa if poly_pa >= poly_pb else pb
        confidence = max(poly_pa, poly_pb)

        # Edge is 0 (no model vs market diff), but still show picks
        slug = mkt.get("slug", "")
        poly_link = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports/tennis/games"

        signals.append({
            "match":        f"{pa} vs {pb}",
            "player_a":     pa,
            "player_b":     pb,
            "bet_on":       predicted_winner,
            "poly_price":   round(max(poly_pa, poly_pb), 1),
            "model_prob":   round(confidence, 1),
            "model_prob_a": round(poly_pa, 1),
            "model_prob_b": round(poly_pb, 1),
            "poly_price_a": round(poly_pa, 1),
            "poly_price_b": round(poly_pb, 1),
            "predicted_winner": predicted_winner,
            "confidence":   round(confidence, 1),
            "edge":         0.0,
            "kelly_stake":  0.0,
            "volume":       int(mkt.get("volume", 0)),
            "liquidity":    int(mkt.get("liquidity", 0)),
            "end_date":     str(mkt.get("end_date", ""))[:10],
            "poly_link":    poly_link,
            "question":     mkt["question"],
            "sa_wr":        None, "sb_wr": None,
            "sa_swr":       None, "sb_swr": None,
            "sa_form":      "—", "sb_form": "—",
            "sa_rest":      None, "sb_rest": None,
            "h2h":          {"total": 0, "p1_wins": 0, "p2_wins": 0},
            "has_edge":     False,
            "rank":         None,
            "market_type":  "h2h",
            "tournament":   tournament,
            "round":        round_name,
            "surface":      surface,
            "data_only":    True,
            # Advanced stats (null in data-only mode)
            "sa_vs_top10": None, "sb_vs_top10": None,
            "sa_vs_top20": None, "sb_vs_top20": None,
            "sa_vs_top50": None, "sb_vs_top50": None,
            "sa_rank_now": None, "sb_rank_now": None,
            "sa_rank_30d": None, "sb_rank_30d": None,
            "sa_rank_180d": None, "sb_rank_180d": None,
            "sa_rank_365d": None, "sb_rank_365d": None,
            "sa_rank_move": None, "sb_rank_move": None,
            "sa_rank_move_90d": None, "sb_rank_move_90d": None,
            "sa_rank_move_365d": None, "sb_rank_move_365d": None,
            "sa_elo": None, "sb_elo": None,
            "sa_elo_pct": None, "sb_elo_pct": None,
            "sa_momentum": None, "sb_momentum": None,
            "sa_wins_ytd": None, "sb_wins_ytd": None,
            "sa_losses_ytd": None, "sb_losses_ytd": None,
        })

    if debug:
        print(f"    [data-only] Parsed {len(signals)} h2h markets")
    return sorted(signals, key=lambda x: x["volume"], reverse=True)


def generate_outright_signals_data_only(markets_df, debug=False):
    """Generate outright tournament signals using ONLY raw Polymarket prices (no hist data)."""
    signals = []

    for _, mkt in markets_df.iterrows():
        prices = mkt.get("prices", {})
        if len(prices) != 2:
            continue

        player, tournament = parse_outright(mkt["question"])
        if not player:
            continue

        # Get Yes price
        poly_yes = prices.get("Yes", prices.get("yes", None))
        if poly_yes is None:
            vals = list(prices.values())
            poly_yes = vals[0]

        if poly_yes <= 0.5 or poly_yes >= 99.5:
            continue

        surface = _detect_surface(mkt["question"])
        slug = mkt.get("slug", "")
        poly_link = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports/tennis/games"

        signals.append({
            "match":        f"{player} — {tournament}",
            "player_a":     player,
            "player_b":     "The Field",
            "bet_on":       player,
            "poly_price":   round(poly_yes, 1),
            "model_prob":   round(poly_yes, 1),  # Use Poly price as model estimate
            "model_prob_a": round(poly_yes, 1),
            "model_prob_b": round(100 - poly_yes, 1),
            "poly_price_a": round(poly_yes, 1),
            "poly_price_b": round(100 - poly_yes, 1),
            "predicted_winner": player,
            "confidence":   round(poly_yes, 1),
            "edge":         0.0,
            "kelly_stake":  0.0,
            "volume":       int(mkt.get("volume", 0)),
            "liquidity":    int(mkt.get("liquidity", 0)),
            "end_date":     str(mkt.get("end_date", ""))[:10],
            "poly_link":    poly_link,
            "question":     mkt["question"],
            "sa_wr": None, "sb_wr": None,
            "sa_swr": None, "sb_swr": None,
            "sa_form": "—", "sb_form": "—",
            "sa_rest": None, "sb_rest": None,
            "h2h":          {"total": 0, "p1_wins": 0, "p2_wins": 0},
            "has_edge":     False,
            "rank":         None,
            "market_type":  "outright",
            "tournament":   tournament,
            "round":        "Outright",
            "surface":      surface,
            "data_only":    True,
        })

    if debug:
        print(f"    [data-only] Parsed {len(signals)} outright markets")
    return sorted(signals, key=lambda x: x["volume"], reverse=True)


def build_html(signals, generated_at, min_edge, min_volume, data_only_mode=False):
    edge_signals = [s for s in signals if s.get("has_edge")]
    n_signals = len(edge_signals)
    n_total   = len(signals)
    total_vol = sum(s["volume"] for s in signals)

    def stat_bar(val, max_val=100):
        if val is None:
            return '<span class="na">N/A</span>'
        pct = min(100, (val / max_val) * 100)
        color = "#087f23" if val >= 55 else "#6c757d" if val >= 45 else "#d84315"
        return f'<div class="bar-wrap"><div class="bar" style="width:{pct}%;background:{color}"></div><span>{val}%</span></div>'

    cards_html = ""
    if not edge_signals:
        cards_html = f'<div class="empty">No betting signals above threshold — but {n_total} match prediction(s) shown below.</div>'
    else:
        for i, s in enumerate(edge_signals):
            tier_label, tier_color, tier_class = edge_tier(s["edge"])
            vbadge_html, is_value = value_badge(s["model_prob"], s["poly_price"])
            edge_sign = "+" if s["edge"] > 0 else ""
            pa, pb = s["player_a"], s["player_b"]
            h2h = s["h2h"]
            h2h_str = f'{h2h["p1_wins"]}–{h2h["p2_wins"]}' if h2h["total"] > 0 else "No H2H"

            rest_a = f'{s["sa_rest"]}d rest' if s["sa_rest"] is not None else "—"
            rest_b = f'{s["sb_rest"]}d rest' if s["sb_rest"] is not None else "—"

            # Format new advanced stats
            def fmt_vs(vs):
                if vs is None: return "—"
                return f'{vs["w"]}W-{vs["l"]}L'
            def fmt_rank_move(move):
                if move is None: return "—"
                if move > 0: return f'<span style="color:#087f23">▲{move}</span>'
                if move < 0: return f'<span style="color:#d84315">▼{abs(move)}</span>'
                return "→0"
            def fmt_elo(e, pct=None):
                if not e: return "—"
                s = str(e)
                if pct is not None: s += f' <span style="color:#888; font-size:9px">(P{pct})</span>'
                return s
            def fmt_momentum(m):
                if m is None: return "—"
                if m >= 70: return f'<span style="color:#087f23">{m}/100</span>'
                if m <= 30: return f'<span style="color:#d84315">{m}/100</span>'
                return f'{m}/100'
            def fmt_rank_hist(now, d30, d180, d365):
                parts = []
                if d365: parts.append(f"1y: #{d365}")
                if d180: parts.append(f"6m: #{d180}")
                if d30: parts.append(f"1m: #{d30}")
                if now: parts.append(f"Now: #{now}")
                return " → ".join(parts) if parts else "—"

            tourney = s.get('tournament', '')
            rnd = s.get('round', '')
            sfc = s.get('surface', '')
            tourney_line = f"{tourney}" if tourney else ""
            if rnd:
                tourney_line += f" • {rnd}" if tourney_line else rnd
            if sfc:
                tourney_line += f" • {sfc}" if tourney_line else sfc

            cards_html += f"""
            <div class="card tier-{tier_class}" data-edge="{abs(s['edge'])}">
                <div class="card-header">
                    <div class="match-info">
                        <span class="match-num">#{i+1}</span>
                        <span class="match-title">{s['match']}</span>
                        <span class="tier-badge" style="color:{tier_color}">{tier_label}</span>{vbadge_html}
                    </div>
                    <div class="card-meta">
                        {f'<span class="tourney">{tourney_line}</span>' if tourney_line else ''}
                        <span class="vol">Vol: ${s['volume']:,}</span>
                        <span class="liq">Liq: ${s['liquidity']:,}</span>
                        <span class="closes">Closes: {s['end_date'] or '—'}</span>
                    </div>
                </div>

                <div class="card-body">
                    <div class="bet-action">
                        <div class="bet-label">BET ON</div>
                        <div class="bet-player">{s['bet_on']}</div>
                        <div class="bet-numbers">
                            <div class="number-block">
                                <div class="num-label">Poly Price</div>
                                <div class="num-value poly">{s['poly_price']}¢</div>
                            </div>
                            <div class="arrow">→</div>
                            <div class="number-block">
                                <div class="num-label">Model Says</div>
                                <div class="num-value model">{s['model_prob']}%</div>
                            </div>
                            <div class="number-block edge-block">
                                <div class="num-label">Edge</div>
                                <div class="num-value edge" style="color:{tier_color}">{edge_sign}{s['edge']}%</div>
                            </div>
                            <div class="number-block">
                                <div class="num-label">Kelly (25%)</div>
                                <div class="num-value kelly">{s['kelly_stake']}% BK</div>
                            </div>
                        </div>
                    </div>

                    <div class="stats-grid">
                        <div class="stats-col">
                            <div class="stats-header">{pa}</div>
                            <div class="stat-row">
                                <span class="stat-label">Win Rate (52w)</span>
                                {stat_bar(s['sa_wr'])}
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Surface WR</span>
                                {stat_bar(s['sa_swr'])}
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Last 10</span>
                                <span class="stat-val">{s['sa_form']}</span>
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Rest</span>
                                <span class="stat-val">{rest_a}</span>
                            </div>
                        </div>

                        <div class="h2h-col">
                            <div class="stats-header">H2H</div>
                            <div class="h2h-record">{h2h_str}</div>
                            <div class="h2h-sub">{h2h['total']} meetings</div>
                        </div>

                        <div class="stats-col">
                            <div class="stats-header">{pb}</div>
                            <div class="stat-row">
                                <span class="stat-label">Win Rate (52w)</span>
                                {stat_bar(s['sb_wr'])}
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Surface WR</span>
                                {stat_bar(s['sb_swr'])}
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Last 10</span>
                                <span class="stat-val">{s['sb_form']}</span>
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">Rest</span>
                                <span class="stat-val">{rest_b}</span>
                            </div>
                        </div>
                    </div>

                    <details class="advanced-stats">
                        <summary>Advanced Stats</summary>
                        <div class="adv-grid">
                            <div class="adv-col">
                                <div class="adv-header">{pa}</div>
                                <div class="stat-row"><span class="stat-label">ELO</span><span class="stat-val">{fmt_elo(s.get('sa_elo'), s.get('sa_elo_pct'))}</span></div>
                                <div class="stat-row"><span class="stat-label">vs Top 10</span><span class="stat-val">{fmt_vs(s.get('sa_vs_top10'))}</span></div>
                                <div class="stat-row"><span class="stat-label">vs Top 20</span><span class="stat-val">{fmt_vs(s.get('sa_vs_top20'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 12m</span><span class="stat-val">{fmt_rank_move(s.get('sa_rank_move_365d'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 6m</span><span class="stat-val">{fmt_rank_move(s.get('sa_rank_move'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 3m</span><span class="stat-val">{fmt_rank_move(s.get('sa_rank_move_90d'))}</span></div>
                                <div class="stat-row"><span class="stat-label">YTD Record</span><span class="stat-val">{s.get('sa_wins_ytd', 0)}W-{s.get('sa_losses_ytd', 0)}L</span></div>
                            </div>
                            <div class="adv-col">
                                <div class="adv-header">{pb}</div>
                                <div class="stat-row"><span class="stat-label">ELO</span><span class="stat-val">{fmt_elo(s.get('sb_elo'), s.get('sb_elo_pct'))}</span></div>
                                <div class="stat-row"><span class="stat-label">vs Top 10</span><span class="stat-val">{fmt_vs(s.get('sb_vs_top10'))}</span></div>
                                <div class="stat-row"><span class="stat-label">vs Top 20</span><span class="stat-val">{fmt_vs(s.get('sb_vs_top20'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 12m</span><span class="stat-val">{fmt_rank_move(s.get('sb_rank_move_365d'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 6m</span><span class="stat-val">{fmt_rank_move(s.get('sb_rank_move'))}</span></div>
                                <div class="stat-row"><span class="stat-label">Rank Move 3m</span><span class="stat-val">{fmt_rank_move(s.get('sb_rank_move_90d'))}</span></div>
                                <div class="stat-row"><span class="stat-label">YTD Record</span><span class="stat-val">{s.get('sb_wins_ytd', 0)}W-{s.get('sb_losses_ytd', 0)}L</span></div>
                            </div>
                        </div>
                    </details>
                </div>

                <div class="card-footer">
                    <div class="instructions">
                        <span class="inst-label">HOW TO BET:</span>
                        Go to Polymarket → Search <strong>"{pa.split()[-1]} vs {pb.split()[-1]}"</strong>
                        → Buy <strong>YES on {s['bet_on']}</strong> at market price
                        → Target entry near <strong>{s['poly_price']}¢ or lower</strong>
                    </div>
                    <div class="card-actions">
                        <button class="bet-toggle" onclick="toggleBet(this)" data-match="{s['match']}" data-bet-on="{s['bet_on']}" data-poly-price="{s['poly_price']}" data-model-prob="{s['model_prob']}" data-edge="{s['edge']}" data-kelly="{s['kelly_stake']}" data-market-type="{s.get('market_type','h2h')}" data-tournament="{s.get('tournament','')}" data-surface="{s.get('surface','')}">I BET THIS</button>
                        <a href="{s['poly_link']}" target="_blank" class="poly-link">Open on Polymarket →</a>
                    </div>
                </div>
            </div>
            """

    # ── STRAIGHT PICKS — WHO WINS (grouped by tournament) ──
    picks_html = ""
    outright_signals = [s for s in signals if s.get("market_type") == "outright"]
    match_signals = [s for s in signals if s.get("market_type") == "h2h"]

    def _conf_style(conf):
        if conf >= 65:
            return "#087f23", "HIGH", "#e8f5e9"
        elif conf >= 55:
            return "#d4740a", "MED", "#fff3e0"
        else:
            return "#6c757d", "LOW", "#f5f5f5"

    def _pick_row(winner, prob_win, opponent, conf, rnd="", poly_link="", poly_price=0):
        conf_color, conf_label, conf_bg = _conf_style(conf)
        rnd_html = f'<span style="color:var(--text-dim); font-size:10px; margin-left:6px">({rnd})</span>' if rnd and rnd != "Outright Winner" else ""
        link_html = f' <a href="{poly_link}" target="_blank" style="color:#1565c0; font-size:10px; text-decoration:none">Polymarket &rarr;</a>' if poly_link else ""
        vbadge, _ = value_badge(prob_win, poly_price)
        poly_html = f'<span style="color:var(--text-dim); font-size:10px; font-family:var(--mono); margin-left:4px">(Poly: {poly_price}¢)</span>' if poly_price else ""
        return f"""
            <tr>
                <td style="color:{conf_color}; font-weight:700; font-size:13px">{winner}{rnd_html}</td>
                <td style="color:var(--text-dim); font-size:12px">{opponent}</td>
                <td style="color:{conf_color}; font-weight:700; font-size:14px; font-family:var(--mono)">{prob_win}%{poly_html}</td>
                <td><span style="background:{conf_bg}; color:{conf_color}; padding:2px 8px; font-size:10px; font-weight:700; letter-spacing:0.05em">{conf_label} {conf}%</span></td>
                <td>{vbadge} <button class="bet-toggle-mini" onclick="toggleBet(this)" data-match="{winner}" data-bet-on="{winner}" data-poly-price="{poly_price}" data-model-prob="{prob_win}" data-edge="0" data-kelly="0" data-market-type="pick" data-tournament="" data-surface="">💰</button>{link_html}</td>
            </tr>"""

    # ── OUTRIGHT WINNERS (grouped by tournament) ──
    outright_html = ""
    if outright_signals:
        from collections import OrderedDict
        tourney_groups = OrderedDict()
        for s in sorted(outright_signals, key=lambda x: x["confidence"], reverse=True):
            t = s.get("tournament", "Unknown") or "Unknown"
            sfc = s.get("surface", "") or ""
            key = f"{t} ({sfc})" if sfc else t
            tourney_groups.setdefault(key, []).append(s)

        outright_rows = ""
        for tourney_label, group in tourney_groups.items():
            outright_rows += f"""
            <tr class="tourney-divider">
                <td colspan="5" style="padding:10px 12px 6px; font-weight:700; font-size:12px; color:var(--amber); letter-spacing:0.04em; border-bottom:1px solid var(--border); background:var(--surface2)">{tourney_label}</td>
            </tr>"""
            for s in group:
                outright_rows += _pick_row(
                    winner=s["player_a"],
                    prob_win=s["model_prob_a"],
                    opponent="vs the field",
                    conf=s["confidence"],
                    poly_link=s.get("poly_link", ""),
                    poly_price=s.get("poly_price", 0),
                )

        outright_html = f"""
        <div class="picks-section">
            <div class="picks-header" style="background:linear-gradient(135deg, #4a148c 0%, #6a1b9a 100%)">
                <h2>OUTRIGHT WINNERS — WHO WINS THE TOURNAMENT</h2>
                <div class="picks-subtitle" style="color:#ce93d8">Model's predicted tournament winner, grouped by event</div>
            </div>
            <table class="picks-table">
                <thead><tr>
                    <th>Player</th><th>vs</th><th>Win % (Poly)</th><th>Confidence</th><th>Signal</th>
                </tr></thead>
                <tbody>{outright_rows}</tbody>
            </table>
        </div>
        """

    # ── MATCH PICKS (grouped by tournament) ──
    match_html = ""
    if match_signals:
        from collections import OrderedDict
        tourney_groups = OrderedDict()
        for s in sorted(match_signals, key=lambda x: (x.get("tournament", ""), -x["confidence"])):
            t = s.get("tournament", "Unknown") or "Unknown"
            sfc = s.get("surface", "") or ""
            key = f"{t} ({sfc})" if sfc else t
            tourney_groups.setdefault(key, []).append(s)

        match_rows = ""
        for tourney_label, group in tourney_groups.items():
            match_rows += f"""
            <tr class="tourney-divider">
                <td colspan="5" style="padding:10px 12px 6px; font-weight:700; font-size:12px; color:var(--amber); letter-spacing:0.04em; border-bottom:1px solid var(--border); background:var(--surface2)">{tourney_label}</td>
            </tr>"""
            for s in sorted(group, key=lambda x: x["confidence"], reverse=True):
                winner = s["predicted_winner"]
                loser = s["player_b"] if winner == s["player_a"] else s["player_a"]
                prob_win = s["model_prob_a"] if winner == s["player_a"] else s["model_prob_b"]
                poly_p = s["poly_price_a"] if winner == s["player_a"] else s["poly_price_b"]
                rnd = s.get("round", "") or ""
                match_rows += _pick_row(
                    winner=winner,
                    prob_win=prob_win,
                    opponent=f"vs {loser}",
                    conf=s["confidence"],
                    rnd=rnd,
                    poly_link=s.get("poly_link", ""),
                    poly_price=poly_p,
                )

        match_html = f"""
        <div class="picks-section">
            <div class="picks-header">
                <h2>MATCH PICKS — WHO WINS EACH MATCH</h2>
                <div class="picks-subtitle" style="color:#a5d6a7">Individual match predictions, grouped by tournament</div>
            </div>
            <table class="picks-table">
                <thead><tr>
                    <th>Pick (Winner)</th><th>Opponent</th><th>Win % (Poly)</th><th>Confidence</th><th>Signal</th>
                </tr></thead>
                <tbody>{match_rows}</tbody>
            </table>
        </div>
        """

    picks_html = outright_html + match_html

    # ── ALL MATCHES: MODEL PREDICTIONS TABLE ──
    predictions_html = ""
    if signals:
        pred_rows = ""
        for s in sorted(signals, key=lambda x: x["confidence"], reverse=True):
            conf = s["confidence"]
            conf_color = "#087f23" if conf >= 65 else "#d4740a" if conf >= 55 else "#6c757d"
            edge_val = s["edge"]
            edge_sign = "+" if edge_val > 0 else ""
            edge_color = "#087f23" if abs(edge_val) >= 5 else "#d4740a" if abs(edge_val) >= 3 else "#6c757d"
            has_edge_marker = "●" if s.get("has_edge") else ""
            edge_marker_color = "#d84315" if abs(edge_val) >= 10 else "#d4740a" if abs(edge_val) >= 5 else "#adb5bd"

            rank_str = f"#{s['rank']}" if s.get('rank') else "—"
            tourney_str = s.get('tournament', '') or '—'
            rnd_str = s.get('round', '') or '—'
            sfc_str = s.get('surface', '') or ''
            pred_rows += f"""
            <tr>
                <td class="pred-match">{s['match']}</td>
                <td class="pred-tourney">{tourney_str}</td>
                <td class="pred-round">{rnd_str}</td>
                <td class="pred-winner" style="color:{conf_color}">{s['predicted_winner']}</td>
                <td class="pred-conf" style="color:{conf_color}">{conf}%</td>
                <td class="pred-rank">{rank_str}</td>
                <td class="pred-poly">{s['poly_price_a']}¢ / {s['poly_price_b']}¢</td>
                <td class="pred-model">{s['model_prob_a']}% / {s['model_prob_b']}%</td>
                <td class="pred-edge" style="color:{edge_color}">{edge_sign}{edge_val}%</td>
                <td class="pred-signal"><button class="bet-toggle-mini" onclick="toggleBet(this)" data-match="{s['match']}" data-bet-on="{s['predicted_winner']}" data-poly-price="{s['poly_price_a']}" data-model-prob="{s['model_prob_a']}" data-edge="{s['edge']}" data-kelly="{s.get('kelly_stake',0)}" data-market-type="{s.get('market_type','')}" data-tournament="{s.get('tournament','')}" data-surface="{s.get('surface','')}" style="color:{edge_marker_color}">💰</button></td>
                <td class="pred-vol">${s['volume']:,}</td>
            </tr>"""

        predictions_html = f"""
        <div class="predictions-section">
            <div class="predictions-header">
                <h2>ALL MATCHES — MODEL PREDICTIONS</h2>
                <div class="predictions-subtitle">Model's win probability for every active market • ● = betting signal above threshold</div>
            </div>
            <table class="predictions-table">
                <thead>
                    <tr>
                        <th>Match</th>
                        <th>Tournament</th>
                        <th>Round</th>
                        <th>Predicted Winner</th>
                        <th>Confidence</th>
                        <th>Rank</th>
                        <th>Poly Price (A/B)</th>
                        <th>Model Prob (A/B)</th>
                        <th>Best Edge</th>
                        <th>Signal</th>
                        <th>Volume</th>
                    </tr>
                </thead>
                <tbody>
                    {pred_rows}
                </tbody>
            </table>
        </div>
        """

    disclaimer = """
    <div class="disclaimer">
        ⚠️ These signals are model outputs, not financial advice. Always verify current Polymarket prices before placing.
        Kelly stakes assume 25% fractional Kelly — size to your own risk tolerance.
        Polymarket charges a 2% fee on net winnings — factor this into your minimum edge threshold.
    </div>
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎾 Tennis Betting Card — {generated_at}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:       #f8f9fa;
    --surface:  #ffffff;
    --surface2: #f1f3f5;
    --border:   #dee2e6;
    --amber:    #d4740a;
    --amber-dim:#b35f00;
    --green:    #087f23;
    --red:      #c62828;
    --orange:   #d84315;
    --text:     #212529;
    --text-dim: #6c757d;
    --mono:     'IBM Plex Mono', monospace;
    --sans:     'IBM Plex Sans', sans-serif;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    padding: 24px;
  }}

  /* ── HEADER ── */
  .header {{
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 28px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 12px;
  }}
  .header-left h1 {{
    font-size: 22px;
    font-weight: 700;
    color: var(--amber);
    letter-spacing: 0.04em;
  }}
  .header-left .subtitle {{
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 4px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .header-right {{
    text-align: right;
    font-size: 11px;
    color: var(--text-dim);
    line-height: 1.8;
  }}
  .header-right strong {{ color: var(--amber-dim); }}

  /* ── SUMMARY BAR ── */
  .summary {{
    display: flex;
    gap: 16px;
    margin-bottom: 28px;
    flex-wrap: wrap;
  }}
  .summary-pill {{
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 8px 16px;
    font-size: 12px;
    color: var(--text-dim);
  }}
  .summary-pill strong {{ color: var(--amber); }}

  /* ── CARDS ── */
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    margin-bottom: 20px;
    overflow: hidden;
    transition: border-color 0.2s;
  }}
  .card:hover {{ border-color: #adb5bd; }}
  .card.tier-strong  {{ border-left: 3px solid var(--orange); }}
  .card.tier-solid   {{ border-left: 3px solid var(--amber); }}
  .card.tier-watch   {{ border-left: 3px solid #adb5bd; }}
  .card.tier-marginal {{ border-left: 3px solid #ced4da; }}

  .card-header {{
    padding: 14px 18px;
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .match-info {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .match-num  {{ color: var(--text-dim); font-size: 11px; }}
  .match-title {{ font-size: 14px; font-weight: 600; color: var(--text); }}
  .tier-badge {{ font-size: 11px; font-weight: 700; letter-spacing: 0.06em; }}
  .card-meta  {{ font-size: 11px; color: var(--text-dim); display: flex; gap: 14px; flex-wrap: wrap; }}

  .card-body {{
    padding: 18px;
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
  }}

  /* ── BET ACTION ── */
  .bet-action {{
    flex: 0 0 auto;
    min-width: 260px;
  }}
  .bet-label {{
    font-size: 10px;
    letter-spacing: 0.1em;
    color: var(--text-dim);
    text-transform: uppercase;
    margin-bottom: 4px;
  }}
  .bet-player {{
    font-size: 17px;
    font-weight: 700;
    color: var(--amber);
    margin-bottom: 14px;
    line-height: 1.2;
  }}
  .bet-numbers {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .number-block {{
    text-align: center;
  }}
  .num-label {{ font-size: 9px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 3px; }}
  .num-value {{ font-size: 18px; font-weight: 700; }}
  .num-value.poly   {{ color: var(--text-dim); }}
  .num-value.model  {{ color: var(--green); }}
  .num-value.edge   {{ font-size: 20px; }}
  .num-value.kelly  {{ color: var(--amber-dim); font-size: 14px; }}
  .arrow {{ font-size: 14px; color: var(--border); margin: 0 2px; }}

  /* ── STATS GRID ── */
  .stats-grid {{
    flex: 1;
    display: grid;
    grid-template-columns: 1fr 80px 1fr;
    gap: 16px;
    min-width: 300px;
  }}
  .stats-header {{
    font-size: 11px;
    font-weight: 600;
    color: var(--amber-dim);
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .stat-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 7px;
    gap: 8px;
  }}
  .stat-label {{ font-size: 10px; color: var(--text-dim); flex: 0 0 auto; }}
  .stat-val   {{ font-size: 11px; color: var(--text); }}
  .na         {{ font-size: 11px; color: #adb5bd; }}

  .bar-wrap {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1;
    max-width: 120px;
  }}
  .bar-wrap span {{ font-size: 10px; color: var(--text); white-space: nowrap; }}
  .bar {{
    height: 4px;
    background: var(--amber);
    flex-shrink: 0;
    min-width: 2px;
  }}

  /* ── H2H ── */
  .h2h-col {{
    text-align: center;
    padding-top: 2px;
  }}
  .h2h-record {{
    font-size: 20px;
    font-weight: 700;
    color: var(--text);
    margin-top: 10px;
  }}
  .h2h-sub {{
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 4px;
  }}

  /* ── ADVANCED STATS (expandable) ── */
  .advanced-stats {{
    margin-top: 8px;
    border-top: 1px solid var(--border);
    padding-top: 8px;
  }}
  .advanced-stats summary {{
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .advanced-stats summary:hover {{
    color: var(--text);
  }}
  .adv-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-top: 10px;
  }}
  .adv-col .adv-header {{
    font-weight: 700;
    font-size: 12px;
    margin-bottom: 6px;
    color: var(--text);
    border-bottom: 1px solid var(--border);
    padding-bottom: 4px;
  }}
  .adv-col .stat-row {{
    font-size: 11px;
    display: flex;
    justify-content: space-between;
    padding: 2px 0;
  }}
  .rank-hist {{
    font-size: 10px !important;
  }}

  /* ── CARD FOOTER ── */
  .card-footer {{
    padding: 12px 18px;
    background: var(--surface2);
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .instructions {{
    font-size: 11px;
    color: var(--text-dim);
    line-height: 1.6;
    flex: 1;
  }}
  .instructions strong {{ color: var(--amber-dim); }}
  .inst-label {{
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-dim);
    text-transform: uppercase;
    margin-right: 6px;
  }}
  .poly-link {{
    font-size: 11px;
    color: var(--amber);
    text-decoration: none;
    border: 1px solid var(--amber-dim);
    padding: 5px 12px;
    white-space: nowrap;
    transition: background 0.15s;
  }}
  .poly-link:hover {{ background: #fff3e0; }}

  /* ── EMPTY / DISCLAIMER ── */
  .empty {{
    text-align: center;
    color: var(--text-dim);
    padding: 60px;
    font-size: 13px;
    border: 1px dashed var(--border);
  }}

  /* ── BET TOGGLE ── */
  .card-actions {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .bet-toggle {{
    background: #1b5e20;
    color: white;
    border: 2px solid #1b5e20;
    padding: 8px 20px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.2s;
  }}
  .bet-toggle:hover {{
    background: #2e7d32;
    border-color: #2e7d32;
  }}
  .bet-toggle.active {{
    background: #e65100;
    border-color: #e65100;
    animation: pulse 0.3s ease;
  }}
  .bet-toggle-mini {{
    background: none;
    border: 1px solid #adb5bd;
    color: #6c757d;
    padding: 1px 6px;
    font-size: 10px;
    cursor: pointer;
    margin-left: 4px;
    font-family: var(--mono);
    transition: all 0.2s;
  }}
  .bet-toggle-mini:hover {{
    border-color: #1b5e20;
    color: #1b5e20;
  }}
  .bet-toggle-mini.active {{
    background: #e65100;
    border-color: #e65100;
    color: white;
  }}
  @keyframes pulse {{
    0% {{ transform: scale(1); }}
    50% {{ transform: scale(1.05); }}
    100% {{ transform: scale(1); }}
  }}
  .bet-summary-bar {{
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: #1b5e20;
    color: white;
    padding: 12px 24px;
    font-family: var(--mono);
    font-size: 13px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 1000;
    box-shadow: 0 -2px 10px rgba(0,0,0,0.2);
    transform: translateY(100%);
    transition: transform 0.3s ease;
  }}
  .bet-summary-bar.visible {{
    transform: translateY(0);
  }}
  .bet-summary-bar button {{
    background: white;
    color: #1b5e20;
    border: none;
    padding: 8px 20px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    letter-spacing: 0.04em;
  }}
  .bet-summary-bar button:hover {{
    background: #e8f5e9;
  }}

  .disclaimer {{
    margin-top: 32px;
    padding: 14px 18px;
    border: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-dim);
    line-height: 1.7;
    background: var(--surface);
  }}

  /* ── LEGEND ── */
  .legend {{
    margin-bottom: 20px;
    display: flex;
    gap: 20px;
    font-size: 11px;
    color: var(--text-dim);
    flex-wrap: wrap;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 8px; height: 8px; flex-shrink: 0; }}

  /* ── VALUE BADGES ── */
  .value-badge {{
    display: inline-block;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    border-radius: 3px;
    margin-left: 6px;
    white-space: nowrap;
  }}
  .value-gold {{
    background: linear-gradient(135deg, #fff8e1 0%, #ffe082 100%);
    color: #e65100;
    border: 1px solid #ffb300;
    box-shadow: 0 0 6px rgba(255, 179, 0, 0.35);
  }}
  .value-green {{
    background: #e8f5e9;
    color: #1b5e20;
    border: 1px solid #66bb6a;
  }}
  .value-blue {{
    background: #e3f2fd;
    color: #0d47a1;
    border: 1px solid #64b5f6;
  }}

  /* ── STRAIGHT PICKS TABLE ── */
  .picks-section {{
    margin-top: 36px;
    border: 1px solid var(--border);
    background: var(--surface);
    overflow: hidden;
  }}
  .picks-header {{
    padding: 16px 18px;
    background: linear-gradient(135deg, #1b5e20 0%, #2e7d32 100%);
    border-bottom: 1px solid var(--border);
  }}
  .picks-header h2 {{
    font-size: 15px;
    font-weight: 700;
    color: #fff;
    letter-spacing: 0.06em;
    margin-bottom: 4px;
  }}
  .picks-subtitle {{
    font-size: 11px;
    color: #a5d6a7;
  }}
  .picks-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  .picks-table th {{
    text-align: left;
    padding: 10px 12px;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
  }}
  .picks-table td {{
    padding: 9px 12px;
    border-bottom: 1px solid #e9ecef;
    color: var(--text);
    font-family: var(--mono);
  }}
  .picks-table tr:hover {{
    background: #e8f5e9;
  }}

  /* ── PREDICTIONS TABLE ── */
  .predictions-section {{
    margin-top: 36px;
    border: 1px solid var(--border);
    background: var(--surface);
    overflow: hidden;
  }}
  .predictions-header {{
    padding: 14px 18px;
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
  }}
  .predictions-header h2 {{
    font-size: 13px;
    font-weight: 700;
    color: var(--amber);
    letter-spacing: 0.06em;
    margin-bottom: 4px;
  }}
  .predictions-subtitle {{
    font-size: 11px;
    color: var(--text-dim);
  }}
  .predictions-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  .predictions-table th {{
    text-align: left;
    padding: 10px 12px;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
  }}
  .predictions-table td {{
    padding: 9px 12px;
    border-bottom: 1px solid #e9ecef;
    color: var(--text);
    font-family: var(--mono);
  }}
  .predictions-table tr:hover {{
    background: #f1f3f5;
  }}
  .pred-match {{ color: var(--text); font-weight: 500; max-width: 220px; }}
  .pred-winner {{ font-weight: 700; }}
  .pred-conf {{ font-weight: 600; }}
  .pred-poly {{ color: var(--text-dim); }}
  .pred-model {{ color: var(--text); }}
  .pred-edge {{ font-weight: 700; }}
  .pred-signal {{ text-align: center; font-size: 14px; }}
  .pred-vol {{ color: var(--text-dim); text-align: right; }}
  .pred-tourney {{ color: var(--amber-dim); font-size: 11px; max-width: 180px; }}
  .pred-round {{ color: var(--text-dim); font-size: 11px; white-space: nowrap; }}
  .tourney {{ color: var(--amber); font-weight: 600; }}

  @media (max-width: 700px) {{
    .bet-numbers {{ gap: 6px; }}
    .stats-grid  {{ grid-template-columns: 1fr; }}
    .h2h-col     {{ text-align: left; }}
    .card-header {{ flex-direction: column; align-items: flex-start; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>🎾 TENNIS BETTING CARD</h1>
    <div class="subtitle">Polymarket Signal System — XGBoost Model</div>
  </div>
  <div class="header-right">
    Generated: <strong>{generated_at}</strong><br>
    Min Edge: <strong>{min_edge*100:.0f}%</strong> &nbsp;|&nbsp;
    Min Volume: <strong>${min_volume:,}</strong><br>
    Matches: <strong>{n_total}</strong> &nbsp;|&nbsp; Signals: <strong>{n_signals}</strong>
  </div>
</div>

<div class="summary">
  <div class="summary-pill">Matches: <strong>{n_total}</strong></div>
  <div class="summary-pill">Bet Signals: <strong>{n_signals}</strong></div>
  <div class="summary-pill">Combined Vol: <strong>${total_vol:,}</strong></div>
  <div class="summary-pill">Avg Edge: <strong>{(sum(abs(s['edge']) for s in signals)/n_signals if n_signals else 0):.1f}%</strong></div>
  <div class="summary-pill">Strong (≥10%): <strong>{sum(1 for s in signals if abs(s['edge'])>=10)}</strong></div>
  <div class="summary-pill">Solid (7-10%): <strong>{sum(1 for s in signals if 7<=abs(s['edge'])<10)}</strong></div>
</div>

<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#d84315"></div> 🔥 STRONG ≥10% edge</div>
  <div class="legend-item"><div class="legend-dot" style="background:#d4740a"></div> ✅ SOLID 7-10% edge</div>
  <div class="legend-item"><div class="legend-dot" style="background:#adb5bd"></div> 👀 WATCH 5-7% edge</div>
</div>
<div class="legend" style="margin-top:6px">
  <div class="legend-item">💰 <strong style="color:#e65100">BEST VALUE</strong> — Model ≥60% win AND Poly ≥15¢ too low</div>
  <div class="legend-item">💎 <strong style="color:#1b5e20">VALUE</strong> — Model ≥55% win AND Poly ≥8¢ too low</div>
  <div class="legend-item">📈 <strong style="color:#0d47a1">EDGE+</strong> — Model favors winner AND Poly ≥3¢ too low</div>
</div>
{'<div style="background:#fff3e0; border:2px solid #ff9800; padding:14px 20px; margin:16px 0; font-size:13px; font-family:var(--mono)"><strong style="color:#e65100">DATA-ONLY MODE</strong> — No trained model or historical data loaded. Showing raw Polymarket prices. Run <code>python3 01_data_pipeline.py</code> then <code>python3 02_features_and_train.py</code> for model-powered predictions with edge detection.</div>' if data_only_mode else ''}

{picks_html}

{cards_html}

{predictions_html}

{disclaimer}

<div class="bet-summary-bar" id="betBar">
  <span id="betCount">0 bets selected</span>
  <div>
    <span id="betTotal" style="margin-right:16px"></span>
    <button onclick="saveBets()">SAVE MY BETS</button>
    <button onclick="exportBets()" style="margin-left:8px; background:#e8f5e9">EXPORT JSON</button>
  </div>
</div>

<script>
const STORAGE_KEY = 'tennis_bets_' + '{generated_at}'.replace(/ /g,'_').replace(/:/g,'').replace(/-/g,'');
let myBets = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');

// Restore previously toggled bets on load
document.addEventListener('DOMContentLoaded', () => {{
  myBets.forEach(bet => {{
    document.querySelectorAll('.bet-toggle, .bet-toggle-mini').forEach(btn => {{
      if (btn.dataset.match === bet.match && btn.dataset.betOn === bet.bet_on) {{
        btn.classList.add('active');
        if (btn.classList.contains('bet-toggle')) btn.textContent = '✓ BET PLACED';
        if (btn.classList.contains('bet-toggle-mini')) btn.textContent = '✓';
      }}
    }});
  }});
  updateBar();
}});

function toggleBet(btn) {{
  const data = {{
    match: btn.dataset.match,
    bet_on: btn.dataset.betOn,
    poly_price: parseFloat(btn.dataset.polyPrice) || 0,
    model_prob: parseFloat(btn.dataset.modelProb) || 0,
    edge: parseFloat(btn.dataset.edge) || 0,
    kelly: parseFloat(btn.dataset.kelly) || 0,
    market_type: btn.dataset.marketType || '',
    tournament: btn.dataset.tournament || '',
    surface: btn.dataset.surface || '',
    timestamp: new Date().toISOString(),
    card_date: '{generated_at}',
  }};

  const idx = myBets.findIndex(b => b.match === data.match && b.bet_on === data.bet_on);
  if (idx >= 0) {{
    myBets.splice(idx, 1);
    btn.classList.remove('active');
    if (btn.classList.contains('bet-toggle')) btn.textContent = 'I BET THIS';
    if (btn.classList.contains('bet-toggle-mini')) btn.textContent = '💰';
  }} else {{
    myBets.push(data);
    btn.classList.add('active');
    if (btn.classList.contains('bet-toggle')) btn.textContent = '✓ BET PLACED';
    if (btn.classList.contains('bet-toggle-mini')) btn.textContent = '✓';
  }}

  localStorage.setItem(STORAGE_KEY, JSON.stringify(myBets));
  updateBar();
}}

function updateBar() {{
  const bar = document.getElementById('betBar');
  const count = document.getElementById('betCount');
  const total = document.getElementById('betTotal');
  if (myBets.length > 0) {{
    bar.classList.add('visible');
    count.textContent = myBets.length + ' bet' + (myBets.length > 1 ? 's' : '') + ' selected';
    const avgEdge = (myBets.reduce((s, b) => s + Math.abs(b.edge), 0) / myBets.length).toFixed(1);
    total.textContent = 'Avg Edge: ' + avgEdge + '%';
  }} else {{
    bar.classList.remove('visible');
  }}
}}

function saveBets() {{
  // Save to localStorage (already done on toggle)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(myBets));

  // Also save to the master bet log
  const masterKey = 'tennis_all_bets';
  let allBets = JSON.parse(localStorage.getItem(masterKey) || '[]');
  myBets.forEach(bet => {{
    const exists = allBets.find(b => b.match === bet.match && b.bet_on === bet.bet_on && b.card_date === bet.card_date);
    if (!exists) allBets.push(bet);
  }});
  localStorage.setItem(masterKey, JSON.stringify(allBets));

  alert('Saved ' + myBets.length + ' bet(s) to local storage!\\nTotal bets tracked: ' + allBets.length);
}}

function exportBets() {{
  const masterKey = 'tennis_all_bets';
  let allBets = JSON.parse(localStorage.getItem(masterKey) || '[]');

  // Merge current unsaved
  myBets.forEach(bet => {{
    const exists = allBets.find(b => b.match === bet.match && b.bet_on === bet.bet_on && b.card_date === bet.card_date);
    if (!exists) allBets.push(bet);
  }});

  const blob = new Blob([JSON.stringify(allBets, null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'my_bets_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(url);
}}
</script>

</body>
</html>"""


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    # Reset global caches for fresh run
    global _elo_cache, _live_rankings_cache, _surface_history_cache
    _elo_cache = None
    _live_rankings_cache = None
    _surface_history_cache = None

    parser = argparse.ArgumentParser()
    parser.add_argument("--min-edge",   type=float, default=0.03)
    parser.add_argument("--min-volume", type=float, default=300)
    parser.add_argument("--open",       action="store_true",
                        help="Auto-open card in browser after generating")
    parser.add_argument("--debug",      action="store_true",
                        help="Print raw market questions for debugging")
    args = parser.parse_args()

    print("=" * 60)
    print("  🎾  TENNIS BETTING CARD GENERATOR")
    print("=" * 60)

    # Load model
    meta_path = MODELS_DIR / "latest_model.json"
    if not meta_path.exists():
        print("\n  ✗ No model found — run 02_features_and_train.py first")
        print("  (Running in data-only mode — showing raw Polymarket prices)\n")
        model, feature_cols = None, []
    else:
        with open(meta_path) as f:
            meta = json.load(f)
        with open(meta["path"], "rb") as f:
            bundle = pickle.load(f)
        model, feature_cols = bundle["model"], bundle["feature_cols"]
        print(f"  Model v{meta['version']} loaded (AUC {meta['mean_auc']:.4f})")

    # Load historical data
    hist_path = DATA_DIR / "raw" / "matches_combined.parquet"
    if hist_path.exists():
        df_hist = pd.read_parquet(hist_path)
        df_hist["date"] = pd.to_datetime(df_hist["date"], errors="coerce")
        print(f"  Historical data: {len(df_hist):,} matches")
    else:
        print("  No historical data — run 01_data_pipeline.py first")
        df_hist = pd.DataFrame(columns=["winner","loser","date","surface"])

    # Fetch live markets
    print(f"\n  Fetching live markets (min vol ${args.min_volume:,.0f})...")
    markets = fetch_live_markets(min_volume=args.min_volume)
    print(f"  Found {len(markets)} markets")

    if args.debug and not markets.empty:
        print("\n  [DEBUG] Sample market questions:")
        for _, m in markets.head(10).iterrows():
            q = m.get("question", "")
            prices = m.get("prices", {})
            pa, pb = parse_players(q)
            parsed = f"→ [{pa}] vs [{pb}]" if pa else "→ PARSE FAILED"
            print(f"    Q: {q}")
            print(f"    Prices: {prices}")
            print(f"    {parsed}")
            print()

    # Generate signals — try head-to-head first, then outright winner markets
    signals = []
    has_model = model is not None
    has_hist  = not df_hist.empty

    if not markets.empty:
        # H2H match signals
        if has_model and has_hist:
            print(f"  Generating head-to-head match signals (min edge {args.min_edge*100:.0f}%)...")
            signals = generate_signals(markets, model, feature_cols, df_hist, args.min_edge, debug=args.debug)
            print(f"  → {len(signals)} head-to-head match signals")
        else:
            # DATA-ONLY MODE: parse h2h markets using raw Polymarket prices
            print(f"  Generating head-to-head signals (data-only — raw Polymarket prices)...")
            signals = generate_signals_data_only(markets, df_hist if has_hist else None, debug=args.debug)
            print(f"  → {len(signals)} head-to-head signals (data-only)")

        # Outright tournament winner signals
        if has_hist:
            print(f"  Generating outright tournament signals...")
            outright = generate_outright_signals(markets, df_hist, args.min_edge, debug=args.debug)
            print(f"  → {len(outright)} outright signals from tournament markets")
            signals.extend(outright)
        else:
            # DATA-ONLY outrights: use raw Polymarket prices
            print(f"  Generating outright signals (data-only — raw Polymarket prices)...")
            outright = generate_outright_signals_data_only(markets, debug=args.debug)
            print(f"  → {len(outright)} outright signals (data-only)")
            signals.extend(outright)

    edge_only = [s for s in signals if s.get("has_edge")]
    print(f"  {len(signals)} match(es) analysed, {len(edge_only)} betting signal(s) above threshold")

    # ── AUTO-LOG PICKS ──
    if signals:
        try:
            from importlib import import_module
            bet_logger = import_module("05_bet_logger")  # same directory
            bet_logger.log_picks(signals)
        except Exception as e:
            print(f"  [warn] Bet logging failed: {e}")

    # Build HTML
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_only_mode = not has_model or not has_hist
    html = build_html(signals, generated_at, args.min_edge, int(args.min_volume), data_only_mode=data_only_mode)

    # Save
    fname = f"betting_card_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    out   = CARDS_DIR / fname
    out.write_text(html, encoding="utf-8")
    print(f"\n  ✓ Betting card saved → {out}")

    if args.open:
        import subprocess as _sp
        _sp.Popen(["open", "-a", "Google Chrome", str(out.resolve())])
        print("  ↗ Opened in Chrome")
    else:
        print(f"  → Open with: open -a 'Google Chrome' {out}")

    # Quick terminal summary — all predictions
    if signals:
        print(f"\n{'─'*105}")
        print(f"  {'MATCH':<28} {'TOURNAMENT':<22} {'RND':<8} {'PICK':<16} {'RANK':>5} {'MODEL':>6} {'POLY':>6} {'EDGE':>6}")
        print(f"{'─'*105}")
        for s in sorted(signals, key=lambda x: abs(x["edge"]), reverse=True)[:25]:
            marker = " *" if s.get("has_edge") else ""
            rank_str = f"#{s['rank']}" if s.get('rank') else "—"
            tourney = (s.get('tournament') or '—')[:21]
            rnd = (s.get('round') or '—')[:7]
            print(f"  {s['match'][:27]:<28} {tourney:<22} {rnd:<8} {s['predicted_winner'][:15]:<16} {rank_str:>5} {s['model_prob']:>5.1f}% {s['poly_price']:>5.1f}¢ {s['edge']:>+5.1f}%{marker}")
        if len(signals) > 25:
            print(f"  ... and {len(signals)-25} more in the card")
        print(f"{'─'*105}")
        print(f"  * = betting signal (edge ≥ {args.min_edge*100:.0f}%)")


if __name__ == "__main__":
    main()
