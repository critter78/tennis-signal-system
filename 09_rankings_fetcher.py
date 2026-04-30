#!/usr/bin/env python3
"""
09_rankings_fetcher.py — Live ATP/WTA Rankings Fetcher
Pulls current rankings from Sofascore's public JSON API.
Caches to disk (rankings_cache.json) so we only hit the API once per day.

Usage:
    from 09_rankings_fetcher import load_cached_rankings, lookup_player_rank

    cache = load_cached_rankings()      # loads from disk or fetches fresh
    rank, tour = lookup_player_rank("Emma Navarro", cache)
    # → (8, "WTA")
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

BASE_DIR = Path(__file__).parent.resolve()
# Use /data on Render (persistent disk), fall back to repo-relative for local dev
_PERSISTENT = Path("/data")
if _PERSISTENT.exists() and _PERSISTENT.is_dir():
    DATA_DIR = _PERSISTENT / "data"
else:
    DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "rankings_cache.json"
CACHE_MAX_AGE_HOURS = 12  # re-fetch if older than this

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

# Sofascore public API endpoints (no auth required)
_SOFASCORE_ATP = "https://api.sofascore.com/api/v1/rankings/type/6"
_SOFASCORE_WTA = "https://api.sofascore.com/api/v1/rankings/type/7"

# Fallback: alternative endpoint format
_SOFASCORE_ATP_ALT = "https://api.sofascore.com/api/v1/rankings/atp-singles"
_SOFASCORE_WTA_ALT = "https://api.sofascore.com/api/v1/rankings/wta-singles"

# JeffSackmann CSV fallback (updated weekly, very reliable)
_SACKMANN_ATP = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_current.csv"
_SACKMANN_WTA = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_rankings_current.csv"
# Player name lookup files
_SACKMANN_ATP_PLAYERS = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv"
_SACKMANN_WTA_PLAYERS = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv"


def _fetch_json(url, timeout=15):
    """Fetch JSON from a URL with proper headers."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_sofascore_rankings(data, tour="ATP"):
    """Parse Sofascore rankings JSON into a clean list."""
    rankings = []
    rows = data.get("rankings", [])
    for row in rows:
        rank = row.get("ranking") or row.get("position")
        team = row.get("team") or row.get("rowTeam") or {}
        player_name = team.get("name") or team.get("shortName", "")
        points = row.get("points") or row.get("ranking_points", 0)

        if not player_name or not rank:
            continue

        # Normalize name: "Sinner J." → keep as-is, we also store full name
        rankings.append({
            "rank": int(rank),
            "name": player_name.strip(),
            "points": int(points) if points else 0,
            "tour": tour,
        })

    return rankings


