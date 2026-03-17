"""
Paper Trading Engine.
Listens for signal events, executes trades in <100ms.

Crypto (BTC/ETH/SOL/XRP):
  - Trades 24/7 (not limited to market hours)
  - 5% of portfolio per trade (fixed, no Kelly)
  - Stop-loss -2%, take-profit +5%
  - Max 5 crypto positions at once
  - Buy threshold: score >= 60

Stocks:
  - Kelly Criterion sizing (capped at 25%, half-Kelly)
  - Stop-loss -3%, take-profit +8%
  - Max 20 total positions
  - Buy threshold: score >= 75
"""
import time
import threading
from core.event_bus import bus, EVT_SIGNAL_READY, EVT_TRADE
from core.data_store import store
from core.config import (
    CRYPTO_SYMBOLS,
    MAX_POSITIONS, MIN_SIGNAL_TO_BUY,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_TRIGGER_PCT,
    CRYPTO_MIN_SIGNAL_TO_BUY, CRYPTO_POSITION_PCT,
    CRYPTO_STOP_LOSS_PCT, CRYPTO_TAKE_PROFIT_PCT, CRYPTO_TRAILING_TRIGGER,
    MAX_CRYPTO_POSITIONS,
)
from trading.kelly import position_size

_lock = threading.Lock()


def _win_rate() -> float:
    trades = store.portfolio.get('trades', [])
    closed = [t for t in trades if t.get('pnl') is not None]
    if not closed:
        return 0.55
    wins = sum(1 for t in closed if t['pnl'] > 0)
    return wins / len(closed)


def _portfolio_value() -> float:
    p = store.portfolio
    equity = sum(
        pos['shares'] * (store.get_latest(sym) or {}).get('price', pos['avg_price'])
        for sym, pos in p['positions'].items()
    )
    return p['cash'] + equity


def _crypto_position_count(p: dict) -> int:
    return sum(1 for sym in p['positions'] if sym in CRYPTO_SYMBOLS)


def _try_buy(symbol: str, price: float, signal: dict):
    """Attempt to open a position."""
    if price <= 0:
        return
    is_crypto = symbol in CRYPTO_SYMBOLS

    def _execute(p):
        # Position-limit checks
        if is_crypto:
            if _crypto_position_count(p) >= MAX_CRYPTO_POSITIONS:
                return
        else:
            # Count total positions; also respect absolute max
            if len(p['positions']) >= MAX_POSITIONS:
                return
        if symbol in p['positions']:
            return
        if p['cash'] < 50:
            return

        pv = _portfolio_value()
        if is_crypto:
            invest = pv * CRYPTO_POSITION_PCT          # fixed 5% of portfolio
        else:
            invest = position_size(pv, _win_rate())    # Kelly Criterion

        invest = min(invest, p['cash'] * 0.95)
        invest = max(invest, 50)
        shares = invest / price

        p['positions'][symbol] = {
            'shares':        shares,
            'avg_price':     price,
            'buy_time':      time.time(),
            'highest_price': price,
            'cost_basis':    invest,
        }
        p['cash'] -= invest
        p['trades'].append({
            'id':     len(p['trades']) + 1,
            'ts':     time.time(),
            'symbol': symbol,
            'action': 'BUY',
            'price':  price,
            'shares': shares,
            'value':  invest,
            'score':  signal.get('score'),
            'signal': signal.get('signal'),
            'pnl':    None,
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
            'id':      len(p['trades']) + 1,
            'ts':      time.time(),
            'symbol':  symbol,
            'action':  'SELL',
            'price':   price,
            'shares':  pos['shares'],
            'value':   proceeds,
            'pnl':     round(pnl, 2),
            'pnl_pct': round(pnl / pos['cost_basis'] * 100, 2),
            'reason':  reason,
            'score':   None,
            'signal':  None,
        })
        bus.publish(EVT_TRADE, {'action': 'SELL', 'symbol': symbol,
                                'price': price, 'pnl': pnl})

    store.update_portfolio(_execute)


def _check_stops():
    """Check stop-loss / take-profit / trailing stop for all open positions."""
    positions = dict(store.portfolio.get('positions', {}))
    for symbol, pos in positions.items():
        is_crypto   = symbol in CRYPTO_SYMBOLS
        stop_pct    = CRYPTO_STOP_LOSS_PCT   if is_crypto else STOP_LOSS_PCT
        tp_pct      = CRYPTO_TAKE_PROFIT_PCT if is_crypto else TAKE_PROFIT_PCT
        trail_trig  = CRYPTO_TRAILING_TRIGGER if is_crypto else TRAILING_TRIGGER_PCT

        latest = store.get_latest(symbol)
        if not latest:
            continue
        price = latest.get('price', 0)
        if price <= 0:
            continue

        avg     = pos['avg_price']
        pnl_pct = (price - avg) / avg

        # Update trailing high
        def _update_high(p, sym=symbol):
            pos_inner = p['positions'].get(sym)
            if pos_inner and price > pos_inner.get('highest_price', 0):
                pos_inner['highest_price'] = price
        store.update_portfolio(_update_high)

        highest    = store.portfolio['positions'].get(symbol, {}).get('highest_price', avg)
        trail_drop = (price - highest) / highest if highest > 0 else 0

        if pnl_pct <= stop_pct:
            _try_sell(symbol, price, 'stop_loss')
        elif pnl_pct >= tp_pct:
            _try_sell(symbol, price, 'take_profit')
        elif pnl_pct >= trail_trig and trail_drop <= -0.015:
            _try_sell(symbol, price, 'trailing_stop')


def _on_signal(data: dict):
    """React to signal events."""
    if not data:
        return

    symbol    = data.get('symbol')
    score     = data.get('score', 0)
    signal    = data.get('signal', '')
    is_crypto = symbol in CRYPTO_SYMBOLS
    min_score = CRYPTO_MIN_SIGNAL_TO_BUY if is_crypto else MIN_SIGNAL_TO_BUY

    latest = store.get_latest(symbol)
    if not latest:
        return
    price = latest.get('price', 0)

    with _lock:
        if score >= min_score and signal in ('Strong Buy', 'Buy'):
            _try_buy(symbol, price, data)
        elif signal in ('Sell', 'Strong Sell'):
            _try_sell(symbol, price, 'signal')

        _check_stops()

        if symbol == 'SPY' and store.portfolio.get('spy_basis') is None and price > 0:
            def _set_spy(p):
                p['spy_basis']      = price
                p['spy_basis_time'] = time.time()
            store.update_portfolio(_set_spy)


def start_paper_trader():
    bus.subscribe(EVT_SIGNAL_READY, _on_signal)
    print('[PaperTrader] started — crypto>=60 (24/7, 5% pos, -2%/+5%), stocks>=75 (Kelly, -3%/+8%)')
