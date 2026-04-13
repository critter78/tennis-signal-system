#!/usr/bin/env python3
"""
Flask web server for Tennis Betting Signal System
Serves dashboard, betting cards, API endpoints, and health checks.
Works with gunicorn for production deployment on Render.
"""

import os
import json
import glob
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_file, render_template_string
import subprocess
import logging

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Base paths
BASE_DIR = Path(__file__).parent.resolve()
CARDS_DIR = BASE_DIR / "cards"
LOGS_DIR = BASE_DIR / "logs"
MODELS_DIR = BASE_DIR / "models"
PICKS_FILE = LOGS_DIR / "picks.jsonl"
BETS_FILE = LOGS_DIR / "my_bets.json"
DASHBOARD_TEMPLATE = BASE_DIR / "dashboard.html"

# Create necessary directories
CARDS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)


# Helper functions
def enable_cors(f):
    """Decorator to enable CORS headers on a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = f(*args, **kwargs)
        if isinstance(response, dict):
            from flask import make_response
            response = make_response(jsonify(response))
        elif isinstance(response, str):
            from flask import make_response
            response = make_response(response)

        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response
    return decorated_function


def get_latest_betting_card():
    """Find the most recent betting card HTML file."""
    card_files = sorted(glob.glob(str(CARDS_DIR / "betting_card_*.html")))
    if card_files:
        return card_files[-1]
    return None


def load_picks_jsonl():
    """Load all picks from picks.jsonl as a list of dicts."""
    picks = []
    try:
        if PICKS_FILE.exists():
            with open(PICKS_FILE, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            picks.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    except Exception as e:
        logger.error(f"Error loading picks: {e}")
    return picks


def load_bets():
    """Load user bets from my_bets.json."""
    try:
        if BETS_FILE.exists():
            with open(BETS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading bets: {e}")
    return {}


def save_bets(bets_data):
    """Save user bets to my_bets.json."""
    try:
        BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BETS_FILE, 'w') as f:
            json.dump(bets_data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving bets: {e}")
        return False


def get_system_status():
    """Get current system health status."""
    try:
        last_card = get_latest_betting_card()
        last_card_time = None
        if last_card:
            mtime = os.path.getmtime(last_card)
            last_card_time = datetime.fromtimestamp(mtime).isoformat()

        picks_count = len(load_picks_jsonl())
        model_exists = (MODELS_DIR / "latest_model.json").exists()

        return {
            "status": "healthy",
            "last_card_generated": last_card_time,
            "picks_count": picks_count,
            "model_available": model_exists,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


# Routes
@app.route("/", methods=["GET"])
def dashboard():
    """
    Serve the dashboard by injecting picks data into dashboard.html template.
    Falls back to latest generated dashboard.html if template processing fails.
    """
    try:
        # Try to inject fresh data into template
        if DASHBOARD_TEMPLATE.exists():
            picks_data = load_picks_jsonl()
            with open(DASHBOARD_TEMPLATE, 'r') as f:
                content = f.read()

            # Inject picks data
            picks_json = json.dumps(picks_data, indent=2)
            content = content.replace("window.PICKS_DATA = []", f"window.PICKS_DATA = {picks_json}")

            from flask import make_response
            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
    except Exception as e:
        logger.warning(f"Error generating dashboard: {e}. Falling back to latest card.")

    # Fallback: serve the latest generated dashboard
    latest_card = get_latest_betting_card()
    if latest_card:
        try:
            with open(latest_card, 'r') as f:
                content = f.read()
            from flask import make_response
            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
        except Exception as e:
            logger.error(f"Error serving latest card: {e}")

    return jsonify({"error": "Dashboard not available"}), 503


@app.route("/card", methods=["GET"])
@enable_cors
def latest_card():
    """Serve the latest betting card."""
    latest = get_latest_betting_card()
    if latest:
        try:
            with open(latest, 'r') as f:
                content = f.read()
            from flask import make_response
            response = make_response(content)
            response.headers["Content-Type"] = "text/html"
            return response
        except Exception as e:
            logger.error(f"Error serving card: {e}")
            return jsonify({"error": "Could not read card"}), 500

    return jsonify({"error": "No betting card available"}), 404


@app.route("/api/picks", methods=["GET"])
@enable_cors
def api_picks():
    """Return all picks as JSON array."""
    try:
        picks = load_picks_jsonl()
        return jsonify({"picks": picks, "count": len(picks)})
    except Exception as e:
        logger.error(f"Error in /api/picks: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bets", methods=["GET", "POST"])
@enable_cors
def api_bets():
    """Get or save user bets."""
    try:
        if request.method == "GET":
            bets = load_bets()
            return jsonify({"bets": bets})

        elif request.method == "POST":
            data = request.get_json() or {}
            bets = load_bets()

            # Merge new bets
            bets.update(data)

            if save_bets(bets):
                return jsonify({"status": "success", "bets": bets}), 201
            else:
                return jsonify({"status": "error", "message": "Failed to save"}), 500

    except Exception as e:
        logger.error(f"Error in /api/bets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
@enable_cors
def api_status():
    """Return system health status."""
    try:
        status = get_system_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error in /api/status: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/generate", methods=["POST"])
@enable_cors
def generate_card():
    """Trigger a new betting card generation by running 04_betting_card.py."""
    try:
        script_path = BASE_DIR / "04_betting_card.py"
        if not script_path.exists():
            return jsonify({"error": "04_betting_card.py not found"}), 404

        logger.info("Triggering betting card generation...")
        result = subprocess.run(
            ["python3", str(script_path)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode == 0:
            latest_card = get_latest_betting_card()
            return jsonify({
                "status": "success",
                "message": "Card generation completed",
                "latest_card": latest_card
            }), 200
        else:
            logger.error(f"Card generation failed: {result.stderr}")
            return jsonify({
                "status": "error",
                "message": "Card generation failed",
                "error": result.stderr[:500]
            }), 500

    except subprocess.TimeoutExpired:
        logger.error("Card generation timed out")
        return jsonify({"error": "Generation timed out"}), 504
    except Exception as e:
        logger.error(f"Error in /generate: {e}")
        return jsonify({"error": str(e)}), 500


# Health check endpoint (required by Render)
@app.route("/health", methods=["GET"])
def health():
    """Simple health check for Render."""
    return jsonify({"status": "ok"}), 200


# Error handlers
@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    logger.error(f"Internal error: {error}")
    return jsonify({"error": "Internal server error"}), 500


# CORS for all routes
@app.after_request
def after_request(response):
    """Add CORS headers to all responses."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


if __name__ == "__main__":
    # Get port from environment or use 5000 for local development
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"

    logger.info(f"Starting Tennis Betting Signal Server on port {port}")
    logger.info(f"Base directory: {BASE_DIR}")
    logger.info(f"Debug mode: {debug}")

    app.run(host="0.0.0.0", port=port, debug=debug)
