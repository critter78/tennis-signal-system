# Tennis Betting Signal System

An intelligent signal generation and betting analytics platform for tennis markets. Combines data pipeline, machine learning, and real-time betting signals with a responsive dashboard.

## Features

- Real-time tennis market data ingestion from Polymarket
- Odds scraping and historical data management
- ML-powered signal generation with LSTM support
- Interactive P&L dashboard with live betting tracking
- RESTful API for programmatic access
- Daily automated card generation
- User bet logging and performance analytics

## Architecture

```
01_data_pipeline.py
    ↓ (Fetch & preprocess tennis data from Polymarket)
02_features_and_train.py
    ↓ (Feature engineering, ML model training)
03_signal_generator.py
    ↓ (Generate betting signals)
04_betting_card.py
    ↓ (Create visual betting card HTML)
05_bet_logger.py
    ↓ (Log user bets)
07_dashboard.py
    ↓ (Inject data into dashboard template)
server.py (Flask web server)
    ↓
Dashboard UI + API endpoints
```

## Quick Start (Local)

### Prerequisites
- Python 3.11+
- pip

### Installation

```bash
pip install -r requirements.txt
```

### Run Locally

```bash
python3 server.py
```

Server will start on http://localhost:5000

- Dashboard: http://localhost:5000/
- Latest card: http://localhost:5000/card
- Picks API: http://localhost:5000/api/picks
- Status: http://localhost:5000/api/status

## Deploy to Render

### Prerequisites
- Render account (https://render.com)
- GitHub repository with this code

### Steps

1. **Push to GitHub**
   ```bash
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

2. **Create Render Service**
   - Go to https://dashboard.render.com
   - Click "New +" → "Blueprint"
   - Select your GitHub repository
   - Authorize Render to access GitHub
   - Render will automatically read `render.yaml` and deploy

3. **Deployment Details**
   - Web service runs Flask with Gunicorn
   - Health checks via `/api/status` every 30 seconds
   - Automatic daily card generation at 8:00 AM UTC
   - Cron job runs `python3 04_betting_card.py`

4. **Monitor Deployment**
   - Dashboard: https://your-app-name.onrender.com
   - Logs: Render dashboard → Logs tab
   - Status: https://your-app-name.onrender.com/api/status

## API Endpoints

### `GET /`
Serves the dashboard with live picks data injected.

### `GET /card`
Returns the latest generated betting card (HTML).

### `GET /api/picks`
Returns all picks as JSON array.
```json
{
  "picks": [...],
  "count": 42
}
```

### `GET /api/status`
System health check and metadata.
```json
{
  "status": "healthy",
  "last_card_generated": "2026-04-13T22:12:00",
  "picks_count": 42,
  "model_available": true,
  "timestamp": "2026-04-13T22:30:45"
}
```

### `GET /api/bets`
Retrieve user bets.

### `POST /api/bets`
Save user bets.
```bash
curl -X POST http://localhost:5000/api/bets \
  -H "Content-Type: application/json" \
  -d '{"bet_1": {"amount": 100, "odds": 1.5}}'
```

### `POST /generate`
Trigger a new betting card generation (long-running).

## File Structure

```
.
├── 01_data_pipeline.py          # Fetch market data
├── 02_features_and_train.py     # Feature engineering & ML training
├── 03_signal_generator.py       # Generate betting signals
├── 04_betting_card.py           # Create visual betting card
├── 05_bet_logger.py             # Log user bets
├── 07_dashboard.py              # Dashboard generator
├── server.py                    # Flask web server
├── dashboard.html               # Dashboard template (injected)
├── requirements.txt             # Python dependencies
├── Procfile                     # Render process definition
├── render.yaml                  # Render deployment blueprint
├── build.sh                     # Build script
├── .gitignore                   # Git ignore patterns
├── README.md                    # This file
├── data/
│   ├── raw/                     # Raw downloaded data
│   ├── polymarket/              # Polymarket API responses
│   ├── odds/                    # Scraped odds
│   └── features.parquet         # Engineered features
├── models/
│   └── latest_model.json        # Trained ML model
├── logs/
│   ├── picks.jsonl              # All generated picks (JSONL)
│   └── my_bets.json             # User bets (JSON)
└── cards/
    ├── betting_card_*.html      # Generated cards (timestamped)
    └── dashboard.html           # Latest injected dashboard
```

## Development

### Running the Data Pipeline

```bash
# Fetch and preprocess data
python3 01_data_pipeline.py

# Train model
python3 02_features_and_train.py

# Generate signals
python3 03_signal_generator.py

# Create betting card
python3 04_betting_card.py
```

### Testing the Server

```bash
# Local development
FLASK_ENV=development python3 server.py

# With gunicorn (production-like)
gunicorn server:app --bind 0.0.0.0:5000 --reload

# Check health
curl http://localhost:5000/api/status
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server port (set by Render) |
| `FLASK_ENV` | `development` | Flask environment mode |

## Troubleshooting

### Dashboard not loading
- Check that `logs/picks.jsonl` exists
- Verify `dashboard.html` template is in root directory
- Check server logs: `curl http://localhost:5000/api/status`

### Betting card not generating
- Ensure all data files exist: `data/raw/`, `data/polymarket/`, etc.
- Check model exists: `models/latest_model.json`
- Run manually: `python3 04_betting_card.py`
- Check logs in Render dashboard

### Render deployment stuck
- Check build logs in Render dashboard
- Ensure `build.sh` is executable: `chmod +x build.sh`
- Verify Python 3.11 is selected in render.yaml

## Performance & Scaling

- **Starter plan** recommended for initial deployment
- Cron jobs run on Render's free tier
- To scale: increase worker count in Procfile
- Production: use PostgreSQL for bet persistence (update `my_bets.json` to use DB)

## License

Proprietary — Tennis Betting System

## Support

For issues or questions, check the logs in Render dashboard or run locally with `FLASK_ENV=development`.
