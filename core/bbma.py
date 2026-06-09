"""
core/bbma.py — BBMA Oma Ally indicator calculations (v2)

Indicator settings per official SOP — DO NOT change these values:
  Bollinger Bands : Period 20, Deviation 2, SMA of Close
  MA5  High/Low   : Period  5, Linear Weighted MA (LWMA) of High / Low
  MA10 High/Low   : Period 10, Linear Weighted MA (LWMA) of High / Low
  MA50            : Period 50, EMA of Close  (long-term trend anchor only)

Two Absolute Laws of BBMA (mutually exclusive):
  Law 1 — MA  CANNOT close outside BB  → if it does = EXTREME
  Law 2 — Candle body CANNOT close outside BB → if it does = CSM

Market Cycle (must be respected in order):
  EXTREME → MHV → CSA → RE-ENTRY  (EXTREM_MHV setup)
  CSM     → RE-ENTRY               (CSM_REENTRY setup)
"""
import numpy as np
import pandas as pd
from config.settings import BB_PERIOD, BB_DEVIATION, MA5_PERIOD, MA10_PERIOD, MA50_PERIOD


# ── Low-level MA helpers ──────────────────────────────────────────────────────
def _lwma(series: pd.Series, period: int) -> pd.Series:
    """
    Linear Weighted Moving Average (LWMA).
    Most recent candle receives the highest weight (= period).
    Required by SOP for MA5/MA10 High & Low.
    """
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True
    )


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average — used for MA50 (trend anchor) only."""
    return series.ewm(span=period, adjust=False).mean()


# ── Main calculator ───────────────────────────────────────────────────────────
def calculate_bbma(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append all BBMA indicator columns to df and return it.
    Requires input columns: open, high, low, close.

    Added columns:
      bb_upper, bb_middle, bb_lower, bb_width
      ma5_high, ma5_low, ma10_high, ma10_low, ma50
      above_ma50   (bool helper — price side of MA50)
    """
    df = df.copy()

    # ── Bollinger Bands (SMA-20 of Close, ±2σ) ──────────────────────────────
    close = df["close"]
    df["bb_middle"] = close.rolling(BB_PERIOD).mean()
    _std            = close.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"]  = df["bb_middle"] + BB_DEVIATION * _std
    df["bb_lower"]  = df["bb_middle"] - BB_DEVIATION * _std
    # Width as a ratio — used for ranging-market filter (MIN_BB_WIDTH_PERCENT)
    df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]

    # ── Moving Averages ──────────────────────────────────────────────────────
    df["ma5_high"]  = _lwma(df["high"], MA5_PERIOD)    # LWMA of High
    df["ma5_low"]   = _lwma(df["low"],  MA5_PERIOD)    # LWMA of Low
    df["ma10_high"] = _lwma(df["high"], MA10_PERIOD)   # LWMA of High
    df["ma10_low"]  = _lwma(df["low"],  MA10_PERIOD)   # LWMA of Low
    df["ma50"]      = _ema(close, MA50_PERIOD)          # EMA of Close

    # ── Derived helper ───────────────────────────────────────────────────────
    df["above_ma50"] = df["close"] > df["ma50"]

    return df.dropna().reset_index(drop=True)


# ── Single-row phase detectors ────────────────────────────────────────────────
# These operate on a single pd.Series (one candle row) and are used by the
# state machine in signals.py as well as the dashboard / diagnostics layer.

def detect_extreme(row: pd.Series) -> dict:
    """
    Law 1 — EXTREME: MA5 or MA10 crosses OUTSIDE Bollinger Bands.
    The candle BODY (close) must still be INSIDE BB.
    If the body has already closed outside BB → that is CSM (Law 2), not Extreme.
    The two laws are mutually exclusive on the same candle.

    Returns: {"buy": bool, "sell": bool}
      buy  → MA Low outside BB Lower  (oversold extreme — BUY setup incoming)
      sell → MA High outside BB Upper (overbought extreme — SELL setup incoming)
    """
    ext_buy = bool(
        (row["ma5_low"] < row["bb_lower"] or row["ma10_low"] < row["bb_lower"])
        and row["close"] > row["bb_lower"]   # body still inside BB — not CSM Sell
    )
    ext_sell = bool(
        (row["ma5_high"] > row["bb_upper"] or row["ma10_high"] > row["bb_upper"])
        and row["close"] < row["bb_upper"]   # body still inside BB — not CSM Buy
    )
    return {"buy": ext_buy, "sell": ext_sell}


def detect_mhv(row: pd.Series, direction: str) -> bool:
    """
    MHV (Market Hilang Volume): After Extreme, MA returns INSIDE BB.
    Signals that old-trend momentum is exhausted; direction change expected.

    direction : "BUY"  → check MA Low returned above BB Lower
                "SELL" → check MA High returned below BB Upper
    """
    if direction == "BUY":
        return bool(
            row["ma5_low"]  > row["bb_lower"]
            and row["ma10_low"] > row["bb_lower"]
        )
    # SELL
    return bool(
        row["ma5_high"]  < row["bb_upper"]
        and row["ma10_high"] < row["bb_upper"]
    )


