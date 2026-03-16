"""Congress trades from House Stock Watcher API (every 5 minutes)."""
import threading
import time
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_URL = 'https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json'


def _refresh_loop():
    while True:
        try:
            resp = requests.get(_URL, timeout=20)
            resp.raise_for_status()
            all_trades = resp.json()
            # Last 60 days
            import time as _time
            cutoff = _time.time() - 60 * 86400
            recent = []
            for t in all_trades:
                try:
                    import datetime
                    dt = datetime.datetime.strptime(t.get('transaction_date',''), '%Y-%m-%d')
                    if dt.timestamp() >= cutoff:
                        recent.append({
                            'politician': t.get('representative',''),
                            'ticker': t.get('ticker','').upper(),
                            'type': t.get('type',''),
                            'amount': t.get('amount',''),
                            'date': t.get('transaction_date',''),
                        })
                except Exception:
                    pass
            store.set_congress(recent[-200:])  # Keep last 200
        except Exception as e:
            print(f'[Congress] error: {e}')
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_congress():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='congress')
    t.start()
