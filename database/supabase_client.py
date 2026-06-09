"""
database/supabase_client.py — Supabase persistence layer for Delta X
Required tables (run supabase_schema.sql once on your project):
• signals      — all generated trade signals
• system_logs  — system events / errors
"""
from __future__ import annotations
import time
import hashlib
from datetime import datetime, timezone
from typing import Optional
from config.settings import SUPABASE_URL, SUPABASE_KEY
from core.signals import SignalResult
from utils.logger import get_logger

log = get_logger("supabase")

_client = None


def _get_client():
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            log.warning("Supabase credentials not set — DB logging disabled")
            return None
        try:
            from supabase import create_client
            _client = create_client(SUPABASE_URL, SUPABASE_KEY)
            log.info("Supabase connected")
        except Exception as e:
            log.error(f"Supabase init failed: {e}")
            return None
    return _client


# ── Helpers ───────────────────────────────────────────────────────────────────
def _signal_id(sig: SignalResult) -> str:
    ts = datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).strftime("%Y%m%d%H%M")
    raw = f"{sig.pair}{sig.timeframe}{sig.direction}{ts}"
    return "DX-" + hashlib.md5(raw.encode()).hexdigest()[:8].upper()


# ── Public API ────────────────────────────────────────────────────────────────
def log_signal(sig: SignalResult, telegram_msg_id: int = 0) -> bool:
    """Insert a new signal record into Supabase."""
    client = _get_client()
    if not client:
        return False

    record = {
        "signal_id": _signal_id(sig),
        "pair": sig.pair,
        "base_asset": sig.pair.replace("USDT", ""),
        "timeframe": sig.timeframe,
        "direction": sig.direction,
        "signal_type": sig.signal_type,
        "entry_price": sig.entry_price,
        "sl_price": sig.sl_price,
        "tp1_price": sig.tp1_price,
        "tp2_price": sig.tp2_price,
        "tp3_price": sig.tp3_price,
        "sl_pct": sig.sl_pct,
        "tp1_pct": sig.tp1_pct,
        "tp2_pct": sig.tp2_pct,
        "tp3_pct": sig.tp3_pct,
        "trend_h1": sig.trend_h1,
        "trend_h4": sig.trend_h4,
        "trend_daily": sig.trend_daily,
        "bb_upper": sig.bb_upper,
        "bb_middle": sig.bb_middle,
        "bb_lower": sig.bb_lower,
        "status": "ACTIVE",
        "created_at": datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).isoformat(),
        "telegram_msg_id": telegram_msg_id,
    }

    try:
        client.table("signals").insert(record).execute()
        log.info(f"Signal logged: {record['signal_id']}")
        return True
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return False


def get_recent_signals(limit: int = 50) -> list[dict]:
    """Fetch the most recent signals for the dashboard."""
    client = _get_client()
    if not client:
        return []
    try:
        res = (
            client.table("signals")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error(f"Supabase fetch failed: {e}")
        return []


def get_active_signals() -> list[dict]:
    """Fetch all signals with status=ACTIVE (for restore after restart)."""
    client = _get_client()
    if not client:
        return []
    try:
        res = (
            client.table("signals")
            .select("*")
            .eq("status", "ACTIVE")
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error(f"Supabase fetch active failed: {e}")
        return []


def log_system_event(level: str, module: str, message: str):
    """Log a system event to Supabase (non-blocking best-effort)."""
    client = _get_client()
    if not client:
        return
    try:
        client.table("system_logs").insert({
            "level": level.upper(),
            "module": module,
            "message": message,
        }).execute()
    except Exception:
        pass  # silently ignore — don't let DB errors break the main loop


def update_signal_status(
    signal_id: str,
    status: str,
    close_price: float,
    pnl_pct: float,
):
    """Update signal status when TP or SL is hit."""
    client = _get_client()
    if not client:
        return
    try:
        client.table("signals").update({
            "status": status,
            "close_price": close_price,
            "pnl_pct": round(pnl_pct, 2),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_id", signal_id).execute()
        log.info(f"Signal {signal_id} → {status} (P&L: {pnl_pct:+.1f}%)")
    except Exception as e:
        log.warning(f"Supabase update failed {signal_id}: {e}")