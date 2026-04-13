#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "MONTHLY RETRAIN — $(date)"
python3 01_data_pipeline.py
python3 02_features_and_train.py
python3 05_bet_logger.py resolve
python3 05_bet_logger.py export-lstm
python3 05_bet_logger.py report
echo "RETRAIN COMPLETE"
