"""
Pure numpy technical indicators — zero external API calls.
All functions take a list/array of floats and return a float.
Designed for <1ms execution on 200-500 data points.
"""
import math
from typing import Optional


def _arr(prices) -> list[float]:
    return [float(p) for p in prices if p is not None and not math.isnan(float(p))]


def rsi(prices, period: int = 14) -> Optional[float]:
    """RSI 0-100. Returns None if insufficient data."""
    p = _arr(prices)
    if len(p) < period + 1:
        return None
    p = p[-(period * 3):]  # use last 3x period for accuracy
    gains, losses = [], []
    for i in range(1, len(p)):
        delta = p[i] - p[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def ema(prices, period: int) -> Optional[float]:
    """Exponential moving average — returns latest value."""
    p = _arr(prices)
    if len(p) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(p[:period]) / period
    for price in p[period:]:
        val = price * k + val * (1 - k)
    return round(val, 6)


def macd(prices, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram) or (None, None, None)."""
    p = _arr(prices)
    if len(p) < slow + signal:
        return None, None, None
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    ema_fast = sum(p[:fast]) / fast
    ema_slow = sum(p[:slow]) / slow
    macd_vals = []
    for price in p[slow:]:
        ema_fast = price * k_fast + ema_fast * (1 - k_fast)
        ema_slow = price * k_slow + ema_slow * (1 - k_slow)
        macd_vals.append(ema_fast - ema_slow)
    if len(macd_vals) < signal:
        return None, None, None
    k_sig = 2.0 / (signal + 1)
    sig_val = sum(macd_vals[:signal]) / signal
    for mv in macd_vals[signal:]:
        sig_val = mv * k_sig + sig_val * (1 - k_sig)
    macd_line = macd_vals[-1]
    hist = macd_line - sig_val
    return round(macd_line, 6), round(sig_val, 6), round(hist, 6)


def bollinger(prices, period: int = 20, std_dev: float = 2.0):
    """Returns (upper, middle, lower, pct_b) or (None, None, None, None).
    pct_b: 0=lower band, 0.5=middle, 1=upper band, can exceed 0-1.
    """
    p = _arr(prices)
    if len(p) < period:
        return None, None, None, None
    window = p[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    current = p[-1]
    band_width = upper - lower
    pct_b = (current - lower) / band_width if band_width != 0 else 0.5
    return round(upper, 6), round(mid, 6), round(lower, 6), round(pct_b, 4)


def ema_crossover(prices, fast: int = 50, slow: int = 200) -> Optional[float]:
    """
    Returns a score between -1 and +1 based on EMA50/EMA200 spread.
    Positive = bullish, Negative = bearish.
    """
    p = _arr(prices)
    ema_fast = ema(p, fast)
    ema_slow = ema(p, slow)
    if ema_fast is None or ema_slow is None:
        return None
    if ema_slow == 0:
        return 0.0
    spread = (ema_fast - ema_slow) / ema_slow
    # Clip to ±5% and scale to ±1
    return max(-1.0, min(1.0, spread / 0.05))


def momentum(prices, lookback: int) -> Optional[float]:
    """
    Price momentum over last N bars.
    Returns value between -1 and +1 (clipped at ±10%).
    """
    p = _arr(prices)
    if len(p) < lookback + 1:
        return None
    past = p[-(lookback + 1)]
    current = p[-1]
    if past == 0:
        return 0.0
    pct = (current - past) / past
    return max(-1.0, min(1.0, pct / 0.10))


def volume_surge(volumes, lookback: int = 20) -> Optional[float]:
    """
    Compare latest volume to rolling average.
    Returns -1 to +1 (1 = 3x+ surge, -1 = near zero).
    """
    v = _arr(volumes)
    if len(v) < lookback + 1:
        return None
    avg = sum(v[-lookback - 1:-1]) / lookback
    if avg == 0:
        return 0.0
    ratio = v[-1] / avg
    # ratio 3x = +1, ratio 0.3x = -1
    return max(-1.0, min(1.0, (ratio - 1.0) / 2.0))
