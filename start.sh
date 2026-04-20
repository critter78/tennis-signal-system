#!/bin/bash
# Render startup script — starts gunicorn FIRST, then fetches data in background
# Card generation is SKIPPED on startup to stay within 512MB memory limit.
# Use the admin panel RUN FULL PIPELINE button to generate cards on-demand.
set -e

echo "=========================================="
echo "Tennis Betting System — Startup"
echo "=========================================="

# Create directories
mkdir -p cards logs data

# Start gunicorn with 1 worker (free tier = 512MB)
echo "[1/2] Starting gunicorn (1 worker, 600s timeout)..."
gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 600 &
GUNICORN_PID=$!

# Give gunicorn a moment to bind the port
sleep 3

# Lightweight data fetch only — NO card generation (too memory-heavy)
echo "[2/2] Fetching fresh data in background..."
{
    echo "  Fetching TML match data..."
    python3 12_tml_live_fetch.py --recent 2>&1 | tail -20 || echo "  Warning: TML fetch failed"
    echo "  TML parquet check: $(ls -la data/tml_history_10y.parquet 2>&1)"

    echo "  Fetching live rankings..."
    python3 09_rankings_fetcher.py --refresh 2>&1 | tail -5 || echo "  Warning: Rankings fetch failed"

    echo "  Startup data fetch complete."
    echo "  NOTE: Card generation skipped on startup (memory constraint)."
    echo "  Use admin panel RUN FULL PIPELINE to generate fresh cards."
} &

# Wait for gunicorn (keeps the script alive)
wait $GUNICORN_PID
