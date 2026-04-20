#!/bin/bash
# Render startup script — gunicorn ONLY, no background processing
# All pipeline work happens on-demand via admin panel RUN FULL PIPELINE
#
# Why: Render free tier = 512MB. Any Python subprocess (pandas import
# alone = ~100MB) running alongside gunicorn causes OOM kills.
set -e

echo "=========================================="
echo "Tennis Betting System — Startup"
echo "=========================================="

mkdir -p cards logs data

echo "Starting gunicorn (1 gevent worker, 600s timeout)..."
exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gevent --timeout 600