def _fetch_csv_text(url, timeout=15):
    """Fetch raw CSV text from a URL."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _load_sackmann_players(url, timeout=15):
    """Load JeffSackmann player ID → name mapping from CSV.
    CSV format: player_id,name_first,name_last,hand,dob,ioc,height,wikidata_id"""
    try:
        text = _fetch_csv_text(url, timeout)
        players = {}
        for line in text.strip().split("\n")[1:]:  # skip header
            parts = line.split(",")
            if len(parts) >= 3:
                pid = parts[0].strip()
                first = parts[1].strip()
                last = parts[2].strip()
                if pid and last:
                    players[pid] = f"{first} {last}".strip()
        return players
    except Exception as e:
        print(f"  [rankings] Failed to load player names: {e}", file=sys.stderr)
        return {}


def _fetch_sackmann_rankings(rankings_url, players_url, tour="ATP"):
    """Fetch rankings from JeffSackmann GitHub CSV files.
    Rankings CSV format: ranking_date,rank,player,points
    Player IDs need to be resolved to names via the players file."""
    # Load player name mapping
    player_names = _load_sackmann_players(players_url)

    text = _fetch_csv_text(rankings_url)
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return {}

    # Find the most recent ranking date (first column, sorted desc)
    # The file has all dates — we want only the latest
    rankings = {}
    latest_date = None

    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 4:
            continue
        date_str = parts[0].strip()
        rank = parts[1].strip()
        player_id = parts[2].strip()
        points = parts[3].strip()

        if not latest_date:
            latest_date = date_str

        # Only process the latest date's rankings
        if date_str != latest_date:
            break

        # Resolve player ID to name
        name = player_names.get(player_id, "")
        if not name:
            continue

        try:
            key = name.lower().strip()
            rankings[key] = {
                "rank": int(rank),
                "name": name,
                "tour": tour,
                "points": int(points) if points else 0,
            }
        except (ValueError, TypeError):
            continue

    print(f"  [rankings] Sackmann {tour}: {len(rankings)} players (date: {latest_date})")
    return rankings


def fetch_live_rankings():
    """Fetch fresh ATP + WTA rankings.
    Tries Sofascore API first, falls back to JeffSackmann GitHub CSVs.
    Returns dict with 'rankings' (name→{rank,tour,points}), 'count', 'fetched_at'."""
    all_rankings = {}
    errors = []
    source = "unknown"

    # ── ATTEMPT 1: Sofascore API (most current) ──
    for tour, urls in [("ATP", [_SOFASCORE_ATP, _SOFASCORE_ATP_ALT]),
                       ("WTA", [_SOFASCORE_WTA, _SOFASCORE_WTA_ALT])]:
        fetched = False
        for url in urls:
            try:
                data = _fetch_json(url)
                parsed = _parse_sofascore_rankings(data, tour)
                if parsed:
                    for p in parsed:
                        key = p["name"].lower().strip()
                        all_rankings[key] = {
                            "rank": p["rank"],
                            "name": p["name"],
                            "tour": tour,
                            "points": p["points"],
                        }
                    print(f"  [rankings] Fetched {len(parsed)} {tour} rankings from Sofascore")
                    source = "sofascore"
                    fetched = True
                    break
            except Exception as e:
                errors.append(f"Sofascore {tour}: {e}")
                continue

        if not fetched:
            print(f"  [rankings] Sofascore {tour} failed, trying Sackmann fallback...",
                  file=sys.stderr)

        time.sleep(1)

    # ── ATTEMPT 2: JeffSackmann CSVs (weekly, very reliable) ──
    if len(all_rankings) < 50:  # Sofascore failed or returned too few
        print("  [rankings] Falling back to JeffSackmann GitHub CSVs...")
        for tour, rank_url, player_url in [
            ("ATP", _SACKMANN_ATP, _SACKMANN_ATP_PLAYERS),
            ("WTA", _SACKMANN_WTA, _SACKMANN_WTA_PLAYERS),
        ]:
            try:
                sack_rankings = _fetch_sackmann_rankings(rank_url, player_url, tour)
                if sack_rankings:
                    # Only add if we don't already have this tour from Sofascore
                    tour_count = sum(1 for v in all_rankings.values() if v["tour"] == tour)
                    if tour_count < 10:
                        all_rankings.update(sack_rankings)
                        source = "sackmann" if source == "unknown" else f"{source}+sackmann"
            except Exception as e:
                errors.append(f"Sackmann {tour}: {e}")
            time.sleep(1)

    if not all_rankings:
        print(f"  [rankings] ALL SOURCES FAILED: {errors}", file=sys.stderr)

    result = {
        "rankings": all_rankings,
        "count": len(all_rankings),
        "source": source,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "errors": errors if not all_rankings else [],
    }
    return result


def save_cache(data):
    """Save rankings cache to disk."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [rankings] Cache saved: {len(data.get('rankings', {}))} players → {CACHE_FILE}")


