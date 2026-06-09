"""
notifications/telegram_bot.py — Delta X Telegram v5

SPOT TRADING ONLY — BUY signals sahaja.
SELL signals di-block automatik.

Destinations:
  TELEGRAM_CHAT_ID  → client channel (signals, sniper entry, TP/SL updates)
  TELEGRAM_ADMIN_ID → admin private   (system, errors)
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


def _fmt(p: float) -> str:
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1:    return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"


def _sid(pair, tf, direction, ts) -> str:
    raw = f"{pair}{tf}{direction}{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y%m%d%H%M')}"
    return "DX-" + hashlib.md5(raw.encode()).hexdigest()[:8].upper()


def _duration_str(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 24:
        d = h // 24
        h = h % 24
        return f"{d}h {h}j {m}m"
    if h > 0:
        return f"{h}j {m}m"
    return f"{m}m"


def _inline_buttons(pair: str) -> dict:
    base = pair.replace("USDT", "")
    pair_us = f"{base}_USDT"
    return {
        "inline_keyboard": [[
            {"text": "📊 Binance",     "url": f"https://www.binance.com/en/trade/{pair_us}"},
            {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{pair}"},
            {"text": "🟢 Gate.io",     "url": f"https://www.gate.io/trade/{pair_us}"},
        ]]
    }


# ── Core send ────────────────────────────────────────────────────────────────

def _send(chat_id: str, text: str, reply_to: int = None, reply_markup: dict = None) -> tuple[bool, int]:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False, 0
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{_BASE}/sendMessage", json=payload, timeout=10)
        if r.status_code == 200:
            msg_id = r.json().get("result", {}).get("message_id", 0)
            return True, msg_id
        log.warning(f"Telegram {chat_id}: {r.status_code} {r.text[:120]}")
        return False, 0
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False, 0


def send_to_channel(text: str, reply_to: int = None, reply_markup: dict = None) -> tuple[bool, int]:
    return _send(TELEGRAM_CHAT_ID, text, reply_to, reply_markup)


def send_to_admin(text: str) -> bool:
    ok, _ = _send(TELEGRAM_ADMIN_ID, text)
    return ok


# ── Signal → CLIENT CHANNEL (BUY only) ───────────────────────────────────────

def send_signal(sig) -> tuple[bool, int]:
    # ── SPOT ONLY: Block SELL ──
    if sig.direction != "BUY":
        log.debug(f"Blocked SELL signal: {sig.pair} (spot trading only)")
        return False, 0

    ts_str = datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    sid    = _sid(sig.pair, sig.timeframe, sig.direction, sig.timestamp)

    trends = [sig.trend_daily, sig.trend_h4, sig.trend_h1]
    bull_c = trends.count("BULLISH")
    align  = "✅ SELARAS" if bull_c >= 2 else ("⚠️ SEPARA" if bull_c >= 1 else "❌ LAWAN")

    msg = "\n".join([
        f"{'─'*30}",
        f"⚡ *{SYSTEM_NAME} — SIGNAL*",
        f"{'─'*30}",
        f"",
        f"📈 *{sig.pair}*  `{sig.timeframe}`  `{sig.signal_type}`",
        f"🟩 BUY",
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
        f"{'─'*30}",
    ])

    ok, msg_id = send_to_channel(msg, reply_markup=_inline_buttons(sig.pair))
    if ok:
        log.info(f"✅ Signal: {sig.pair} BUY {sig.timeframe} (msg_id={msg_id})")
    return ok, msg_id


# ── Sniper Entry → CLIENT CHANNEL (BUY only) ─────────────────────────────────

def send_near_entry(warn) -> bool:
    # ── SPOT ONLY: Block SELL ──
    if warn.direction != "BUY":
        log.debug(f"Blocked SELL sniper: {warn.pair} (spot trading only)")
        return False

    ts_str = datetime.fromtimestamp(warn.timestamp, tz=timezone.utc).strftime("%d %b %Y  %H:%M UTC")

    msg = "\n".join([
        f"{'─'*30}",
        f"🎯 *{SYSTEM_NAME} — SNIPER ENTRY*",
        f"{'─'*30}",
        f"",
        f"📈 *{warn.pair}*  `{warn.timeframe}`  `{warn.signal_type}`",
        f"🟩 BUY",
        f"",
        f"📍 *Harga semasa :* `{_fmt(warn.current_price)}`",
        f"📐 *Zon entry    :* `{_fmt(warn.zone_bot)}` — `{_fmt(warn.zone_top)}`",
        f"📏 *Jarak ke zon :* `{warn.pct_away:.2f}%` lagi",
        f"",
        f"⏳ _Bersedia — signal akan muncul_",
        f"_apabila harga masuk zon entry_",
        f"",
        f"🕐 `{ts_str}`",
        f"{'─'*30}",
    ])

    ok, _ = send_to_channel(msg, reply_markup=_inline_buttons(warn.pair))
    if ok:
        log.info(f"🎯 Sniper: {warn.pair} BUY {warn.timeframe} ({warn.pct_away:.1f}% away)")
    return ok


# ── TP/SL Hit → REPLY to original signal ─────────────────────────────────────

def send_tp_hit(trade: dict, tp_level: str, hit_price: float) -> bool:
    entry    = trade["entry"]
    pnl      = (hit_price - entry) / entry * 100
    duration = _duration_str((__import__('time').time()) - trade["ts"])
    tp_price = trade[tp_level.lower()]

    if tp_level == "TP1":
        new_sl  = entry
        footer  = f"📌 *SL → * `{_fmt(new_sl)}`  *(Breakeven)*\n🎯 Menunggu TP2: `{_fmt(trade['tp2'])}`"
    elif tp_level == "TP2":
        new_sl  = trade["tp1"]
        footer  = f"📌 *SL → * `{_fmt(new_sl)}`  *(Lock TP1)*\n🎯 Menunggu TP3: `{_fmt(trade['tp3'])}`"
    else:
        footer  = "🏆 *Semua TP tercapai! Trade ditutup.*"

    msg = "\n".join([
        f"{'─'*30}",
        f"✅ *{tp_level} HIT — PROFIT*",
        f"{'─'*30}",
        f"",
        f"📈 *{trade['pair']}*  `{trade.get('timeframe','')}`",
        f"",
        f"💰 Entry   : `{_fmt(entry)}`",
        f"🎯 {tp_level}     : `{_fmt(tp_price)}`",
        f"📍 Harga   : `{_fmt(hit_price)}`",
        f"",
        f"💵 *P&L : +{pnl:.1f}%*",
        f"⏱ Durasi : {duration}",
        f"",
        f"{footer}",
        f"{'─'*30}",
    ])

    ok, _ = send_to_channel(msg, reply_to=trade.get("msg_id"))
    if ok:
        log.info(f"✅ {tp_level}: {trade['pair']} +{pnl:.1f}%")
    return ok


def send_sl_hit(trade: dict, hit_price: float) -> bool:
    entry    = trade["entry"]
    pnl      = (hit_price - entry) / entry * 100
    duration = _duration_str((__import__('time').time()) - trade["ts"])

    tps_hit = []
    if trade.get("tp1_hit"): tps_hit.append("TP1")
    if trade.get("tp2_hit"): tps_hit.append("TP2")

    if tps_hit:
        status_line = f"⚠️ *SL HIT — Separa untung ({', '.join(tps_hit)} tercapai)*"
    else:
        status_line = f"🛑 *SL HIT — RUGI*"

    msg = "\n".join([
        f"{'─'*30}",
        f"{status_line}",
        f"{'─'*30}",
        f"",
        f"📈 *{trade['pair']}*  `{trade.get('timeframe','')}`",
        f"",
        f"💰 Entry   : `{_fmt(entry)}`",
        f"🛑 SL      : `{_fmt(trade['sl'])}`",
        f"📍 Harga   : `{_fmt(hit_price)}`",
        f"",
        f"💔 *P&L : {pnl:.1f}%*",
        f"⏱ Durasi : {duration}",
        f"{'─'*30}",
    ])

    ok, _ = send_to_channel(msg, reply_to=trade.get("msg_id"))
    if ok:
        log.info(f"🛑 SL: {trade['pair']} {pnl:.1f}%")
    return ok


# ── System → ADMIN ONLY ──────────────────────────────────────────────────────

def send_system_message(text: str) -> bool:
    return send_to_admin(f"🤖 *{SYSTEM_NAME}*\n\n{text}")
