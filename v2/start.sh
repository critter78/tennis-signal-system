#!/bin/bash
# Startup for tennisv2.critterlabs.io web service.
# Brings up gunicorn fast, then runs the first pick generation in the background.
set -e

echo "=========================================="
echo "Tennis V2 Signal — Startup"
echo "=========================================="

cd "$(dirname "$0")"

# If Render persistent disk is attached, symlink data/ and logs/ to it
# so fetch_tml.py / build_v2_dataset.py / generate_v2_picks.py persist across deploys.
if [ -d /data ]; then
  echo "[persist] /data exists — wiring persistent storage"
  mkdir -p /data/v2_logs /data/v2_data
  # Seed parquet from repo on very first boot
  if [ ! -f /data/v2_data/matches_combined_v2.parquet ] && [ -f data/matches_combined_v2.parquet ]; then
    cp data/matches_combined_v2.parquet /data/v2_data/
    echo "[persist] seeded matches_combined_v2.parquet"
  fi
  # Seed TML raw dir if empty
  mkdir -p /data/v2_data/raw/tml
  # Replace ephemeral dirs with symlinks to persistent ones
  rm -rf logs data
  ln -sfn /data/v2_logs logs
  ln -sfn /data/v2_data data
fi

mkdir -p logs data models

# Open port fast so Render doesn't time out
echo "[1/2] starting gunicorn on :$PORT"
gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 300 &
GUN=$!
sleep 3

# First data refresh + pick gen runs in background
{
  echo "[2/2] first data refresh"
  python3 fetch_tml.py --rebuild 2>&1 | tail -20 || echo "  fetch/rebuild failed"
  echo "[2/2] first pick generation"
  python3 generate_v2_picks.py 2>&1 | tail -20 || echo "  pick gen failed"
  echo "[2/2] first sell-monitor pass"
  python3 auto_sell_monitor.py --once 2>&1 | tail -10 || true
} &

wait $GUN
