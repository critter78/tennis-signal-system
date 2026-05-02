#!/usr/bin/env python3
"""
09_rankings_fetcher.py — Live ATP/WTA Rankings Fetcher
Pulls current rankings from official ATP Tour and WTA Tour websites.
Caches to disk (rankings_cache.json) so we only hit the sites once per day.

Priority order:
  1. Official ATP Tour (atptour.com) + WTA Tour (wtatennis.com)
  2. Sofascore API (real-time, but may block server IPs)
  3. TML match data (extract ranks from recent matches)

Usage:
    from 09_rankings_fetcher import load_cached_rankings, lookup_player_rank

    cache = load_cached_rankings()      # loads from disk or fetches fresh
    rank, tour = lookup_player_rank("Emma Navarro", cache)
    # → (8, "WTA")
"""

import json
import os
import re
import sys
import time
import requests as _req
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
    print("  [rankings] WARNING: beautifulsoup4 not installed, HTML scraping disabled", file=sys.stderr)

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

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

_JSON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

# ── Official ATP/WTA URLs ──
_ATP_RANKINGS_URL = "https://www.atptour.com/en/rankings/singles"
_WTA_RANKINGS_URL = "https://www.wtatennis.com/rankings/singles"

# ── Sofascore API (fallback) ──
_SOFASCORE_ATP = "https://api.sofascore.com/api/v1/rankings/type/6"
_SOFASCORE_WTA = "https://api.sofascore.com/api/v1/rankings/type/7"
_SOFASCORE_ATP_ALT = "https://api.sofascore.com/api/v1/rankings/atp-singles"
_SOFASCORE_WTA_ALT = "https://api.sofascore.com/api/v1/rankings/wta-singles"

# ── TML match data (last resort — extract ranks from recent matches) ──
_TML_DATA_DIR = BASE_DIR / "data" / "raw"


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: Official ATP Tour + WTA Tour websites
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_atp_rankings(top_n=200):
    """Fetch current ATP singles rankings from atptour.com.
    Tries multiple approaches: JSON API, then HTML scraping."""
    rankings = {}

    # Approach A: ATP internal JSON API (powers their rankings page)
    for api_url in [
        "https://www.atptour.com/en/-/ajax/RankingsController/Rankings?rankRange=1-200&rankDate=&region=&is498=true",
        "https://www.atptour.com/en/rankings/singles?rankRange=1-200&ajax=true",
    ]:
        try:
            resp = _req.get(api_url, headers=_BROWSER_HEADERS, timeout=20)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, list):
                        for item in data:
                            name = item.get("playerName", "") or item.get("name", "")
                            rank = item.get("sglRank", 0) or item.get("rank", 0) or item.get("ranking", 0)
                            points = item.get("points", 0) or item.get("sglPts", 0)
                            if name and rank:
                                key = name.lower().strip()
                                rankings[key] = {"rank": int(rank), "name": name.strip(), "tour": "ATP", "points": int(points) if points else 0}
                    elif isinstance(data, dict):
                        rows = data.get("rankings", []) or data.get("data", []) or data.get("rows", [])
                        for item in rows:
                            name = item.get("playerName", "") or item.get("name", "") or item.get("player", "")
                            rank = item.get("sglRank", 0) or item.get("rank", 0) or item.get("ranking", 0)
                            points = item.get("points", 0) or item.get("sglPts", 0)
                            if name and rank:
                                key = name.lower().strip()
                                rankings[key] = {"rank": int(rank), "name": name.strip(), "tour": "ATP", "points": int(points) if points else 0}
                    if len(rankings) >= 10:
                        print(f"  [rankings] ATP JSON API: {len(rankings)} players")
                        return rankings
                except (json.JSONDecodeError, ValueError):
                    if _HAS_BS4 and '<table' in resp.text.lower():
                        parsed = _parse_rankings_html(resp.text, "ATP")
                        if parsed:
                            print(f"  [rankings] ATP JSON endpoint returned HTML: {len(parsed)} players")
                            return parsed
        except Exception as e:
            print(f"  [rankings] ATP API attempt failed: {e}", file=sys.stderr)

    # Approach B: Scrape the HTML rankings page
    if _HAS_BS4:
        try:
            resp = _req.get(_ATP_RANKINGS_URL, headers=_BROWSER_HEADERS, timeout=20)
            resp.raise_for_status()
            parsed = _parse_rankings_html(resp.text, "ATP")
            if parsed:
                print(f"  [rankings] ATP HTML scrape: {len(parsed)} players")
                return parsed
        except Exception as e:
            print(f"  [rankings] ATP HTML scrape failed: {e}", file=sys.stderr)

    return rankings


