"""Whale movements from Blockchair API (every 5 minutes)."""
import threading
import time
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_URLS = [
    ('BTC', 'https://api.blockchair.com/bitcoin/transactions?q=output_total_usd(10000000..)&limit=10&s=id(desc)'),
    ('ETH', 'https://api.blockchair.com/ethereum/transactions?q=value_usd(10000000..)&limit=10&s=id(desc)'),
]


def _refresh_loop():
    while True:
        whales = []
        for sym, url in _URLS:
            try:
                resp = requests.get(url, timeout=15)
                data = resp.json().get('data', [])
                for tx in data:
                    usd = tx.get('output_total_usd') or tx.get('value_usd') or 0
                    if float(usd) > 5_000_000:
                        whales.append({
                            'symbol': sym,
                            'usd_value': float(usd),
                            'hash': tx.get('hash', '')[:16] + '...',
                            'ts': time.time(),
                        })
            except Exception as e:
                print(f'[Whale] {sym} error: {e}')
        if whales:
            store.set_whale(whales)
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_whale():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='whale')
    t.start()
