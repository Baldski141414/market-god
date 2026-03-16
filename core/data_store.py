"""
Thread-safe in-memory data store.
All data lives here — never written to disk during trading.
"""
import threading
import time
from collections import deque
from typing import Optional
from core.config import PRICE_HISTORY_LEN, STARTING_CASH

class DataStore:
    def __init__(self):
        self._lock = threading.RLock()

        # Price history: symbol -> deque of (timestamp, price, volume)
        self.price_history: dict[str, deque] = {}

        # Latest prices: symbol -> {price, volume, change_pct, ts}
        self.latest_prices: dict[str, dict] = {}

        # Signals: symbol -> {score, signal, components, ts}
        self.signals: dict[str, dict] = {}

        # Top opportunities list
        self.opportunities: list[dict] = []

        # Auxiliary data
        self.fear_greed: dict = {}
        self.reddit: dict = {}
        self.macro: dict = {}
        self.options: dict = {}
        self.congress: list = []
        self.whale: list = []
        self.insider: dict = {}
        self.coingecko: list = []

        # Order book: symbol -> {bids: [[price,qty],...], asks: [[price,qty],...]}
        self.order_book: dict[str, dict] = {}

        # Portfolio state
        self.portfolio: dict = {
            'cash': STARTING_CASH,
            'positions': {},   # symbol -> {shares, avg_price, buy_time, highest_price}
            'trades': [],
            'spy_basis': None,
            'spy_basis_time': None,
            'created': time.time(),
        }

        # Signal accuracy tracking: signal_name -> {correct, total}
        self.signal_accuracy: dict[str, dict] = {}

        # Adaptive weights (starts as default, updated daily)
        from core.config import DEFAULT_WEIGHTS
        self.weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self.weights_updated_at: float = 0.0

    # ── Price history ──────────────────────────────────────────────────────
    def push_price(self, symbol: str, price: float, volume: float, ts: Optional[float] = None):
        ts = ts or time.time()
        with self._lock:
            if symbol not in self.price_history:
                self.price_history[symbol] = deque(maxlen=PRICE_HISTORY_LEN)
            self.price_history[symbol].append((ts, price, volume))
            self.latest_prices[symbol] = {
                'price': price, 'volume': volume, 'ts': ts,
                'change_pct': self._calc_change(symbol, price),
            }

    def _calc_change(self, symbol: str, current: float) -> float:
        hist = self.price_history.get(symbol)
        if not hist or len(hist) < 2:
            return 0.0
        oldest = hist[0][1]
        if oldest == 0:
            return 0.0
        return (current - oldest) / oldest * 100

    def get_prices(self, symbol: str) -> list[tuple]:
        with self._lock:
            h = self.price_history.get(symbol)
            return list(h) if h else []

    def get_close_series(self, symbol: str) -> list[float]:
        with self._lock:
            h = self.price_history.get(symbol)
            return [p[1] for p in h] if h else []

    def get_volume_series(self, symbol: str) -> list[float]:
        with self._lock:
            h = self.price_history.get(symbol)
            return [p[2] for p in h] if h else []

    def get_latest(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return dict(self.latest_prices.get(symbol, {}))

    # ── Signal store ───────────────────────────────────────────────────────
    def set_signal(self, symbol: str, result: dict):
        with self._lock:
            self.signals[symbol] = {**result, 'ts': time.time()}

    def get_signal(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return dict(self.signals.get(symbol, {}))

    def get_all_signals(self) -> dict:
        with self._lock:
            return dict(self.signals)

    # ── Portfolio ──────────────────────────────────────────────────────────
    def get_portfolio(self) -> dict:
        with self._lock:
            import copy
            return copy.deepcopy(self.portfolio)

    def update_portfolio(self, updater_fn):
        """Thread-safe portfolio update via callback."""
        with self._lock:
            updater_fn(self.portfolio)

    # ── Aux data setters ───────────────────────────────────────────────────
    def set_fear_greed(self, data: dict):
        with self._lock: self.fear_greed = data

    def set_reddit(self, data: dict):
        with self._lock: self.reddit = data

    def set_macro(self, data: dict):
        with self._lock: self.macro = data

    def set_options(self, data: dict):
        with self._lock: self.options = data

    def set_congress(self, data: list):
        with self._lock: self.congress = data

    def set_whale(self, data: list):
        with self._lock: self.whale = data

    def set_insider(self, data: dict):
        with self._lock: self.insider = data

    def set_coingecko(self, data: list):
        with self._lock: self.coingecko = data

    def set_order_book(self, symbol: str, bids: list, asks: list):
        with self._lock:
            self.order_book[symbol] = {'bids': bids[:20], 'asks': asks[:20]}

    def set_opportunities(self, opps: list):
        with self._lock: self.opportunities = opps

    # ── Aux data getters ───────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Full snapshot for API response."""
        with self._lock:
            return {
                'prices': dict(self.latest_prices),
                'signals': dict(self.signals),
                'opportunities': list(self.opportunities),
                'fear_greed': dict(self.fear_greed),
                'reddit': dict(self.reddit),
                'macro': dict(self.macro),
                'options': dict(self.options),
                'congress': list(self.congress),
                'whale': list(self.whale),
                'insider': dict(self.insider),
                'coingecko': list(self.coingecko),
                'order_book': dict(self.order_book),
                'weights': dict(self.weights),
                'ts': time.time(),
            }

    def reset_portfolio(self):
        with self._lock:
            self.portfolio = {
                'cash': STARTING_CASH,
                'positions': {},
                'trades': [],
                'spy_basis': None,
                'spy_basis_time': None,
                'created': time.time(),
            }

# Global singleton
store = DataStore()
