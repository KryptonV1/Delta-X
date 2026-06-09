"""
core/signals.py — BBMA Signal Detection State Machine v4 (BUY ONLY / SPOT)

Full BBMA Oma Ally cycle per SOP:
  EXTREM_MHV path : WATCHING → EXTREM → MHV → CSA → [RE-ENTRY] → SIGNAL
  CSM_REENTRY path: WATCHING → CSM_PULLBACK → [RE-ENTRY] → SIGNAL

Changes from v3:
  • Warning system REMOVED entirely — no more "Bersedia" spam
  • BUY ONLY — all SELL signal paths removed (spot trading)
  • ROOT BUG FIXED: 20% minimum-TP1 check removed from risk gate.
    TP1 on 15m/30m is typically 0.5–5% — requiring 20% blocked every signal.
    New Gate 3: SL must be valid + TP1 must be above entry. That's it.
  • update() still returns (Optional[SignalResult], None) for backward compat
  • Dead code pruned: _pct_from_zone helpers and sell-zone functions removed
  • csm_sell still detected — needed to cancel/reset active BUY setups
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from config.settings import (
    SL_BUFFER,
    MAX_LOSS_PERCENT,
    TREND_FILTER_ENABLED,
    MIN_BB_WIDTH_PERCENT,
)
from utils.logger import get_logger

log = get_logger("signals")


# ────────────────────────────────────────────────────────────────────────────
# Result data class
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class SignalResult:
    pair: str
    timeframe: str
    direction: str          # always "BUY" in v4
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_pct: float           # negative  e.g. -2.1
    tp1_pct: float          # positive  e.g. +1.8
    tp2_pct: float
    tp3_pct: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    trend_h1: str = "NEUTRAL"
    trend_h4: str = "NEUTRAL"
    trend_daily: str = "NEUTRAL"
    timestamp: float = field(default_factory=time.time)


# ────────────────────────────────────────────────────────────────────────────
# Low-level detectors
# ────────────────────────────────────────────────────────────────────────────
def _detect_extrem(df: pd.DataFrame):
    """
    EXTREME (Law 1): MA5 or MA10 crosses outside BB.
    Body (close) must remain inside BB — if close is also outside, that is CSM.
    Only ext_buy is used for signal generation; ext_sell returned for completeness.
    """
    ext_buy = (
        ((df["ma5_low"] < df["bb_lower"]) | (df["ma10_low"] < df["bb_lower"]))
        & (df["close"] > df["bb_lower"])
    )
    ext_sell = (
        ((df["ma5_high"] > df["bb_upper"]) | (df["ma10_high"] > df["bb_upper"]))
        & (df["close"] < df["bb_upper"])
    )
    return ext_buy, ext_sell


def _detect_csm(df: pd.DataFrame):
    """
    CSM (Law 2): Candle body (close) outside BB.
    csm_buy  → BUY setup (enters CSM_PULLBACK)
    csm_sell → cancels any active BUY state (global reset trigger)
    """
    csm_buy  = df["close"] > df["bb_upper"]
    csm_sell = df["close"] < df["bb_lower"]
    return csm_buy, csm_sell


def _detect_csa(row: pd.Series, direction: str) -> tuple[bool, bool]:
    """
    CSA (Candlestick Arah): direction confirmation after Extreme + MHV.
    CSA Early  → close crosses MA5 & MA10 (inside BB)
    CSA Strong → above + close crosses Mid BB
    Returns: (early, strong)
    """
    if direction == "BUY":
        early = bool(
            row["close"] > row["ma5_low"]
            and row["close"] > row["ma10_low"]
            and row["close"] < row["bb_upper"]
            and row["ma5_low"]  > row["bb_lower"]
            and row["ma10_low"] > row["bb_lower"]
        )
        strong = early and bool(row["close"] > row["bb_middle"])
    else:
        early  = False
        strong = False
    return early, strong


def _in_ma_zone_buy(row: pd.Series, tol: float = 0.01) -> bool:
    """
    BUY Re-Entry zone: price pulled back to MA5/MA10 Low band.
    Low must touch zone top (MA5 Low ±1%) AND close must not crash
    through MA10 Low (allows 1% tolerance below).
    """
    ma_top    = row["ma5_low"] * (1 + tol)
    touched   = row["low"] <= ma_top
    closed_ok = row["close"] >= row["ma10_low"] * (1 - tol)
    return bool(touched and closed_ok)


# ────────────────────────────────────────────────────────────────────────────
# Event constants  (for scanner stats / logging)
# ────────────────────────────────────────────────────────────────────────────
EV_NONE        = "none"
EV_EXTREM      = "extrem"
EV_MHV         = "mhv"
EV_CSA         = "csa"
EV_CSM         = "csm_detected"
EV_ENTRY_ZONE  = "entry_zone"
EV_RISK_BLOCK  = "risk_blocked"
EV_SIGNAL      = "signal"
EV_CSM_REENTRY = "csm_reentry"
EV_RESET       = "reset"


# ────────────────────────────────────────────────────────────────────────────
# Tracker
# ────────────────────────────────────────────────────────────────────────────
class BBMATracker:
    """
    Stateful BBMA tracker — BUY signals only (spot trading).

    Cycle:
      EXTREM_MHV : WATCHING → EXTREM → MHV → CSA → RE-ENTRY → SIGNAL
      CSM_REENTRY: WATCHING → CSM_PULLBACK → RE-ENTRY → SIGNAL

    update() returns (SignalResult | None, None).
    The second element is always None (warning system removed).
    Kept as tuple for backward compatibility with callers.
    """

    def __init__(self, pair: str, timeframe: str):
        self.pair      = pair
        self.timeframe = timeframe
        self._reset()

    # ── Public ──────────────────────────────────────────────────────────────

    def update(
        self,
        df: pd.DataFrame,
        trends: dict | None = None,
    ) -> tuple[Optional[SignalResult], None]:
        """
        Process latest BBMA candle data.

        df     : DataFrame with BBMA columns (from calculate_bbma). Min 50 rows.
        trends : {"1h": "BULLISH"|"BEARISH"|"NEUTRAL",
                  "4h": ..., "1d": ...}
                 When TREND_FILTER_ENABLED, BUY is blocked if H4 or Daily
                 is explicitly BEARISH.

        Returns (signal, None). Signal is None until entry zone is hit.
        """
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        trends = trends or {}
        self.last_event = EV_NONE

        ext_buy, _         = _detect_extrem(df)   # ext_sell not used (BUY only)
        csm_buy, csm_sell  = _detect_csm(df)
        last = df.iloc[-1]

        # ── GLOBAL: SELL CSM cancels any active BUY state ────────────────────
        # A candle closing below BB Lower invalidates all BUY setups regardless
        # of which state we are in.
        if self.state != "WATCHING" and csm_sell.iloc[-1]:
            log.debug(
                f"{self.pair}/{self.timeframe} CSM SELL — "
                f"cancels {self.state} BUY setup"
            )
            self._reset()
            self.last_event = EV_RESET
            return None, None

        # ════════════════════════ STATE MACHINE ═════════════════════════════

        # ── WATCHING ─────────────────────────────────────────────────────────
        if self.state == "WATCHING":

            if ext_buy.iloc[-1]:
                self._set_extrem(len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            if csm_buy.iloc[-1]:
                self.state      = "CSM_PULLBACK"
                self.direction  = "BUY"
                self.csm_candle = last.copy()
                self.last_event = EV_CSM
                log.debug(f"{self.pair}/{self.timeframe} CSM BUY detected")
                return None, None

        # ── EXTREM: wait for MA Low to return inside BB (MHV) ─────────────────
        elif self.state == "EXTREM":

            mhv = (
                last["ma5_low"]  > last["bb_lower"]
                and last["ma10_low"] > last["bb_lower"]
            )

            if mhv:
                csa_early, csa_strong = _detect_csa(last, "BUY")
                if csa_early:
                    # MHV + CSA on same candle → skip MHV state
                    self.state      = "CSA"
                    self.csa_strong = csa_strong
                    self.last_event = EV_CSA
                    log.debug(
                        f"{self.pair}/{self.timeframe} MHV+CSA "
                        f"{'STRONG' if csa_strong else 'EARLY'} BUY (same candle)"
                    )
                else:
                    self.state      = "MHV"
                    self.last_event = EV_MHV
                    log.debug(f"{self.pair}/{self.timeframe} MHV BUY confirmed")
                return None, None

        # ── MHV: wait for CSA (directional confirmation) ──────────────────────
        elif self.state == "MHV":

            # MA dips back outside BB → re-enter EXTREM with fresh candle
            if ext_buy.iloc[-1]:
                log.debug(
                    f"{self.pair}/{self.timeframe} New EXTREM BUY in MHV "
                    f"— updating reference candle"
                )
                self._set_extrem(len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            csa_early, csa_strong = _detect_csa(last, "BUY")
            if csa_early:
                self.state      = "CSA"
                self.csa_strong = csa_strong
                self.last_event = EV_CSA
                log.debug(
                    f"{self.pair}/{self.timeframe} CSA "
                    f"{'STRONG' if csa_strong else 'EARLY'} BUY confirmed"
                )
                return None, None

        # ── CSA: direction confirmed — wait for Re-Entry pullback ─────────────
        elif self.state == "CSA":

            # CSM BUY during CSA → upgrade (tighter SL from CSM candle)
            if csm_buy.iloc[-1]:
                self.state      = "CSM_PULLBACK"
                self.csm_candle = last.copy()
                self.last_event = EV_CSM
                log.debug(
                    f"{self.pair}/{self.timeframe} CSM BUY during CSA "
                    f"— upgrading to CSM_PULLBACK"
                )
                return None, None

            if _in_ma_zone_buy(last):
                self.last_event = EV_ENTRY_ZONE
                signal = self._build_signal(last, df, trends, "EXTREM_MHV")
                if signal is None:
                    self.last_event = EV_RISK_BLOCK
                    self._reset()
                    return None, None
                self._reset()
                self.last_event = EV_SIGNAL
                return signal, None

        # ── CSM_PULLBACK: wait for Re-Entry after CSM ─────────────────────────
        elif self.state == "CSM_PULLBACK":

            if _in_ma_zone_buy(last):
                self.last_event = EV_ENTRY_ZONE
                signal = self._build_signal(last, df, trends, "CSM_REENTRY")
                if signal is None:
                    self.last_event = EV_RISK_BLOCK
                    self._reset()
                    return None, None
                self._reset()
                self.last_event = EV_CSM_REENTRY
                return signal, None

            # Price blew straight through zone → entry missed, reset
            if last["close"] < last["ma10_low"] * 0.97:
                log.debug(
                    f"{self.pair}/{self.timeframe} Entry zone blown through — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None

        return None, None

    # ── Private ─────────────────────────────────────────────────────────────

    def _reset(self):
        self.state         = "WATCHING"
        self.direction     = "BUY"      # always BUY — kept for logging clarity
        self.extrem_idx    = None
        self.extrem_candle = None
        self.csm_candle    = None
        self.csa_strong    = False
        self.last_event    = EV_NONE

    def _set_extrem(self, idx: int, candle: pd.Series):
        self.state         = "EXTREM"
        self.direction     = "BUY"
        self.extrem_idx    = idx
        self.extrem_candle = candle.copy()
        self.csm_candle    = None
        self.csa_strong    = False
        log.debug(
            f"{self.pair}/{self.timeframe} EXTREM BUY "
            f"@ {candle['close']:.6f}"
        )

    def _build_signal(
        self,
        last: pd.Series,
        df: pd.DataFrame,
        trends: dict,
        signal_type: str,
    ) -> Optional[SignalResult]:
        """
        Build a BUY SignalResult with three validation gates.

        Gate 1 — BB width    : skip ranging/choppy market
        Gate 2 — Multi-TF    : BUY blocked if H4 or Daily is BEARISH
        Gate 3 — Risk check  : SL must not be excessive; TP1 must be above entry
                               NOTE: 20% minimum-TP1 check intentionally removed.
                               On 15m/30m, TP1 (bb_middle) is typically 1–5%
                               above entry — far below the 20% threshold that
                               was silently blocking every single signal.

        SL reference:
          EXTREM_MHV  → Extreme candle low (per SOP)
          CSM_REENTRY → CSM candle low

        TP levels (per SOP):
          TP1 = nearer of bb_middle or ma5_high
          TP2 = bb_upper
          TP3 = TP2 + (TP1 distance from SL)  [projection]
        """
        entry = float(last["close"])

        # ── Gate 1: BB width ─────────────────────────────────────────────────
        if float(last["bb_width"]) < MIN_BB_WIDTH_PERCENT:
            log.debug(
                f"{self.pair}/{self.timeframe} BB too narrow "
                f"({float(last['bb_width']):.4f} < {MIN_BB_WIDTH_PERCENT}) — skip"
            )
            return None

        # ── Gate 2: Multi-TF trend alignment ────────────────────────────────
        if TREND_FILTER_ENABLED:
            trend_h4    = trends.get("4h", "NEUTRAL")
            trend_daily = trends.get("1d", "NEUTRAL")
            if trend_h4 == "BEARISH" or trend_daily == "BEARISH":
                log.debug(
                    f"{self.pair}/{self.timeframe} BUY blocked — "
                    f"H4={trend_h4} Daily={trend_daily}"
                )
                return None

        # ── SL reference ─────────────────────────────────────────────────────
        ref_candle = (
            self.extrem_candle if signal_type == "EXTREM_MHV"
            else self.csm_candle
        )
        if ref_candle is None:
            log.warning(
                f"{self.pair}/{self.timeframe} ref_candle is None "
                f"for {signal_type} — skip"
            )
            return None

        # ── Price levels ─────────────────────────────────────────────────────
        sl = float(ref_candle["low"]) * (1 - SL_BUFFER)

        # TP1: nearer of bb_middle or ma5_high above entry (per SOP)
        tp1_candidates = [
            x for x in [float(last["bb_middle"]), float(last["ma5_high"])]
            if x > entry
        ]
        tp1 = min(tp1_candidates) if tp1_candidates else float(last["bb_middle"])
        tp2 = float(last["bb_upper"])
        tp3 = tp2 + abs(tp1 - sl)

        # ── Gate 3: Risk validation ──────────────────────────────────────────
        sl_pct = abs((sl - entry) / entry * 100)

        if sl_pct > MAX_LOSS_PERCENT:
            log.debug(
                f"{self.pair}/{self.timeframe} SL too large "
                f"({sl_pct:.1f}% > {MAX_LOSS_PERCENT}%) — skip"
            )
            return None

        if tp1 <= entry:
            log.debug(
                f"{self.pair}/{self.timeframe} TP1 not above entry — skip"
            )
            return None

        if sl >= entry:
            log.debug(
                f"{self.pair}/{self.timeframe} SL above entry — skip"
            )
            return None

        def _pct(a: float, b: float) -> float:
            return round((a - b) / b * 100, 2)

        return SignalResult(
            pair=self.pair,
            timeframe=self.timeframe,
            direction="BUY",
            signal_type=signal_type,
            entry_price=entry,
            sl_price=round(sl,  8),
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            tp3_price=round(tp3, 8),
            sl_pct=_pct(sl,  entry),
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
