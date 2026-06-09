"""
core/signals.py — BBMA Signal Detection State Machine v3

Full BBMA Oma Ally cycle per SOP:
  EXTREM_MHV path : WATCHING → EXTREM → MHV → CSA → [RE-ENTRY] → SIGNAL
  CSM_REENTRY path: WATCHING → CSM_PULLBACK → [RE-ENTRY] → SIGNAL

Changes from v2:
  • CSA state added  (mandatory step between MHV and RE-ENTRY per SOP)
  • Multi-TF trend filter enforced via TREND_FILTER_ENABLED (settings)
  • BB width filter added via MIN_BB_WIDTH_PERCENT (settings — avoids ranging)
  • NEAR_ENTRY_THRESHOLD imported from settings (removed hardcode)
  • _detect_extrem now excludes CSM candles (body must be inside BB)
    — upholds the mutual exclusivity of Law 1 vs Law 2
  • TP1 picks the NEARER of bb_middle / MA5 opposite (per SOP)
  • Opposite CSM now cancels ALL non-WATCHING states (not only EXTREM)
  • New-direction CSA in MHV state invalidates and resets the tracker
  • Same-direction Extreme re-detected in MHV state resets to EXTREM
  • CSM during CSA state upgrades smoothly to CSM_PULLBACK
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from config.settings import (
    SL_BUFFER,
    MAX_LOSS_PERCENT,
    MIN_TP1_PERCENT,
    NEAR_ENTRY_THRESHOLD,       # 0.02 → 2% proximity threshold
    TREND_FILTER_ENABLED,       # True → enforce H4 + Daily alignment
    MIN_BB_WIDTH_PERCENT,       # 0.02 → skip ranging markets
)
from utils.logger import get_logger

log = get_logger("signals")


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
    """Fired when price is approaching the entry zone but hasn't triggered yet."""
    pair: str
    timeframe: str
    direction: str          # BUY | SELL
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    current_price: float
    zone_top: float         # upper bound of entry zone
    zone_bot: float         # lower bound of entry zone
    pct_away: float         # % distance from nearest zone edge
    timestamp: float = field(default_factory=time.time)


# ────────────────────────────────────────────────────────────────────────────
# Low-level detectors  (operate on the last row / full DataFrame)
# ────────────────────────────────────────────────────────────────────────────
def _detect_extrem(df: pd.DataFrame):
    """
    EXTREME (Law 1): MA5 or MA10 crosses OUTSIDE BB.
    The candle BODY (close) must remain INSIDE BB.
    → If close is also outside BB that is CSM (Law 2), not Extreme.
    The two laws are mutually exclusive — enforced by the close-filter below.

    Returns: (ext_buy Series, ext_sell Series)
    """
    ext_buy = (
        ((df["ma5_low"] < df["bb_lower"]) | (df["ma10_low"] < df["bb_lower"]))
        & (df["close"] > df["bb_lower"])   # body inside BB — not CSM Sell
    )
    ext_sell = (
        ((df["ma5_high"] > df["bb_upper"]) | (df["ma10_high"] > df["bb_upper"]))
        & (df["close"] < df["bb_upper"])   # body inside BB — not CSM Buy
    )
    return ext_buy, ext_sell


def _detect_csm(df: pd.DataFrame):
    """
    CSM (Law 2): Candle BODY (close) closes OUTSIDE Bollinger Bands.
    Strong trend-continuation signal — do NOT chase; wait for Re-Entry.

    Returns: (csm_buy Series, csm_sell Series)
    """
    csm_buy  = df["close"] > df["bb_upper"]
    csm_sell = df["close"] < df["bb_lower"]
    return csm_buy, csm_sell


def _detect_csa(row: pd.Series, direction: str) -> tuple[bool, bool]:
    """
    CSA (Candlestick Arah): Confirms new direction after Extreme + MHV.
    Candle close crosses past MA5 & MA10 in the expected direction, while
    remaining inside BB (not yet a CSM event).

    CSA Early  → close crosses MA5 & MA10
    CSA Strong → close also crosses Mid BB

    direction: "BUY"  → expecting price to move UP through MA5/MA10 Low
               "SELL" → expecting price to move DOWN through MA5/MA10 High

    Returns: (early: bool, strong: bool)
    """
    if direction == "BUY":
        early = bool(
            row["close"] > row["ma5_low"]
            and row["close"] > row["ma10_low"]
            and row["close"] < row["bb_upper"]       # not CSM Buy
            and row["ma5_low"]  > row["bb_lower"]    # MA already inside BB
            and row["ma10_low"] > row["bb_lower"]
        )
        strong = early and bool(row["close"] > row["bb_middle"])
    else:  # SELL
        early = bool(
            row["close"] < row["ma5_high"]
            and row["close"] < row["ma10_high"]
            and row["close"] > row["bb_lower"]        # not CSM Sell
            and row["ma5_high"]  < row["bb_upper"]    # MA already inside BB
            and row["ma10_high"] < row["bb_upper"]
        )
        strong = early and bool(row["close"] < row["bb_middle"])

    return bool(early), bool(strong)


