"""
Coinbase Advanced Trade WebSocket feed (replaces geo-blocked Binance).
Streams: ticker prices + market trades + level2 order book for BTC/ETH/SOL/XRP.
Runs in two daemon threads — one for tickers/trades, one for order book depth.
"""
import json
import threading
import time
import websocket
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

_WS_URL = 'wss://advanced-trade-api-ws.coinbase.com/ws/public'

_PRODUCTS = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD']
_DISPLAY  = {'BTC-USD': 'BTC', 'ETH-USD': 'ETH', 'SOL-USD': 'SOL', 'XRP-USD': 'XRP'}

# Depth products (limit to two to keep bandwidth manageable)
_DEPTH_PRODUCTS = ['BTC-USD', 'ETH-USD']


class CoinbaseTickerStream:
    """Subscribes to ticker + market_trades channels for real-time prices."""

    def __init__(self):
        self._ws = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='coinbase-ticker')
        self._thread.start()
        print('[Coinbase] Ticker stream starting...')

    def _subscribe(self, ws):
        msg = json.dumps({
            'type': 'subscribe',
            'product_ids': _PRODUCTS,
            'channel': 'ticker',
        })
        ws.send(msg)
        # Also subscribe to market_trades for individual trade ticks
        ws.send(json.dumps({
            'type': 'subscribe',
            'product_ids': _PRODUCTS,
            'channel': 'market_trades',
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
                print(f'[Coinbase] Ticker stream error: {e}')
            time.sleep(5)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
            channel = msg.get('channel', '')
            events  = msg.get('events', [])

            for event in events:
                if channel == 'ticker':
                    for t in event.get('tickers', []):
                        product = t.get('product_id', '')
                        symbol  = _DISPLAY.get(product)
                        if not symbol:
                            continue
                        price = float(t.get('price') or 0)
                        vol   = float(t.get('volume_24_h') or 0)
                        if price > 0:
                            store.push_price(symbol, price, vol, time.time())
                            bus.publish(EVT_PRICE_TICK, {
                                'symbol': symbol,
                                'price':  price,
                                'volume': vol,
                                'ts':     time.time(),
                                'source': 'coinbase',
                            })

                elif channel == 'market_trades':
                    for trade in event.get('trades', []):
                        product = trade.get('product_id', '')
                        symbol  = _DISPLAY.get(product)
                        if not symbol:
                            continue
                        price = float(trade.get('price') or 0)
                        qty   = float(trade.get('size')  or 0)
                        if price > 0:
                            store.push_price(symbol, price, qty, time.time())
                            bus.publish(EVT_PRICE_TICK, {
                                'symbol': symbol,
                                'price':  price,
                                'volume': qty,
                                'ts':     time.time(),
                                'source': 'coinbase',
                            })
        except Exception as e:
            print(f'[Coinbase] parse error: {e}')

    def _on_error(self, ws, error):
        print(f'[Coinbase] WS error: {error}')

    def _on_close(self, ws, code, msg):
        print(f'[Coinbase] WS closed: {code}')


class CoinbaseDepthStream:
    """Subscribes to level2 channel for order book depth (BTC + ETH)."""

    def __init__(self):
        self._thread = None
        # Maintain local order book state
        self._books = {p: {'bids': {}, 'asks': {}} for p in _DEPTH_PRODUCTS}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='coinbase-depth')
        self._thread.start()

    def _subscribe(self, ws):
        ws.send(json.dumps({
            'type': 'subscribe',
            'product_ids': _DEPTH_PRODUCTS,
            'channel': 'level2',
        }))

    def _run(self):
        while True:
            try:
                ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open=self._subscribe,
                    on_message=self._on_message,
                    on_error=lambda ws, e: print(f'[Depth] error: {e}'),
                    on_close=lambda ws, c, m: None,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                pass
            time.sleep(5)

    def _on_message(self, ws, raw):
        try:
            msg    = json.loads(raw)
            events = msg.get('events', [])

            for event in events:
                product = event.get('product_id', '')
                symbol  = _DISPLAY.get(product)
                if not symbol or product not in self._books:
                    continue

                evt_type = event.get('type', '')
                updates  = event.get('updates', [])

                if evt_type == 'snapshot':
                    # Reset book
                    self._books[product] = {'bids': {}, 'asks': {}}

                for upd in updates:
                    side     = upd.get('side', '')        # 'bid' or 'offer'
                    price_lv = upd.get('price_level', '')
                    new_qty  = float(upd.get('new_quantity') or 0)
                    book_key = 'bids' if side == 'bid' else 'asks'

                    if new_qty == 0:
                        self._books[product][book_key].pop(price_lv, None)
                    else:
                        self._books[product][book_key][price_lv] = new_qty

                # Build sorted lists and push to store
                bids = sorted(
                    [[float(p), q] for p, q in self._books[product]['bids'].items()],
                    key=lambda x: -x[0]
                )[:20]
                asks = sorted(
                    [[float(p), q] for p, q in self._books[product]['asks'].items()],
                    key=lambda x: x[0]
                )[:20]
                store.set_order_book(symbol, bids, asks)
        except Exception:
            pass


def start_binance():
    """Kept for backwards compatibility — starts Coinbase streams."""
    CoinbaseTickerStream().start()
    CoinbaseDepthStream().start()
