"""
notifications/admin_bot.py — Delta X Admin Command Bot

Runs a background long-poll loop.
Only responds to TELEGRAM_ADMIN_ID — all other users are silently ignored.

Available commands:
  /help      — Senarai command
  /status    — Status sistem
  /pause     — Pause scanning
  /resume    — Resume scanning
  /stats     — Statistik signals hari ini
  /clearcd   — Clear semua cooldown (allow re-signal segera)
  /trend XYZ — Check trend semasa untuk satu pair
"""
from __future__ import annotations

import time
import threading
import requests

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, SYSTEM_NAME
from web.app import get_state, update_state
from utils.logger import get_logger

log = get_logger("admin_bot")

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Callback registry — main.py registers functions here to avoid circular import
_callbacks: dict = {}


def register(name: str, fn):
    """Register a callback from main.py (e.g. clear_cooldown, get_trend)."""
    _callbacks[name] = fn


# ── Private send ──────────────────────────────────────────────────────────────

def _reply(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        return
    try:
        requests.post(
            f"{_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_ADMIN_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Admin reply failed: {e}")


# ── Command handlers ──────────────────────────────────────────────────────────

def _cmd_help() -> str:
    return (
        f"⚡ *{SYSTEM_NAME} — Admin Commands*\n\n"
        "`/status`        — Status sistem\n"
        "`/pause`         — Pause semua scan\n"
        "`/resume`        — Resume scan\n"
        "`/stats`         — Statistik hari ini\n"
        "`/clearcd`       — Clear semua cooldown\n"
        "`/trend BTCUSDT` — Semak trend pair\n"
        "`/help`          — Senarai command ini\n\n"
        "_Hanya kau yang boleh gunakan command ini._"
    )


def _cmd_status() -> str:
    state = get_state()
    paused = state.get("paused", False)
    icon   = "⏸ PAUSED" if paused else "🟢 RUNNING"
    last   = state.get("last_scan")
    last_str = (
        f"<t={int(last)}>" if last
        else "Belum scan"
    )
    return (
        f"⚡ *{SYSTEM_NAME} Status*\n\n"
        f"Status        : {icon}\n"
        f"Pairs pantau  : `{state.get('pairs_total', 0)}`\n"
        f"Pairs scan    : `{state.get('pairs_scanned', 0)}`\n"
        f"Signal hari ini: `{state.get('signals_today', 0)}`\n"
        f"Active signals: `{len(state.get('active_signals', []))}`\n"
    )


def _cmd_pause() -> str:
    update_state(paused=True)
    log.info("Admin: system PAUSED")
    return "⏸ *Scan DIPAUSED*\n\nGunakan /resume untuk sambung."


def _cmd_resume() -> str:
    update_state(paused=False)
    log.info("Admin: system RESUMED")
    return "▶️ *Scan DISAMBUNG SEMULA*"


def _cmd_stats() -> str:
    state = get_state()
    active = state.get("active_signals", [])
    lines  = [f"📊 *Statistik Hari Ini*\n"]
    lines += [f"Signals fired  : `{state.get('signals_today', 0)}`"]
    lines += [f"Active signals : `{len(active)}`"]
    lines += [f"Pairs monitored: `{state.get('pairs_total', 0)}`"]

    if active:
        lines += ["\n*Active signals (terkini):*"]
        for s in active[:5]:
            lines += [
                f"  `{s['pair']}` {s['direction']} {s['timeframe']} "
                f"entry=`{s['entry']:.4f}`"
            ]
    return "\n".join(lines)


def _cmd_clearcd() -> str:
    fn = _callbacks.get("clear_cooldown")
    if fn:
        count = fn()
        log.info(f"Admin: cooldown cleared ({count} entries)")
        return f"✅ *Cooldown cleared!*\n`{count}` entries dibuang.\nSignal boleh fire semula untuk semua pairs."
    return "⚠️ Callback not registered yet. Cuba lagi selepas boot selesai."


def _cmd_trend(pair: str) -> str:
    if not pair:
        return "⚠️ Guna: `/trend BTCUSDT`"
    pair = pair.upper()
    fn   = _callbacks.get("get_trend")
    if not fn:
        return "⚠️ Callback not ready. Cuba selepas boot selesai."
    trends = fn(pair)
    if not trends:
        return f"❓ `{pair}` tidak ditemui dalam cache trend."
    return (
        f"📊 *Trend — {pair}*\n\n"
        f"D1  : `{trends.get('1d',  '–')}`\n"
        f"H4  : `{trends.get('4h',  '–')}`\n"
        f"H1  : `{trends.get('1h',  '–')}`\n"
    )


# ── Update processor ──────────────────────────────────────────────────────────

def _process(update: dict):
    msg  = update.get("message") or update.get("edited_message", {})
    if not msg:
        return

    user_id = str(msg.get("from", {}).get("id", ""))
    text    = (msg.get("text") or "").strip()

    # ── Security: ignore everyone except admin ──────────────────────────────
    if user_id != str(TELEGRAM_ADMIN_ID):
        log.debug(f"Ignoring message from non-admin user_id={user_id}")
        return

    if not text.startswith("/"):
        return

    parts   = text.split()
    command = parts[0].lower().split("@")[0]   # strip @botname suffix
    arg     = parts[1] if len(parts) > 1 else ""

    log.info(f"Admin command: {command} {arg}")

    if command in ("/start", "/help"):
        _reply(_cmd_help())
    elif command == "/status":
        _reply(_cmd_status())
    elif command == "/pause":
        _reply(_cmd_pause())
    elif command == "/resume":
        _reply(_cmd_resume())
    elif command == "/stats":
        _reply(_cmd_stats())
    elif command == "/clearcd":
        _reply(_cmd_clearcd())
    elif command == "/trend":
        _reply(_cmd_trend(arg))
    else:
        _reply(f"❓ Command tidak dikenali: `{command}`\nGunakan /help")


# ── Polling loop ──────────────────────────────────────────────────────────────

def _poll_loop():
    last_update_id = 0
    log.info(f"Admin bot polling — admin_id={TELEGRAM_ADMIN_ID}")

    while True:
        try:
            r = requests.get(
                f"{_BASE}/getUpdates",
                params={
                    "offset":           last_update_id + 1,
                    "timeout":          30,
                    "allowed_updates":  ["message"],
                },
                timeout=35,
            )
            if r.status_code == 200:
                for upd in r.json().get("result", []):
                    last_update_id = upd["update_id"]
                    try:
                        _process(upd)
                    except Exception as e:
                        log.error(f"Process update error: {e}")
            else:
                time.sleep(5)

        except requests.exceptions.Timeout:
            pass       # normal for long-polling
        except Exception as e:
            log.error(f"Poll loop error: {e}")
            time.sleep(10)


def start():
    """Start admin bot in background thread. Call once during boot."""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing — admin bot disabled")
        return
    if not TELEGRAM_ADMIN_ID:
        log.warning("TELEGRAM_ADMIN_ID missing — admin bot disabled")
        return

    threading.Thread(target=_poll_loop, daemon=True, name="admin_bot").start()
    log.info("Admin bot started ✅")