def _in_ma_zone_buy(row: pd.Series, tol: float = 0.015) -> bool:
    """
    BUY Re-Entry zone: price has pulled back to the MA5/MA10 Low band.
    Candle low touches the zone AND close does not collapse through MA10 Low.
    tol = 1% tolerance to catch near-misses without excessive lag.
    """
    ma_top   = row["ma5_low"] * (1 + tol)
    touched  = row["low"] <= ma_top
    closed_ok = row["close"] >= row["ma10_low"] * (1 - tol)
    return bool(touched and closed_ok)


def _in_ma_zone_sell(row: pd.Series, tol: float = 0.015) -> bool:
    """
    SELL Re-Entry zone: price has pulled back to the MA5/MA10 High band.
    Candle high touches the zone AND close does not push through MA10 High.
    """
    ma_bot   = row["ma5_high"] * (1 - tol)
    touched  = row["high"] >= ma_bot
    closed_ok = row["close"] <= row["ma10_high"] * (1 + tol)
    return bool(touched and closed_ok)


def _pct_from_zone_buy(row: pd.Series) -> float:
    """
    How far (%) price is ABOVE the BUY entry zone.
    0.0 if already inside or below zone.
    Used for near-entry warning.
    """
    zone_top = row["ma5_low"]
    if row["close"] <= zone_top:
        return 0.0
    return float((row["close"] - zone_top) / zone_top * 100)


def _pct_from_zone_sell(row: pd.Series) -> float:
    """
    How far (%) price is BELOW the SELL entry zone.
    0.0 if already inside or above zone.
    """
    zone_bot = row["ma5_high"]
    if row["close"] >= zone_bot:
        return 0.0
    return float((zone_bot - row["close"]) / zone_bot * 100)


def _passes_risk(entry: float, sl: float, tp1: float) -> bool:
    sl_pct  = abs((sl  - entry) / entry * 100)
    tp1_pct = abs((tp1 - entry) / entry * 100)
    return sl_pct <= MAX_LOSS_PERCENT and tp1_pct >= MIN_TP1_PERCENT


# ────────────────────────────────────────────────────────────────────────────
# Event constants  (used by scanner for stats counting / logging)
# ────────────────────────────────────────────────────────────────────────────
EV_NONE        = "none"
EV_EXTREM      = "extrem"
EV_MHV         = "mhv"
EV_CSA         = "csa"           # NEW in v3
EV_CSM         = "csm_detected"
EV_ENTRY_ZONE  = "entry_zone"
EV_RISK_BLOCK  = "risk_blocked"
EV_SIGNAL      = "signal"
EV_CSM_REENTRY = "csm_reentry"
EV_NEAR_ENTRY  = "near_entry"
EV_RESET       = "reset"


