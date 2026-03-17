"""
Alpaca Paper Trading Integration.
Mirrors trades from the signal engine to Alpaca paper trading API.
Requires ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.
Base URL: https://paper-api.alpaca.markets
"""
import os
import logging
import requests
from core.config import CRYPTO_SYMBOLS

log = logging.getLogger(__name__)

_BASE = 'https://paper-api.alpaca.markets'

# Map internal symbols → Alpaca symbol format
_CRYPTO_MAP = {
    'BTC': 'BTC/USD',
    'ETH': 'ETH/USD',
    'SOL': 'SOL/USD',
    'XRP': 'XRP/USD',
}

_session = None


def _get_session():
    global _session
    if _session is not None:
        return _session
    key    = os.environ.get('ALPACA_API_KEY', '').strip()
    secret = os.environ.get('ALPACA_SECRET_KEY', '').strip()
    if not key or not secret:
        log.warning('[Alpaca] ALPACA_API_KEY / ALPACA_SECRET_KEY not set — mirroring disabled')
        return None
    s = requests.Session()
    s.headers.update({
        'APCA-API-KEY-ID':     key,
        'APCA-API-SECRET-KEY': secret,
        'Content-Type':        'application/json',
    })
    _session = s
    log.info('[Alpaca] Session initialised (paper trading)')
    return s


def _alpaca_symbol(symbol: str) -> str:
    return _CRYPTO_MAP.get(symbol, symbol)


def submit_order(symbol: str, side: str, notional: float = None, qty: float = None):
    """
    Submit a market order to Alpaca paper API.
      side     : 'buy' or 'sell'
      notional : dollar amount  (preferred for buys — supports fractional)
      qty      : share/coin qty (preferred for sells to close exact position)
    Returns Alpaca order dict or None on failure.
    """
    s = _get_session()
    if not s:
        return None

    is_crypto   = symbol in CRYPTO_SYMBOLS
    alpaca_sym  = _alpaca_symbol(symbol)

    order: dict = {
        'symbol':        alpaca_sym,
        'side':          side,
        'type':          'market',
        'time_in_force': 'gtc' if is_crypto else 'day',
    }

    if notional and notional > 0:
        order['notional'] = str(round(notional, 2))
    elif qty and qty > 0:
        order['qty'] = str(round(qty, 8) if is_crypto else round(qty, 6))

    try:
        resp = s.post(f'{_BASE}/v2/orders', json=order, timeout=6)
        if resp.status_code in (200, 201):
            data = resp.json()
            log.info(f'[Alpaca] {side.upper()} {alpaca_sym}  id={data.get("id")}')
            return data
        log.warning(f'[Alpaca] Order rejected {resp.status_code}: {resp.text[:300]}')
        return None
    except Exception as exc:
        log.warning(f'[Alpaca] submit_order error: {exc}')
        return None


def get_account():
    """Return Alpaca account dict or None."""
    s = _get_session()
    if not s:
        return None
    try:
        resp = s.get(f'{_BASE}/v2/account', timeout=6)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f'[Alpaca] get_account {resp.status_code}')
    except Exception as exc:
        log.warning(f'[Alpaca] get_account error: {exc}')
    return None


def get_positions():
    """Return list of Alpaca position dicts or []."""
    s = _get_session()
    if not s:
        return []
    try:
        resp = s.get(f'{_BASE}/v2/positions', timeout=6)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f'[Alpaca] get_positions {resp.status_code}')
    except Exception as exc:
        log.warning(f'[Alpaca] get_positions error: {exc}')
    return []


def get_portfolio_summary():
    """
    Fetch account + positions and return a dashboard-ready dict.
    Always returns a dict; 'enabled' key signals whether creds are configured.
    """
    acct = get_account()
    if not acct:
        return {'enabled': False}

    positions_raw = get_positions()
    positions = []
    for p in positions_raw:
        avg   = float(p.get('avg_entry_price') or 0)
        cur   = float(p.get('current_price') or avg)
        mv    = float(p.get('market_value') or 0)
        upl   = float(p.get('unrealized_pl') or 0)
        uplpc = float(p.get('unrealized_plpc') or 0) * 100
        positions.append({
            'symbol':          p.get('symbol', ''),
            'qty':             float(p.get('qty') or 0),
            'avg_price':       round(avg, 4),
            'current_price':   round(cur, 4),
            'market_value':    round(mv, 2),
            'unrealized_pl':   round(upl, 2),
            'unrealized_plpc': round(uplpc, 2),
            'side':            p.get('side', 'long'),
        })

    equity        = float(acct.get('equity') or 0)
    cash          = float(acct.get('cash') or 0)
    buying_power  = float(acct.get('buying_power') or 0)
    start_equity  = 100_000.0   # paper account starts at $100k
    pnl           = equity - start_equity

    return {
        'enabled':       True,
        'equity':        round(equity, 2),
        'cash':          round(cash, 2),
        'buying_power':  round(buying_power, 2),
        'pnl':           round(pnl, 2),
        'pnl_pct':       round(pnl / start_equity * 100, 2),
        'positions':     positions,
        'num_positions': len(positions),
        'status':        acct.get('status', 'unknown'),
    }
