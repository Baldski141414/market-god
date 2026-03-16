"""
Lightweight synchronous pub/sub event bus.
Events fire on the same thread as the publisher (no async overhead).
"""
import threading
from collections import defaultdict
from typing import Callable, Any

class EventBus:
    def __init__(self):
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable) -> None:
        with self._lock:
            self._subs[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        with self._lock:
            self._subs[event_type] = [c for c in self._subs[event_type] if c != callback]

    def publish(self, event_type: str, data: Any = None) -> None:
        with self._lock:
            callbacks = list(self._subs[event_type])
        for cb in callbacks:
            try:
                cb(data)
            except Exception as e:
                print(f"[EventBus] error in handler for {event_type}: {e}")

# Global singleton
bus = EventBus()

# Event type constants
EVT_PRICE_TICK   = 'price_tick'    # {symbol, price, volume, ts}
EVT_SIGNAL_READY = 'signal_ready'  # {symbol, score, signal, components}
EVT_ALERT        = 'alert'         # {symbol, score, message}
EVT_TRADE        = 'trade'         # {action, symbol, price, shares, pnl}
EVT_RANK_UPDATE  = 'rank_update'   # [top opportunities list]
