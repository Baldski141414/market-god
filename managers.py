"""
managers.py — Portfolio state, position sizing, risk controls, circuit breakers.

All state is persisted to paper_trading.json after every mutation.
"""
import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from config import (
    DAILY_LOSS_LIMIT_PCT,
    LOSS_STREAK_LIMIT,
    LOSS_STREAK_PAUSE_MINUTES,
    MAX_POSITION_PCT,
    MAX_POSITIONS,
    MIN_CASH_PCT,
    RISK_PER_TRADE,
    STARTING_CAPITAL,
    STATE_FILE,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAIL_STOP_PCT,
    TRAIL_TRIGGER_PCT,
)

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    shares: float
    entry_price: float
    entry_time: str
    stop_loss: float
    take_profit: float
    highest_price: float
    trailing_active: bool = False
    entry_reason: str = ""


@dataclass
class Portfolio:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)     # symbol → Position dict
    trades: list = field(default_factory=list)         # completed trade dicts
    daily_start_equity: float = STARTING_CAPITAL
    daily_date: str = ""
    loss_streak: int = 0
    paused_until: Optional[str] = None


class PortfolioManager:
    """Thread-safe portfolio manager (caller must hold external lock if needed)."""

    def __init__(self):
        self.portfolio = Portfolio()
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
            p = raw.get("portfolio", {})
            self.portfolio.cash = p.get("cash", STARTING_CAPITAL)
            self.portfolio.daily_start_equity = p.get("daily_start_equity", STARTING_CAPITAL)
            self.portfolio.daily_date = p.get("daily_date", "")
            self.portfolio.loss_streak = p.get("loss_streak", 0)
            self.portfolio.paused_until = p.get("paused_until")
            for sym, d in p.get("positions", {}).items():
                self.portfolio.positions[sym] = Position(**d)
            self.portfolio.trades = p.get("trades", [])[-500:]
            logger.info(f"State loaded: cash=${self.portfolio.cash:.2f}, "
                        f"{len(self.portfolio.positions)} open positions")
        except FileNotFoundError:
            logger.info("No state file found — starting fresh at $10,000")
            self._save()
        except Exception as e:
            logger.error(f"State load error: {e} — starting fresh")
            self.portfolio = Portfolio()
            self._save()

    def _save(self):
        data = {
            "portfolio": {
                "cash": round(self.portfolio.cash, 4),
                "daily_start_equity": round(self.portfolio.daily_start_equity, 4),
                "daily_date": self.portfolio.daily_date,
                "loss_streak": self.portfolio.loss_streak,
                "paused_until": self.portfolio.paused_until,
                "positions": {
                    sym: asdict(pos)
                    for sym, pos in self.portfolio.positions.items()
                },
                "trades": self.portfolio.trades[-500:],
            }
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"State save error: {e}")

    # ── Computed properties ─────────────────────────────────────────────────────

    def total_equity(self, prices: dict) -> float:
        equity = self.portfolio.cash
        for sym, pos in self.portfolio.positions.items():
            equity += pos.shares * prices.get(sym, pos.entry_price)
        return equity

    def is_paused(self) -> bool:
        if not self.portfolio.paused_until:
            return False
        until = dt.datetime.fromisoformat(self.portfolio.paused_until)
        if dt.datetime.now(dt.timezone.utc) >= until:
            self.portfolio.paused_until = None
            self._save()
            return False
        return True

    # ── Circuit breakers ────────────────────────────────────────────────────────

    def _refresh_daily(self, prices: dict):
        today = dt.date.today().isoformat()
        if self.portfolio.daily_date != today:
            self.portfolio.daily_date = today
            self.portfolio.daily_start_equity = self.total_equity(prices)
            self._save()

    def can_trade(self, prices: dict) -> tuple[bool, str]:
        """Return (ok, reason) — checks all circuit breakers."""
        if self.is_paused():
            remaining = ""
            if self.portfolio.paused_until:
                until = dt.datetime.fromisoformat(self.portfolio.paused_until)
                secs = int((until - dt.datetime.now(dt.timezone.utc)).total_seconds())
                remaining = f" ({secs // 60}m {secs % 60}s left)"
            return False, f"Loss streak pause{remaining}"

        self._refresh_daily(prices)
        equity = self.total_equity(prices)

        daily_chg = (equity - self.portfolio.daily_start_equity) / max(self.portfolio.daily_start_equity, 1)
        if daily_chg <= -DAILY_LOSS_LIMIT_PCT:
            return False, f"Daily loss limit hit ({daily_chg:.1%})"

        if len(self.portfolio.positions) >= MAX_POSITIONS:
            return False, f"Max {MAX_POSITIONS} positions open"

        cash_pct = self.portfolio.cash / max(equity, 1)
        if cash_pct < MIN_CASH_PCT:
            return False, f"Cash reserve low ({cash_pct:.0%} < {MIN_CASH_PCT:.0%})"

        return True, "OK"

    # ── Position sizing ─────────────────────────────────────────────────────────

    def calc_position_size(self, price: float, prices: dict) -> float:
        """
        Risk-based sizing: risk exactly RISK_PER_TRADE of equity at STOP_LOSS_PCT.
        Further capped by MAX_POSITION_PCT and available cash.
        """
        equity = self.total_equity(prices)
        risk_dollars = equity * RISK_PER_TRADE
        risk_per_share = price * STOP_LOSS_PCT
        by_risk = risk_dollars / risk_per_share if risk_per_share > 0 else 0

        by_pct = (equity * MAX_POSITION_PCT) / price if price > 0 else 0
        by_cash = self.portfolio.cash / price if price > 0 else 0

        return max(0.0, round(min(by_risk, by_pct, by_cash), 4))

    # ── Order execution ─────────────────────────────────────────────────────────

    def open_position(self, symbol: str, price: float, reason: str, prices: dict) -> Optional[str]:
        """
        Open a new long position.  Returns None on success, error string on failure.
        """
        if symbol in self.portfolio.positions:
            return f"{symbol}: already in portfolio"

        ok, msg = self.can_trade(prices)
        if not ok:
            return msg

        shares = self.calc_position_size(price, prices)
        cost = shares * price
        if shares <= 0 or cost < 1.0:
            return "Position size too small (insufficient capital or risk params)"

        self.portfolio.cash -= cost
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.portfolio.positions[symbol] = Position(
            symbol=symbol,
            shares=shares,
            entry_price=price,
            entry_time=now,
            stop_loss=round(price * (1 - STOP_LOSS_PCT), 4),
            take_profit=round(price * (1 + TAKE_PROFIT_PCT), 4),
            highest_price=price,
            trailing_active=False,
            entry_reason=reason,
        )
        self._save()
        logger.info(f"BUY  {symbol:6s} {shares:.4f} @ ${price:.2f}  cost=${cost:.2f}  {reason}")
        return None

    def close_position(self, symbol: str, price: float, reason: str):
        """Close a position, record the trade, and update circuit breakers."""
        pos = self.portfolio.positions.get(symbol)
        if not pos:
            return

        proceeds = price * pos.shares
        pnl = proceeds - (pos.entry_price * pos.shares)
        pnl_pct = (price - pos.entry_price) / pos.entry_price

        self.portfolio.cash += proceeds
        self.portfolio.trades.append({
            "symbol": symbol,
            "shares": pos.shares,
            "entry_price": pos.entry_price,
            "exit_price": price,
            "entry_time": pos.entry_time,
            "exit_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
            "entry_reason": pos.entry_reason,
            "exit_reason": reason,
        })

        if pnl < 0:
            self.portfolio.loss_streak += 1
            if self.portfolio.loss_streak >= LOSS_STREAK_LIMIT:
                until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=LOSS_STREAK_PAUSE_MINUTES)
                self.portfolio.paused_until = until.isoformat()
                logger.warning(
                    f"Loss streak {self.portfolio.loss_streak} reached — "
                    f"pausing until {self.portfolio.paused_until}"
                )
        else:
            self.portfolio.loss_streak = 0

        del self.portfolio.positions[symbol]
        self._save()
        logger.info(
            f"SELL {symbol:6s} @ ${price:.2f}  P&L ${pnl:+.2f} ({pnl_pct:+.1%})  {reason}"
        )

    # ── Ongoing position management ─────────────────────────────────────────────

    def update_trailing_stops(self, prices: dict):
        """Ratchet trailing stop up as price rises; never move it down."""
        dirty = False
        for sym, pos in self.portfolio.positions.items():
            price = prices.get(sym, pos.entry_price)
            if price > pos.highest_price:
                pos.highest_price = price
                dirty = True
            gain = (price - pos.entry_price) / pos.entry_price
            if gain >= TRAIL_TRIGGER_PCT and not pos.trailing_active:
                pos.trailing_active = True
                dirty = True
                logger.info(f"{sym}: trailing stop activated at gain +{gain:.1%}")
            if pos.trailing_active:
                new_stop = round(pos.highest_price * (1 - TRAIL_STOP_PCT), 4)
                if new_stop > pos.stop_loss:
                    pos.stop_loss = new_stop
                    dirty = True
        if dirty:
            self._save()

    def check_exits(self, prices: dict) -> list[dict]:
        """
        Evaluate every open position against SL/TP.
        Returns list of closed-trade dicts for logging.
        """
        exits = []
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions.get(symbol)
            if not pos:
                continue
            price = prices.get(symbol, pos.entry_price)
            reason = None
            if price <= pos.stop_loss:
                reason = f"Stop loss (${pos.stop_loss:.2f})"
            elif price >= pos.take_profit:
                reason = f"Take profit (${pos.take_profit:.2f})"
            if reason:
                self.close_position(symbol, price, reason)
                exits.append({"symbol": symbol, "price": price, "reason": reason})
        return exits

    # ── Stats helpers ───────────────────────────────────────────────────────────

    def stats(self) -> dict:
        trades = self.portfolio.trades
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "avg_win": 0, "avg_loss": 0, "profit_factor": 0}
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0,
            "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else 0,
        }
