#!/usr/bin/env python3
"""
Live ATP/WTA Rankings Fetcher

Fetches current world rankings from multiple reliable sources.
Primary: live-tennis.eu (server-rendered, accurate live rankings)
Fallback: Tennis Explorer, ATP/WTA official sites

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _normalize_name(name):
    """Normalize a player name for lookup matching."""
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


# ─── PRIMARY SOURCE: live-tennis.eu ──────────────────────────────────────────

def fetch_from_live_tennis():
    """
    Fetch rankings from live-tennis.eu — server-rendered HTML with accurate
    live rankings that update in real-time during tournaments.
    Returns dict of normalized_name -> {rank, name, tour}
    """
    rankings = {}

    for tour, url in [
        ("ATP", "https://live-tennis.eu/en/atp-live-ranking"),
        ("WTA", "https://live-tennis.eu/en/wta-live-ranking"),
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            html = r.text
            found = 0

            # live-tennis.eu uses a table with class "live-ranking"
            # Each row has: rank, move, player name, points
            # Multiple regex strategies for robustness

            patterns = [
                # Pattern 1: table row with rank number and player link
                re.compile(
                    r'<td[^>]*>\s*(\d{1,4})\s*</td>'
                    r'.*?<a[^>]*href="[^"]*player[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                    re.DOTALL
                ),
                # Pattern 2: rank in first td, player name in link
                re.compile(
                    r'<td[^>]*class="[^"]*"[^>]*>\s*(\d{1,4})\s*</td>'
                    r'(?:.*?<td[^>]*>.*?</td>){0,3}?'
                    r'.*?<a[^>]*>\s*([A-Z][a-zA-Z\s\.\-\']+?)\s*</a>',
                    re.DOTALL
                ),
                # Pattern 3: broader — any number followed by a name link
                re.compile(
                    r'>(\d{1,4})</td>'
                    r'.*?<a[^>]*>([A-Z][a-zA-Z\.\s\-\']{2,40})</a>',
                    re.DOTALL
                ),
            ]

            for pattern in patterns:
                for match in pattern.finditer(html):
                    rank = int(match.group(1))
                    name = match.group(2).strip()
                    # Sanity checks
                    if rank < 1 or rank > 500:
                        continue
                    if len(name) < 3 or len(name) > 50:
                        continue
                    # Skip if name looks like a number or HTML
                    if name.isdigit() or '<' in name:
                        continue
                    key = _normalize_name(name)
                    if key not in rankings:
                        rankings[key] = {"rank": rank, "name": name, "tour": tour}
                        found += 1
                if found > 30:
                    break  # Good pattern found

            print(f"    live-tennis.eu {tour}: {found} rankings")
            time.sleep(0.5)  # Be polite

        except Exception as e:
            print(f"    [warn] live-tennis.eu {tour}: {e}")

    return rankings


# ─── FALLBACK 1: Tennis Explorer ─────────────────────────────────────────────

def fetch_from_tennis_explorer():
    """
    Fetch rankings from Tennis Explorer — server-rendered HTML, reliable fallback.
    """
    rankings = {}

    for tour, base_url in [
        ("ATP", "https://www.tennisexplorer.com/ranking/atp-men/"),
        ("WTA", "https://www.tennisexplorer.com/ranking/wta-women/"),
    ]:
        for page_suffix in ["", "?page=2", "?page=3", "?page=4", "?page=5"]:
            try:
                url = base_url + page_suffix
                r = requests.get(url, headers=HEADERS, timeout=15)
                r.raise_for_status()
                html = r.text
                found_on_page = 0

                patterns = [
                    re.compile(
                        r'<td[^>]*class="[^"]*rank[^"]*"[^>]*>\s*(\d+)\.?\s*</td>'
                        r'.*?<a[^>]*href="/player/[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                        re.DOTALL
                    ),
                    re.compile(
                        r'<td[^>]*>\s*(\d{1,4})\.?\s*</td>\s*<td[^>]*>.*?'
                        r'<a[^>]*href="/player/[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                        re.DOTALL
                    ),
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
                        break

                if found_on_page == 0 and page_suffix:
                    break

            except Exception as e:
                print(f"    [warn] Tennis Explorer {tour} page {page_suffix or '1'}: {e}")
                break

            time.sleep(0.5)

        tour_count = sum(1 for v in rankings.values() if v["tour"] == tour)
        print(f"    Tennis Explorer {tour}: {tour_count} rankings")

    return rankings


# ─── FALLBACK 2: ATP/WTA official sites ──────────────────────────────────────

def fetch_atp_rankings(max_rank=200):
    """Fetch from ATP official site — often returns empty due to JS rendering."""
    rankings = {}

    for api_url in [
        "https://www.atptour.com/en/-/www/rank/singles",
        "https://www.atptour.com/-/ajax/RankingsController/GetRankings?rankDate=&countryCode=&rankRange=1-200&rankType=rankSingles",
    ]:
        try:
            r = requests.get(api_url, headers={**HEADERS, "Accept": "application/json,text/html"}, timeout=12)
            if r.status_code == 200:
                try:
                    data = r.json() if "json" in r.headers.get("content-type", "") else json.loads(r.text)
                except (json.JSONDecodeError, ValueError):
                    continue

                items = data if isinstance(data, list) else data.get("rankings", data.get("data", data.get("Rows", [])))
                if not isinstance(items, list):
                    continue

                for item in items[:max_rank]:
                    name = (item.get("playerName") or item.get("Player") or item.get("name")
                            or item.get("fullName") or item.get("PlayerName") or "").strip()
                    rank = (item.get("ranking") or item.get("Rank") or item.get("rank")
                            or item.get("sglRank") or item.get("Position") or 0)
                    if name and rank:
                        rankings[_normalize_name(name)] = {"rank": int(rank), "name": name, "tour": "ATP"}
                if len(rankings) >= 50:
                    print(f"    ATP API returned {len(rankings)} rankings")
                    return rankings
        except Exception as e:
            pass

    return rankings


def fetch_wta_rankings(max_rank=200):
    """Fetch from WTA official site."""
    rankings = {}

    for url in [
        "https://www.wtatennis.com/rankings/singles",
        "https://api.wtatennis.com/tennis/v2/content/rankings/singles",
    ]:
        try:
            r = requests.get(url, headers={**HEADERS, "Accept": "application/json,text/html"}, timeout=12)
            if r.status_code == 200:
                # Try JSON parse
                try:
                    data = r.json() if "json" in r.headers.get("content-type", "") else json.loads(r.text)
                except (json.JSONDecodeError, ValueError):
                    # Try HTML parsing
                    html = r.text
                    pattern = re.compile(r'"rank"\s*:\s*(\d+).*?"(?:player|name|fullName)"\s*:\s*"([^"]+)"', re.DOTALL)
                    for match in pattern.finditer(html):
                        rank = int(match.group(1))
                        name = match.group(2).strip()
                        if name and rank <= max_rank:
                            rankings[_normalize_name(name)] = {"rank": rank, "name": name, "tour": "WTA"}
                    continue

                items = data if isinstance(data, list) else data.get("rankings", data.get("data", []))
                if isinstance(items, list):
                    for item in items[:max_rank]:
                        name = (item.get("playerName") or item.get("name") or item.get("fullName") or "").strip()
                        rank = item.get("ranking") or item.get("rank") or item.get("position") or 0
                        if name and rank:
                            rankings[_normalize_name(name)] = {"rank": int(rank), "name": name, "tour": "WTA"}

                if len(rankings) >= 50:
                    print(f"    WTA API returned {len(rankings)} rankings")
                    return rankings
        except Exception as e:
            pass

    return rankings


# ─── FALLBACK 3: Flashscore/Livesport API ────────────────────────────────────

def fetch_from_flashscore():
    """Fetch from Flashscore's internal API."""
    rankings = {}

    for tour, url in [
        ("ATP", "https://www.flashscore.com/tennis/atp-singles/rankings/"),
        ("WTA", "https://www.flashscore.com/tennis/wta-singles/rankings/"),
    ]:
        try:
            r = requests.get(url, headers={**HEADERS, "x-fsign": "SW9D1eZo"}, timeout=12)
            if r.status_code == 200:
                html = r.text
                pattern = re.compile(r'"rank"\s*:\s*(\d+).*?"name"\s*:\s*"([^"]+)"', re.DOTALL)
                for match in pattern.finditer(html):
                    rank = int(match.group(1))
                    name = match.group(2).strip()
                    if name and rank <= 500:
                        rankings[_normalize_name(name)] = {"rank": rank, "name": name, "tour": tour}
        except Exception as e:
            print(f"    [warn] Flashscore {tour}: {e}")

    return rankings


# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def fetch_and_cache_rankings():
    """Fetch rankings from all sources (best first) and cache them."""
    CACHE_DIR.mkdir(exist_ok=True)

    print("  Fetching live rankings...")
    all_rankings = {}

    # ── Step 1: PRIMARY — live-tennis.eu (most accurate, server-rendered) ──
    print("  [1/4] Primary: live-tennis.eu...")
    lt = fetch_from_live_tennis()
    if lt:
        all_rankings.update(lt)

    atp_count = sum(1 for v in all_rankings.values() if v.get("tour") == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v.get("tour") == "WTA")

    # ── Step 2: FALLBACK 1 — Tennis Explorer (if live-tennis.eu didn't work well) ──
    if atp_count < 50 or wta_count < 50:
        print(f"  [2/4] Fallback: Tennis Explorer (ATP={atp_count}, WTA={wta_count})...")
        te = fetch_from_tennis_explorer()
        for key, val in te.items():
            if key not in all_rankings:
                all_rankings[key] = val
    else:
        print(f"  [2/4] Skipped Tennis Explorer (ATP={atp_count}, WTA={wta_count} sufficient)")

    atp_count = sum(1 for v in all_rankings.values() if v.get("tour") == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v.get("tour") == "WTA")

    # ── Step 3: FALLBACK 2 — ATP/WTA official APIs ──
    if atp_count < 50:
        print(f"  [3/4] Fallback: ATP official API...")
        atp_off = fetch_atp_rankings()
        for key, val in atp_off.items():
            if key not in all_rankings:
                all_rankings[key] = val

    if wta_count < 50:
        print(f"  [3/4] Fallback: WTA official API...")
        wta_off = fetch_wta_rankings()
        for key, val in wta_off.items():
            if key not in all_rankings:
                all_rankings[key] = val

    atp_count = sum(1 for v in all_rankings.values() if v.get("tour") == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v.get("tour") == "WTA")

    # ── Step 4: FALLBACK 3 — Flashscore ──
    if atp_count < 30 or wta_count < 30:
        print(f"  [4/4] Fallback: Flashscore...")
        fs = fetch_from_flashscore()
        for key, val in fs.items():
            if key not in all_rankings:
                all_rankings[key] = val
    else:
        print(f"  [4/4] Skipped Flashscore (ATP={atp_count}, WTA={wta_count} sufficient)")

    atp_count = sum(1 for v in all_rankings.values() if v.get("tour") == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v.get("tour") == "WTA")

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
        "atp_count": atp_count,
        "wta_count": wta_count,
        "rankings": all_rankings,
        "lastname_index": lastname_index,
    }

    with open(RANKINGS_CACHE, 'w') as f:
        json.dump(cache, f, indent=2)

    print(f"  Cached {len(all_rankings)} rankings (ATP={atp_count}, WTA={wta_count}) to {RANKINGS_CACHE}")
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

    # Partial last name match
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
            print(f"  {args.lookup}: not found in rankings cache")
    elif args.refresh:
        fetch_and_cache_rankings()
    else:
        cache = load_cached_rankings()
        show_status()
