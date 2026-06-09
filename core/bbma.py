import numpy as np
import pandas as pd
from config.settings import BB_PERIOD, BB_DEVIATION, MA5_PERIOD, MA10_PERIOD, MA50_PERIOD


def _lwma(series, period):
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True
    )


def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calculate_bbma(df):
    df = df.copy()
    close = df["close"]
    df["bb_middle"] = close.rolling(BB_PERIOD).mean()
    _std = close.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = df["bb_middle"] + BB_DEVIATION * _std
    df["bb_lower"] = df["bb_middle"] - BB_DEVIATION * _std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]

    df["ma5_high"] = _lwma(df["high"], MA5_PERIOD)
    df["ma5_low"] = _lwma(df["low"], MA5_PERIOD)
    df["ma10_high"] = _lwma(df["high"], MA10_PERIOD)
    df["ma10_low"] = _lwma(df["low"], MA10_PERIOD)
    df["ma50"] = _ema(close, MA50_PERIOD)
    df["above_ma50"] = df["close"] > df["ma50"]

    return df.dropna().reset_index(drop=True)


def get_trend_direction(df):
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


def is_trending_market(df, min_width=0.02):
    if len(df) < 20:
        return False
    current_width = df["bb_width"].iloc[-1]
    avg_width = df["bb_width"].rolling(20).mean().iloc[-1]
    return current_width > avg_width * 0.8 and current_width > min_width


def has_confirmation_candle(df, direction, num_candles=2):
    if len(df) < num_candles + 1:
        return False
    recent = df.iloc[-(num_candles + 1):]
    if direction == "BUY":
        ma5_ok = all(r["close"] > r["ma5_low"] for _, r in recent.iterrows())
        close_rising = all(
            recent.iloc[i]["close"] < recent.iloc[i + 1]["close"]
            for i in range(len(recent) - 1)
        )
        return ma5_ok and close_rising
    else:
        ma5_ok = all(r["close"] < r["ma5_high"] for _, r in recent.iterrows())
        close_falling = all(
            recent.iloc[i]["close"] > recent.iloc[i + 1]["close"]
            for i in range(len(recent) - 1)
        )
        return ma5_ok and close_falling
