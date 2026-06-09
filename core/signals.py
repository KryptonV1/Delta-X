from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from config.settings import (
    SL_BUFFER, MAX_LOSS_PERCENT, MIN_TP1_PERCENT,
    TREND_FILTER_ENABLED, COOLDOWN_SECONDS,
    REQUIRE_CONFIRMATION, MIN_BB_WIDTH_PERCENT, CONFIRMATION_CANDLES,
)
from core.bbma import is_trending_market, has_confirmation_candle
from utils.logger import get_logger

log = get_logger("signals")

NEAR_ENTRY_THRESHOLD = 0.02


@dataclass
class SignalResult:
    pair: str
    timeframe: str
    direction: str
    signal_type: str
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


@dataclass
class NearEntryWarning:
    pair: str
    timeframe: str
    direction: str
    signal_type: str
    current_price: float
    zone_top: float
    zone_bot: float
    pct_away: float
    timestamp: float = field(default_factory=time.time)


def _detect_extrem(df):
    ext_buy = (df["ma5_low"] < df["bb_lower"]) | (df["ma10_low"] < df["bb_lower"])
    ext_sell = (df["ma5_high"] > df["bb_upper"]) | (df["ma10_high"] > df["bb_upper"])
    return ext_buy, ext_sell


def _detect_csm(df):
    csm_buy = df["close"] > df["bb_upper"]
    csm_sell = df["close"] < df["bb_lower"]
    return csm_buy, csm_sell


def _in_ma_zone_buy(row, tol=0.01):
    ma_top = row["ma5_low"] * (1 + tol)
    ma_bot = row["ma10_low"] * (1 - tol)
    touched = row["low"] <= ma_top
    closed_ok = row["close"] >= row["ma10_low"] * (1 - tol)
    return touched and closed_ok


def _in_ma_zone_sell(row, tol=0.01):
    ma_bot = row["ma5_high"] * (1 - tol)
    ma_top = row["ma10_high"] * (1 + tol)
    touched = row["high"] >= ma_bot
    closed_ok = row["close"] <= row["ma10_high"] * (1 + tol)
    return touched and closed_ok


def _pct_from_zone_buy(row):
    zone_top = row["ma5_low"]
    if row["close"] <= zone_top:
        return 0.0
    return (row["close"] - zone_top) / zone_top * 100


def _pct_from_zone_sell(row):
    zone_bot = row["ma5_high"]
    if row["close"] >= zone_bot:
        return 0.0
    return (zone_bot - row["close"]) / zone_bot * 100


def _passes_risk(entry, sl, tp1):
    sl_pct = abs((sl - entry) / entry * 100)
    tp1_pct = abs((tp1 - entry) / entry * 100)
    return sl_pct <= MAX_LOSS_PERCENT and tp1_pct >= MIN_TP1_PERCENT


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
    def __init__(self, pair, timeframe):
        self.pair = pair
        self.timeframe = timeframe
        self.last_signal_time = 0
        self.cooldown_seconds = COOLDOWN_SECONDS
        self._reset()

    def update(self, df, trends=None):
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        if time.time() - self.last_signal_time < self.cooldown_seconds:
            return None, None

        trends = trends or {}
        self.last_event = EV_NONE

        ext_buy, ext_sell = _detect_extrem(df)
        csm_buy, csm_sell = _detect_csm(df)
        last = df.iloc[-1]

        if self.state == "EXTREM":
            if self.direction == "BUY" and csm_sell.iloc[-1]:
                self._reset()
                self.last_event = EV_RESET
                return None, None
            if self.direction == "SELL" and csm_buy.iloc[-1]:
                self._reset()
                self.last_event = EV_RESET
                return None, None

        if self.state == "WATCHING":
            if ext_sell.iloc[-1] and not ext_buy.iloc[-1]:
                self._set_extrem("SELL", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None
            if ext_buy.iloc[-1] and not ext_sell.iloc[-1]:
                self._set_extrem("BUY", len(df) - 1, last)
                self.last_event = EV_EXTREM
                return None, None

            if csm_buy.iloc[-1]:
                self.state = "CSM_PULLBACK"
                self.direction = "BUY"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                return None, None

            if csm_sell.iloc[-1]:
                self.state = "CSM_PULLBACK"
                self.direction = "SELL"
                self.csm_candle = last.copy()
                self.near_warned = False
                self.last_event = EV_CSM
                return None, None

        elif self.state == "EXTREM":
            if self.direction == "SELL":
                if last["ma5_high"] < last["bb_upper"] and last["ma10_high"] < last["bb_upper"]:
                    self.state = "MHV"
                    self.near_warned = False
                    self.last_event = EV_MHV
                    return None, None
            else:
                if last["ma5_low"] > last["bb_lower"] and last["ma10_low"] > last["bb_lower"]:
                    self.state = "MHV"
                    self.near_warned = False
                    self.last_event = EV_MHV
                    return None, None

        elif self.state == "MHV":
            near_warn = self._check_near_entry(last)
            in_zone = _in_ma_zone_buy(last) if self.direction == "BUY" else _in_ma_zone_sell(last)
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
            near_warn = self._check_near_entry(last)
            in_zone = _in_ma_zone_buy(last) if self.direction == "BUY" else _in_ma_zone_sell(last)
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

    def _reset(self):
        self.state = "WATCHING"
        self.direction = None
        self.extrem_idx = None
        self.extrem_candle = None
        self.csm_candle = None
        self.near_warned = False
        self.last_event = EV_NONE

    def _set_extrem(self, direction, idx, candle):
        self.state = "EXTREM"
        self.direction = direction
        self.extrem_idx = idx
        self.extrem_candle = candle.copy()
        self.csm_candle = None
        self.near_warned = False

    def _check_near_entry(self, last):
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

    def _passes_trend_filter(self, trends):
        if not TREND_FILTER_ENABLED:
            return True
        trend_scores = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}
        h1_score = trend_scores.get(trends.get("1h", "NEUTRAL"), 0)
        h4_score = trend_scores.get(trends.get("4h", "NEUTRAL"), 0)
        daily_score = trend_scores.get(trends.get("1d", "NEUTRAL"), 0)
        total_score = h1_score + h4_score + daily_score
        if self.direction == "BUY":
            return total_score >= 0
        else:
            return total_score <= 0

    def _build_signal(self, last, df, trends, signal_type):
        entry = float(last["close"])

        if not self._passes_trend_filter(trends):
            log.debug(f"{self.pair}/{self.timeframe} REJECTED by trend filter")
            return None

        if not is_trending_market(df, MIN_BB_WIDTH_PERCENT):
            log.debug(f"{self.pair}/{self.timeframe} Ranging market, skip")
            return None

        if REQUIRE_CONFIRMATION:
            if not has_confirmation_candle(df, self.direction, CONFIRMATION_CANDLES):
                log.debug(f"{self.pair}/{self.timeframe} No confirmation candle")
                return None

        ref_candle = self.extrem_candle if signal_type == "EXTREM_MHV" else self.csm_candle

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
            log.debug(f"{self.pair}/{self.timeframe} risk FAIL")
            return None

        def _pct(a, b):
            return round((a - b) / b * 100, 2)

        result = SignalResult(
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

        self.last_signal_time = time.time()
        return result
