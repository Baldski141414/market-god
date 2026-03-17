"""
Congress trades — House + Senate stock watcher, with SEC EFTS fallback.
Refreshes every 5 minutes. Tries sources in order until one succeeds.
"""
import datetime
import threading
import time
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

# Primary: House Stock Watcher
_HOUSE_URL  = 'https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json'
# Backup 1: Senate Stock Watcher
_SENATE_URL = 'https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json'
# Backup 2: SEC EFTS Form 4 search
_SEC_URL    = 'https://efts.sec.gov/LATEST/search-index?q=%22form+4%22'

_CUTOFF_DAYS = 60
_TIMEOUT     = 20


def _parse_house(records: list) -> list:
    cutoff = time.time() - _CUTOFF_DAYS * 86400
    out = []
    for t in records:
        try:
            dt = datetime.datetime.strptime(t.get('transaction_date', ''), '%Y-%m-%d')
            if dt.timestamp() < cutoff:
                continue
            ticker = t.get('ticker', '').upper().strip()
            if not ticker or ticker in ('', '--', 'N/A'):
                continue
            out.append({
                'politician': t.get('representative', ''),
                'ticker':     ticker,
                'type':       t.get('type', ''),
                'amount':     t.get('amount', ''),
                'date':       t.get('transaction_date', ''),
                'chamber':    'house',
            })
        except Exception:
            pass
    return out


def _parse_senate(records: list) -> list:
    cutoff = time.time() - _CUTOFF_DAYS * 86400
    out = []
    for t in records:
        try:
            # Senate format uses 'transaction_date' and 'senator' fields
            date_str = t.get('transaction_date', '') or t.get('date', '')
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            if dt.timestamp() < cutoff:
                continue
            ticker = t.get('ticker', '').upper().strip()
            if not ticker or ticker in ('', '--', 'N/A'):
                continue
            out.append({
                'politician': t.get('senator', '') or t.get('first_name', '') + ' ' + t.get('last_name', ''),
                'ticker':     ticker,
                'type':       t.get('type', ''),
                'amount':     t.get('amount', ''),
                'date':       date_str,
                'chamber':    'senate',
            })
        except Exception:
            pass
    return out


def _fetch_house() -> list | None:
    try:
        resp = requests.get(_HOUSE_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        records = resp.json()
        parsed  = _parse_house(records)
        print(f'[Congress] House: {len(parsed)} recent trades')
        return parsed
    except Exception as e:
        print(f'[Congress] House source failed: {e}')
        return None


def _fetch_senate() -> list | None:
    try:
        resp = requests.get(_SENATE_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        records = resp.json()
        parsed  = _parse_senate(records)
        print(f'[Congress] Senate: {len(parsed)} recent trades')
        return parsed
    except Exception as e:
        print(f'[Congress] Senate source failed: {e}')
        return None


def _fetch_sec_fallback() -> list | None:
    """Minimal fallback: extract ticker mentions from SEC EFTS Form 4 results."""
    try:
        headers = {'User-Agent': 'MarketGod/5.0 contact@marketgod.io'}
        resp = requests.get(_SEC_URL, timeout=_TIMEOUT, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get('hits', {}).get('hits', [])
        out  = []
        today = datetime.date.today().isoformat()
        for h in hits[:100]:
            src    = h.get('_source', {})
            entity = src.get('entity_name', '') or src.get('display_names', [''])[0]
            ticker = src.get('file_num', '')  # best-effort; SEC EFTS lacks clean ticker field
            out.append({
                'politician': entity,
                'ticker':     ticker,
                'type':       'form4',
                'amount':     '',
                'date':       today,
                'chamber':    'sec',
            })
        print(f'[Congress] SEC fallback: {len(out)} entries')
        return out or None
    except Exception as e:
        print(f'[Congress] SEC fallback failed: {e}')
        return None


def _refresh_loop():
    while True:
        trades = None

        # Try sources in order
        house  = _fetch_house()
        senate = _fetch_senate()

        if house is not None or senate is not None:
            trades = (house or []) + (senate or [])
        else:
            trades = _fetch_sec_fallback()

        if trades:
            # Sort by date descending, keep last 200
            trades.sort(key=lambda x: x.get('date', ''), reverse=True)
            store.set_congress(trades[:200])
        else:
            print('[Congress] all sources failed — keeping stale data')

        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_congress():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='congress')
    t.start()
