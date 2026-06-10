"""
main.py — Delta X v4 · Scanner + Trade Monitor

New in v4:
  • ACTIVE_TRADES tracking with telegram message_id
  • _monitor_active_signals() — checks TP/SL hits every 5 min
  • TP1 hit → SL moves to entry (breakeven)
  • TP2 hit → SL moves to TP1 (lock profit)
  • TP3 hit / SL hit → trade closed, reply sent
  • Win/loss stats
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import (
    ENTRY_TIMEFRAMES, TREND_TIMEFRAMES,
    INTERVAL_H1, INTERVAL_H4, INTERVAL_DAILY,
    BATCH_SIZE, BATCH_DELAY,
    SYSTEM_NAME, SYSTEM_VERSION,
)
from core.bbma    import calculate_bbma, get_trend_direction
from core.signals import (
    BBMATracker,
    EV_EXTREM, EV_MHV, EV_CSA, EV_ENTRY_ZONE, EV_RISK_BLOCK,
    EV_SIGNAL, EV_CSM, EV_CSM_REENTRY, EV_NEAR_ENTRY,
)
from data.binance_feed  import get_all_symbols, get_klines, get_current_price
from data.pair_filter   import filter_pairs
from database.supabase_client import log_signal, update_signal_status, get_active_signals
from notifications.telegram_bot import (
    send_signal, send_system_message,
    send_tp_hit, send_sl_hit,
)
from notifications import admin_bot
from web.app import app, update_state, get_state
from utils.logger import get_logger

log = get_logger("main")


# ─────────────────────────────────────────────────────────────────────────────
# Runtime state
# ─────────────────────────────────────────────────────────────────────────────

PAIRS:         list[str]                         = []
TRACKERS:      dict[str, dict[str, BBMATracker]] = defaultdict(dict)
TREND_CACHE:   dict[str, dict[str, str]]         = defaultdict(dict)
PRICE_CACHE:   dict[str, float]                  = {}
SIGNALS_TODAY: int = 0
WINS_TODAY:    int = 0
LOSSES_TODAY:  int = 0

COOLDOWN:      dict[str, dict[str, float]] = defaultdict(dict)
COOLDOWN_HOURS = 4

# Active trades being monitored for TP/SL
# key = signal_id, value = trade dict
ACTIVE_TRADES: dict[str, dict] = {}

_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Admin callbacks
# ─────────────────────────────────────────────────────────────────────────────

def _cb_clear_cooldown() -> int:
    with _lock:
        total = sum(len(v) for v in COOLDOWN.values())
        COOLDOWN.clear()
    return total


def _cb_get_trend(pair: str) -> dict:
    with _lock:
        return dict(TREND_CACHE.get(pair, {}))


# ─────────────────────────────────────────────────────────────────────────────
# Scan stats
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanStats:
    tf: str
    pairs_total:     int = 0
    pairs_checked:   int = 0
    errors:          int = 0
    new_extrem:      int = 0
    mhv_confirmed:   int = 0
    csa_confirmed:   int = 0
    in_entry_zone:   int = 0
    risk_blocked:    int = 0
    trend_blocked:   int = 0
    cooldown_skipped:int = 0
    signals_fired:   int = 0

    def print_summary(self):
        w = 52
        log.info("╔" + "═" * w + "╗")
        log.info(f"║  [{self.tf}] SCAN SELESAI".ljust(w+1) + "║")
        log.info("╠" + "═" * w + "╣")
        log.info(f"║  Pairs diperiksa  : {self.pairs_checked}/{self.pairs_total}  (error: {self.errors})".ljust(w+1) + "║")
        log.info("║" + "─" * w + "║")
        log.info(f"║  Extrem baru      : {self.new_extrem:<4}  ← MA keluar dari BB".ljust(w+1) + "║")
        log.info(f"║  MHV disahkan     : {self.mhv_confirmed:<4}  ← momentum mula lemah".ljust(w+1) + "║")
        log.info(f"║  CSA disahkan     : {self.csa_confirmed:<4}  ← arah disahkan".ljust(w+1) + "║")
        log.info(f"║  Dalam zon entry  : {self.in_entry_zone:<4}  ← harga retrace ke MA".ljust(w+1) + "║")
        log.info("║" + "─" * w + "║")
        log.info(f"║  ✗ Trend block    : {self.trend_blocked:<4}  ← lawan trend H4/D1".ljust(w+1) + "║")
        log.info(f"║  ✗ Risk block     : {self.risk_blocked:<4}  ← SL/TP tak valid".ljust(w+1) + "║")
        log.info(f"║  ✗ Cooldown skip  : {self.cooldown_skipped:<4}  ← signal terlalu kerap".ljust(w+1) + "║")
        log.info("║" + "─" * w + "║")
        log.info(f"║  🔔 SIGNAL FIRED  : {self.signals_fired:<4}".ljust(w+1) + "║")
        log.info("╚" + "═" * w + "╝")


# ─────────────────────────────────────────────────────────────────────────────
# Trend filter + Cooldown
# ─────────────────────────────────────────────────────────────────────────────

def _trend_allows(direction: str, trends: dict) -> bool:
    d1 = trends.get("1d", "NEUTRAL")
    h4 = trends.get("4h", "NEUTRAL")
    if direction == "BUY"  and d1 == "BEARISH" and h4 in ("BEARISH", "NEUTRAL"):
        return False
    if direction == "SELL" and d1 == "BULLISH" and h4 in ("BULLISH", "NEUTRAL"):
        return False
    return True


def _in_cooldown(pair: str, direction: str) -> bool:
    with _lock:
        last_ts = COOLDOWN.get(pair, {}).get(direction)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < COOLDOWN_HOURS * 3600


def _set_cooldown(pair: str, direction: str):
    with _lock:
        COOLDOWN.setdefault(pair, {})[direction] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Trend updater
# ─────────────────────────────────────────────────────────────────────────────

def _update_trend_single(pair: str):
    for tf in TREND_TIMEFRAMES:
        try:
            df = get_klines(pair, tf)
            if df is None or len(df) < 55:
                continue
            df = calculate_bbma(df)
            with _lock:
                TREND_CACHE[pair][tf] = get_trend_direction(df)
                PRICE_CACHE[pair]     = float(df.iloc[-1]["close"])
            time.sleep(BATCH_DELAY)
        except Exception as e:
            log.warning(f"Trend {pair}/{tf}: {e}")


def _update_trends_batch(pairs: list[str]):
    for i in range(0, len(pairs), BATCH_SIZE):
        for pair in pairs[i : i + BATCH_SIZE]:
            _update_trend_single(pair)


# ─────────────────────────────────────────────────────────────────────────────
# Restore active trades after restart
# ─────────────────────────────────────────────────────────────────────────────

def _restore_active_trades():
    """Restore ACTIVE_TRADES from Supabase so TP/SL monitoring survives restart."""
    active = get_active_signals()
    if not active:
        log.info("Tiada active trades untuk di-restore")
        return

    restored = 0
    with _lock:
        for s in active:
            sig_id = s.get("signal_id", "")
            try:
                created = s.get("created_at", "")
                ts = (
                    datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    if created else time.time()
                )
                ACTIVE_TRADES[sig_id] = {
                    "pair":        s["pair"],
                    "direction":   s["direction"],
                    "timeframe":   s.get("timeframe", "15m"),
                    "entry":       float(s["entry_price"]),
                    "tp1":         float(s["tp1_price"]),
                    "tp2":         float(s.get("tp2_price") or 0),
                    "tp3":         float(s.get("tp3_price") or 0),
                    "sl":          float(s["sl_price"]),
                    "sl_orig":     float(s["sl_price"]),
                    "msg_id":      s.get("telegram_msg_id") or 0,
                    "ts":          ts,
                    "tp1_hit":     False,
                    "tp2_hit":     False,
                    "tp3_hit":     False,
                    "signal_type": s.get("signal_type", ""),
                }
                restored += 1
            except Exception as e:
                log.warning(f"Restore skip {sig_id}: {e}")

    log.info(f"✅ Restored {restored} active trades dari Supabase")


# ─────────────────────────────────────────────────────────────────────────────
# TP/SL Trade Monitor
# ─────────────────────────────────────────────────────────────────────────────

def _monitor_active_signals():
    """Check all active trades against current prices for TP/SL hits."""
    global WINS_TODAY, LOSSES_TODAY

    if not ACTIVE_TRADES:
        return

    log.info(f"🔍 Monitoring {len(ACTIVE_TRADES)} active trades …")

    closed = []

    for sig_id, trade in list(ACTIVE_TRADES.items()):
        try:
            price = get_current_price(trade["pair"])
            if price is None:
                continue

            with _lock:
                PRICE_CACHE[trade["pair"]] = price

            is_buy = trade["direction"] == "BUY"

            # ── Check TP3 (full close) ────────────────────────────────────────
            tp3_hit = price >= trade["tp3"] if is_buy else price <= trade["tp3"]
            if tp3_hit:
                send_tp_hit(trade, "TP3", price)
                _close_trade(sig_id, "HIT_TP3", price)
                WINS_TODAY += 1
                closed.append(sig_id)
                continue

            # ── Check TP2 ─────────────────────────────────────────────────────
            if not trade.get("tp2_hit"):
                tp2_hit = price >= trade["tp2"] if is_buy else price <= trade["tp2"]
                if tp2_hit:
                    trade["tp2_hit"] = True
                    trade["sl"] = trade["tp1"]   # SL → TP1 (lock profit)
                    send_tp_hit(trade, "TP2", price)
                    log.info(f"  🎯 TP2: {trade['pair']} — SL moved to TP1 ({trade['tp1']})")
                    continue

            # ── Check TP1 ─────────────────────────────────────────────────────
            if not trade.get("tp1_hit"):
                tp1_hit = price >= trade["tp1"] if is_buy else price <= trade["tp1"]
                if tp1_hit:
                    trade["tp1_hit"] = True
                    trade["sl"] = trade["entry"]  # SL → Entry (breakeven)
                    send_tp_hit(trade, "TP1", price)
                    log.info(f"  🎯 TP1: {trade['pair']} — SL moved to breakeven ({trade['entry']})")
                    continue

            # ── Check SL ──────────────────────────────────────────────────────
            sl_hit = price <= trade["sl"] if is_buy else price >= trade["sl"]
            if sl_hit:
                send_sl_hit(trade, price)
                status = "HIT_SL"
                if trade.get("tp1_hit") or trade.get("tp2_hit"):
                    status = "PARTIAL_WIN"
                    WINS_TODAY += 1
                else:
                    LOSSES_TODAY += 1
                _close_trade(sig_id, status, price)
                closed.append(sig_id)

            time.sleep(0.3)

        except Exception as e:
            log.warning(f"Monitor error {trade['pair']}: {e}")

    # Clean up closed trades
    for sig_id in closed:
        with _lock:
            ACTIVE_TRADES.pop(sig_id, None)

    if closed:
        log.info(f"  📊 Closed {len(closed)} trades | Today: W={WINS_TODAY} L={LOSSES_TODAY}")


def _close_trade(sig_id: str, status: str, close_price: float):
    """Update Supabase with final status."""
    trade = ACTIVE_TRADES.get(sig_id, {})
    entry = trade.get("entry", 0)
    is_buy = trade.get("direction") == "BUY"
    pnl = ((close_price - entry) / entry * 100) if is_buy else ((entry - close_price) / entry * 100)

    try:
        update_signal_status(sig_id, status, close_price, pnl)
    except Exception as e:
        log.warning(f"Supabase update failed for {sig_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry scanner
# ─────────────────────────────────────────────────────────────────────────────

def _scan_entry(tf: str):
    if get_state().get("paused", False):
        log.info(f"⏸  [{tf}] DIPAUSED — skip")
        return

    if not PAIRS:
        return

    stats = ScanStats(tf=tf, pairs_total=len(PAIRS))
    update_state(last_scan=time.time())

    for i in range(0, len(PAIRS), BATCH_SIZE):
        batch = PAIRS[i : i + BATCH_SIZE]
        for pair in batch:
            try:
                df = get_klines(pair, tf)
                if df is None or len(df) < 55:
                    continue

                df = calculate_bbma(df)
                with _lock:
                    PRICE_CACHE[pair] = float(df.iloc[-1]["close"])
                    trends = dict(TREND_CACHE.get(pair, {}))
                    tracker = TRACKERS[pair].get(tf)
                    if tracker is None:
                        tracker = BBMATracker(pair, tf)
                        TRACKERS[pair][tf] = tracker

                signal, _ = tracker.update(df, trends)
                ev = tracker.last_event

                if ev == EV_EXTREM:       stats.new_extrem    += 1
                elif ev == EV_MHV:        stats.mhv_confirmed += 1
                elif ev == EV_CSA:        stats.csa_confirmed += 1
                elif ev == EV_RISK_BLOCK: stats.risk_blocked  += 1

                if signal:
                    stats.in_entry_zone += 1
                    if not _trend_allows(signal.direction, trends):
                        stats.trend_blocked += 1
                        continue
                    if _in_cooldown(pair, signal.direction):
                        stats.cooldown_skipped += 1
                        continue

                    _handle_signal(signal)
                    _set_cooldown(pair, signal.direction)
                    stats.signals_fired += 1

                stats.pairs_checked += 1
            except Exception as e:
                stats.errors += 1
                log.warning(f"Scan error {pair}/{tf}: {e}")

        time.sleep(BATCH_DELAY)

    stats.print_summary()

    with _lock:
        # Build active trades with current prices for dashboard
        trades_data = []
        for sig_id, t in ACTIVE_TRADES.items():
            trades_data.append({
                "signal_id": sig_id,
                "pair": t["pair"],
                "timeframe": t["timeframe"],
                "direction": t.get("direction", "BUY"),
                "signal_type": t.get("signal_type", ""),
                "entry": t["entry"],
                "tp1": t["tp1"],
                "tp2": t.get("tp2", 0),
                "tp3": t.get("tp3", 0),
                "sl": t["sl"],
                "sl_orig": t.get("sl_orig", t["sl"]),
                "tp1_hit": t.get("tp1_hit", False),
                "tp2_hit": t.get("tp2_hit", False),
                "current_price": PRICE_CACHE.get(t["pair"], 0),
                "ts": t.get("ts", 0),
            })

        update_state(
            pairs_scanned  = stats.pairs_checked,
            price_cache    = dict(list(PRICE_CACHE.items())[:100]),
            trend_cache    = {p: dict(t) for p, t in list(TREND_CACHE.items())[:100]},
            signals_today  = SIGNALS_TODAY,
            active_trades  = len(ACTIVE_TRADES),
            active_trades_data = trades_data,
            wins_today     = WINS_TODAY,
            losses_today   = LOSSES_TODAY,
        )


def _handle_signal(signal):
    """Send signal to Telegram, log to DB, store for TP/SL monitoring."""
    global SIGNALS_TODAY

    log.info(
        f"🔔 SIGNAL: {signal.pair} {signal.direction} {signal.timeframe} "
        f"[{signal.signal_type}]  Entry={signal.entry_price:.6f}  "
        f"SL={signal.sl_pct:.1f}%  TP1={signal.tp1_pct:.1f}%"
    )

    # Send to Telegram (synchronous to capture msg_id)
    ok, msg_id = send_signal(signal)

    # Log to Supabase
    threading.Thread(target=log_signal, args=(signal, msg_id), daemon=True).start()

    # Generate signal ID
    import hashlib
    ts_str = datetime.fromtimestamp(signal.timestamp, tz=timezone.utc).strftime("%Y%m%d%H%M")
    sig_id = "DX-" + hashlib.md5(
        f"{signal.pair}{signal.timeframe}{signal.direction}{ts_str}".encode()
    ).hexdigest()[:8].upper()

    # Store in ACTIVE_TRADES for monitoring
    with _lock:
        SIGNALS_TODAY += 1
        ACTIVE_TRADES[sig_id] = {
            "pair":      signal.pair,
            "direction": signal.direction,
            "timeframe": signal.timeframe,
            "entry":     signal.entry_price,
            "tp1":       signal.tp1_price,
            "tp2":       signal.tp2_price,
            "tp3":       signal.tp3_price,
            "sl":        signal.sl_price,    # will be adjusted on TP hits
            "sl_orig":   signal.sl_price,    # original SL (never changes)
            "msg_id":    msg_id,
            "ts":        signal.timestamp,
            "tp1_hit":   False,
            "tp2_hit":   False,
            "tp3_hit":   False,
            "signal_type": signal.signal_type,
        }

        active = get_state().get("active_signals", [])
        active.insert(0, {
            "pair": signal.pair, "timeframe": signal.timeframe,
            "direction": signal.direction, "entry": signal.entry_price,
            "tp1": signal.tp1_price, "sl": signal.sl_price,
            "tp1_pct": signal.tp1_pct, "sl_pct": signal.sl_pct,
            "signal_type": signal.signal_type, "ts": signal.timestamp,
        })
        update_state(active_signals=active[:20])


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler jobs
# ─────────────────────────────────────────────────────────────────────────────

def job_scan_h1():
    log.info("─" * 60)
    log.info("⏱  H1 scan …")
    _scan_entry("1h")

def job_scan_h4():
    log.info("─" * 60)
    log.info("⏱  H4 scan …")
    _scan_entry("4h")

def job_update_trends():
    log.info("📊 Trend refresh …")
    _update_trends_batch(PAIRS)

def job_monitor():
    _monitor_active_signals()

def job_daily_reset():
    global SIGNALS_TODAY, WINS_TODAY, LOSSES_TODAY
    with _lock:
        SIGNALS_TODAY = 0
        WINS_TODAY    = 0
        LOSSES_TODAY  = 0
    log.info("🔄 Daily counters reset")


# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────

def initialise_pairs():
    global PAIRS
    log.info("Fetching Binance symbols …")
    PAIRS = filter_pairs(get_all_symbols())
    log.info(f"Monitoring {len(PAIRS)} pairs")

    with _lock:
        for pair in PAIRS:
            for tf in ENTRY_TIMEFRAMES:
                TRACKERS[pair][tf] = BBMATracker(pair, tf)

    update_state(
        pairs_total=len(PAIRS),
        started_at=datetime.now(timezone.utc).isoformat(),
        status="running", paused=False,
    )

    log.info("Warming trend cache …")
    _update_trends_batch(PAIRS)
    log.info("Warm-up ✅")

    send_system_message(
        f"🚀 *{SYSTEM_NAME} v{SYSTEM_VERSION} Online*\n\n"
        f"Pairs  : `{len(PAIRS)}`\n"
        f"Entry  : H1 + H4\n"
        f"Trend  : H4 / D1\n"
        f"Monitor: TP/SL setiap 5 min\n\n"
        f"/help untuk command"
    )


def start_scheduler():
    s = BackgroundScheduler(timezone="UTC")
    s.add_job(job_scan_h1,        IntervalTrigger(seconds=INTERVAL_H1),  id="h1_entry", misfire_grace_time=120)
    s.add_job(job_scan_h4,        IntervalTrigger(seconds=INTERVAL_H4),  id="h4_entry",
              next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
              misfire_grace_time=300)
    s.add_job(job_update_trends,  IntervalTrigger(seconds=INTERVAL_H4),  id="trends",   misfire_grace_time=300)
    s.add_job(job_monitor,        IntervalTrigger(minutes=5),            id="monitor",  misfire_grace_time=60)
    s.add_job(job_daily_reset,    IntervalTrigger(hours=24),             id="daily")
    s.start()
    log.info("Scheduler ✅ (scan H1/H4 + monitor setiap 5m)")
    return s


def _boot():
    try:
        time.sleep(2)
        admin_bot.register("clear_cooldown", _cb_clear_cooldown)
        admin_bot.register("get_trend",      _cb_get_trend)
        admin_bot.start()
        initialise_pairs()
        _restore_active_trades()
        start_scheduler()
        threading.Thread(target=job_scan_h1, daemon=True).start()
        time.sleep(120)   # 2 min gap before H4
        threading.Thread(target=job_scan_h4, daemon=True).start()
    except Exception as e:
        log.error(f"Boot failed: {e}")
        update_state(status="error")
        send_system_message(f"❌ Boot error:\n`{e}`")


threading.Thread(target=_boot, daemon=True, name="boot").start()

if __name__ == "__main__":
    log.info(f"Starting {SYSTEM_NAME} v{SYSTEM_VERSION}")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
