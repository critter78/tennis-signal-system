#!/bin/bash
# Render startup script — starts gunicorn FIRST, then runs pipeline in background
# This ensures the port opens immediately so Render doesn't time out
set -e

echo "=========================================="
echo "Tennis Betting System — Startup"
echo "=========================================="

# Create directories
mkdir -p cards logs data

# Start gunicorn IMMEDIATELY so Render detects the port
echo "[1/2] Starting gunicorn..."
gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 &
GUNICORN_PID=$!

# Give gunicorn a moment to bind the port
sleep 3

# Run pipeline in background — server is already serving
echo "[2/2] Running data pipeline in background..."
{
    echo "  Fetching live rankings..."
    python3 09_rankings_fetcher.py --refresh 2>&1 | tail -5 || echo "  Warning: Rankings fetch failed"

    echo "  Generating fresh betting cards..."
    python3 04_betting_card.py --min-volume 500 2>&1 | tail -5 || {
        echo "  Card gen failed at $500, trying $300..."
        python3 04_betting_card.py --min-volume 300 2>&1 | tail -5 || echo "  Warning: Card generation failed"
    }

    echo "  Resolving bet outcomes..."
    python3 08_outcome_resolver.py 2>&1 | tail -10 || echo "  Warning: Outcome resolution failed"

    echo "  Background pipeline complete."
} &

# Wait for gunicorn (keeps the script alive)
wait $GUNICORN_PID
