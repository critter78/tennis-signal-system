#!/bin/bash
set -e

echo "=========================================="
echo "Tennis Betting System — Build Script"
echo "=========================================="

# Install dependencies
echo "[1/4] Installing Python dependencies..."
pip install -r requirements.txt

# Create necessary directories
echo "[2/4] Creating directory structure..."
mkdir -p data/raw data/polymarket data/odds models cards logs

# Check for model
echo "[3/4] Checking for trained model..."
if [ ! -f models/latest_model.json ]; then
    echo "No model found — running training pipeline..."
    echo "[3a/4] Running data pipeline (01_data_pipeline.py)..."
    python3 01_data_pipeline.py || echo "Warning: Data pipeline failed, but continuing..."

    echo "[3b/4] Running feature engineering and training (02_features_and_train.py)..."
    python3 02_features_and_train.py || echo "Warning: Training failed, but continuing..."
else
    echo "Model found: models/latest_model.json"
fi

echo "[4/4] Build complete!"
echo "=========================================="
echo "Server ready to start. Use:"
echo "  python3 server.py              (local development)"
echo "  gunicorn server:app            (production)"
echo "=========================================="
