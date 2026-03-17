"""
signal_engine.py — Evaluates 6 conditions per symbol and computes a quality score.

Trade gate: conditions_met >= MIN_CONDITIONS AND quality_score >= MIN_QUALITY_SCORE

Conditions
----------
1. Momentum  : close > prev_close  AND  EMA(5) > EMA(20)
2. Volume    : current bar volume > VOLUME_MULTIPLIER × 30-day daily avg / 78
3. RSI       : RSI(14) < RSI_MAX (70)
4. Catalyst  : external signal (mock for MVP; plug in Unusual Whales / FMP later)
5. Regime    : VIX < VIX_MAX (25)
6. No-pos    : no existing position in this symbol

Quality score = (conditions_met × 10) + bonus_catalyst(+2) + bonus_strong_vol(+1) + bonus_strong_momentum(+1)

To reach 35 you need either:
  • 4+ conditions (40+), OR
  • 3 conditions (30) + external catalyst (2) + one strong-bonus (1) = 33  … still short,
    meaning the bar is intentionally high: 4 conditions = instant qualify.
"""
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config import (
    MIN_CONDITIONS,
    MIN_QUALITY_SCORE,
    VOLUME_MULTIPLIER,
    STRONG_VOLUME_MULTIPLIER,
    RSI_MAX,
    VIX_MAX,
    STRONG_EMA_DIFF_PCT,
)

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    symbol: str
    signal: bool            # True → BUY
    quality_score: int
    conditions_met: int
    conditions: dict        # name → bool
    bonuses: dict           # name → int
    reason: str             # human-readable breakdown shown on dashboard
    price: float
    timestamp: str


def evaluate_signal(
    symbol: str,
    df: pd.DataFrame,
    avg_daily_volume: float,
    vix: Optional[float],
    has_position: bool,
    mock_catalyst: bool = False,
) -> SignalResult:
    """
    Evaluate all 6 conditions and compute the quality score.
    Returns a SignalResult with signal=True only when all gates pass.
    """
    if df is None or len(df) < 5:
        return _null(symbol, "Insufficient data", 0.0)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(latest["close"])

    conditions: dict[str, bool] = {}
    bonuses: dict[str, int] = {}

    # ── 1. Momentum ──────────────────────────────────────────────────────────
    ema5 = latest.get("ema5")
    ema20 = latest.get("ema20")
    momentum_ok = (
        price > float(prev["close"])
        and not pd.isna(ema5)
        and not pd.isna(ema20)
        and float(ema5) > float(ema20)
    )
    conditions["momentum"] = momentum_ok

    # ── 2. Volume ─────────────────────────────────────────────────────────────
    # Convert daily avg → per-5m-bar avg (390 trading minutes / 5 = 78 bars/day)
    bar_vol_avg = avg_daily_volume / 78.0 if avg_daily_volume and avg_daily_volume > 0 else 0.0
    curr_vol = float(latest["volume"])
    vol_ok = bar_vol_avg > 0 and curr_vol > VOLUME_MULTIPLIER * bar_vol_avg
    conditions["volume"] = vol_ok

    # ── 3. RSI < 70 ───────────────────────────────────────────────────────────
    rsi = latest.get("rsi")
    rsi_ok = not pd.isna(rsi) and float(rsi) < RSI_MAX
    conditions["rsi"] = rsi_ok

    # ── 4. External catalyst (mock; TODO: real API) ───────────────────────────
    conditions["catalyst"] = mock_catalyst

    # ── 5. Regime: VIX < 25 ──────────────────────────────────────────────────
    conditions["regime"] = vix is not None and vix < VIX_MAX

    # ── 6. No existing position ───────────────────────────────────────────────
    conditions["no_position"] = not has_position

    # ── Count & score ─────────────────────────────────────────────────────────
    met_keys = [k for k, v in conditions.items() if v]
    conditions_met = len(met_keys)

    # Bonuses
    if mock_catalyst:
        bonuses["catalyst"] = 2
    if bar_vol_avg > 0 and curr_vol > STRONG_VOLUME_MULTIPLIER * bar_vol_avg:
        bonuses["strong_volume"] = 1
    if (
        not pd.isna(ema5)
        and not pd.isna(ema20)
        and float(ema20) > 0
        and (float(ema5) - float(ema20)) / float(ema20) > STRONG_EMA_DIFF_PCT
    ):
        bonuses["strong_momentum"] = 1

    quality_score = conditions_met * 10 + sum(bonuses.values())
    signal = conditions_met >= MIN_CONDITIONS and quality_score >= MIN_QUALITY_SCORE

    # ── Build human-readable reason ───────────────────────────────────────────
    parts = []
    for name, ok in conditions.items():
        icon = "✓" if ok else "✗"
        label = name.replace("_", " ").title()
        if name == "volume" and bar_vol_avg > 0:
            label += f" ({curr_vol / bar_vol_avg:.1f}×)"
        elif name == "rsi" and not pd.isna(rsi):
            label += f" ({rsi:.1f})"
        elif name == "regime" and vix is not None:
            label += f" (VIX {vix:.1f})"
        parts.append(f"{icon} {label}")
    if bonuses:
        parts.append(f"[bonus +{sum(bonuses.values())}: {', '.join(bonuses.keys())}]")
    parts.append(f"→ score {quality_score}")
    if not signal:
        if conditions_met < MIN_CONDITIONS:
            parts.append(f"[SKIP: {conditions_met} conds < {MIN_CONDITIONS}]")
        else:
            parts.append(f"[SKIP: score {quality_score} < {MIN_QUALITY_SCORE}]")

    return SignalResult(
        symbol=symbol,
        signal=signal,
        quality_score=quality_score,
        conditions_met=conditions_met,
        conditions=conditions,
        bonuses=bonuses,
        reason=" | ".join(parts),
        price=price,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def _null(symbol: str, reason: str, price: float) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        signal=False,
        quality_score=0,
        conditions_met=0,
        conditions={},
        bonuses={},
        reason=reason,
        price=price,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
