#!/usr/bin/env python3
"""
Live ATP/WTA Rankings Fetcher

Scrapes current world rankings from the ATP and WTA official websites.
Caches results to avoid repeated requests.

Usage:
    python3 09_rankings_fetcher.py                # Fetch and cache rankings
    python3 09_rankings_fetcher.py --status        # Show cache status
    python3 09_rankings_fetcher.py --lookup "Sinner"  # Look up a player

Called by 04_betting_card.py to get accurate rankings for signal cards.
"""

import json
import re
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import requests

CACHE_DIR = Path("data")
RANKINGS_CACHE = CACHE_DIR / "live_rankings.json"
CACHE_MAX_AGE_HOURS = 12  # Re-fetch if cache is older than this


def fetch_atp_rankings(max_rank=500):
    """
    Fetch current ATP singles rankings from multiple sources.
    Tries in order: ATP API endpoint, Tennis Explorer, Tennis Abstract.
    """
    rankings = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # ── Source 1: ATP Tour Rankings API (JSON) ──
    # The ATP site's frontend fetches from this endpoint
    for api_url in [
        "https://www.atptour.com/en/-/www/rank/singles",
        "https://www.atptour.com/-/ajax/RankingsController/GetRankings?rankDate=&countryCode=&rankRange=1-200&rankType=rankSingles",
    ]:
        try:
            r = requests.get(api_url, headers={**headers, "Accept": "application/json,text/html"}, timeout=12)
            if r.status_code == 200:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
                if not data:
                    # Try parsing as JSON anyway
                    try:
                        data = json.loads(r.text)
                    except json.JSONDecodeError:
                        continue

                # Handle various response shapes
                items = data if isinstance(data, list) else data.get("rankings", data.get("data", data.get("Rows", [])))
                if not isinstance(items, list):
                    continue

                for item in items[:max_rank]:
                    # Try various key names used by different ATP API versions
                    name = (item.get("playerName") or item.get("Player") or item.get("name")
                            or item.get("fullName") or item.get("PlayerName") or "").strip()
                    rank = (item.get("ranking") or item.get("Rank") or item.get("rank")
                            or item.get("sglRank") or item.get("Position") or 0)
                    if name and rank:
                        rankings[_normalize_name(name)] = {
                            "rank": int(rank),
                            "name": name,
                            "tour": "ATP",
                        }
                if len(rankings) >= 50:
                    print(f"    ATP API returned {len(rankings)} rankings")
                    return rankings
        except Exception as e:
            print(f"    [debug] ATP API {api_url}: {e}")

    # ── Source 2: ATP Tour HTML page (multiple parsing strategies) ──
    try:
        r = requests.get("https://www.atptour.com/en/rankings/singles", headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text

        # Strategy A: JSON embedded in page (rankingsData, __NEXT_DATA__, etc.)
        for pattern in [
            r'rankingsData\s*=\s*(\[.*?\]);',
            r'__NEXT_DATA__.*?"rankings"\s*:\s*(\[.*?\])',
            r'"rankingsList"\s*:\s*(\[.*?\])',
        ]:
            json_match = re.search(pattern, html, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    for item in data[:max_rank]:
                        name = (item.get("playerName") or item.get("name") or item.get("fullName") or "").strip()
                        rank = item.get("ranking") or item.get("rank") or item.get("position") or 0
                        if name and rank:
                            rankings[_normalize_name(name)] = {"rank": int(rank), "name": name, "tour": "ATP"}
                    if rankings:
                        print(f"    ATP HTML/JSON returned {len(rankings)} rankings")
                        return rankings
                except (json.JSONDecodeError, KeyError):
                    pass

        # Strategy B: HTML table parsing (multiple row formats)
        for row_pattern in [
            re.compile(r'<td[^>]*class="[^"]*rank[^"]*"[^>]*>\s*(\d+)\s*</td>.*?<a[^>]*href="/en/players/[^"]*"[^>]*>\s*([^<]+?)\s*</a>', re.DOTALL),
            re.compile(r'class="rank-cell"[^>]*>.*?(\d+).*?</.*?class="player-cell"[^>]*>.*?<a[^>]*>\s*([^<]+?)\s*</a>', re.DOTALL),
            re.compile(r'"rank"\s*:\s*(\d+).*?"(?:player|name)"\s*:\s*"([^"]+)"', re.DOTALL),
        ]:
            for match in row_pattern.finditer(html):
                rank = int(match.group(1))
                name = match.group(2).strip()
                if rank <= max_rank and name and len(name) > 3:
                    rankings[_normalize_name(name)] = {"rank": rank, "name": name, "tour": "ATP"}
            if len(rankings) >= 50:
                print(f"    ATP HTML table returned {len(rankings)} rankings")
                return rankings

    except Exception as e:
        print(f"  [warn] ATP HTML fetch failed: {e}")

    return rankings


def fetch_wta_rankings(max_rank=500):
    """Fetch current WTA singles rankings from multiple sources."""
    rankings = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text

        # WTA site also has various HTML structures
        # Try JSON data first
        json_match = re.search(r'rankingsData\s*[:=]\s*(\[.*?\])', html, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                for item in data[:max_rank]:
                    name = item.get("playerName", item.get("name", "")).strip()
                    rank = item.get("ranking", item.get("rank", 0))
                    if name and rank:
                        rankings[_normalize_name(name)] = {
                            "rank": int(rank),
                            "name": name,
                            "tour": "WTA",
                        }
                if rankings:
                    return rankings
            except (json.JSONDecodeError, KeyError):
                pass

        # HTML parsing fallback
        row_pattern = re.compile(
            r'<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>.*?'
            r'<a[^>]*>\s*([^<]+?)\s*</a>',
            re.DOTALL
        )
        for match in row_pattern.finditer(html):
            rank = int(match.group(1))
            name = match.group(2).strip()
            if rank <= max_rank and name:
                rankings[_normalize_name(name)] = {
                    "rank": rank,
                    "name": name,
                    "tour": "WTA",
                }

    except Exception as e:
        print(f"  [warn] WTA rankings fetch failed: {e}")

    return rankings


def fetch_rankings_from_tennis_explorer():
    """
    Fetch rankings from Tennis Explorer — server-rendered HTML, very reliable.
    Fetches multiple pages to get top 500 for both ATP and WTA.
    """
    rankings = {}

    for tour, base_url in [
        ("ATP", "https://www.tennisexplorer.com/ranking/atp-men/"),
        ("WTA", "https://www.tennisexplorer.com/ranking/wta-women/"),
    ]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        # Fetch pages to get top ~500 rankings
        for page_suffix in ["", "?page=2", "?page=3", "?page=4", "?page=5"]:
            try:
                url = base_url + page_suffix
                r = requests.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                html = r.text

                found_on_page = 0

                # Tennis Explorer patterns (multiple attempts)
                patterns = [
                    # Primary: rank cell + player link
                    re.compile(
                        r'<td[^>]*class="[^"]*rank[^"]*"[^>]*>\s*(\d+)\.?\s*</td>'
                        r'.*?<a[^>]*href="/player/[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                        re.DOTALL
                    ),
                    # Alt: simpler table structure
                    re.compile(
                        r'<td[^>]*>\s*(\d{1,4})\.?\s*</td>\s*<td[^>]*>.*?'
                        r'<a[^>]*href="/player/[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                        re.DOTALL
                    ),
                    # Alt: even simpler
                    re.compile(r'>(\d{1,3})\.\s*</.*?<a[^>]*href="/player/[^"]*"[^>]*>([^<]{3,40})</a>', re.DOTALL),
                ]

                for pattern in patterns:
                    for match in pattern.finditer(html):
                        rank = int(match.group(1))
                        name = match.group(2).strip()
                        if name and rank <= 500 and len(name) > 2:
                            key = _normalize_name(name)
                            if key not in rankings:
                                rankings[key] = {"rank": rank, "name": name, "tour": tour}
                                found_on_page += 1
                    if found_on_page > 10:
                        break  # Good pattern found

                if found_on_page == 0 and page_suffix:
                    break  # No more pages

            except Exception as e:
                print(f"  [warn] Tennis Explorer {tour} page {page_suffix or '1'}: {e}")
                break

            time.sleep(0.5)  # Be polite

        tour_count = sum(1 for v in rankings.values() if v["tour"] == tour)
        print(f"    Tennis Explorer {tour}: {tour_count} rankings")

    return rankings


def fetch_rankings_from_livesport():
    """
    Fallback 2: fetch from flashscore/livesport which has a clean API.
    """
    rankings = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html",
        "x-fsign": "SW9D1eZo",
    }

    for tour, url in [
        ("ATP", "https://www.flashscore.com/tennis/atp-singles/rankings/"),
        ("WTA", "https://www.flashscore.com/tennis/wta-singles/rankings/"),
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                html = r.text
                # Flashscore embeds data in a specific format
                pattern = re.compile(r'"rank"\s*:\s*(\d+).*?"name"\s*:\s*"([^"]+)"', re.DOTALL)
                for match in pattern.finditer(html):
                    rank = int(match.group(1))
                    name = match.group(2).strip()
                    if name and rank <= 500:
                        rankings[_normalize_name(name)] = {"rank": rank, "name": name, "tour": tour}
        except Exception as e:
            print(f"  [warn] Livesport {tour}: {e}")

    return rankings


def _normalize_name(name):
    """Normalize a player name for lookup matching."""
    # Remove accents, lowercase, clean up
    name = name.strip().lower()
    name = re.sub(r'\s+', ' ', name)
    # Handle "Last, First" format
    if ',' in name:
        parts = name.split(',', 1)
        name = f"{parts[1].strip()} {parts[0].strip()}"
    return name


def _last_name(name):
    """Extract last name from a full name."""
    parts = name.strip().split()
    return parts[-1].lower() if parts else ""


def fetch_and_cache_rankings():
    """Fetch rankings from all sources and cache them."""
    CACHE_DIR.mkdir(exist_ok=True)

    print("  Fetching live rankings...")
    all_rankings = {}

    # Try primary sources first (ATP/WTA official sites)
    print("  [1/4] Fetching ATP rankings from official sources...")
    atp = fetch_atp_rankings()
    if atp:
        all_rankings.update(atp)
        print(f"    Got {len(atp)} ATP rankings")

    print("  [2/4] Fetching WTA rankings from official sources...")
    wta = fetch_wta_rankings()
    if wta:
        all_rankings.update(wta)
        print(f"    Got {len(wta)} WTA rankings")

    # Tennis Explorer is very reliable (server-rendered) — always use as primary/supplement
    atp_count = sum(1 for v in all_rankings.values() if v.get("tour") == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v.get("tour") == "WTA")

    if atp_count < 100 or wta_count < 100:
        print(f"  [3/4] Official sources: ATP={atp_count}, WTA={wta_count} — supplementing with Tennis Explorer...")
        te = fetch_rankings_from_tennis_explorer()
        if te:
            for key, val in te.items():
                if key not in all_rankings:
                    all_rankings[key] = val
            print(f"    Tennis Explorer added {len(te)} total rankings")
    else:
        print(f"  [3/4] Official sources sufficient (ATP={atp_count}, WTA={wta_count})")

    # Final fallback
    if len(all_rankings) < 50:
        print("  [4/4] Still insufficient, trying Livesport fallback...")
        ls = fetch_rankings_from_livesport()
        if ls:
            for key, val in ls.items():
                if key not in all_rankings:
                    all_rankings[key] = val
    else:
        print(f"  [4/4] Total rankings: {len(all_rankings)}, no further fallback needed")

    # Build a last-name index for fuzzy matching
    lastname_index = {}
    for norm_name, data in all_rankings.items():
        last = _last_name(norm_name)
        if last and len(last) > 2:
            if last not in lastname_index:
                lastname_index[last] = []
            lastname_index[last].append(data)

    cache = {
        "fetched_at": datetime.utcnow().isoformat(),
        "count": len(all_rankings),
        "rankings": all_rankings,
        "lastname_index": lastname_index,
    }

    with open(RANKINGS_CACHE, 'w') as f:
        json.dump(cache, f, indent=2)

    print(f"  Cached {len(all_rankings)} rankings to {RANKINGS_CACHE}")
    return cache


def load_cached_rankings():
    """Load cached rankings, re-fetching if stale."""
    if RANKINGS_CACHE.exists():
        try:
            with open(RANKINGS_CACHE) as f:
                cache = json.load(f)

            fetched_at = datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))
            age_hours = (datetime.utcnow() - fetched_at).total_seconds() / 3600

            if age_hours < CACHE_MAX_AGE_HOURS:
                return cache
            else:
                print(f"  Rankings cache is {age_hours:.1f}h old, refreshing...")
        except (json.JSONDecodeError, KeyError, ValueError):
            print("  Rankings cache corrupted, refreshing...")

    return fetch_and_cache_rankings()


def lookup_player_rank(player_name, cache=None):
    """
    Look up a player's current world ranking.

    Args:
        player_name: Full player name (e.g., "Jannik Sinner")
        cache: Pre-loaded cache dict, or None to load from file

    Returns:
        (rank, tour) tuple, or (None, None) if not found
    """
    if cache is None:
        cache = load_cached_rankings()

    rankings = cache.get("rankings", {})
    lastname_index = cache.get("lastname_index", {})

    norm = _normalize_name(player_name)

    # Exact match
    if norm in rankings:
        r = rankings[norm]
        return r["rank"], r["tour"]

    # Last name match (most reliable for fuzzy matching)
    last = _last_name(player_name)
    if last in lastname_index:
        candidates = lastname_index[last]
        if len(candidates) == 1:
            # Unique last name — confident match
            return candidates[0]["rank"], candidates[0]["tour"]
        else:
            # Multiple players with same last name — try first name
            first = player_name.strip().split()[0].lower() if player_name.strip().split() else ""
            for c in candidates:
                if first and first in c["name"].lower():
                    return c["rank"], c["tour"]
            # If still ambiguous, return the highest-ranked one
            best = min(candidates, key=lambda x: x["rank"])
            return best["rank"], best["tour"]

    # Partial last name match (for names like "Rakotomanga" that might be truncated)
    for ln, candidates in lastname_index.items():
        if len(ln) > 3 and (ln in last or last in ln):
            best = min(candidates, key=lambda x: x["rank"])
            return best["rank"], best["tour"]

    return None, None


def show_status():
    """Show rankings cache status."""
    if not RANKINGS_CACHE.exists():
        print("  No rankings cache found. Run: python3 09_rankings_fetcher.py")
        return

    with open(RANKINGS_CACHE) as f:
        cache = json.load(f)

    fetched_at = cache.get("fetched_at", "unknown")
    count = cache.get("count", 0)
    rankings = cache.get("rankings", {})

    atp_count = sum(1 for r in rankings.values() if r.get("tour") == "ATP")
    wta_count = sum(1 for r in rankings.values() if r.get("tour") == "WTA")

    age = "unknown"
    try:
        dt = datetime.fromisoformat(fetched_at)
        age_hours = (datetime.utcnow() - dt).total_seconds() / 3600
        age = f"{age_hours:.1f} hours ago"
    except ValueError:
        pass

    print(f"\n  Rankings Cache Status:")
    print(f"  Fetched: {fetched_at} ({age})")
    print(f"  Total: {count} players")
    print(f"  ATP: {atp_count}")
    print(f"  WTA: {wta_count}")

    # Show top 10 ATP and WTA
    atp_top = sorted(
        [(k, v) for k, v in rankings.items() if v.get("tour") == "ATP"],
        key=lambda x: x[1]["rank"]
    )[:10]
    wta_top = sorted(
        [(k, v) for k, v in rankings.items() if v.get("tour") == "WTA"],
        key=lambda x: x[1]["rank"]
    )[:10]

    if atp_top:
        print(f"\n  Top 10 ATP:")
        for _, r in atp_top:
            print(f"    #{r['rank']:>3} {r['name']}")

    if wta_top:
        print(f"\n  Top 10 WTA:")
        for _, r in wta_top:
            print(f"    #{r['rank']:>3} {r['name']}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch live ATP/WTA rankings")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    parser.add_argument("--lookup", type=str, help="Look up a player's ranking")
    parser.add_argument("--refresh", action="store_true", help="Force refresh cache")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.lookup:
        cache = load_cached_rankings()
        rank, tour = lookup_player_rank(args.lookup, cache)
        if rank:
            print(f"  {args.lookup}: #{rank} ({tour})")
        else:
            print(f"  {args.lookup}: Not found in rankings")
    elif args.refresh:
        fetch_and_cache_rankings()
    else:
        fetch_and_cache_rankings()
