"""
core/signals.py — BBMA Signal Detection State Machine v2
Improvements in this version:
• Relaxed entry zone (1% tolerance — tak miss harga tepat kat MA5)
• Trend filter baked-in per-signal
• CSM Reentry logic (trend continuation entry)
• Near-entry detection (⚠ hampir entry)
• last_event property for scan stats counting
• NearEntryWarning dataclass
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from config.settings import SL_BUFFER, MAX_LOSS_PERCENT, MIN_TP1_PERCENT
from utils.logger import get_logger

log = get_logger("signals")

# How close price must be to MA zone to trigger near-entry warning (2%)
NEAR_ENTRY_THRESHOLD = 0.02


# ────────────────────────────────────────────────────────────────────────────
# Result data classes
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class SignalResult:
    pair: str
    timeframe: str
    direction: str          # BUY | SELL
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_pct: float           # negative  e.g. -15.2
    tp1_pct: float          # positive  e.g. +25.0
    tp2_pct: float
    tp3_pct: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    trend_h1: str = "NEUTRAL"
    trend_h4: str = "NEUTRAL"
    trend_daily: str = "NEUTRAL"
    timestamp: float = field(default_factory=time.time)


@dataclass
class NearEntryWarning:
    """Fired when price is close to entry zone but not yet triggered."""
    pair: str
    timeframe: str
    direction: str          # BUY | SELL
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    current_price: float
    zone_top: float         # upper bound of entry zone (MA5 High or MA10 High)
    zone_bot: float         # lower bound of entry zone (MA10 Low or MA5 Low)
    pct_away: float         # % distance from nearest zone edge
    timestamp: float = field(default_factory=time.time)


# ────────────────────────────────────────────────────────────────────────────
# Low-level detectors
# ────────────────────────────────────────────────────────────────────────────
def _detect_extrem(df: pd.DataFrame):
    ext_buy = (df["ma5_low"] < df["bb_lower"]) | (df["ma10_low"] < df["bb_lower"])
    ext_sell = (df["ma5_high"] > df["bb_upper"]) | (df["ma10_high"] > df["bb_upper"])
    return ext_buy, ext_sell


def _detect_csm(df: pd.DataFrame):
    csm_buy = df["close"] > df["bb_upper"]   # body closes above BB Upper
    csm_sell = df["close"] < df["bb_lower"]  # body closes below BB Lower
    return csm_buy, csm_sell


def _in_ma_zone_buy(row: pd.Series, tol: float = 0.01) -> bool:
    """
    Relaxed BUY entry zone (1% tolerance).
    Price retraced into MA5/MA10 Low band.
    Previously strict: low <= ma10_low AND close >= ma5_low
    Now: within tol % of either MA boundary counts.
    """
    ma_top = row["ma5_low"] * (1 + tol)    # slightly above MA5 Low
    ma_bot = row["ma10_low"] * (1 - tol)   # slightly below MA10 Low
    touched = row["low"] <= ma_top
    closed_ok = row["close"] >= row["ma10_low"] * (1 - tol)
    return touched and closed_ok


def _in_ma_zone_sell(row: pd.Series, tol: float = 0.01) -> bool:
    """
    Relaxed SELL entry zone (1% tolerance).
    """
    ma_bot = row["ma5_high"] * (1 - tol)
    ma_top = row["ma10_high"] * (1 + tol)
    touched = row["high"] >= ma_bot
    closed_ok = row["close"] <= row["ma10_high"] * (1 + tol)
    return touched and closed_ok


def _pct_from_zone_buy(row: pd.Series) -> float:
    """How far (%) current price is above the BUY MA zone (positive = outside zone)."""
    zone_top = row["ma5_low"]
    if row["close"] <= zone_top:
        return 0.0
    return (row["close"] - zone_top) / zone_top * 100


def _pct_from_zone_sell(row: pd.Series) -> float:
    """How far (%) current price is below the SELL MA zone."""
    zone_bot = row["ma5_high"]
    if row["close"] >= zone_bot:
        return 0.0
    return (zone_bot - row["close"]) / zone_bot * 100


def _passes_risk(entry: float, sl: float, tp1: float) -> bool:
    sl_pct = abs((sl - entry) / entry * 100)
    tp1_pct = abs((tp1 - entry) / entry * 100)
    return sl_pct <= MAX_LOSS_PERCENT and tp1_pct >= MIN_TP1_PERCENT


# ────────────────────────────────────────────────────────────────────────────
# Tracker
# ────────────────────────────────────────────────────────────────────────────
# last_event values (used by scanner for stats counting)
EV_NONE = "none"
EV_EXTREM = "extrem"
EV_MHV = "mhv"
EV_CSM = "csm_detected"
EV_ENTRY_ZONE = "entry_zone"
EV_RISK_BLOCK = "risk_blocked"
EV_SIGNAL = "signal"
EV_CSM_REENTRY = "csm_reentry"
EV_NEAR_ENTRY = "near_entry"
EV_RESET = "reset"


class BBMATracker:
    """
    Stateful BBMA tracker for one (pair, timeframe) slot.
    Returns (SignalResult|None, NearEntryWarning|None) from update().
    self.last_event is set after every call for external stats counting.
    """

    def __init__(self, pair: str, timeframe: str):
        self.pair = pair
        self.timeframe = timeframe
        self._reset()

    # ── Public ──────────────────────────────────────────────────────────────

    def update(
        self,
        df: pd.DataFrame,
        trends: dict | None = None,
    ) -> tuple[Optional[SignalResult], Optional[NearEntryWarning]]:
        """
        Process latest BBMA data.
        Returns (signal, near_entry_warning) — either can be None.
        Sets self.last_event for external stats counting.
        """
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        trends = trends or {}
        self.last_event = EV_NONE

        ext_buy, ext_sell = _detect_extrem(df)
        csm_buy, csm_sell = _detect_csm(df)
        last = df.iloc[-1]

        # ── CSM cancels opposite Extrem ──────────────────────────────────────
        if self.state == "EXTREM":
            if self.direction == "BUY" and csm_sell.iloc[-1]:
                log.debug(f"{self.pair}/{self.timeframe} CSM SELL cancels BUY Extrem")
                self._reset()
                self.last_event = EV_RESET
                return None, None
            if self.direction == "SELL" and csm_buy.iloc[-1]:
                log.debug(f"{self.pair}/{self.timeframe} CSM BUY cancels SELL Extrem")
                self._reset()
                self.last_event = EV_RESET
                return None, None

        # ════════════════ STATE MACHINE ════════════════

        if self.state == "WATCHING":
            # ── Detect Extrem ────────────────────────────────────────────────
            if ext_sell.iloc[-1] and not ext_buy.iloc[-1]:
                self._set_extrem("SELL", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None
            if ext_buy.iloc[-1] and not ext_sell.iloc[-1]:
                self._set_extrem("BUY", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            # ── Detect CSM (Reentry setup) ───────────────────────────────────
            if csm_buy.iloc[-1]:
                self.state = "CSM_PULLBACK"
                self.direction = "BUY"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                log.debug(f"{self.pair}/{self.timeframe} CSM BUY detected")
                return None, None

            if csm_sell.iloc[-1]:
                self.state = "CSM_PULLBACK"
                self.direction = "SELL"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                log.debug(f"{self.pair}/{self.timeframe} CSM SELL detected")
                return None, None

        elif self.state == "EXTREM":
            # ── Wait for MHV ─────────────────────────────────────────────────
            if self.direction == "SELL":
                if last["ma5_high"] < last["bb_upper"] and last["ma10_high"] < last["bb_upper"]:
                    self.state = "MHV"
                    self.near_warned = False
                    self.last_event = EV_MHV
                    log.debug(f"{self.pair}/{self.timeframe} MHV SELL confirmed")
                    return None, None
            else:
                if last["ma5_low"] > last["bb_lower"] and last["ma10_low"] > last["bb_lower"]:
                    self.state = "MHV"
                    self.near_warned = False
                    self.last_event = EV_MHV
                    log.debug(f"{self.pair}/{self.timeframe} MHV BUY confirmed")
                    return None, None

        elif self.state == "MHV":
            # ── Near-entry warning ───────────────────────────────────────────
            near_warn = self._check_near_entry(last)

            # ── Check entry zone ─────────────────────────────────────────────
            in_zone = (
                _in_ma_zone_buy(last) if self.direction == "BUY"
                else _in_ma_zone_sell(last)
            )
            if in_zone:
                self.last_event = EV_ENTRY_ZONE
                signal = self._build_signal(last, df, trends, "EXTREM_MHV")
                if signal is None:
                    self.last_event = EV_RISK_BLOCK
                    self._reset()
                    return None, near_warn
                self._reset()
                self.last_event = EV_SIGNAL
                return signal, None

            return None, near_warn

        elif self.state == "CSM_PULLBACK":
            # ── CSM Reentry: wait for pullback to MA zone ────────────────────
            near_warn = self._check_near_entry(last)

            in_zone = (
                _in_ma_zone_buy(last) if self.direction == "BUY"
                else _in_ma_zone_sell(last)
            )
            if in_zone:
                self.last_event = EV_ENTRY_ZONE
                signal = self._build_signal(last, df, trends, "CSM_REENTRY")
                if signal is None:
                    self.last_event = EV_RISK_BLOCK
                    self._reset()
                    return None, near_warn
                self._reset()
                self.last_event = EV_CSM_REENTRY
                return signal, None

            # If price shoots through MA zone (missed entry), reset
            if self.direction == "BUY" and last["close"] < last["ma10_low"] * 0.97:
                self._reset()
                self.last_event = EV_RESET
                return None, None
            if self.direction == "SELL" and last["close"] > last["ma10_high"] * 1.03:
                self._reset()
                self.last_event = EV_RESET
                return None, None

            return None, near_warn

        return None, None

    # ── Private ─────────────────────────────────────────────────────────────

    def _reset(self):
        self.state = "WATCHING"
        self.direction = None
        self.extrem_idx = None
        self.extrem_candle = None
        self.csm_candle = None
        self.near_warned = False
        self.last_event = EV_NONE

    def _set_extrem(self, direction: str, idx: int, candle: pd.Series):
        self.state = "EXTREM"
        self.direction = direction
        self.extrem_idx = idx
        self.extrem_candle = candle.copy()
        self.csm_candle = None
        self.near_warned = False
        log.debug(f"{self.pair}/{self.timeframe} EXTREM {direction} @ {candle['close']:.6f}")

    def _check_near_entry(self, last: pd.Series) -> Optional[NearEntryWarning]:
        """Return NearEntryWarning if close to MA zone and not yet warned."""
        if self.near_warned:
            return None

        if self.direction == "BUY":
            pct = _pct_from_zone_buy(last)
            zone_top = last["ma5_low"]
            zone_bot = last["ma10_low"]
        else:
            pct = _pct_from_zone_sell(last)
            zone_top = last["ma10_high"]
            zone_bot = last["ma5_high"]

        if 0 < pct <= NEAR_ENTRY_THRESHOLD * 100:
            self.near_warned = True
            self.last_event = EV_NEAR_ENTRY
            return NearEntryWarning(
                pair=self.pair,
                timeframe=self.timeframe,
                direction=self.direction,
                signal_type="EXTREM_MHV" if self.state == "MHV" else "CSM_REENTRY",
                current_price=float(last["close"]),
                zone_top=float(zone_top),
                zone_bot=float(zone_bot),
                pct_away=round(pct, 2),
            )
        return None

    def _build_signal(
        self,
        last: pd.Series,
        df: pd.DataFrame,
        trends: dict,
        signal_type: str,
    ) -> Optional[SignalResult]:
        """Build and risk-validate a SignalResult. Returns None if risk fails."""
        entry = float(last["close"])

        # Reference candle for SL: Extrem candle (EXTREM_MHV) or CSM candle (CSM_REENTRY)
        ref_candle = (
            self.extrem_candle if signal_type == "EXTREM_MHV"
            else self.csm_candle
        )

        if self.direction == "BUY":
            sl = float(ref_candle["low"]) * (1 - SL_BUFFER)
            tp1 = float(last["bb_middle"])
            tp2 = float(last["bb_upper"])
            tp3 = tp2 + abs(tp1 - sl)
        else:
            sl = float(ref_candle["high"]) * (1 + SL_BUFFER)
            tp1 = float(last["bb_middle"])
            tp2 = float(last["bb_lower"])
            tp3 = tp2 - abs(sl - tp1)

        if not _passes_risk(entry, sl, tp1):
            log.debug(
                f"{self.pair}/{self.timeframe} {self.direction} risk FAIL "
                f"SL={abs((sl - entry) / entry * 100):.1f}% TP1={abs((tp1 - entry) / entry * 100):.1f}%"
            )
            return None

        def _pct(a, b):
            return round((a - b) / b * 100, 2)

        return SignalResult(
            pair=self.pair,
            timeframe=self.timeframe,
            direction=self.direction,
            signal_type=signal_type,
            entry_price=entry,
            sl_price=round(sl, 8),
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            tp3_price=round(tp3, 8),
            sl_pct=_pct(sl, entry),
            tp1_pct=_pct(tp1, entry),
            tp2_pct=_pct(tp2, entry),
            tp3_pct=_pct(tp3, entry),
            bb_upper=float(last["bb_upper"]),
            bb_middle=float(last["bb_middle"]),
            bb_lower=float(last["bb_lower"]),
            trend_h1=trends.get("1h", "NEUTRAL"),
            trend_h4=trends.get("4h", "NEUTRAL"),
            trend_daily=trends.get("1d", "NEUTRAL"),
        )
