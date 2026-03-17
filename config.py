"""
config.py — All constants for Market God trading system.
"""
from typing import Final

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST: Final[list[str]] = [
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN",
    "META", "AMD", "GOOGL", "NFLX", "COIN",
]
VIX_SYMBOL: Final[str] = "^VIX"

# ── Engine ─────────────────────────────────────────────────────────────────────
POLL_INTERVAL: Final[int] = 2          # seconds between full scan loops
AVG_VOL_REFRESH: Final[int] = 600      # seconds between 30-day vol refreshes

# ── Signal thresholds ──────────────────────────────────────────────────────────
MIN_CONDITIONS: Final[int] = 3          # minimum conditions that must be true
MIN_QUALITY_SCORE: Final[int] = 35      # minimum quality score to trade
VOLUME_MULTIPLIER: Final[float] = 1.5  # vol must exceed 1.5× 5m-bar average
STRONG_VOLUME_MULTIPLIER: Final[float] = 2.0  # threshold for strong-vol bonus
RSI_MAX: Final[float] = 70.0            # RSI must be below this
VIX_MAX: Final[float] = 25.0           # VIX must be below this
STRONG_EMA_DIFF_PCT: Final[float] = 0.003  # ema5-ema20 spread for strong-momentum bonus

# ── Capital & risk ─────────────────────────────────────────────────────────────
STARTING_CAPITAL: Final[float] = 10_000.0
MAX_POSITIONS: Final[int] = 5
MAX_POSITION_PCT: Final[float] = 0.15   # ≤15% of equity per position
MIN_CASH_PCT: Final[float] = 0.25       # always keep ≥25% cash
RISK_PER_TRADE: Final[float] = 0.02     # risk 2% of portfolio per trade
STOP_LOSS_PCT: Final[float] = 0.03      # 3% hard stop loss
TAKE_PROFIT_PCT: Final[float] = 0.12    # 12% take profit
TRAIL_TRIGGER_PCT: Final[float] = 0.06  # activate trailing after +6%
TRAIL_STOP_PCT: Final[float] = 0.05     # trailing stop trails 5% below peak

# ── Circuit breakers ───────────────────────────────────────────────────────────
LOSS_STREAK_LIMIT: Final[int] = 3
LOSS_STREAK_PAUSE_MINUTES: Final[int] = 30
DAILY_LOSS_LIMIT_PCT: Final[float] = 0.05   # halt trading if down 5% on the day

# ── Storage ────────────────────────────────────────────────────────────────────
STATE_FILE: Final[str] = "paper_trading.json"
LOG_FILE: Final[str] = "market_god.log"
