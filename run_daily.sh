#!/bin/bash
set -e
cd "$(dirname "$0")"
echo ""
echo "TENNIS SIGNAL SYSTEM — $(date)"
echo ""
echo "--- Generate Betting Card ---"
python3 04_betting_card.py --min-edge 0.03 --min-volume 500 --open
if [ "$1" != "--quick" ]; then
echo ""
echo "--- Resolve Outcomes ---"
python3 05_bet_logger.py resolve
echo ""
echo "--- Performance Report ---"
python3 05_bet_logger.py report
fi
echo ""
echo "DAILY RUN COMPLETE"
