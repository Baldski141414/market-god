"""Fear & Greed index from alternative.me (every 5 minutes)."""
import threading
import time
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_URL = 'https://api.alternative.me/fng/?limit=2'


def _refresh_loop():
    while True:
        try:
            resp = requests.get(_URL, timeout=10)
            data = resp.json().get('data', [])
            if data:
                latest = data[0]
                store.set_fear_greed({
                    'value': int(latest.get('value', 50)),
                    'label': latest.get('value_classification', 'Neutral'),
                    'ts': time.time(),
                })
        except Exception as e:
            print(f'[FearGreed] error: {e}')
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_fear_greed():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='fear-greed')
    t.start()
