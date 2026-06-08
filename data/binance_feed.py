"""
data/binance_feed.py — Binance REST data fetching with rate-limit safety
"""
import time
import requests
import pandas as pd
from typing import Optional

from config.settings import BINANCE_BASE_URL, CANDLE_LIMIT, BATCH_DELAY
from utils.logger import get_logger

log = get_logger("binance_feed")

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


# ── Low-level REST helpers ────────────────────────────────────────────────────

def _get(endpoint: str, params: dict) -> Optional[list]:
    url = f"{BINANCE_BASE_URL}{endpoint}"
    try:
        r = SESSION.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.warning(f"Binance request failed [{endpoint}]: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_symbols() -> list[str]:
    """Return list of all spot trading symbols on Binance."""
    data = _get("/api/v3/exchangeInfo", {})
    if not data:
        return []
    return [
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
    ]


def get_klines(symbol: str, interval: str, limit: int = CANDLE_LIMIT) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candlestick data for a symbol/interval.
    Returns a DataFrame with columns: open_time, open, high, low, close, volume
    """
    raw = _get(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not raw:
        return None

    # Build cleanly in one shot — avoids pandas CoW FutureWarning
    df = pd.DataFrame({
        "open_time": pd.to_datetime([r[0] for r in raw], unit="ms", utc=True),
        "open":   pd.array([r[1] for r in raw], dtype="float64"),
        "high":   pd.array([r[2] for r in raw], dtype="float64"),
        "low":    pd.array([r[3] for r in raw], dtype="float64"),
        "close":  pd.array([r[4] for r in raw], dtype="float64"),
        "volume": pd.array([r[5] for r in raw], dtype="float64"),
    })
    return df.reset_index(drop=True)


def get_current_price(symbol: str) -> Optional[float]:
    """Get the latest price for a symbol."""
    data = _get("/api/v3/ticker/price", {"symbol": symbol})
    if data:
        return float(data["price"])
    return None


def get_24h_tickers(symbols: list[str] | None = None) -> dict[str, dict]:
    """
    Fetch 24-hour ticker stats.
    Returns dict keyed by symbol: {price, change_pct, volume}
    """
    params = {}
    if symbols:
        # Binance accepts a JSON array string for multiple symbols
        import json
        params["symbols"] = json.dumps(symbols)

    raw = _get("/api/v3/ticker/24hr", params)
    if not raw:
        return {}

    result = {}
    for t in (raw if isinstance(raw, list) else [raw]):
        result[t["symbol"]] = {
            "price":      float(t["lastPrice"]),
            "change_pct": float(t["priceChangePercent"]),
            "volume":     float(t["quoteVolume"]),
        }
    return result


# ── Batch helper ──────────────────────────────────────────────────────────────

def fetch_multi_tf(symbol: str, timeframes: list[str]) -> dict[str, pd.DataFrame]:
    """
    Fetch klines for multiple timeframes for one symbol.
    Returns {tf: DataFrame} — missing/failed TFs are excluded.
    """
    result = {}
    for tf in timeframes:
        df = get_klines(symbol, tf)
        if df is not None and len(df) >= 60:
            result[tf] = df
        time.sleep(BATCH_DELAY)
    return result
