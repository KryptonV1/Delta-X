"""
web/app.py — Flask dashboard for Delta X
Serves the real-time monitoring UI and JSON API endpoints.
"""
from flask import Flask, jsonify, render_template
from flask_cors import CORS

from config.settings import SECRET_KEY, SYSTEM_NAME, SYSTEM_VERSION
from database.supabase_client import get_recent_signals
from utils.logger import get_logger

log = get_logger("web")

app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY
CORS(app)

# Shared in-memory state — written by the scanner, read by the API
_state: dict = {
    "started_at":     None,
    "pairs_total":    0,
    "pairs_scanned":  0,
    "signals_today":  0,
    "active_signals": [],
    "last_scan":      None,
    "status":         "starting",
    "price_cache":    {},
    "trend_cache":    {},
}


def get_state() -> dict:
    return _state


def update_state(**kwargs):
    _state.update(kwargs)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", system_name=SYSTEM_NAME, version=SYSTEM_VERSION)


@app.route("/ping")
def ping():
    """UptimeRobot keep-alive endpoint."""
    return "OK", 200


@app.route("/api/status")
def api_status():
    return jsonify({
        "system":         SYSTEM_NAME,
        "version":        SYSTEM_VERSION,
        "status":         _state["status"],
        "started_at":     _state["started_at"],
        "last_scan":      _state["last_scan"],
        "pairs_total":    _state["pairs_total"],
        "pairs_scanned":  _state["pairs_scanned"],
        "signals_today":  _state["signals_today"],
        "active_count":   len(_state["active_signals"]),
    })


@app.route("/api/signals")
def api_signals():
    """Recent signals from Supabase."""
    signals = get_recent_signals(50)
    return jsonify(signals)


@app.route("/api/active")
def api_active():
    """Live active signals held in memory."""
    return jsonify(_state["active_signals"])


@app.route("/api/prices")
def api_prices():
    """Latest prices and trend from in-memory cache."""
    combined = []
    for pair, price in list(_state["price_cache"].items())[:50]:
        trends = _state["trend_cache"].get(pair, {})
        combined.append({
            "pair":    pair,
            "price":   price,
            "trend_h1":    trends.get("1h",  "–"),
            "trend_h4":    trends.get("4h",  "–"),
            "trend_daily": trends.get("1d",  "–"),
        })
    return jsonify(combined)