def _fetch_wta_rankings(top_n=200):
    """Fetch current WTA singles rankings from wtatennis.com.
    Tries multiple approaches: JSON API, then HTML scraping."""
    rankings = {}

    # Approach A: WTA internal API endpoints
    for api_url in [
        "https://api.wtatennis.com/tennis/v2/rankings/singles?page=1&pageSize=200",
        "https://www.wtatennis.com/rankings/singles?ajax=true",
    ]:
        try:
            resp = _req.get(api_url, headers=_BROWSER_HEADERS, timeout=20)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    rows = []
                    if isinstance(data, list):
                        rows = data
                    elif isinstance(data, dict):
                        rows = data.get("data", []) or data.get("rankings", []) or data.get("rows", [])

                    for item in rows:
                        player = item.get("player", {}) if isinstance(item.get("player"), dict) else {}
                        name = (player.get("fullName", "") or player.get("name", "") or
                                item.get("playerName", "") or item.get("name", "") or item.get("fullName", ""))
                        rank = item.get("ranking", 0) or item.get("rank", 0) or item.get("sglRank", 0)
                        points = item.get("points", 0) or item.get("rankingPoints", 0)
                        if name and rank:
                            key = name.lower().strip()
                            rankings[key] = {"rank": int(rank), "name": name.strip(), "tour": "WTA", "points": int(points) if points else 0}

                    if len(rankings) >= 10:
                        print(f"  [rankings] WTA JSON API: {len(rankings)} players")
                        return rankings
                except (json.JSONDecodeError, ValueError):
                    if _HAS_BS4 and '<table' in resp.text.lower():
                        parsed = _parse_rankings_html(resp.text, "WTA")
                        if parsed:
                            print(f"  [rankings] WTA JSON endpoint returned HTML: {len(parsed)} players")
                            return parsed
        except Exception as e:
            print(f"  [rankings] WTA API attempt failed: {e}", file=sys.stderr)

    # Approach B: Scrape the HTML rankings page
    if _HAS_BS4:
        try:
            resp = _req.get(_WTA_RANKINGS_URL, headers=_BROWSER_HEADERS, timeout=20)
            resp.raise_for_status()
            parsed = _parse_rankings_html(resp.text, "WTA")
            if parsed:
                print(f"  [rankings] WTA HTML scrape: {len(parsed)} players")
                return parsed
        except Exception as e:
            print(f"  [rankings] WTA HTML scrape failed: {e}", file=sys.stderr)

    return rankings


