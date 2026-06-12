"""
web/app.py — Flask dashboard for Delta X
Serves the real-time monitoring UI and JSON API endpoints.

v2 changes:
  • /api/signals — added error handling + timeout protection
  • /api/journal — NEW: full trade journal with P&L, duration, status
  • /api/stats   — NEW: win rate, total P&L, streaks, best pairs
  • All endpoints wrapped in try/except so dashboard never hangs
"""
from flask import Flask, jsonify, render_template
from flask_cors import CORS
import time as _time

from config.settings import SECRET_KEY, SYSTEM_NAME, SYSTEM_VERSION
from database.supabase_client import get_recent_signals
from utils.logger import get_logger

log = get_logger("web")

app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY
CORS(app)

# ── Supabase query cache (30s TTL) ───────────────────────────────────────────
# Dashboard polls every 30s; without cache, each viewer = 2 Supabase hits per
# poll. Cache collapses all viewers into ≤2 queries per 30s window.
_cache: dict = {}
CACHE_TTL = 30

def _cached(key: str, fn, *args):
    now = _time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < CACHE_TTL:
        return hit[1]
    val = fn(*args)
    _cache[key] = (now, val)
    return val

# Shared in-memory state — written by the scanner, read by the API
_state: dict = {
    "started_at":     None,
    "pairs_total":    0,
    "pairs_scanned":  0,
    "signals_today":  0,
    "active_signals": [],
    "active_trades":  0,
    "wins_today":     0,
    "losses_today":   0,
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
        "active_count":   _state.get("active_trades", len(_state["active_signals"])),
        "wins_today":     _state.get("wins_today", 0),
        "losses_today":   _state.get("losses_today", 0),
    })


@app.route("/api/signals")
def api_signals():
    """Recent signals from Supabase — with error protection."""
    try:
        signals = _cached("sig50", get_recent_signals, 50)
        return jsonify(signals)
    except Exception as e:
        log.error(f"/api/signals error: {e}")
        return jsonify([]), 200


@app.route("/api/active")
def api_active():
    """Live active signals held in memory."""
    return jsonify(_state["active_signals"])


@app.route("/api/trades")
def api_trades():
    """Active trades with current prices and TP hit status — for dashboard cards."""
    return jsonify(_state.get("active_trades_data", []))


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


# ── Journal & Stats ───────────────────────────────────────────────────────────

@app.route("/api/journal")
def api_journal():
    """
    Full trade journal — all closed + active signals with P&L.
    Data comes from Supabase signals table.
    """
    try:
        raw = _cached("sig200", get_recent_signals, 200)
        journal = []
        for s in raw:
            entry   = float(s.get("entry_price") or 0)
            close_p = float(s.get("close_price") or 0)
            status  = s.get("status", "ACTIVE")
            pnl     = float(s.get("pnl_pct") or 0)

            # Calculate duration if closed
            duration = ""
            created  = s.get("created_at", "")
            closed   = s.get("closed_at", "")
            if created and closed:
                try:
                    from datetime import datetime
                    t1 = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    mins = int((t2 - t1).total_seconds() / 60)
                    if mins >= 60:
                        duration = f"{mins // 60}j {mins % 60}m"
                    else:
                        duration = f"{mins}m"
                except Exception:
                    duration = ""

            journal.append({
                "signal_id":   s.get("signal_id", ""),
                "pair":        s.get("pair", ""),
                "timeframe":   s.get("timeframe", ""),
                "direction":   s.get("direction", ""),
                "signal_type": s.get("signal_type", ""),
                "entry_price": entry,
                "sl_price":    float(s.get("sl_price") or 0),
                "tp1_price":   float(s.get("tp1_price") or 0),
                "tp2_price":   float(s.get("tp2_price") or 0),
                "tp3_price":   float(s.get("tp3_price") or 0),
                "close_price": close_p,
                "pnl_pct":     pnl,
                "status":      status,
                "duration":    duration,
                "trend_d1":    s.get("trend_daily", ""),
                "trend_h4":    s.get("trend_h4", ""),
                "trend_h1":    s.get("trend_h1", ""),
                "created_at":  created,
                "closed_at":   closed,
            })

        return jsonify(journal)
    except Exception as e:
        log.error(f"/api/journal error: {e}")
        return jsonify({"error": str(e), "journal": []}), 200


@app.route("/api/stats")
def api_stats():
    """
    Trading statistics summary.
    """
    try:
        raw = _cached("sig500", get_recent_signals, 500)

        closed  = [s for s in raw if s.get("status") != "ACTIVE"]
        active  = [s for s in raw if s.get("status") == "ACTIVE"]

        wins    = [s for s in closed if float(s.get("pnl_pct") or 0) > 0]
        losses  = [s for s in closed if float(s.get("pnl_pct") or 0) < 0]
        breakeven = [s for s in closed if float(s.get("pnl_pct") or 0) == 0]

        total_closed = len(closed)
        win_rate = (len(wins) / total_closed * 100) if total_closed > 0 else 0

        total_pnl = sum(float(s.get("pnl_pct") or 0) for s in closed)
        avg_win   = (sum(float(s.get("pnl_pct") or 0) for s in wins) / len(wins)) if wins else 0
        avg_loss  = (sum(float(s.get("pnl_pct") or 0) for s in losses) / len(losses)) if losses else 0
        best_pnl  = max((float(s.get("pnl_pct") or 0) for s in closed), default=0)
        worst_pnl = min((float(s.get("pnl_pct") or 0) for s in closed), default=0)

        # Best pairs by total P&L
        pair_pnl: dict[str, float] = {}
        pair_count: dict[str, int] = {}
        for s in closed:
            p = s.get("pair", "")
            pnl = float(s.get("pnl_pct") or 0)
            pair_pnl[p] = pair_pnl.get(p, 0) + pnl
            pair_count[p] = pair_count.get(p, 0) + 1

        best_pairs = sorted(pair_pnl.items(), key=lambda x: x[1], reverse=True)[:10]

        # Signal type breakdown
        extrem_mhv = [s for s in closed if s.get("signal_type") == "EXTREM_MHV"]
        csm_re     = [s for s in closed if s.get("signal_type") == "CSM_REENTRY"]

        def _wr(lst):
            if not lst:
                return 0
            w = sum(1 for s in lst if float(s.get("pnl_pct") or 0) > 0)
            return round(w / len(lst) * 100, 1)

        return jsonify({
            "total_signals": len(raw),
            "total_closed":  total_closed,
            "active":        len(active),
            "wins":          len(wins),
            "losses":        len(losses),
            "breakeven":     len(breakeven),
            "win_rate":      round(win_rate, 1),
            "total_pnl":     round(total_pnl, 2),
            "avg_win":       round(avg_win, 2),
            "avg_loss":      round(avg_loss, 2),
            "best_trade":    round(best_pnl, 2),
            "worst_trade":   round(worst_pnl, 2),
            "by_type": {
                "EXTREM_MHV":  {"count": len(extrem_mhv), "win_rate": _wr(extrem_mhv)},
                "CSM_REENTRY": {"count": len(csm_re),     "win_rate": _wr(csm_re)},
            },
            "best_pairs": [
                {"pair": p, "pnl": round(pnl, 2), "trades": pair_count.get(p, 0)}
                for p, pnl in best_pairs
            ],
            "today": {
                "signals": _state.get("signals_today", 0),
                "wins":    _state.get("wins_today", 0),
                "losses":  _state.get("losses_today", 0),
            },
        })
    except Exception as e:
        log.error(f"/api/stats error: {e}")
        return jsonify({"error": str(e)}), 200
