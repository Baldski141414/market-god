"""
Kraken WebSocket feed (wss://ws.kraken.com).
Streams: ticker prices for XBT/USD, ETH/USD, SOL/USD, XRP/USD.
XBT is mapped to BTC internally. Order book depth for BTC + ETH.
"""
import json
import threading
import time
import websocket
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

_WS_URL = 'wss://ws.kraken.com'

_PAIRS = ['XBT/USD', 'ETH/USD', 'SOL/USD', 'XRP/USD']

# Map Kraken pair names to internal symbols (XBT → BTC)
_DISPLAY = {
    'XBT/USD': 'BTC',
    'ETH/USD': 'ETH',
    'SOL/USD': 'SOL',
    'XRP/USD': 'XRP',
}

_DEPTH_PAIRS = ['XBT/USD', 'ETH/USD']


class KrakenTickerStream:
    """Subscribes to Kraken ticker channel for real-time prices."""

    def __init__(self):
        self._ws = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='kraken-ticker')
        self._thread.start()
        print('[Kraken] Ticker stream starting...')

    def _subscribe(self, ws):
        ws.send(json.dumps({
            'event': 'subscribe',
            'pair': _PAIRS,
            'subscription': {'name': 'ticker'},
        }))

    def _run(self):
        while True:
            try:
                self._ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open=self._subscribe,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f'[Kraken] Ticker stream error: {e}')
            time.sleep(5)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)

            # Skip event messages (heartbeat, subscriptionStatus, systemStatus)
            if isinstance(msg, dict):
                return

            # Ticker update is: [channelID, data, "ticker", "XBT/USD"]
            if not isinstance(msg, list) or len(msg) < 4:
                return
            if msg[2] != 'ticker':
                return

            pair   = msg[3]
            symbol = _DISPLAY.get(pair)
            if not symbol:
                return

            data  = msg[1]
            # 'c' = last trade closed: [price, lot_volume]
            # 'v' = volume: [today, last_24h]
            price = float(data['c'][0])
            vol   = float(data['v'][1])  # 24h volume

            if price > 0:
                store.push_price(symbol, price, vol, time.time())
                bus.publish(EVT_PRICE_TICK, {
                    'symbol': symbol,
                    'price':  price,
                    'volume': vol,
                    'ts':     time.time(),
                    'source': 'kraken',
                })
        except Exception as e:
            print(f'[Kraken] parse error: {e}')

    def _on_error(self, ws, error):
        print(f'[Kraken] WS error: {error}')

    def _on_close(self, ws, code, msg):
        print(f'[Kraken] WS closed: {code}')


class KrakenDepthStream:
    """Subscribes to Kraken book channel for order book depth (BTC + ETH)."""

    def __init__(self):
        self._thread = None
        # Local order book: {pair: {'bids': {price_str: vol}, 'asks': {price_str: vol}}}
        self._books = {p: {'bids': {}, 'asks': {}} for p in _DEPTH_PAIRS}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='kraken-depth')
        self._thread.start()

    def _subscribe(self, ws):
        ws.send(json.dumps({
            'event': 'subscribe',
            'pair': _DEPTH_PAIRS,
            'subscription': {'name': 'book', 'depth': 20},
        }))

    def _run(self):
        while True:
            try:
                ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open=self._subscribe,
                    on_message=self._on_message,
                    on_error=lambda ws, e: print(f'[Kraken Depth] error: {e}'),
                    on_close=lambda ws, c, m: None,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                pass
            time.sleep(5)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)

            if isinstance(msg, dict):
                return
            if not isinstance(msg, list) or len(msg) < 4:
                return

            channel_name = msg[-2]  # e.g. "book-20"
            pair         = msg[-1]  # e.g. "XBT/USD"

            if not channel_name.startswith('book'):
                return

            symbol = _DISPLAY.get(pair)
            if not symbol or pair not in self._books:
                return

            # Snapshot has 'bs'/'as'; incremental updates have 'b'/'a'
            # msg[1] is always the data dict (sometimes msg[2] also if both sides update)
            data_parts = msg[1:-2]  # everything between channelID and channel_name/pair

            for data in data_parts:
                if not isinstance(data, dict):
                    continue

                # Snapshot keys: 'bs' (bids snapshot), 'as' (asks snapshot)
                if 'bs' in data:
                    self._books[pair]['bids'] = {
                        entry[0]: float(entry[1]) for entry in data['bs']
                    }
                if 'as' in data:
                    self._books[pair]['asks'] = {
                        entry[0]: float(entry[1]) for entry in data['as']
                    }

                # Incremental update keys: 'b' (bids), 'a' (asks)
                for entry in data.get('b', []):
                    price_str, vol = entry[0], float(entry[1])
                    if vol == 0:
                        self._books[pair]['bids'].pop(price_str, None)
                    else:
                        self._books[pair]['bids'][price_str] = vol

                for entry in data.get('a', []):
                    price_str, vol = entry[0], float(entry[1])
                    if vol == 0:
                        self._books[pair]['asks'].pop(price_str, None)
                    else:
                        self._books[pair]['asks'][price_str] = vol

            bids = sorted(
                [[float(p), q] for p, q in self._books[pair]['bids'].items()],
                key=lambda x: -x[0]
            )[:20]
            asks = sorted(
                [[float(p), q] for p, q in self._books[pair]['asks'].items()],
                key=lambda x: x[0]
            )[:20]
            store.set_order_book(symbol, bids, asks)

        except Exception:
            pass


def start_binance():
    """Entry point — starts Kraken ticker and depth streams."""
    KrakenTickerStream().start()
    KrakenDepthStream().start()
