"""
notifications/telegram_bot.py — Telegram dispatcher for Delta X v3

Two separate destinations:
  TELEGRAM_CHAT_ID  → client channel  (signals only)
  TELEGRAM_ADMIN_ID → admin private   (system, errors, near-entry, scan stats)
"""
import hashlib
from datetime import datetime, timezone

import requests

from config.settings import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_ID, SYSTEM_NAME
)
from utils.logger import get_logger

log = get_logger("telegram")

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

TREND_EMOJI = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}


# ── Core send helpers ─────────────────────────────────────────────────────────

def _send(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"{_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Telegram {chat_id}: {r.status_code} {r.text[:100]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def send_to_channel(text: str) -> bool:
    """→ Client channel (signals)."""
    return _send(TELEGRAM_CHAT_ID, text)


def send_to_admin(text: str) -> bool:
    """→ Admin private chat (system/errors/commands)."""
    return _send(TELEGRAM_ADMIN_ID, text)

def _send_with_buttons(chat_id: str, text: str, reply_markup: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"{_BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Telegram {chat_id}: {r.status_code} {r.text[:100]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False

# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt(p: float) -> str:
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1:    return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"


def _sid(pair, tf, direction, ts) -> str:
    raw = f"{pair}{tf}{direction}{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y%m%d%H%M')}"
    return "DX-" + hashlib.md5(raw.encode()).hexdigest()[:8].upper()


# ── Signal → CLIENT CHANNEL ───────────────────────────────────────────────────

def send_signal(sig) -> bool:
    """Send trade signal to CLIENT channel only."""
    arrow = "📈" if sig.direction == "BUY" else "📉"
    badge = "🟩 BUY"  if sig.direction == "BUY" else "🟥 SELL"
    ts_str = datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    sid    = _sid(sig.pair, sig.timeframe, sig.direction, sig.timestamp)

    trends = [sig.trend_daily, sig.trend_h4, sig.trend_h1]
    bull_c = trends.count("BULLISH")
    bear_c = trends.count("BEARISH")
    if sig.direction == "BUY":
        align = "✅ SELARAS" if bull_c >= 2 else ("⚠️ SEPARA" if bull_c >= 1 else "❌ LAWAN")
    else:
        align = "✅ SELARAS" if bear_c >= 2 else ("⚠️ SEPARA" if bear_c >= 1 else "❌ LAWAN")

    msg = "\n".join([
        f"{'─'*32}",
        f"⚡ *{SYSTEM_NAME} — SIGNAL*",
        f"{'─'*32}",
        f"",
        f"{arrow} *{sig.pair}*  `{sig.timeframe}`  `{sig.signal_type}`",
        f"{badge}",
        f"",
        f"💰 *Entry :* `{_fmt(sig.entry_price)}`",
        f"🎯 *TP1   :* `{_fmt(sig.tp1_price)}`  *(+{sig.tp1_pct:.1f}%)*",
        f"🎯 *TP2   :* `{_fmt(sig.tp2_price)}`  *(+{sig.tp2_pct:.1f}%)*",
        f"🎯 *TP3   :* `{_fmt(sig.tp3_price)}`  *(+{sig.tp3_pct:.1f}%)*",
        f"🛑 *SL    :* `{_fmt(sig.sl_price)}`  *({sig.sl_pct:.1f}%)*",
        f"",
        f"📊 *Trend*  {align}",
        f"  D1 {TREND_EMOJI.get(sig.trend_daily,'🟡')} `{sig.trend_daily}`",
        f"  H4 {TREND_EMOJI.get(sig.trend_h4,'🟡')} `{sig.trend_h4}`",
        f"  H1 {TREND_EMOJI.get(sig.trend_h1,'🟡')} `{sig.trend_h1}`",
        f"",
        f"🕐 `{ts_str}`  •  `{sid}`",
        f"{'─'*32}",
        f"_Delta X · BBMA X_",
    ])

    # Build inline keyboard buttons
    base = sig.pair.replace("USDT", "")
    pair_underscore = f"{base}_USDT"
    
    reply_markup = {
        "inline_keyboard": [[
            {"text": "📊 Binance",      "url": f"https://www.binance.com/en/trade/{pair_underscore}"},
            {"text": "📈 TradingView",  "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{sig.pair}"},
            {"text": "🟢 Gate.io",      "url": f"https://www.gate.io/trade/{pair_underscore}"},
        ]]
    }

    ok = _send_with_buttons(TELEGRAM_CHAT_ID, msg, reply_markup)


# ── Near-entry → ADMIN ONLY ───────────────────────────────────────────────────

def send_near_entry(warn) -> bool:
    """Near-entry warning → ADMIN only (klien tak perlu tahu ini)."""
    arrow = "📈" if warn.direction == "BUY" else "📉"
    ts_str = datetime.fromtimestamp(warn.timestamp, tz=timezone.utc).strftime("%H:%M UTC")

    msg = "\n".join([
        f"⚠️ *HAMPIR ENTRY*",
        f"{arrow} *{warn.pair}*  `{warn.timeframe}`  `{warn.signal_type}`",
        f"Arah     : *{'BELI' if warn.direction=='BUY' else 'JUAL'}*",
        f"Harga    : `{_fmt(warn.current_price)}`",
        f"Zon entry: `{_fmt(warn.zone_bot)}` — `{_fmt(warn.zone_top)}`",
        f"Jarak    : `{warn.pct_away:.2f}%` lagi",
        f"🕐 `{ts_str}`",
    ])
    return send_to_admin(msg)


# ── System messages → ADMIN ONLY ──────────────────────────────────────────────

def send_system_message(text: str) -> bool:
    """System/boot/error messages → ADMIN only."""
    return send_to_admin(f"🤖 *{SYSTEM_NAME}*\n\n{text}")
