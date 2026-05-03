#!/usr/bin/env python3
"""
09_rankings_fetcher.py — Live ATP/WTA Rankings Fetcher
Pulls current rankings from official ATP Tour and WTA Tour websites.
Caches to disk (rankings_cache.json) so we only hit the sites once per day.

Priority order:
  1. Official ATP Tour + WTA Tour websites (most current, authoritative)
  2. Sofascore API (real-time, but may block server IPs)
  3. TML match data (last resort — infer approximate rank from recent results)

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

# ── TML match database (last resort — NOT Sackmann) ──
_TML_BASE = "https://stats.tennismylife.org"


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: Official ATP Tour + WTA Tour websites
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_atp_html(html):
    """Parse ATP rankings from atptour.com HTML using lightweight regex.
    No BeautifulSoup needed — avoids OOM on 512MB Render Starter plan.

    Known structure (verified May 2026):
        <tr>
          <td class="rank bold heavy tiny-cell">1</td>
          ...
          <a href="/en/players/jannik-sinner/s0ag/overview">
            <span class="lastName">J. Sinner</span>
          </a>
          ...
          <td class="points ...">
            <a href="...">13,350</a>
          </td>
        </tr>
    """
    rankings = {}

    # Extract just the <tbody>...</tbody> to minimize memory
    tbody_start = html.find("<tbody")
    tbody_end = html.find("</tbody>")
    if tbody_start < 0 or tbody_end < 0:
        print("  [rankings] ATP: no <tbody> found in HTML", file=sys.stderr)
        return {}
    table_html = html[tbody_start:tbody_end + 10]

    # Free the full HTML from memory
    del html

    # Split into rows and parse each one with regex
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    rank_pattern = re.compile(r'class="rank[^"]*"[^>]*>(\d+)<')
    slug_pattern = re.compile(r'href="/en/players/([a-z][\w-]+)/')
    name_pattern = re.compile(r'class="lastName"[^>]*>([^<]+)<')
    points_pattern = re.compile(r'class="points[^"]*"[^>]*>.*?>([\d,]+)<', re.DOTALL)

    for row_match in row_pattern.finditer(table_html):
        row = row_match.group(1)
        try:
            # Rank
            rm = rank_pattern.search(row)
            if not rm:
                continue
            rank = int(rm.group(1))
            if rank < 1 or rank > 500:
                continue

            # Player name from span.lastName
            nm = name_pattern.search(row)
            display_name = nm.group(1).strip() if nm else ""
            if not display_name or len(display_name) < 2:
                continue

            # Full name from URL slug (e.g., /players/jannik-sinner/ → Jannik Sinner)
            full_name = display_name
            sm = slug_pattern.search(row)
            if sm:
                full_name = sm.group(1).replace("-", " ").title()

            # Points
            pm = points_pattern.search(row)
            points = int(pm.group(1).replace(",", "")) if pm else 0

            # Store under both full name and display name
            key = full_name.lower().strip()
            entry = {"rank": rank, "name": full_name.strip(), "tour": "ATP", "points": points}
            rankings[key] = entry
            disp_key = display_name.lower().strip()
            if disp_key != key:
                rankings[disp_key] = entry

        except Exception:
            continue

    return rankings


def _parse_wta_html(html):
    """Parse WTA rankings from wtatennis.com HTML using lightweight regex.
    No BeautifulSoup needed — avoids OOM on 512MB Render Starter plan.

    Known structure (verified May 2026):
        <tr>
          <td class="player-row__cell player-row__cell--rank">1 -</td>
          <td class="player-row__cell player-row__cell--player">Aryna Sabalenka BLR</td>
          <td class="player-row__cell player-row__cell--age ...">27</td>
          <td class="player-row__cell player-row__cell--tournaments ...">19</td>
          <td class="player-row__cell player-row__cell--points ...">10,895</td>
        </tr>
    """
    rankings = {}

    # Extract just the rankings table to minimize memory
    table_start = html.find('class="rankings__list')
    if table_start < 0:
        # Fallback: find any table
        table_start = html.find("<table")
    if table_start < 0:
        print("  [rankings] WTA: no rankings table found in HTML", file=sys.stderr)
        return {}
    # Back up to the <table tag
    table_tag = html.rfind("<table", 0, table_start + 1)
    if table_tag >= 0:
        table_start = table_tag
    table_end = html.find("</table>", table_start)
    if table_end < 0:
        table_end = len(html)
    table_html = html[table_start:table_end + 10]

    # Free the full HTML from memory
    del html

    # Parse rows with regex
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    rank_pattern = re.compile(r'player-row__cell--rank[^>]*>\s*(\d+)')
    player_pattern = re.compile(r'player-row__cell--player[^>]*>(.*?)</td>', re.DOTALL)
    points_pattern = re.compile(r'player-row__cell--points[^>]*>\s*([\d,]+)')
    tag_strip = re.compile(r"<[^>]+>")

    for row_match in row_pattern.finditer(table_html):
        row = row_match.group(1)
        try:
            # Rank — text like "1 -" or "2 +1", extract leading number
            rm = rank_pattern.search(row)
            if not rm:
                continue
            rank = int(rm.group(1))
            if rank < 1 or rank > 500:
                continue

            # Player name
            pm = player_pattern.search(row)
            if not pm:
                continue
            raw_name = tag_strip.sub("", pm.group(1)).strip()
            # Collapse whitespace
            raw_name = re.sub(r"\s+", " ", raw_name).strip()
            if not raw_name or len(raw_name) < 3:
                continue

            # Strip trailing 2-3 letter country code (e.g., "Aryna Sabalenka BLR")
            name_match = re.match(r"^(.+?)\s+[A-Z]{2,3}$", raw_name)
            player_name = name_match.group(1).strip() if name_match else raw_name

            # Points
            pts_match = points_pattern.search(row)
            points = int(pts_match.group(1).replace(",", "")) if pts_match else 0

            key = player_name.lower().strip()
            rankings[key] = {
                "rank": rank,
                "name": player_name.strip(),
                "tour": "WTA",
                "points": points,
            }

        except Exception:
            continue

    return rankings


def _stream_html_section(url, start_marker, end_marker, max_bytes=300_000):
    """Stream an HTML page and extract only the section between start/end markers.
    Stops downloading as soon as end_marker is found — never holds the full page.
    This keeps memory under control on Render's 512MB Starter plan."""
    buffer = ""
    found_start = False
    section = ""

    resp = _req.get(url, headers=_BROWSER_HEADERS, timeout=30, stream=True)
    resp.raise_for_status()

    for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
        if chunk is None:
            continue
        # If response is bytes, decode
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")

        buffer += chunk

        if not found_start:
            idx = buffer.find(start_marker)
            if idx >= 0:
                buffer = buffer[idx:]  # trim everything before the marker
                found_start = True
            elif len(buffer) > max_bytes:
                # Marker not found within max_bytes — give up
                resp.close()
                return ""
            else:
                # Keep only the tail (marker could span chunks)
                if len(buffer) > len(start_marker) * 2:
                    buffer = buffer[-(len(start_marker) * 2):]
                continue

        # We've found the start — now look for the end
        end_idx = buffer.find(end_marker)
        if end_idx >= 0:
            section = buffer[:end_idx + len(end_marker)]
            resp.close()
            return section

        # Safety: don't accumulate more than max_bytes after start
        if len(buffer) > max_bytes:
            resp.close()
            return buffer[:max_bytes]

    resp.close()
    return buffer if found_start else ""


