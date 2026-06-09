"""
core/bbma.py — BBMA Oma Ally indicator calculations
  • Bollinger Bands : Period 20, Deviation 2, applied to Close
  • MA5  High/Low   : Period 5,  Linear Weighted MA
  • MA10 High/Low   : Period 10, Linear Weighted MA
  • MA50            : Period 50, EMA of Close  (trend anchor)
"""
import numpy as np
import pandas as pd

from config.settings import BB_PERIOD, BB_DEVIATION, MA5_PERIOD, MA10_PERIOD, MA50_PERIOD


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lwma(series: pd.Series, period: int) -> pd.Series:
    """Linear Weighted Moving Average (LWMA)."""
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True
    )


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


# ── Main ──────────────────────────────────────────────────────────────────────

def calculate_bbma(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append BBMA columns to *df* and return it.
    Requires: open, high, low, close columns.
    """
    df = df.copy()

    # ── Bollinger Bands ──
    close = df["close"]
    df["bb_middle"] = close.rolling(BB_PERIOD).mean()
    _std             = close.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"]  = df["bb_middle"] + BB_DEVIATION * _std
    df["bb_lower"]  = df["bb_middle"] - BB_DEVIATION * _std
    df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]

    # ── Moving Averages ──
    df["ma5_high"]  = _lwma(df["high"], MA5_PERIOD)
    df["ma5_low"]   = _lwma(df["low"],  MA5_PERIOD)
    df["ma10_high"] = _lwma(df["high"], MA10_PERIOD)
    df["ma10_low"]  = _lwma(df["low"],  MA10_PERIOD)
    df["ma50"]      = _ema(close,       MA50_PERIOD)

    # ── Derived helpers ──
    df["above_ma50"] = df["close"] > df["ma50"]

    return df.dropna().reset_index(drop=True)


def get_trend_direction(df: pd.DataFrame) -> str:
    """
    BBMA multi-candle trend direction.
    Uses last 3 candles to confirm:
      BULLISH  → close > MA50 AND MA5/MA10 Low family above BB Middle
      BEARISH  → close < MA50 AND MA5/MA10 High family below BB Middle
      NEUTRAL  → mixed / transitional
    """
    if len(df) < 3:
        return "NEUTRAL"

    recent = df.iloc[-3:]

    def _bullish(r):
        return (
            r["close"] > r["ma50"]
            and r["ma5_low"] > r["bb_lower"]
            and r["ma10_low"] > r["bb_lower"]
        )

    def _bearish(r):
        return (
            r["close"] < r["ma50"]
            and r["ma5_high"] < r["bb_upper"]
            and r["ma10_high"] < r["bb_upper"]
        )

    bull_count = sum(_bullish(row) for _, row in recent.iterrows())
    bear_count = sum(_bearish(row) for _, row in recent.iterrows())

    if bull_count >= 2:
        return "BULLISH"
    if bear_count >= 2:
        return "BEARISH"
    return "NEUTRAL"

    def is_trending_market(df: pd.DataFrame, min_width: float = 0.02) -> bool:
        """
        Check if BB width indicates trending market (bukan sideways).
        Returns True jika BB width > min_width dan melebar berbanding purata.
        """
        if len(df) < 20:
            return False
    
        current_width = df["bb_width"].iloc[-1]
        avg_width = df["bb_width"].rolling(20).mean().iloc[-1]
    
        # Trending jika BB width > 80% dari purata DAN melebihi min threshold
        return current_width > avg_width * 0.8 and current_width > min_width

    def has_confirmation_candle(df: pd.DataFrame, direction: str, num_candles: int = 2) -> bool:
        """
        Check untuk confirmation candle selepas harga sentuh MA zone.
        BUY: Close > MA5 Low dan close menaik berturut-turut
        SELL: Close < MA5 High dan close menurun berturut-turut
        """
        if len(df) < num_candles + 1:
            return False
    
        recent = df.iloc[-(num_candles+1):]
    
        if direction == "BUY":
            # Semua candle recent mesti close > MA5 Low dan close menaik
            ma5_ok = all(r["close"] > r["ma5_low"] for _, r in recent.iterrows())
            close_rising = all(
                recent.iloc[i]["close"] < recent.iloc[i+1]["close"]
                for i in range(len(recent)-1)
            )
            return ma5_ok and close_rising
    else:  # SELL
        # Semua candle recent mesti close < MA5 High dan close menurun
        ma5_ok = all(r["close"] < r["ma5_high"] for _, r in recent.iterrows())
        close_falling = all(
            recent.iloc[i]["close"] > recent.iloc[i+1]["close"]
            for i in range(len(recent)-1)
        )
        return ma5_ok and close_falling

