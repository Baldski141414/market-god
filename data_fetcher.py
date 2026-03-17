"""
data_fetcher.py — Market data retrieval via yfinance + pandas_ta indicators.
"""
import logging
from typing import Optional

import pandas as pd
import yfinance as yf

from config import VIX_SYMBOL
from indicators import ema, rsi as calc_rsi

logger = logging.getLogger(__name__)

# Silence yfinance's noisy progress output
yf.set_tz_cache_location("/tmp/yf_tz")


def fetch_bars(symbol: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV bars and compute EMAs, RSI, and volume MA.
    Returns None on error or insufficient data.
    """
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
        if df.empty or len(df) < 26:
            logger.debug(f"{symbol}: not enough bars ({len(df)})")
            return None

        df.columns = [c.lower() for c in df.columns]
        df.dropna(subset=["close", "volume"], inplace=True)

        df["ema5"] = ema(df["close"], 5)
        df["ema20"] = ema(df["close"], 20)
        df["rsi"] = calc_rsi(df["close"], 14)
        # 30-bar rolling average of 5-minute volume (≈2.5 hours)
        df["vol_ma30"] = df["volume"].rolling(window=30, min_periods=15).mean()
        return df

    except Exception as e:
        logger.error(f"fetch_bars({symbol}): {e}")
        return None


def get_vix() -> Optional[float]:
    """Return the latest VIX close. Returns None on error."""
    try:
        df = yf.Ticker(VIX_SYMBOL).history(period="1d", interval="5m", auto_adjust=True)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"get_vix: {e}")
        return None


def get_30day_avg_volume(symbol: str) -> Optional[float]:
    """
    Fetch 30-day average *daily* volume.
    The signal engine converts this to a per-5m-bar baseline.
    """
    try:
        df = yf.Ticker(symbol).history(period="35d", interval="1d", auto_adjust=True)
        if len(df) < 15:
            return None
        return float(df["Volume"].tail(30).mean())
    except Exception as e:
        logger.error(f"get_30day_avg_volume({symbol}): {e}")
        return None
