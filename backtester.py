"""
backtester.py — Historical simulation of the Market God signal engine.

Usage:
    python backtester.py                          # default symbols, 60 days
    python backtester.py --symbols AAPL NVDA TSLA --days 90
    python backtester.py --symbols AAPL --days 30 --csv results.csv

Output:
    Per-symbol stats + overall report: win rate, profit factor, max drawdown,
    Sharpe ratio, avg hold time.
"""
import argparse
import csv
import datetime as dt
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import yfinance as yf

from indicators import ema, rsi as calc_rsi
from config import (
    MIN_CONDITIONS, MIN_QUALITY_SCORE,
    STARTING_CAPITAL, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    TRAIL_TRIGGER_PCT, TRAIL_STOP_PCT,
    VOLUME_MULTIPLIER, STRONG_VOLUME_MULTIPLIER,
    RSI_MAX, VIX_MAX, STRONG_EMA_DIFF_PCT,
    RISK_PER_TRADE, MAX_POSITION_PCT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backtester")

# ── Data helpers ───────────────────────────────────────────────────────────────

def fetch_history(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Download 5-minute bars for the last `days` calendar days."""
    # yfinance limits 5m data to ~60 days; use 1m for shorter windows
    try:
        df = yf.Ticker(symbol).history(period=f"{days}d", interval="5m", auto_adjust=True)
        if df.empty or len(df) < 50:
            logger.warning(f"{symbol}: insufficient history")
            return None
        df.columns = [c.lower() for c in df.columns]
        df.sort_index(inplace=True)
        df["ema5"] = ema(df["close"], 5)
        df["ema20"] = ema(df["close"], 20)
        df["rsi"] = calc_rsi(df["close"], 14)
        df["vol_ma30"] = df["volume"].rolling(window=30, min_periods=15).mean()
        return df
    except Exception as e:
        logger.error(f"fetch_history({symbol}): {e}")
        return None


def fetch_vix_history(days: int) -> dict:
    """Return {date_str: vix_close} mapping for VIX lookups by bar date."""
    try:
        df = yf.Ticker("^VIX").history(period=f"{days+5}d", interval="1d", auto_adjust=True)
        if df.empty:
            return {}
        result = {}
        for ts, row in df.iterrows():
            d = ts.date().isoformat()
            result[d] = float(row["Close"])
        return result
    except Exception:
        return {}


# ── Signal evaluation (standalone, no live feeds) ─────────────────────────────

@dataclass
class BtSignal:
    signal: bool
    score: int
    conditions_met: int
    price: float
    reason: str


def evaluate(
    idx: int,
    df: pd.DataFrame,
    avg_daily_vol: float,
    vix: Optional[float],
    has_position: bool,
) -> BtSignal:
    if idx < 2:
        return BtSignal(False, 0, 0, 0.0, "warmup")

    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    price = float(row["close"])

    conds: dict[str, bool] = {}

    # Momentum
    ema5 = row.get("ema5")
    ema20 = row.get("ema20")
    conds["momentum"] = (
        price > float(prev["close"])
        and pd.notna(ema5) and pd.notna(ema20)
        and float(ema5) > float(ema20)
    )

    # Volume
    bar_avg = avg_daily_vol / 78.0 if avg_daily_vol > 0 else 0.0
    curr_vol = float(row["volume"])
    conds["volume"] = bar_avg > 0 and curr_vol > VOLUME_MULTIPLIER * bar_avg

    # RSI
    rsi = row.get("rsi")
    conds["rsi"] = pd.notna(rsi) and float(rsi) < RSI_MAX

    # Catalyst (disabled in backtester)
    conds["catalyst"] = False

    # Regime
    conds["regime"] = vix is not None and vix < VIX_MAX

    # No position
    conds["no_position"] = not has_position

    met = sum(1 for v in conds.values() if v)

    # Bonuses
    bonus = 0
    if bar_avg > 0 and curr_vol > STRONG_VOLUME_MULTIPLIER * bar_avg:
        bonus += 1
    if (
        pd.notna(ema5) and pd.notna(ema20) and float(ema20) > 0
        and (float(ema5) - float(ema20)) / float(ema20) > STRONG_EMA_DIFF_PCT
    ):
        bonus += 1

    score = met * 10 + bonus
    signal = met >= MIN_CONDITIONS and score >= MIN_QUALITY_SCORE

    reason_parts = [f"{k}={'Y' if v else 'N'}" for k, v in conds.items()]
    reason = " ".join(reason_parts) + f" score={score}"
    return BtSignal(signal=signal, score=score, conditions_met=met, price=price, reason=reason)


# ── Trade simulation ───────────────────────────────────────────────────────────

@dataclass
class BtTrade:
    symbol: str
    entry_idx: int
    entry_price: float
    entry_time: str
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    hold_bars: int = 0
    score: int = 0


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    avg_daily_vol: float,
    vix_map: dict,
    equity: float,
) -> list[BtTrade]:
    """Simulate trades on a single symbol. Returns list of completed trades."""
    trades: list[BtTrade] = []
    open_trade: Optional[BtTrade] = None
    highest_price = 0.0
    trailing_active = False

    for i in range(25, len(df)):
        row = df.iloc[i]
        price = float(row["close"])
        ts = str(df.index[i])
        date = str(df.index[i])[:10]
        vix = vix_map.get(date)

        if open_trade:
            # Update trailing
            if price > highest_price:
                highest_price = price
            gain = (price - open_trade.entry_price) / open_trade.entry_price
            if gain >= TRAIL_TRIGGER_PCT:
                trailing_active = True
            stop = open_trade.entry_price * (1 - STOP_LOSS_PCT)
            if trailing_active:
                trail_stop = highest_price * (1 - TRAIL_STOP_PCT)
                stop = max(stop, trail_stop)

            exit_reason = None
            if price <= stop:
                exit_reason = f"SL ${stop:.2f}"
            elif price >= open_trade.entry_price * (1 + TAKE_PROFIT_PCT):
                exit_reason = f"TP ${open_trade.entry_price * (1 + TAKE_PROFIT_PCT):.2f}"

            if exit_reason:
                pnl = (price - open_trade.entry_price) / open_trade.entry_price
                risk_dollars = equity * RISK_PER_TRADE
                shares = risk_dollars / (open_trade.entry_price * STOP_LOSS_PCT)
                shares = min(shares, equity * MAX_POSITION_PCT / open_trade.entry_price)
                dollar_pnl = pnl * shares * open_trade.entry_price
                equity += dollar_pnl

                open_trade.exit_idx = i
                open_trade.exit_price = price
                open_trade.exit_time = ts
                open_trade.exit_reason = exit_reason
                open_trade.pnl_pct = round(pnl * 100, 2)
                open_trade.pnl = round(dollar_pnl, 2)
                open_trade.hold_bars = i - open_trade.entry_idx
                trades.append(open_trade)
                open_trade = None
                trailing_active = False
                highest_price = 0.0
        else:
            sig = evaluate(i, df, avg_daily_vol, vix, has_position=False)
            if sig.signal:
                open_trade = BtTrade(
                    symbol=symbol,
                    entry_idx=i,
                    entry_price=price,
                    entry_time=ts,
                    score=sig.score,
                )
                highest_price = price

    return trades


# ── Reporting ──────────────────────────────────────────────────────────────────

def report(all_trades: list[BtTrade], starting_equity: float):
    if not all_trades:
        print("\n⚠  No trades generated. Check signal thresholds or data availability.")
        return

    wins = [t for t in all_trades if t.pnl > 0]
    losses = [t for t in all_trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(all_trades) * 100

    # Equity curve for drawdown + Sharpe
    equity = starting_equity
    equity_curve = []
    for t in sorted(all_trades, key=lambda x: x.entry_time):
        equity += t.pnl
        equity_curve.append(equity)

    # Max drawdown
    peak = starting_equity
    max_dd = 0.0
    running = starting_equity
    for t in sorted(all_trades, key=lambda x: x.entry_time):
        running += t.pnl
        if running > peak:
            peak = running
        dd = (peak - running) / peak
        max_dd = max(max_dd, dd)

    # Sharpe (annualised, assuming each bar = 5min, 78 bars/day, 252 trading days)
    returns = pd.Series([t.pnl_pct for t in all_trades])
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * (len(returns) ** 0.5)

    avg_hold = sum(t.hold_bars for t in all_trades) / len(all_trades) * 5  # → minutes

    final_equity = starting_equity + sum(t.pnl for t in all_trades)
    total_return = (final_equity - starting_equity) / starting_equity * 100

    print("\n" + "=" * 60)
    print("  MARKET GOD — BACKTEST REPORT")
    print("=" * 60)
    print(f"  Trades          : {len(all_trades)}  ({len(wins)} W / {len(losses)} L)")
    print(f"  Win Rate        : {win_rate:.1f}%")
    print(f"  Profit Factor   : {profit_factor:.2f}")
    print(f"  Total Return    : {total_return:+.2f}%  (${final_equity - starting_equity:+.2f})")
    print(f"  Max Drawdown    : {max_dd * 100:.2f}%")
    print(f"  Sharpe (simple) : {sharpe:.2f}")
    print(f"  Avg Hold Time   : {avg_hold:.0f} min")
    print(f"  Avg Win         : ${gross_win / len(wins):.2f}" if wins else "  Avg Win         : —")
    print(f"  Avg Loss        : ${gross_loss / len(losses):.2f}" if losses else "  Avg Loss        : —")
    print("=" * 60)

    # Per-symbol breakdown
    from collections import defaultdict
    by_sym: dict[str, list[BtTrade]] = defaultdict(list)
    for t in all_trades:
        by_sym[t.symbol].append(t)

    print(f"\n{'SYM':<8} {'Trades':>6} {'WR':>6} {'P&L':>9} {'PF':>6}")
    print("-" * 42)
    for sym, ts in sorted(by_sym.items()):
        sw = [x for x in ts if x.pnl > 0]
        sl = [x for x in ts if x.pnl <= 0]
        gw = sum(x.pnl for x in sw)
        gl = abs(sum(x.pnl for x in sl))
        pf = f"{gw/gl:.2f}" if gl > 0 else "∞"
        pnl = sum(x.pnl for x in ts)
        wr = len(sw) / len(ts) * 100
        print(f"{sym:<8} {len(ts):>6} {wr:>5.1f}% {pnl:>+9.2f} {pf:>6}")
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Market God backtester")
    parser.add_argument("--symbols", nargs="+",
                        default=["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META", "AMD"],
                        help="Symbols to backtest")
    parser.add_argument("--days", type=int, default=59,
                        help="Calendar days of 5m history (max ~59 via yfinance free)")
    parser.add_argument("--csv", type=str, default="",
                        help="Optional path to export trade CSV")
    args = parser.parse_args()

    logger.info(f"Fetching VIX history ({args.days}d)...")
    vix_map = fetch_vix_history(args.days)

    all_trades: list[BtTrade] = []
    equity = STARTING_CAPITAL

    for sym in args.symbols:
        logger.info(f"Fetching {sym}...")
        df = fetch_history(sym, args.days)
        if df is None:
            continue
        avg_vol = float(df["volume"].resample("1D").sum().mean()) if not df.empty else 0.0
        trades = simulate_symbol(sym, df, avg_vol, vix_map, equity)
        logger.info(f"  {sym}: {len(trades)} trades")
        all_trades.extend(trades)

    report(all_trades, STARTING_CAPITAL)

    if args.csv and all_trades:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "symbol", "entry_time", "exit_time", "entry_price", "exit_price",
                "pnl", "pnl_pct", "hold_bars", "exit_reason", "score"
            ])
            writer.writeheader()
            for t in all_trades:
                writer.writerow({
                    "symbol": t.symbol, "entry_time": t.entry_time,
                    "exit_time": t.exit_time, "entry_price": t.entry_price,
                    "exit_price": t.exit_price, "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct, "hold_bars": t.hold_bars,
                    "exit_reason": t.exit_reason, "score": t.score,
                })
        logger.info(f"Trades exported to {args.csv}")


if __name__ == "__main__":
    main()