def load_cached_rankings(max_age_hours=CACHE_MAX_AGE_HOURS):
    """Load rankings from cache file. Re-fetches if stale or missing."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)

            fetched_at = data.get("fetched_at", "")
            if fetched_at:
                try:
                    fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                    age = datetime.now(fetched_dt.tzinfo) - fetched_dt
                    if age < timedelta(hours=max_age_hours):
                        count = data.get("count", len(data.get("rankings", {})))
                        print(f"  [rankings] Using cached rankings ({count} players, {age.seconds//3600}h old)")
                        return data
                    else:
                        print(f"  [rankings] Cache stale ({age.seconds//3600}h old), refreshing...")
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, Exception) as e:
            print(f"  [rankings] Cache read error: {e}", file=sys.stderr)

    # Fetch fresh
    print("  [rankings] Fetching fresh rankings from Sofascore...")
    data = fetch_live_rankings()
    if data.get("count", 0) > 0:
        save_cache(data)
    return data


def lookup_player_rank(player_name, cache=None):
    """Look up a player's current world ranking.
    Returns (rank, tour) or (None, None) if not found.

    Uses fuzzy matching to handle name variations:
    - "E. Maia" → "Emiliana Arango Maia" (if exists)
    - "Navarro" → "Emma Navarro"
    """
    if cache is None:
        cache = load_cached_rankings()

    rankings = cache.get("rankings", {})
    if not rankings:
        return None, None

    name = player_name.strip()
    name_lower = name.lower()

    # 1. Exact match
    if name_lower in rankings:
        r = rankings[name_lower]
        return r["rank"], r["tour"]

    # 2. Last name match (most common for Polymarket names)
    parts = name.split()
    last_name = parts[-1].lower() if parts else ""
    first_name = parts[0].lower() if len(parts) > 1 else ""

    # Collect all matches by last name
    last_matches = []
    for key, r in rankings.items():
        key_parts = key.split()
        key_last = key_parts[-1].lower() if key_parts else ""
        if key_last == last_name:
            last_matches.append(r)

    if len(last_matches) == 1:
        return last_matches[0]["rank"], last_matches[0]["tour"]

    # Multiple last name matches — try first name too
    if len(last_matches) > 1 and first_name:
        for r in last_matches:
            r_parts = r["name"].lower().split()
            r_first = r_parts[0] if r_parts else ""
            if r_first.startswith(first_name) or first_name.startswith(r_first):
                return r["rank"], r["tour"]
            # Check first initial match (e.g., "E" matches "Emma")
            if len(first_name) == 1 and r_first.startswith(first_name):
                return r["rank"], r["tour"]
        # Still ambiguous — return highest ranked
        best = min(last_matches, key=lambda x: x["rank"])
        return best["rank"], best["tour"]

    # 3. Fuzzy match (for name variations)
    best_score = 0
    best_match = None
    for key, r in rankings.items():
        score = SequenceMatcher(None, name_lower, key).ratio()
        if score > best_score:
            best_score = score
            best_match = r

    if best_score >= 0.70:
        return best_match["rank"], best_match["tour"]

    return None, None


# ── CLI for testing / manual refresh ──
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch/lookup tennis rankings")
    parser.add_argument("--refresh", action="store_true", help="Force refresh from API")
    parser.add_argument("--lookup", type=str, help="Look up a player by name")
    parser.add_argument("--top", type=int, default=0, help="Print top N rankings")
    args = parser.parse_args()

    if args.refresh:
        data = fetch_live_rankings()
        save_cache(data)
        print(f"\nFetched {data['count']} rankings")
    else:
        data = load_cached_rankings()

    if args.lookup:
        rank, tour = lookup_player_rank(args.lookup, data)
        if rank:
            print(f"{args.lookup}: #{rank} ({tour})")
        else:
            print(f"{args.lookup}: NOT FOUND")
            # Show close matches
            rankings = data.get("rankings", {})
            last = args.lookup.split()[-1].lower()
            close = [(k, v["rank"]) for k, v in rankings.items() if last in k][:5]
            if close:
                print(f"  Close matches: {close}")

    if args.top:
        rankings = data.get("rankings", {})
        sorted_r = sorted(rankings.values(), key=lambda x: x["rank"])
        for r in sorted_r[:args.top]:
            print(f"  #{r['rank']:>4}  {r['name']:<30}  {r['tour']}  {r['points']}pts")
