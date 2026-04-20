#!/bin/bash
# Render startup script — starts gunicorn FIRST, then runs pipeline in background
# This ensures the port opens immediately so Render doesn't time out
#
# MEMORY BUDGET: Render free tier = 512MB
#   - 1 gunicorn worker ~150MB
#   - Each pipeline subprocess ~200MB (pandas + parquet)
#   - Run pipeline steps SEQUENTIALLY so only 1 subprocess at a time
set -e

echo "=========================================="
echo "Tennis Betting System — Startup"
echo "=========================================="

# Create directories
mkdir -p cards logs data

# Start gunicorn with 1 worker to save memory (free tier = 512MB)
echo "[1/2] Starting gunicorn (1 worker, 600s timeout)..."
gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 600 &
GUNICORN_PID=$!

# Give gunicorn a moment to bind the port
sleep 3

# Run pipeline in background — SEQUENTIALLY to stay under memory limit
# Each step must finish before the next starts (no parallel subprocesses)
echo "[2/2] Running data pipeline in background..."
{
    echo "  [step 1/4] Fetching TML match data..."
    python3 12_tml_live_fetch.py --recent 2>&1 | tail -20
    echo "  TML parquet check: $(ls -la data/tml_history_10y.parquet 2>&1)"

    echo "  [step 2/4] Fetching live rankings..."
    python3 09_rankings_fetcher.py --refresh 2>&1 | tail -5 || echo "  Warning: Rankings fetch failed"

    echo "  [step 3/4] Generating fresh betting cards..."
    python3 04_betting_card.py --min-volume 500 2>&1 | tail -5 || {
        echo "  Card gen failed at 500, trying 300..."
        python3 04_betting_card.py --min-volume 300 2>&1 | tail -5 || echo "  Warning: Card generation failed"
    }

    echo "  [step 4/4] Resolving bet outcomes..."
    python3 08_outcome_resolver.py 2>&1 | tail -10 || echo "  Warning: Outcome resolution failed"

    echo "  Background pipeline complete."
} &

# Wait for gunicorn (keeps the script alive)
wait $GUNICORN_PID
