"""
Binance WebSocket data feed.
Streams: trade ticks (100ms) + 1m klines + order book depth for BTC/ETH.
Runs in two daemon threads — one for trades/klines, one for depth.
"""
import json
import threading
import time
import websocket
from core.config import BINANCE_SYMBOLS, BINANCE_DISPLAY
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

# Combined stream URL
_TRADE_URL = (
    'wss://stream.binance.com:9443/stream?streams='
    + '/'.join(f'{s}@trade' for s in BINANCE_SYMBOLS)
)
_DEPTH_URL = (
    'wss://stream.binance.com:9443/stream?streams='
    + 'btcusdt@depth20@1000ms/ethusdt@depth20@1000ms'
)


class BinanceTradeStream:
    def __init__(self):
        self._ws = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='binance-trade')
        self._thread.start()
        print('[Binance] Trade stream starting...')

    def _run(self):
        while True:
            try:
                self._ws = websocket.WebSocketApp(
                    _TRADE_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f'[Binance] Trade stream error: {e}')
            time.sleep(5)  # reconnect delay

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
            data = msg.get('data', msg)
            stream = msg.get('stream', '')
            sym_raw = data.get('s', '').lower()
            symbol = BINANCE_DISPLAY.get(sym_raw, sym_raw.upper().replace('USDT', ''))
            price = float(data.get('p', 0))
            qty = float(data.get('q', 0))
            ts = data.get('T', time.time() * 1000) / 1000.0

            if price > 0:
                store.push_price(symbol, price, qty, ts)
                bus.publish(EVT_PRICE_TICK, {
                    'symbol': symbol,
                    'price': price,
                    'volume': qty,
                    'ts': ts,
                    'source': 'binance',
                })
        except Exception as e:
            print(f'[Binance] parse error: {e}')

    def _on_error(self, ws, error):
        print(f'[Binance] WS error: {error}')

    def _on_close(self, ws, code, msg):
        print(f'[Binance] WS closed: {code}')


class BinanceDepthStream:
    def __init__(self):
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='binance-depth')
        self._thread.start()

    def _run(self):
        while True:
            try:
                ws = websocket.WebSocketApp(
                    _DEPTH_URL,
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
            msg = json.loads(raw)
            data = msg.get('data', {})
            sym_raw = msg.get('stream', '').split('@')[0]
            symbol = BINANCE_DISPLAY.get(sym_raw, sym_raw.upper().replace('USDT', ''))
            bids = [[float(p), float(q)] for p, q in data.get('bids', [])]
            asks = [[float(p), float(q)] for p, q in data.get('asks', [])]
            store.set_order_book(symbol, bids, asks)
        except Exception:
            pass


def start_binance():
    BinanceTradeStream().start()
    BinanceDepthStream().start()
