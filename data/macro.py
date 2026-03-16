"""Macro indicators: VIX, yields, DXY, Fed rate via Yahoo Finance (every 5 min)."""
import threading
import time
import requests
import yfinance as yf
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_MACRO_TICKERS = {
    '^VIX': 'vix',
    '^TNX': 'yield_10y',
    '^TYX': 'yield_30y',
    '^IRX': 'yield_3m',
    'DX-Y.NYB': 'dxy',
    'GC=F': 'gold',
    'CL=F': 'oil',
}


def _get_regime(vix: float, yield_10y: float) -> str:
    if vix > 30:
        return 'RISK_OFF'
    if vix < 15 and yield_10y < 4.0:
        return 'RISK_ON'
    if yield_10y > 5.0:
        return 'RATE_PRESSURE'
    return 'NEUTRAL'


def _refresh_loop():
    while True:
        result = {}
        try:
            tickers = yf.Tickers(' '.join(_MACRO_TICKERS.keys()))
            for ticker_sym, key in _MACRO_TICKERS.items():
                try:
                    info = tickers.tickers[ticker_sym].fast_info
                    result[key] = round(float(info.last_price or 0), 4)
                except Exception:
                    pass
        except Exception as e:
            print(f'[Macro] error: {e}')

        vix = result.get('vix', 20)
        yield_10y = result.get('yield_10y', 4.0)
        result['regime'] = _get_regime(vix, yield_10y)
        result['ts'] = time.time()
        store.set_macro(result)
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_macro():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='macro')
    t.start()
