"""
Paper Trading Engine.
Listens for signal events, executes trades in <100ms.
Kelly Criterion sizing, trailing stops, take-profit, stop-loss.
"""
import time
import threading
from core.event_bus import bus, EVT_SIGNAL_READY, EVT_TRADE
from core.data_store import store
from core.config import (
    MAX_POSITIONS, MIN_SIGNAL_TO_BUY,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_TRIGGER_PCT,
)
from trading.kelly import position_size

_lock = threading.Lock()


def _win_rate() -> float:
    """Calculate current win rate from closed trades."""
    trades = store.portfolio.get('trades', [])
    closed = [t for t in trades if t.get('pnl') is not None]
    if not closed:
        return 0.55  # default assumption
    wins = sum(1 for t in closed if t['pnl'] > 0)
    return wins / len(closed)


def _portfolio_value() -> float:
    p = store.portfolio
    equity = sum(
        pos['shares'] * (store.get_latest(sym) or {}).get('price', pos['avg_price'])
        for sym, pos in p['positions'].items()
    )
    return p['cash'] + equity


def _try_buy(symbol: str, price: float, signal: dict):
    """Attempt to open a position."""
    if price <= 0:
        return

    def _execute(p):
        if len(p['positions']) >= MAX_POSITIONS:
            return
        if symbol in p['positions']:
            return
        if p['cash'] < 100:
            return

        pv = _portfolio_value()
        invest = min(position_size(pv, _win_rate()), p['cash'] * 0.95)
        invest = max(invest, 50)  # minimum $50
        shares = invest / price

        p['positions'][symbol] = {
            'shares': shares,
            'avg_price': price,
            'buy_time': time.time(),
            'highest_price': price,
            'cost_basis': invest,
        }
        p['cash'] -= invest
        p['trades'].append({
            'id': len(p['trades']) + 1,
            'ts': time.time(),
            'symbol': symbol,
            'action': 'BUY',
            'price': price,
            'shares': shares,
            'value': invest,
            'score': signal.get('score'),
            'signal': signal.get('signal'),
            'pnl': None,
        })
        bus.publish(EVT_TRADE, {'action': 'BUY', 'symbol': symbol,
                                'price': price, 'shares': shares})

    store.update_portfolio(_execute)


def _try_sell(symbol: str, price: float, reason: str = 'signal'):
    """Close a position."""
    if price <= 0:
        return

    def _execute(p):
        pos = p['positions'].get(symbol)
        if not pos:
            return
        proceeds = pos['shares'] * price
        pnl = proceeds - pos['cost_basis']
        p['cash'] += proceeds
        del p['positions'][symbol]
        p['trades'].append({
            'id': len(p['trades']) + 1,
            'ts': time.time(),
            'symbol': symbol,
            'action': 'SELL',
            'price': price,
            'shares': pos['shares'],
            'value': proceeds,
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / pos['cost_basis'] * 100, 2),
            'reason': reason,
            'score': None,
            'signal': None,
        })
        bus.publish(EVT_TRADE, {'action': 'SELL', 'symbol': symbol,
                                'price': price, 'pnl': pnl})

    store.update_portfolio(_execute)


def _check_stops():
    """Check stop-loss, take-profit, trailing stop for all positions."""
    positions = dict(store.portfolio.get('positions', {}))
    for symbol, pos in positions.items():
        latest = store.get_latest(symbol)
        if not latest:
            continue
        price = latest.get('price', 0)
        if price <= 0:
            continue

        avg = pos['avg_price']
        pnl_pct = (price - avg) / avg

        # Update trailing high
        def _update_high(p):
            pos_inner = p['positions'].get(symbol)
            if pos_inner and price > pos_inner.get('highest_price', 0):
                pos_inner['highest_price'] = price
        store.update_portfolio(_update_high)

        highest = store.portfolio['positions'].get(symbol, {}).get('highest_price', avg)
        trail_drop = (price - highest) / highest if highest > 0 else 0

        if pnl_pct <= STOP_LOSS_PCT:
            _try_sell(symbol, price, 'stop_loss')
        elif pnl_pct >= TAKE_PROFIT_PCT:
            _try_sell(symbol, price, 'take_profit')
        elif pnl_pct >= TRAILING_TRIGGER_PCT and trail_drop <= -0.015:
            _try_sell(symbol, price, 'trailing_stop')


def _on_signal(data: dict):
    """React to signal events."""
    if not data:
        return

    symbol = data.get('symbol')
    score  = data.get('score', 0)
    signal = data.get('signal', '')

    latest = store.get_latest(symbol)
    if not latest:
        return
    price = latest.get('price', 0)

    with _lock:
        if score >= MIN_SIGNAL_TO_BUY and signal in ('Strong Buy', 'Buy'):
            # Only buy on high-confidence signals
            if score >= 75:
                _try_buy(symbol, price, data)
        elif signal in ('Sell', 'Strong Sell'):
            _try_sell(symbol, price, 'signal')

        # Always check stops
        _check_stops()

        # Set SPY basis if not set
        if symbol == 'SPY' and store.portfolio.get('spy_basis') is None and price > 0:
            def _set_spy(p):
                p['spy_basis'] = price
                p['spy_basis_time'] = time.time()
            store.update_portfolio(_set_spy)


def start_paper_trader():
    bus.subscribe(EVT_SIGNAL_READY, _on_signal)
    print('[PaperTrader] started — listening for signals >= 75')
