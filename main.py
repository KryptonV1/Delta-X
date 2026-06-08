"""
main.py — Delta X v3 · Scanner Engine

Changes vs v2:
  • Admin bot integrated (command handler in background thread)
  • System messages routed to ADMIN only (klien tak nampak)
  • Pause/resume via /pause /resume admin commands
  • Callbacks registered for /clearcd and /trend
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import (
    ENTRY_TIMEFRAMES, TREND_TIMEFRAMES,
    INTERVAL_M15, INTERVAL_M30, INTERVAL_H1,
    BATCH_SIZE, BATCH_DELAY,
    SYSTEM_NAME, SYSTEM_VERSION,
)
from core.bbma    import calculate_bbma, get_trend_direction
from core.signals import (
    BBMATracker,
    EV_EXTREM, EV_MHV, EV_ENTRY_ZONE, EV_RISK_BLOCK,
    EV_SIGNAL, EV_CSM, EV_CSM_REENTRY, EV_NEAR_ENTRY,
)
from data.binance_feed  import get_all_symbols, get_klines
from data.pair_filter   import filter_pairs
from database.supabase_client   import log_signal
from notifications.telegram_bot import send_signal, send_near_entry, send_system_message
from notifications              import admin_bot
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
SIGNALS_TODAY: int                               = 0
COOLDOWN:      dict[str, dict[str, float]]       = defaultdict(dict)
COOLDOWN_HOURS = 4
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Admin callbacks (registered once at boot, used by admin_bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def _cb_clear_cooldown() -> int:
    with _lock:
        total = sum(len(v) for v in COOLDOWN.values())
        COOLDOWN.clear()
    log.info(f"Cooldown cleared by admin ({total} entries)")
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
    in_entry_zone:   int = 0
    risk_blocked:    int = 0
    trend_blocked:   int = 0
    cooldown_skipped:int = 0
    near_entry_sent: int = 0
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
        log.info(f"║  Dalam zon entry  : {self.in_entry_zone:<4}  ← harga retrace ke MA".ljust(w+1) + "║")
        log.info("║" + "─" * w + "║")
        log.info(f"║  ✗ Trend block    : {self.trend_blocked:<4}  ← lawan trend H4/D1".ljust(w+1) + "║")
        log.info(f"║  ✗ Risk block     : {self.risk_blocked:<4}  ← SL>20% atau TP1<20%".ljust(w+1) + "║")
        log.info(f"║  ✗ Cooldown skip  : {self.cooldown_skipped:<4}  ← signal terlalu kerap".ljust(w+1) + "║")
        log.info("║" + "─" * w + "║")
        log.info(f"║  ⚠  Hampir entry  : {self.near_entry_sent:<4}  ← warning dihantar".ljust(w+1) + "║")
        log.info(f"║  🔔 SIGNAL FIRED  : {self.signals_fired:<4}".ljust(w+1) + "║")
        log.info("╚" + "═" * w + "╝")


# ─────────────────────────────────────────────────────────────────────────────
# Trend filter
# ─────────────────────────────────────────────────────────────────────────────

def _trend_allows(direction: str, trends: dict) -> bool:
    d1 = trends.get("1d", "NEUTRAL")
    h4 = trends.get("4h", "NEUTRAL")
    if direction == "BUY":
        if d1 == "BEARISH" and h4 in ("BEARISH", "NEUTRAL"):
            return False
    else:
        if d1 == "BULLISH" and h4 in ("BULLISH", "NEUTRAL"):
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────────────────────

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
            trend = get_trend_direction(df)
            with _lock:
                TREND_CACHE[pair][tf] = trend
                PRICE_CACHE[pair]     = float(df.iloc[-1]["close"])
            time.sleep(BATCH_DELAY)
        except Exception as e:
            log.warning(f"Trend {pair}/{tf}: {e}")


def _update_trends_batch(pairs: list[str]):
    for i in range(0, len(pairs), BATCH_SIZE):
        for pair in pairs[i : i + BATCH_SIZE]:
            _update_trend_single(pair)
        log.debug(f"Trend cache: {min(i+BATCH_SIZE, len(pairs))}/{len(pairs)}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry scanner
# ─────────────────────────────────────────────────────────────────────────────

def _scan_entry(tf: str):
    # ── Pause check ─────────────────────────────────────────────────────────
    if get_state().get("paused", False):
        log.info(f"⏸  [{tf}] Scan DIPAUSED — skip")
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

                signal, near_warn = tracker.update(df, trends)
                ev = tracker.last_event

                if ev == EV_EXTREM:       stats.new_extrem    += 1
                elif ev == EV_MHV:        stats.mhv_confirmed += 1
                elif ev == EV_RISK_BLOCK: stats.risk_blocked  += 1
                elif ev == EV_NEAR_ENTRY: stats.near_entry_sent += 1

                # Near-entry → admin only
                if near_warn:
                    stats.near_entry_sent += 1
                    threading.Thread(
                        target=send_near_entry, args=(near_warn,), daemon=True
                    ).start()

                if signal:
                    stats.in_entry_zone += 1

                    if not _trend_allows(signal.direction, trends):
                        stats.trend_blocked += 1
                        log.debug(
                            f"  ✗ TREND: {pair} {signal.direction} "
                            f"D1={trends.get('1d','?')} H4={trends.get('4h','?')}"
                        )
                        continue

                    if _in_cooldown(pair, signal.direction):
                        stats.cooldown_skipped += 1
                        log.debug(f"  ✗ COOLDOWN: {pair} {signal.direction}")
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
        update_state(
            pairs_scanned = stats.pairs_checked,
            price_cache   = dict(list(PRICE_CACHE.items())[:100]),
            trend_cache   = {p: dict(t) for p, t in list(TREND_CACHE.items())[:100]},
            signals_today = SIGNALS_TODAY,
        )


def _handle_signal(signal):
    global SIGNALS_TODAY
    log.info(
        f"🔔 SIGNAL: {signal.pair} {signal.direction} {signal.timeframe} "
        f"[{signal.signal_type}]  Entry={signal.entry_price:.6f}  "
        f"SL={signal.sl_pct:.1f}%  TP1={signal.tp1_pct:.1f}%"
    )
    threading.Thread(target=send_signal,  args=(signal,), daemon=True).start()
    threading.Thread(target=log_signal,   args=(signal,), daemon=True).start()

    with _lock:
        SIGNALS_TODAY += 1
        active = get_state().get("active_signals", [])
        active.insert(0, {
            "pair":        signal.pair,
            "timeframe":   signal.timeframe,
            "direction":   signal.direction,
            "entry":       signal.entry_price,
            "tp1":         signal.tp1_price,
            "sl":          signal.sl_price,
            "tp1_pct":     signal.tp1_pct,
            "sl_pct":      signal.sl_pct,
            "signal_type": signal.signal_type,
            "ts":          signal.timestamp,
        })
        update_state(active_signals=active[:20])


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

def job_scan_m15():
    log.info("─" * 60)
    log.info("⏱  M15 scan dimulakan …")
    _scan_entry("15m")

def job_scan_m30():
    log.info("─" * 60)
    log.info("⏱  M30 scan dimulakan …")
    _scan_entry("30m")

def job_update_trends():
    log.info("📊 Refresh trend H1/H4/D1 …")
    _update_trends_batch(PAIRS)

def job_daily_reset():
    global SIGNALS_TODAY
    with _lock:
        SIGNALS_TODAY = 0
    log.info("🔄 Daily counter reset")


# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────

def initialise_pairs():
    global PAIRS
    log.info("Fetching Binance symbol list …")
    PAIRS = filter_pairs(get_all_symbols())
    log.info(f"Monitoring {len(PAIRS)} pairs")

    with _lock:
        for pair in PAIRS:
            for tf in ENTRY_TIMEFRAMES:
                TRACKERS[pair][tf] = BBMATracker(pair, tf)

    update_state(
        pairs_total = len(PAIRS),
        started_at  = datetime.now(timezone.utc).isoformat(),
        status      = "running",
        paused      = False,
    )

    log.info("Warming up trend cache …")
    _update_trends_batch(PAIRS)
    log.info("Warm-up complete ✅")

    # Boot message → admin only
    send_system_message(
        f"🚀 *{SYSTEM_NAME} v{SYSTEM_VERSION} Online*\n\n"
        f"Pairs   : `{len(PAIRS)}`\n"
        f"Entry TF: M15 + M30\n"
        f"Trend TF: H1 / H4 / D1\n"
        f"Filter  : Trend ✅  Cooldown ✅\n\n"
        f"Taip /help untuk senarai command."
    )


def start_scheduler():
    s = BackgroundScheduler(timezone="UTC")
    s.add_job(job_scan_m15,      IntervalTrigger(seconds=INTERVAL_M15), id="m15", misfire_grace_time=60)
    s.add_job(job_scan_m30,      IntervalTrigger(seconds=INTERVAL_M30), id="m30", misfire_grace_time=60)
    s.add_job(job_update_trends, IntervalTrigger(seconds=INTERVAL_H1),  id="h1",  misfire_grace_time=300)
    s.add_job(job_daily_reset,   IntervalTrigger(hours=24),             id="daily")
    s.start()
    log.info("Scheduler started ✅")
    return s


def _boot():
    try:
        time.sleep(2)

        # Register callbacks for admin_bot before starting it
        admin_bot.register("clear_cooldown", _cb_clear_cooldown)
        admin_bot.register("get_trend",      _cb_get_trend)
        admin_bot.start()

        initialise_pairs()
        start_scheduler()

        threading.Thread(target=job_scan_m15, daemon=True).start()
        threading.Thread(target=job_scan_m30, daemon=True).start()

    except Exception as e:
        log.error(f"Boot failed: {e}")
        update_state(status="error")
        send_system_message(f"❌ Boot error:\n`{e}`")


threading.Thread(target=_boot, daemon=True, name="boot").start()

if __name__ == "__main__":
    log.info(f"Starting {SYSTEM_NAME} v{SYSTEM_VERSION}")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
