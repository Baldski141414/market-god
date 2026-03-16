"""Options flow: put/call ratio + unusual activity via Yahoo Finance (every 5 min)."""
import threading
import time
import yfinance as yf
from core.config import SLOW_DATA_REFRESH_SECS, ALL_STOCK_TICKERS
from core.data_store import store

_WATCH = ['SPY','QQQ','AAPL','TSLA','NVDA','AMD','META','AMZN','MSFT','GOOGL',
          'GME','AMC','PLTR','MSTR','COIN']


def _fetch_options() -> dict:
    result = {}
    for sym in _WATCH:
        try:
            t = yf.Ticker(sym)
            expirations = t.options
            if not expirations:
                continue
            exp = expirations[0]
            chain = t.option_chain(exp)
            calls_vol = chain.calls['volume'].sum()
            puts_vol  = chain.puts['volume'].sum()
            total = calls_vol + puts_vol
            pc_ratio = puts_vol / calls_vol if calls_vol > 0 else 1.0
            result[sym] = {
                'pc_ratio': round(float(pc_ratio), 3),
                'calls_vol': int(calls_vol),
                'puts_vol': int(puts_vol),
                'signal': 'bullish' if pc_ratio < 0.7 else ('bearish' if pc_ratio > 1.3 else 'neutral'),
            }
        except Exception:
            pass
    return result


def _refresh_loop():
    while True:
        data = _fetch_options()
        if data:
            store.set_options(data)
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_options():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='options')
    t.start()
