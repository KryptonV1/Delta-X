"""
core/signals.py — BBMA Signal Detection State Machine v5 (BUY ONLY / SPOT)

Full BBMA Oma Ally cycle per SOP:
  EXTREM_MHV path : WATCHING → EXTREM → MHV → CSA → RE-ENTRY → SIGNAL
  CSM_REENTRY path: WATCHING → CSM_PULLBACK → RE-ENTRY → SIGNAL

Changes from v4:
  • CRITICAL FIX: State machine now uses while-loop to process ALL
    applicable transitions in a single update() call.
    Old behaviour: each state change did `return None, None` — the next
    candle arrived 15 min later and the moment was gone.
    New behaviour: EXTREM→MHV→CSA→entry can all fire on ONE candle.
  • Warning system fully removed (BUY only, spot trading)
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
    direction: str          # always "BUY" in v5
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_pct: float
    tp1_pct: float
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
def _detect_extrem_buy(row: pd.Series) -> bool:
    """
    EXTREME BUY (Law 1): MA5 or MA10 Low crosses below BB Lower.
    Body (close) must remain inside BB — if close is also below BB, that's CSM.
    """
    ma_outside = (row["ma5_low"] < row["bb_lower"]) or (row["ma10_low"] < row["bb_lower"])
    body_inside = row["close"] > row["bb_lower"]
    return bool(ma_outside and body_inside)


def _detect_csm_buy(row: pd.Series) -> bool:
    """CSM BUY (Law 2): Candle body (close) closes ABOVE BB Upper."""
    return bool(row["close"] > row["bb_upper"])


def _detect_csm_sell(row: pd.Series) -> bool:
    """CSM SELL: body closes below BB Lower — cancels any active BUY setup."""
    return bool(row["close"] < row["bb_lower"])


def _detect_mhv_buy(row: pd.Series) -> bool:
    """MHV BUY: MA5 Low and MA10 Low both returned inside BB Lower."""
    return bool(row["ma5_low"] > row["bb_lower"] and row["ma10_low"] > row["bb_lower"])


def _detect_csa_buy(row: pd.Series) -> tuple[bool, bool]:
    """
    CSA BUY: Close crosses above MA5 Low & MA10 Low (direction confirmed).
    CSA Early  → close crosses MA5 & MA10 (inside BB)
    CSA Strong → also crosses Mid BB
    Returns: (early, strong)
    """
    early = bool(
        row["close"] > row["ma5_low"]
        and row["close"] > row["ma10_low"]
        and row["close"] < row["bb_upper"]       # not CSM
        and row["ma5_low"]  > row["bb_lower"]    # MA inside BB
        and row["ma10_low"] > row["bb_lower"]
    )
    strong = early and bool(row["close"] > row["bb_middle"])
    return early, strong


def _in_ma_zone_buy(row: pd.Series, tol: float = 0.01) -> bool:
    """
    BUY Re-Entry zone: price pulled back to MA5/MA10 Low band.
    Low must touch zone (MA5 Low ±1%) AND close must hold above MA10 Low.
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

    CRITICAL: uses a while-loop internally so that if a single candle
    satisfies EXTREM → MHV → CSA → entry zone, ALL transitions fire
    in one update() call.  Previous versions advanced only one state
    per call, losing the candle by the next 15-minute scan.

    update() returns (SignalResult | None, None).
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
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        trends = trends or {}
        self.last_event = EV_NONE

        last = df.iloc[-1]

        # ── GLOBAL: SELL CSM cancels any active BUY state ────────────────────
        if self.state != "WATCHING" and _detect_csm_sell(last):
            log.debug(
                f"{self.pair}/{self.timeframe} CSM SELL — "
                f"cancels {self.state} BUY setup"
            )
            self._reset()
            self.last_event = EV_RESET
            return None, None

        # ════════════════ STATE MACHINE (while-loop fall-through) ════════════
        #
        # The loop allows multi-step transitions on a single candle:
        #   WATCHING → EXTREM → MHV → CSA → entry → signal
        # Each iteration advances at most one state.  The loop exits when
        # no further transition is possible on the current candle, or when
        # a signal is produced.
        #
        max_steps = 5  # safety cap — can never loop infinitely
        for _ in range(max_steps):

            # ── WATCHING ─────────────────────────────────────────────────────
            if self.state == "WATCHING":

                if _detect_extrem_buy(last):
                    self._set_extrem(len(df) - 1, last)
                    self.last_event = EV_EXTREM
                    # fall through — check MHV on same candle
                    continue

                if _detect_csm_buy(last):
                    self.state      = "CSM_PULLBACK"
                    self.direction  = "BUY"
                    self.csm_candle = last.copy()
                    self.last_event = EV_CSM
                    log.debug(f"{self.pair}/{self.timeframe} CSM BUY detected")
                    # CSM and entry zone cannot coexist on same candle
                    # (CSM = close > bb_upper; entry = low near ma5_low)
                    break

                break  # nothing detected

            # ── EXTREM: check MHV ────────────────────────────────────────────
            elif self.state == "EXTREM":

                if _detect_mhv_buy(last):
                    # MHV confirmed — check if CSA also present on same candle
                    csa_early, csa_strong = _detect_csa_buy(last)
                    if csa_early:
                        self.state      = "CSA"
                        self.csa_strong = csa_strong
                        self.last_event = EV_CSA
                        log.debug(
                            f"{self.pair}/{self.timeframe} MHV+CSA "
                            f"{'STRONG' if csa_strong else 'EARLY'} BUY (same candle)"
                        )
                        # fall through — check entry zone on same candle
                        continue
                    else:
                        self.state      = "MHV"
                        self.last_event = EV_MHV
                        log.debug(f"{self.pair}/{self.timeframe} MHV BUY confirmed")
                        # fall through — check CSA on same candle
                        continue

                break  # MHV not yet confirmed — wait

            # ── MHV: check CSA ───────────────────────────────────────────────
            elif self.state == "MHV":

                # MA dips back outside BB → re-enter EXTREM
                if _detect_extrem_buy(last):
                    log.debug(
                        f"{self.pair}/{self.timeframe} New EXTREM BUY in MHV — "
                        f"updating reference candle"
                    )
                    self._set_extrem(len(df) - 1, last)
                    self.last_event = EV_EXTREM
                    break  # can't also be MHV on same candle

                csa_early, csa_strong = _detect_csa_buy(last)
                if csa_early:
                    self.state      = "CSA"
                    self.csa_strong = csa_strong
                    self.last_event = EV_CSA
                    log.debug(
                        f"{self.pair}/{self.timeframe} CSA "
                        f"{'STRONG' if csa_strong else 'EARLY'} BUY confirmed"
                    )
                    # fall through — check entry zone on same candle
                    continue

                break  # CSA not yet — wait

            # ── CSA: check Re-Entry zone ─────────────────────────────────────
            elif self.state == "CSA":

                # CSM BUY during CSA → upgrade to CSM_PULLBACK
                if _detect_csm_buy(last):
                    self.state      = "CSM_PULLBACK"
                    self.csm_candle = last.copy()
                    self.last_event = EV_CSM
                    log.debug(
                        f"{self.pair}/{self.timeframe} CSM BUY during CSA — "
                        f"upgrading to CSM_PULLBACK"
                    )
                    break  # CSM candle is not an entry candle

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

                break  # not in zone yet — wait for pullback

            # ── CSM_PULLBACK: check Re-Entry zone ────────────────────────────
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

                # Price blew through zone → missed entry
                if last["close"] < last["ma10_low"] * 0.97:
                    log.debug(
                        f"{self.pair}/{self.timeframe} Entry zone blown — reset"
                    )
                    self._reset()
                    self.last_event = EV_RESET
                    return None, None

                break  # not in zone yet — wait

            else:
                break  # unknown state — shouldn't happen

        return None, None

    # ── Private ─────────────────────────────────────────────────────────────

    def _reset(self):
        self.state         = "WATCHING"
        self.direction     = "BUY"
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
        Build a BUY SignalResult with validation gates.

        Gate 1 — BB width    : skip ranging/choppy market
        Gate 2 — Multi-TF    : BUY blocked if H4 or Daily is BEARISH
        Gate 3 — Risk check  : SL valid, TP1 above entry

        SL reference:
          EXTREM_MHV  → Extreme candle low
          CSM_REENTRY → CSM candle low

        TP levels (per SOP):
          TP1 = nearer of bb_middle or ma5_high (above entry)
          TP2 = bb_upper
          TP3 = TP2 + (TP1-to-SL distance)  [projection]
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
