"""CoinGecko top-50 crypto poller (60 second refresh, no API key required)."""
import threading
import time
import requests
from core.config import COINGECKO_REFRESH_SECS
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

_URL = (
    'https://api.coingecko.com/api/v3/coins/markets'
    '?vs_currency=usd&order=market_cap_desc&per_page=50&page=1'
    '&sparkline=false&price_change_percentage=1h,24h'
)

_SESSION = requests.Session()
_SESSION.headers.update({'User-Agent': 'MarketGod/5.0'})


def _refresh_loop():
    while True:
        try:
            resp = _SESSION.get(_URL, timeout=15)
            resp.raise_for_status()
            coins = resp.json()
            store.set_coingecko(coins)

            for coin in coins:
                sym = coin.get('symbol', '').upper()
                price = float(coin.get('current_price') or 0)
                vol = float(coin.get('total_volume') or 0)
                if price > 0:
                    store.push_price(sym, price, vol)
                    bus.publish(EVT_PRICE_TICK, {
                        'symbol': sym, 'price': price,
                        'volume': vol,
                        'change_pct': coin.get('price_change_percentage_24h') or 0,
                        'ts': time.time(), 'source': 'coingecko',
                    })
        except Exception as e:
            print(f'[CoinGecko] error: {e}')
        time.sleep(COINGECKO_REFRESH_SECS)


def start_coingecko():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='coingecko')
    t.start()
    print('[CoinGecko] poller starting...')