def _fetch_atp_rankings(top_n=200):
    """Fetch current ATP singles rankings from atptour.com.
    Streams HTML and extracts only <tbody>...</tbody> to minimize memory."""
    rankings = {}

    try:
        print("  [rankings] Streaming ATP Tour HTML (tbody only)...")
        section = _stream_html_section(
            _ATP_RANKINGS_URL,
            start_marker="<tbody",
            end_marker="</tbody>",
            max_bytes=200_000,  # ATP table is ~130KB
        )
        if section:
            # Wrap in minimal HTML so parser can find it
            rankings = _parse_atp_html("<table class='mega-table'>" + section + "</table>")
        if rankings and len(rankings) >= 10:
            print(f"  [rankings] ATP HTML scrape: {len(rankings)} entries")
            return rankings
        else:
            print(f"  [rankings] ATP HTML parse returned only {len(rankings)} entries", file=sys.stderr)
    except Exception as e:
        print(f"  [rankings] ATP HTML scrape failed: {e}", file=sys.stderr)

    return rankings


def _fetch_wta_rankings(top_n=200):
    """Fetch current WTA singles rankings from wtatennis.com.
    Streams HTML and extracts only the rankings table to minimize memory."""
    rankings = {}

    try:
        print("  [rankings] Streaming WTA Tour HTML (table only)...")
        section = _stream_html_section(
            _WTA_RANKINGS_URL,
            start_marker='class="rankings__list',
            end_marker="</table>",
            max_bytes=250_000,
        )
        if section:
            # Find the <table tag that precedes the class attribute
            table_idx = section.rfind("<table", 0, 50)
            if table_idx < 0:
                section = "<table " + section
            rankings = _parse_wta_html(section)
        if rankings and len(rankings) >= 10:
            print(f"  [rankings] WTA HTML scrape: {len(rankings)} entries")
            return rankings
        else:
            print(f"  [rankings] WTA HTML parse returned only {len(rankings)} entries", file=sys.stderr)
    except Exception as e:
        print(f"  [rankings] WTA HTML scrape failed: {e}", file=sys.stderr)

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
# SOURCE 3: TML match data (last resort — infer rankings from recent results)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_tml_rankings():
    """Fetch approximate rankings from TML (tennismylife.org) match database.
    TML doesn't have a direct rankings endpoint, so we look for ranking data
    embedded in recent match records. Returns whatever we can find."""
    all_rankings = {}
    errors = []

    # Try to pull ranking info from TML match pages
    for tour, gender in [("ATP", "men"), ("WTA", "women")]:
        try:
            # TML stores recent matches with player rankings embedded
            url = f"{_TML_BASE}/tennis-match-database"
            resp = _req.get(url, headers=_BROWSER_HEADERS, timeout=20)
            if resp.status_code == 200 and _HAS_BS4:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Look for player ranking data in match listings
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    for cell in cells:
                        text = cell.get_text(strip=True)
                        # TML often shows rank in parentheses: "(1) Sinner"
                        match = re.match(r"\((\d+)\)\s+(.+)", text)
                        if match:
                            rank = int(match.group(1))
                            name = match.group(2).strip()
                            if rank <= 500 and len(name) > 2:
                                key = name.lower()
                                if key not in all_rankings:
                                    all_rankings[key] = {
                                        "rank": rank,
                                        "name": name,
                                        "tour": tour,
                                        "points": 0,
                                    }
            print(f"  [rankings] TML {tour}: {sum(1 for v in all_rankings.values() if v['tour'] == tour)} players")
        except Exception as e:
            errors.append(f"TML {tour}: {e}")
            print(f"  [rankings] TML {tour} failed: {e}", file=sys.stderr)

    return all_rankings, errors


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN FETCH LOGIC — cascading sources
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_live_rankings():
    """Fetch fresh ATP + WTA rankings.
    Priority: 1) Official ATP/WTA sites  2) Sofascore  3) TML match data
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
            print(f"  [rankings] ✓ ATP Tour: {len(atp)} entries")
        else:
            errors.append(f"ATP Tour: only {len(atp)} entries returned")
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
            print(f"  [rankings] ✓ WTA Tour: {len(wta)} entries")
        else:
            errors.append(f"WTA Tour: only {len(wta)} entries returned")
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
            # Only fill gaps — don't overwrite official data
            for key, val in sofa_rankings.items():
                if key not in all_rankings:
                    all_rankings[key] = val
            sofa_added = len(sofa_rankings)
            source = f"{source}+sofascore" if source != "unknown" else "sofascore"
            print(f"  [rankings] Sofascore filled {sofa_added} additional entries")

    time.sleep(1)

    # ── SOURCE 3: TML match data (last resort) ──
    atp_count = sum(1 for v in all_rankings.values() if v["tour"] == "ATP")
    wta_count = sum(1 for v in all_rankings.values() if v["tour"] == "WTA")

    if atp_count < 20 or wta_count < 20:
        print(f"  [rankings] Still low coverage (ATP:{atp_count}, WTA:{wta_count}), trying TML...")
        tml_rankings, tml_errors = _fetch_tml_rankings()
        errors.extend(tml_errors)
        if tml_rankings:
            for key, val in tml_rankings.items():
                if key not in all_rankings:
                    all_rankings[key] = val
            source = f"{source}+tml" if source != "unknown" else "tml"
            print(f"  [rankings] TML added {len(tml_rankings)} entries")

    if not all_rankings:
        print(f"  [rankings] ALL SOURCES FAILED: {errors}", file=sys.stderr)

    # Log top 5 for verification
    if all_rankings:
        atp_top = sorted([v for v in all_rankings.values() if v["tour"] == "ATP"], key=lambda x: x["rank"])[:5]
        wta_top = sorted([v for v in all_rankings.values() if v["tour"] == "WTA"], key=lambda x: x["rank"])[:5]
        atp_str = ", ".join(f"#{r['rank']} {r['name']}" for r in atp_top)
        wta_str = ", ".join(f"#{r['rank']} {r['name']}" for r in wta_top)
        print(f"  [rankings] ATP Top 5: {atp_str}")
        print(f"  [rankings] WTA Top 5: {wta_str}")

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
            if len(first_name) == 1 and r_first.startswith(first_name):
                return r["rank"], r["tour"]
        # Still ambiguous — return highest ranked
        best = min(last_matches, key=lambda x: x["rank"])
        return best["rank"], best["tour"]

    # 3. Handle "F. Lastname" format (common in ATP display names)
    if len(parts) == 2 and len(parts[0]) == 2 and parts[0].endswith("."):
        initial = parts[0][0].lower()
        for key, r in rankings.items():
            key_parts = key.split()
            if len(key_parts) >= 2:
                key_last = key_parts[-1].lower()
                key_first_initial = key_parts[0][0].lower() if key_parts[0] else ""
                if key_last == last_name and key_first_initial == initial:
                    return r["rank"], r["tour"]

    # 4. Fuzzy match (for name variations)
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