def detect_csa(row: pd.Series, direction: str) -> dict:
    """
    CSA (Candlestick Arah / Direction): Confirms the NEW direction after
    Extreme + MHV.  Candle close crosses past MA5 & MA10 in the expected
    direction, while staying inside BB (not yet CSM).

    This is NOT a trade entry — it is purely a directional compass.

    CSA Early  → Close crosses MA5 & MA10 (still inside BB)
    CSA Strong → Close also crosses Mid BB (stronger confirmation)

    direction : "BUY"  → expect price moving UP through MA5/MA10 Low
                "SELL" → expect price moving DOWN through MA5/MA10 High

    Returns: {"early": bool, "strong": bool}
    """
    if direction == "BUY":
        early = bool(
            row["close"] > row["ma5_low"]
            and row["close"] > row["ma10_low"]
            and row["close"] < row["bb_upper"]      # body inside BB (not CSM Buy)
            and row["ma5_low"]  > row["bb_lower"]   # MA already returned inside BB
            and row["ma10_low"] > row["bb_lower"]
        )
        strong = early and bool(row["close"] > row["bb_middle"])
    else:  # SELL
        early = bool(
            row["close"] < row["ma5_high"]
            and row["close"] < row["ma10_high"]
            and row["close"] > row["bb_lower"]       # body inside BB (not CSM Sell)
            and row["ma5_high"]  < row["bb_upper"]   # MA already returned inside BB
            and row["ma10_high"] < row["bb_upper"]
        )
        strong = early and bool(row["close"] < row["bb_middle"])

    return {"early": bool(early), "strong": bool(strong)}


def detect_csm(row: pd.Series) -> dict:
    """
    Law 2 — CSM (Candlestick Momentum): Candle BODY (close) closes OUTSIDE BB.
    Signals strong trend continuation / high volatility.
    After CSM: do NOT chase price — wait for Re-Entry pullback at MA5/MA10.

    Returns: {"buy": bool, "sell": bool}
    """
    return {
        "buy":  bool(row["close"] > row["bb_upper"]),
        "sell": bool(row["close"] < row["bb_lower"]),
    }


# ── Multi-candle helpers ──────────────────────────────────────────────────────
def get_trend_direction(df: pd.DataFrame) -> str:
    """
    BBMA trend direction via MA50 — the sole long-term trend anchor per SOP.
    Uses majority vote over the last 3 candles for stability.

    Rule: Trade in the direction of MA50.
      BULLISH → price above MA50 (2 of last 3 candles)
      BEARISH → price below MA50 (2 of last 3 candles)
      NEUTRAL → mixed (do not enter counter-trend)

    Returns: "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    if len(df) < 3:
        return "NEUTRAL"

    recent     = df.iloc[-3:]
    bull_count = int((recent["close"] > recent["ma50"]).sum())
    bear_count = int((recent["close"] < recent["ma50"]).sum())

    if bull_count >= 2:
        return "BULLISH"
    if bear_count >= 2:
        return "BEARISH"
    return "NEUTRAL"


def get_market_phase(df: pd.DataFrame) -> dict:
    """
    Detect the current BBMA market phase on the latest candle.
    Useful for dashboard display, logging, and multi-TF analysis.

    Phase priority (evaluated in order — first match wins):
      EXTREME_BUY   / EXTREME_SELL   — Law 1: MA outside BB
      CSM_BUY       / CSM_SELL       — Law 2: body close outside BB
      CSA_BUY_STRONG/ CSA_SELL_STRONG — Close past MA5, MA10 AND Mid BB
      CSA_BUY_EARLY / CSA_SELL_EARLY  — Close past MA5 & MA10 (inside BB)
      NORMAL                          — All indicators within normal BB range

    Returns: {"phase": str, "trend": str}
    """
    if len(df) < 1:
        return {"phase": "UNKNOWN", "trend": "NEUTRAL"}

    row   = df.iloc[-1]
    trend = get_trend_direction(df)

    ext = detect_extreme(row)
    csm = detect_csm(row)

    if ext["buy"]:
        phase = "EXTREME_BUY"
    elif ext["sell"]:
        phase = "EXTREME_SELL"
    elif csm["buy"]:
        phase = "CSM_BUY"
    elif csm["sell"]:
        phase = "CSM_SELL"
    else:
        csa_buy  = detect_csa(row, "BUY")
        csa_sell = detect_csa(row, "SELL")
        if csa_buy["strong"]:
            phase = "CSA_BUY_STRONG"
        elif csa_buy["early"]:
            phase = "CSA_BUY_EARLY"
        elif csa_sell["strong"]:
            phase = "CSA_SELL_STRONG"
        elif csa_sell["early"]:
            phase = "CSA_SELL_EARLY"
        else:
            phase = "NORMAL"

    return {"phase": phase, "trend": trend}
