#!/bin/bash
# Render startup script — runs on every deploy before gunicorn starts
# Ensures resolved outcomes and fresh cards are available immediately
set -e

echo "=========================================="
echo "Tennis Betting System — Startup"
echo "=========================================="

# Create directories
mkdir -p cards logs data

# Step 1: Fetch live ATP/WTA rankings
echo "[1/4] Fetching live rankings..."
python3 09_rankings_fetcher.py 2>&1 | tail -5 || echo "  Warning: Rankings fetch failed (will use historical)"

# Step 2: Generate fresh betting cards (pulls latest Polymarket data, filters completed matches)
echo "[2/4] Generating fresh betting cards..."
python3 04_betting_card.py --min-volume 500 2>&1 | tail -5 || {
    echo "  Card generation failed, trying lower volume threshold..."
    python3 04_betting_card.py --min-volume 300 2>&1 | tail -5 || echo "  Warning: Card generation failed"
}

# Step 3: Resolve outcomes for all picks (updates picks.jsonl with W/L results)
echo "[3/4] Resolving bet outcomes..."
python3 08_outcome_resolver.py 2>&1 | tail -10 || echo "  Warning: Outcome resolution failed"

# Step 4: Launch gunicorn
echo "[4/4] Starting gunicorn..."
echo "=========================================="
exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