def _parse_rankings_html(html, tour="ATP"):
    """Parse rankings from HTML page (ATP or WTA).
    Looks for ranking tables and extracts rank, name, points."""
    if not _HAS_BS4:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    rankings = {}

    # Strategy 1: Look for table rows with ranking data
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            rank_text = ""
            name_text = ""
            points_text = ""

            for i, cell in enumerate(cells):
                text = cell.get_text(strip=True)
                if not rank_text and text.isdigit() and int(text) <= 500:
                    rank_text = text
                elif rank_text and not name_text and len(text) > 2 and not text.replace(",", "").replace(".", "").isdigit():
                    name_text = text
                elif rank_text and name_text and text.replace(",", "").isdigit() and int(text.replace(",", "")) > 10:
                    points_text = text.replace(",", "")

            if rank_text and name_text:
                key = name_text.lower().strip()
                rankings[key] = {
                    "rank": int(rank_text),
                    "name": name_text.strip(),
                    "tour": tour,
                    "points": int(points_text) if points_text else 0,
                }

    # Strategy 2: Look for structured data in divs/spans with ranking classes
    if len(rankings) < 10:
        rank_elements = soup.find_all(attrs={"class": re.compile(r"rank|ranking|position", re.I)})
        for elem in rank_elements:
            text = elem.get_text(strip=True)
            if text.isdigit():
                parent = elem.parent
                if parent:
                    name_elem = parent.find(attrs={"class": re.compile(r"name|player", re.I)})
                    if name_elem:
                        name = name_elem.get_text(strip=True)
                        if name and len(name) > 2:
                            key = name.lower().strip()
                            rankings[key] = {
                                "rank": int(text),
                                "name": name.strip(),
                                "tour": tour,
                                "points": 0,
                            }

    # Strategy 3: Look for embedded JSON data in script tags
    if len(rankings) < 10:
        for script in soup.find_all("script"):
            script_text = script.string or ""
            json_matches = re.findall(r'\[[\s\S]*?"rank(?:ing)?"[\s\S]*?\]', script_text)
            for match in json_matches[:3]:
                try:
                    data = json.loads(match)
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                name = (item.get("playerName", "") or item.get("name", "") or
                                        item.get("fullName", "") or "")
                                rank = item.get("ranking", 0) or item.get("rank", 0)
                                points = item.get("points", 0)
                                if name and rank and isinstance(rank, int) and rank <= 500:
                                    key = name.lower().strip()
                                    rankings[key] = {"rank": rank, "name": name.strip(), "tour": tour, "points": int(points) if points else 0}
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue

    return rankings


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: Sofascore API (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_json(url, timeout=15):
    """Fetch JSON from a URL with proper headers."""
    resp = _req.get(url, headers=_JSON_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _parse_sofascore_rankings(data, tour="ATP"):
    """Parse Sofascore rankings JSON into a clean dict."""
    rankings = {}
    rows = data.get("rankings", [])
    for row in rows:
        rank = row.get("ranking") or row.get("position")
        team = row.get("team") or row.get("rowTeam") or {}
        player_name = team.get("name") or team.get("shortName", "")
        points = row.get("points") or row.get("ranking_points", 0)

        if not player_name or not rank:
            continue

        key = player_name.strip().lower()
        rankings[key] = {
            "rank": int(rank),
            "name": player_name.strip(),
            "points": int(points) if points else 0,
            "tour": tour,
        }

    return rankings


def _fetch_sofascore_rankings():
    """Try Sofascore API for both ATP and WTA."""
    all_rankings = {}
    errors = []

    for tour, urls in [("ATP", [_SOFASCORE_ATP, _SOFASCORE_ATP_ALT]),
                       ("WTA", [_SOFASCORE_WTA, _SOFASCORE_WTA_ALT])]:
        fetched = False
        for url in urls:
            try:
                data = _fetch_json(url)
                parsed = _parse_sofascore_rankings(data, tour)
                if parsed:
                    all_rankings.update(parsed)
                    print(f"  [rankings] Sofascore {tour}: {len(parsed)} players")
                    fetched = True
                    break
            except Exception as e:
                errors.append(f"Sofascore {tour}: {e}")
                continue

        if not fetched:
            print(f"  [rankings] Sofascore {tour} failed", file=sys.stderr)
        time.sleep(1)

    return all_rankings, errors


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: TML match data (last resort — extract ranks from recent matches)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_tml_rankings():
    """Extract current rankings from TML match data CSV files.
    Uses the most recent match record for each player to get their rank.
    TML data is updated more frequently than weekly Sackmann CSVs."""
    rankings = {}

    # Also check persistent disk location
    tml_dirs = [_TML_DATA_DIR]
    persistent_tml = Path("/data/data/raw")
    if persistent_tml.exists():
        tml_dirs.append(persistent_tml)

    # Find TML CSV files (most recent year first)
    tml_files = []
    for d in tml_dirs:
        if d.exists():
            for f in sorted(d.glob("tml_*.csv"), reverse=True):
                tml_files.append(f)

    if not tml_files:
        print("  [rankings] No TML CSV files found", file=sys.stderr)
        return rankings

    try:
        import pandas as pd
    except ImportError:
        print("  [rankings] pandas not available for TML parsing", file=sys.stderr)
        return rankings

    for csv_path in tml_files[:3]:  # check up to 3 most recent files
        try:
            df = pd.read_csv(csv_path, low_memory=False)

            # TML CSVs use Sackmann-compatible column names
            # winner_name, winner_rank, loser_name, loser_rank, tourney_date
            date_col = None
            for col in ["tourney_date", "date", "match_date"]:
                if col in df.columns:
                    date_col = col
                    break

            if date_col:
                df[date_col] = pd.to_numeric(df[date_col], errors="coerce")
                df = df.sort_values(date_col, ascending=False)

            # Extract winner ranks
            if "winner_name" in df.columns and "winner_rank" in df.columns:
                for _, row in df.iterrows():
                    name = str(row.get("winner_name", "")).strip()
                    rank = row.get("winner_rank")
                    if name and pd.notna(rank) and int(rank) > 0 and int(rank) <= 500:
                        key = name.lower()
                        if key not in rankings:  # keep most recent
                            # Determine tour from tourney_name or surface hints
                            tour = "ATP"  # TML is primarily ATP
                            tourney = str(row.get("tourney_name", "")).lower()
                            if any(w in tourney for w in ["wta", "women"]):
                                tour = "WTA"
                            rankings[key] = {
                                "rank": int(rank),
                                "name": name,
                                "tour": tour,
                                "points": 0,
                            }

            # Extract loser ranks
            if "loser_name" in df.columns and "loser_rank" in df.columns:
                for _, row in df.iterrows():
                    name = str(row.get("loser_name", "")).strip()
                    rank = row.get("loser_rank")
                    if name and pd.notna(rank) and int(rank) > 0 and int(rank) <= 500:
                        key = name.lower()
                        if key not in rankings:
                            tour = "ATP"
                            tourney = str(row.get("tourney_name", "")).lower()
                            if any(w in tourney for w in ["wta", "women"]):
                                tour = "WTA"
                            rankings[key] = {
                                "rank": int(rank),
                                "name": name,
                                "tour": tour,
                                "points": 0,
                            }

            print(f"  [rankings] TML {csv_path.name}: {len(rankings)} players extracted")

        except Exception as e:
            print(f"  [rankings] TML parse error ({csv_path.name}): {e}", file=sys.stderr)

    return rankings


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN FETCH LOGIC — cascading sources
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_live_rankings():
    """Fetch fresh ATP + WTA rankings.
    Priority: 1) Official ATP/WTA Tour sites  2) Sofascore  3) TML match data
    Returns dict with 'rankings', 'count', 'source', 'fetched_at'."""
    all_rankings = {}
    errors = []
    source = "unknown"

    # ── SOURCE 1: Official ATP Tour + WTA Tour ──
    print("  [rankings] Trying official ATP Tour website...")
    try:
        atp = _fetch_atp_rankings()
        if atp and len(atp) >= 10:
            all_rankings.update(atp)
            source = "atptour.com"
            print(f"  [rankings] ✓ ATP Tour: {len(atp)} players")
        else:
            errors.append(f"ATP Tour: only {len(atp)} players returned")
    except Exception as e:
        errors.append(f"ATP Tour: {e}")
        print(f"  [rankings] ATP Tour failed: {e}", file=sys.stderr)

    time.sleep(1)

    print("  [rankings] Trying official WTA Tour website...")
    try:
        wta = _fetch_wta_rankings()
        if wta and len(wta) >= 10:
            all_rankings.update(wta)
            source = f"{source}+wtatennis.com" if source != "unknown" else "wtatennis.com"
            print(f"  [rankings] ✓ WTA Tour: {len(wta)} players")
        else:
            errors.append(f"WTA Tour: only {len(wta)} players returned")
    except Exception as e:
        errors.append(f"WTA Tour: {e}")
        print(f"  [rankings] WTA Tour failed: {e}", file=sys.stderr)

    time.sleep(1)

    # ── SOURCE 2: Sofascore API (fill in gaps) ──
    atp_count = sum(1 for v in all_rankings.values() if v["tour"] == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v["tour"] == "WTA")

    if atp_count < 50 or wta_count < 50:
        print(f"  [rankings] Official sources incomplete (ATP:{atp_count}, WTA:{wta_count}), trying Sofascore...")
        sofa_rankings, sofa_errors = _fetch_sofascore_rankings()
        errors.extend(sofa_errors)
        if sofa_rankings:
            for key, val in sofa_rankings.items():
                if key not in all_rankings:
                    all_rankings[key] = val
            source = f"{source}+sofascore" if source != "unknown" else "sofascore"
            print(f"  [rankings] Sofascore added {len(sofa_rankings)} players")

    time.sleep(1)

    # ── SOURCE 3: TML match data (last resort) ──
    atp_count = sum(1 for v in all_rankings.values() if v["tour"] == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v["tour"] == "WTA")

    if atp_count < 20 or wta_count < 20:
        print(f"  [rankings] Still low coverage (ATP:{atp_count}, WTA:{wta_count}), trying TML match data...")
        try:
            tml = _fetch_tml_rankings()
            if tml:
                for key, val in tml.items():
                    if key not in all_rankings:
                        all_rankings[key] = val
                source = f"{source}+tml" if source != "unknown" else "tml"
                print(f"  [rankings] TML added {len(tml)} players")
        except Exception as e:
            errors.append(f"TML: {e}")

    if not all_rankings:
        print(f"  [rankings] ALL SOURCES FAILED: {errors}", file=sys.stderr)

    # Log top 5 for verification
    if all_rankings:
        atp_top = sorted([v for v in all_rankings.values() if v["tour"] == "ATP"], key=lambda x: x["rank"])[:5]
        wta_top = sorted([v for v in all_rankings.values() if v["tour"] == "WTA"], key=lambda x: x["rank"])[:5]
        if atp_top:
            print(f"  [rankings] ATP Top 5: {', '.join(f'#{r[\"rank\"]} {r[\"name\"]}' for r in atp_top)}")
        if wta_top:
            print(f"  [rankings] WTA Top 5: {', '.join(f'#{r[\"rank\"]} {r[\"name\"]}' for r in wta_top)}")

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
                        src = data.get("source", "unknown")
                        print(f"  [rankings] Using cached rankings ({count} players, {age.seconds//3600}h old, source: {src})")
                        return data
                    else:
                        print(f"  [rankings] Cache stale ({age.seconds//3600}h old), refreshing...")
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, Exception) as e:
            print(f"  [rankings] Cache read error: {e}", file=sys.stderr)

    # Fetch fresh
    print("  [rankings] Fetching fresh rankings...")
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

    last_matches = []
    for key, r in rankings.items():
        key_parts = key.split()
        key_last = key_parts[-1].lower() if key_parts else ""
        if key_last == last_name:
            last_matches.append(r)

    if len(last_matches) == 1:
        return last_matches[0]["rank"], last_matches[0]["tour"]

    if len(last_matches) > 1 and first_name:
        for r in last_matches:
            r_parts = r["name"].lower().split()
            r_first = r_parts[0] if r_parts else ""
            if r_first.startswith(first_name) or first_name.startswith(r_first):
                return r["rank"], r["tour"]
            if len(first_name) == 1 and r_first.startswith(first_name):
                return r["rank"], r["tour"]
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
        print(f"\nFetched {data['count']} rankings (source: {data.get('source', 'unknown')})")
    else:
        data = load_cached_rankings()

    if args.lookup:
        rank, tour = lookup_player_rank(args.lookup, data)
        if rank:
            print(f"{args.lookup}: #{rank} ({tour})")
        else:
            print(f"{args.lookup}: NOT FOUND")
            rankings = data.get("rankings", {})
            last = args.lookup.split()[-1].lower()
            close = [(k, v["rank"]) for k, v in rankings.items() if last in k][:5]
            if close:
                print(f"  Close matches: {close}")

    if args.top:
        rankings = data.get("rankings", {})
        sorted_r = sorted(rankings.values(), key=lambda x: (x["tour"], x["rank"]))
        current_tour = None
        count = 0
        for r in sorted_r:
            if r["tour"] != current_tour:
                current_tour = r["tour"]
                count = 0
                print(f"\n  ── {current_tour} ──")
            if count < args.top:
                print(f"  #{r['rank']:>4}  {r['name']:<30}  {r['points']}pts")
                count += 1
