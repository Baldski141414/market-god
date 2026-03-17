"""
indicators.py — Pure-pandas EMA and RSI (no numba/pandas_ta dependency).
"""
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average using pandas ewm (standard Wilder alpha)."""
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """RSI via Wilder smoothing (RMA = EMA with alpha=1/length)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))