# ────────────────────────────────────────────────────────────────────────────
# Tracker
# ────────────────────────────────────────────────────────────────────────────
class BBMATracker:
    """
    Stateful BBMA tracker for one (pair, timeframe) slot.

    Implements the complete BBMA Oma Ally cycle:
      EXTREM_MHV : WATCHING → EXTREM → MHV → CSA → RE-ENTRY → SIGNAL
      CSM_REENTRY: WATCHING → CSM_PULLBACK → RE-ENTRY → SIGNAL

    Call update() on every new closed candle.
    Returns (SignalResult | None, NearEntryWarning | None).
    self.last_event is set after every call for external stats counting.
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
    ) -> tuple[Optional[SignalResult], Optional[NearEntryWarning]]:
        """
        Process latest BBMA candle data.

        df     : DataFrame with BBMA columns already calculated (calculate_bbma).
                 Must contain ≥50 rows.
        trends : dict mapping TF string → "BULLISH" | "BEARISH" | "NEUTRAL"
                 e.g. {"1h": "BULLISH", "4h": "BULLISH", "1d": "NEUTRAL"}
                 When TREND_FILTER_ENABLED, BUY requires non-BEARISH H4 & Daily;
                 SELL requires non-BULLISH H4 & Daily.

        Returns (signal, near_entry_warning) — either can be None.
        self.last_event is always set.
        """
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        trends = trends or {}
        self.last_event = EV_NONE

        ext_buy, ext_sell = _detect_extrem(df)
        csm_buy, csm_sell = _detect_csm(df)
        last = df.iloc[-1]

        # ── GLOBAL: Opposite CSM cancels ANY active state ────────────────────
        # Per SOP: CSM in the opposite direction invalidates the current setup
        # entirely. Applies to EXTREM, MHV, CSA, and CSM_PULLBACK states.
        if self.state != "WATCHING":
            if self.direction == "BUY" and csm_sell.iloc[-1]:
                log.debug(
                    f"{self.pair}/{self.timeframe} CSM SELL invalidates "
                    f"{self.state} BUY setup — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None
            if self.direction == "SELL" and csm_buy.iloc[-1]:
                log.debug(
                    f"{self.pair}/{self.timeframe} CSM BUY invalidates "
                    f"{self.state} SELL setup — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None

        # ════════════════════════ STATE MACHINE ═════════════════════════════

        # ── WATCHING: detect the triggering event ────────────────────────────
        if self.state == "WATCHING":

            # Extreme takes priority over CSM in the same candle
            if ext_sell.iloc[-1] and not ext_buy.iloc[-1]:
                self._set_extrem("SELL", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            if ext_buy.iloc[-1] and not ext_sell.iloc[-1]:
                self._set_extrem("BUY", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            if csm_buy.iloc[-1]:
                self.state      = "CSM_PULLBACK"
                self.direction  = "BUY"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                log.debug(f"{self.pair}/{self.timeframe} CSM BUY detected")
                return None, None

            if csm_sell.iloc[-1]:
                self.state      = "CSM_PULLBACK"
                self.direction  = "SELL"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                log.debug(f"{self.pair}/{self.timeframe} CSM SELL detected")
                return None, None

        # ── EXTREM: wait for MA to return inside BB (MHV condition) ──────────
        elif self.state == "EXTREM":

            mhv_confirmed = (
                (last["ma5_low"]  > last["bb_lower"] and
                 last["ma10_low"] > last["bb_lower"])
                if self.direction == "BUY"
                else
                (last["ma5_high"]  < last["bb_upper"] and
                 last["ma10_high"] < last["bb_upper"])
            )

            if mhv_confirmed:
                # Check whether CSA has already occurred on this same candle
                csa_early, csa_strong = _detect_csa(last, self.direction)
                if csa_early:
                    # Skip MHV state — go directly to CSA
                    self.state      = "CSA"
                    self.csa_strong = csa_strong
                    self.near_warned = False
                    self.last_event = EV_CSA
                    log.debug(
                        f"{self.pair}/{self.timeframe} MHV+CSA "
                        f"{'STRONG' if csa_strong else 'EARLY'} {self.direction} "
                        f"detected on same candle"
                    )
                else:
                    self.state      = "MHV"
                    self.near_warned = False
                    self.last_event = EV_MHV
                    log.debug(
                        f"{self.pair}/{self.timeframe} MHV {self.direction} confirmed"
                    )
                return None, None

        # ── MHV: wait for CSA (directional candle confirmation) ──────────────
        elif self.state == "MHV":

            # Edge case: if MA dips back outside BB in same direction → re-enter
            # EXTREM state with the fresh candle as the new reference.
            new_extreme = (
                ext_buy.iloc[-1]  if self.direction == "BUY"
                else ext_sell.iloc[-1]
            )
            if new_extreme:
                log.debug(
                    f"{self.pair}/{self.timeframe} New EXTREM {self.direction} "
                    f"while in MHV — updating reference candle"
                )
                self._set_extrem(self.direction, len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            # Detect CSA in expected direction
            csa_early, csa_strong = _detect_csa(last, self.direction)
            if csa_early:
                self.state      = "CSA"
                self.csa_strong = csa_strong
                self.near_warned = False
                self.last_event = EV_CSA
                log.debug(
                    f"{self.pair}/{self.timeframe} CSA "
                    f"{'STRONG' if csa_strong else 'EARLY'} {self.direction} confirmed"
                )
                return None, None

            # Opposite CSA means the anticipated direction was wrong → reset
            opposite = "SELL" if self.direction == "BUY" else "BUY"
            opp_early, _ = _detect_csa(last, opposite)
            if opp_early:
                log.debug(
                    f"{self.pair}/{self.timeframe} Opposite CSA {opposite} "
                    f"invalidates MHV {self.direction} — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None

        # ── CSA: direction confirmed — wait for Re-Entry (pullback to MA zone)
        elif self.state == "CSA":

            near_warn = self._check_near_entry(last)

            # CSM in our direction during CSA → upgrade to CSM_PULLBACK.
            # The CSM candle becomes the new (tighter) SL reference.
            if self.direction == "BUY" and csm_buy.iloc[-1]:
                self.state       = "CSM_PULLBACK"
                self.csm_candle  = last.copy()
                self.near_warned = False   # re-arm warning for the new zone
                self.last_event  = EV_CSM
                log.debug(
                    f"{self.pair}/{self.timeframe} CSM BUY during CSA — "
                    f"upgrading to CSM_PULLBACK (tighter SL)"
                )
                return None, near_warn

            if self.direction == "SELL" and csm_sell.iloc[-1]:
                self.state       = "CSM_PULLBACK"
                self.csm_candle  = last.copy()
                self.near_warned = False
                self.last_event  = EV_CSM
                log.debug(
                    f"{self.pair}/{self.timeframe} CSM SELL during CSA — "
                    f"upgrading to CSM_PULLBACK (tighter SL)"
                )
                return None, near_warn

            # Check Re-Entry zone (the only valid trade entry per SOP)
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

        # ── CSM_PULLBACK: wait for Re-Entry after CSM ────────────────────────
        elif self.state == "CSM_PULLBACK":

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

            # Price blew straight through entry zone (missed) → reset
            if self.direction == "BUY" and last["close"] < last["ma10_low"] * 0.97:
                log.debug(
                    f"{self.pair}/{self.timeframe} Entry zone blown through "
                    f"(BUY) — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None
            if self.direction == "SELL" and last["close"] > last["ma10_high"] * 1.03:
                log.debug(
                    f"{self.pair}/{self.timeframe} Entry zone blown through "
                    f"(SELL) — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                return None, None

            return None, near_warn

        return None, None

    # ── Private ─────────────────────────────────────────────────────────────

    def _reset(self):
        self.state         = "WATCHING"
        self.direction     = None
        self.extrem_idx    = None
        self.extrem_candle = None
        self.csm_candle    = None
        self.csa_strong    = False    # was the CSA confirmation strong?
        self.near_warned   = False
        self.last_event    = EV_NONE

    def _set_extrem(self, direction: str, idx: int, candle: pd.Series):
        self.state         = "EXTREM"
        self.direction     = direction
        self.extrem_idx    = idx
        self.extrem_candle = candle.copy()
        self.csm_candle    = None
        self.csa_strong    = False
        self.near_warned   = False
        log.debug(
            f"{self.pair}/{self.timeframe} EXTREM {direction} "
            f"@ {candle['close']:.6f}"
        )

    def _check_near_entry(self, last: pd.Series) -> Optional[NearEntryWarning]:
        """
        Return NearEntryWarning if price is within NEAR_ENTRY_THRESHOLD
        of the entry zone and a warning hasn't been sent yet for this setup.
        """
        if self.near_warned:
            return None

        if self.direction == "BUY":
            pct      = _pct_from_zone_buy(last)
            zone_top = float(last["ma5_low"])
            zone_bot = float(last["ma10_low"])
        else:
            pct      = _pct_from_zone_sell(last)
            zone_top = float(last["ma10_high"])
            zone_bot = float(last["ma5_high"])

        # NEAR_ENTRY_THRESHOLD is stored as a decimal (e.g. 0.02 = 2%)
        threshold_pct = NEAR_ENTRY_THRESHOLD * 100
        if 0 < pct <= threshold_pct:
            self.near_warned = True
            self.last_event  = EV_NEAR_ENTRY
            # Determine which path this warning belongs to
            signal_type = (
                "EXTREM_MHV"   if self.state == "CSA"
                else "CSM_REENTRY"
            )
            return NearEntryWarning(
                pair=self.pair,
                timeframe=self.timeframe,
                direction=self.direction,
                signal_type=signal_type,
                current_price=float(last["close"]),
                zone_top=zone_top,
                zone_bot=zone_bot,
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
        """
        Build a SignalResult with three sequential validation gates:

          Gate 1 — BB width filter   : reject if market is ranging/choppy
          Gate 2 — Multi-TF trend    : reject if H4 or Daily opposes direction
          Gate 3 — Risk filter       : reject if SL% > MAX or TP1% < MIN

        SL reference:
          EXTREM_MHV  → outside the Extreme candle low/high (per SOP)
          CSM_REENTRY → outside the CSM candle low/high

        TP targets (per SOP):
          TP1 = nearer of bb_middle OR MA5 opposite
          TP2 = BB upper / lower
          TP3 = TP2 ± (distance TP1 → SL)  [projection]

        Returns None if any gate rejects the signal.
        """
        entry = float(last["close"])

        # ── Gate 1: BB width filter — avoid ranging market ───────────────────
        if float(last["bb_width"]) < MIN_BB_WIDTH_PERCENT:
            log.debug(
                f"{self.pair}/{self.timeframe} BB too narrow "
                f"(width={float(last['bb_width']):.4f} < {MIN_BB_WIDTH_PERCENT}) "
                f"— ranging market, skip"
            )
            return None

        # ── Gate 2: Multi-TF trend alignment ────────────────────────────────
        # Per SOP: TF Major (H4 + Daily) and TF Setup MUST agree.
        # BUY only if H4 & Daily are NOT explicitly BEARISH.
        # SELL only if H4 & Daily are NOT explicitly BULLISH.
        if TREND_FILTER_ENABLED:
            trend_h4    = trends.get("4h", "NEUTRAL")
            trend_daily = trends.get("1d", "NEUTRAL")

            if self.direction == "BUY":
                if trend_h4 == "BEARISH" or trend_daily == "BEARISH":
                    log.debug(
                        f"{self.pair}/{self.timeframe} BUY blocked by trend filter "
                        f"(H4={trend_h4}, Daily={trend_daily})"
                    )
                    return None

            else:  # SELL
                if trend_h4 == "BULLISH" or trend_daily == "BULLISH":
                    log.debug(
                        f"{self.pair}/{self.timeframe} SELL blocked by trend filter "
                        f"(H4={trend_h4}, Daily={trend_daily})"
                    )
                    return None

        # ── SL reference candle ──────────────────────────────────────────────
        ref_candle = (
            self.extrem_candle if signal_type == "EXTREM_MHV"
            else self.csm_candle
        )
        if ref_candle is None:
            log.warning(
                f"{self.pair}/{self.timeframe} ref_candle is None "
                f"for {signal_type} — skipping signal"
            )
            return None

        # ── Price levels ─────────────────────────────────────────────────────
        if self.direction == "BUY":
            sl = float(ref_candle["low"]) * (1 - SL_BUFFER)

            # TP1: nearer of bb_middle OR ma5_high (per SOP: "Mid BB atau MA5/MA10 bertentangan")
            tp1_candidates = [
                x for x in [float(last["bb_middle"]), float(last["ma5_high"])]
                if x > entry
            ]
            tp1 = min(tp1_candidates) if tp1_candidates else float(last["bb_middle"])

            tp2 = float(last["bb_upper"])
            tp3 = tp2 + abs(tp1 - sl)   # projection: TP2 + (TP1-to-SL distance)

        else:  # SELL
            sl = float(ref_candle["high"]) * (1 + SL_BUFFER)

            # TP1: nearer of bb_middle OR ma5_low
            tp1_candidates = [
                x for x in [float(last["bb_middle"]), float(last["ma5_low"])]
                if x < entry
            ]
            tp1 = max(tp1_candidates) if tp1_candidates else float(last["bb_middle"])

            tp2 = float(last["bb_lower"])
            tp3 = tp2 - abs(sl - tp1)   # projection: TP2 - (SL-to-TP1 distance)

        # ── Gate 3: Risk validation ──────────────────────────────────────────
        if not _passes_risk(entry, sl, tp1):
            log.debug(
                f"{self.pair}/{self.timeframe} {self.direction} risk FAIL — "
                f"SL={abs((sl - entry) / entry * 100):.1f}% "
                f"TP1={abs((tp1 - entry) / entry * 100):.1f}%"
            )
            return None

        def _pct(a: float, b: float) -> float:
            return round((a - b) / b * 100, 2)

        return SignalResult(
            pair=self.pair,
            timeframe=self.timeframe,
            direction=self.direction,
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
