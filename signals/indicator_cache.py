"""
Incremental indicator cache.

Maintains per-symbol EMA/RSI running state so each price tick is O(1) to process
instead of re-iterating the full 500-bar history.  On first use for a symbol the
cache bootstraps itself from the existing store price history, then all subsequent
updates are a handful of arithmetic operations.
"""
import threading
from collections import deque
from typing import Optional

_RSI_PERIOD = 14
_MACD_FAST  = 12
_MACD_SLOW  = 26
_MACD_SIG   = 9
_EMA50      = 50
_EMA200     = 200
# Price window big enough for: Bollinger(20), momentum_5m(6 bars), momentum_1h(61 bars)
_PRICE_WIN  = 61
# Volume window: 20-bar average + 1 current
_VOL_WIN    = 21


class _State:
    __slots__ = [
        # RSI
        'rsi_gain', 'rsi_loss', 'rsi_last', 'rsi_n',
        # MACD
        'mf', 'ms', 'msig', 'mf_n', 'ms_n', 'msig_n', 'msig_buf',
        # EMA 50/200
        'e50', 'e50_n', 'e200', 'e200_n',
        # sliding windows
        'pw', 'vw',
        # cached outputs
        'rsi_val', 'macd_hist', 'macd_line', 'ema_cross',
        'boll_pct_b', 'boll_upper', 'boll_mid', 'boll_lower',
    ]

    def __init__(self):
        self.rsi_gain = 0.0; self.rsi_loss = 0.0
        self.rsi_last: Optional[float] = None; self.rsi_n = 0

        self.mf = 0.0; self.mf_n = 0
        self.ms = 0.0; self.ms_n = 0
        self.msig = 0.0; self.msig_n = 0; self.msig_buf: list = []

        self.e50 = 0.0; self.e50_n = 0
        self.e200 = 0.0; self.e200_n = 0

        self.pw: deque = deque(maxlen=_PRICE_WIN)
        self.vw: deque = deque(maxlen=_VOL_WIN)

        self.rsi_val: Optional[float] = None
        self.macd_hist: Optional[float] = None
        self.macd_line: Optional[float] = None
        self.ema_cross: Optional[float] = None
        self.boll_pct_b: Optional[float] = None
        self.boll_upper: Optional[float] = None
        self.boll_mid:   Optional[float] = None
        self.boll_lower: Optional[float] = None


class IndicatorCache:
    """Thread-safe per-symbol incremental indicator cache."""

    def __init__(self):
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    def update(self, symbol: str, price: float, volume: float = 0.0) -> dict:
        """Add new price tick; returns fresh indicator values."""
        with self._lock:
            s = self._states.get(symbol)
            if s is None:
                s = _State()
                self._bootstrap(symbol, s)
                self._states[symbol] = s
        # Step outside the global lock (each symbol owns its state)
        _step(s, price, volume)
        return _values(s)

    def get(self, symbol: str) -> dict:
        """Return latest cached values without adding a new price."""
        with self._lock:
            s = self._states.get(symbol)
            if s is None:
                s = _State()
                self._bootstrap(symbol, s)
                self._states[symbol] = s
        return _values(s)

    @staticmethod
    def _bootstrap(symbol: str, s: _State):
        """Warm up state from existing store history (runs once per symbol)."""
        try:
            from core.data_store import store
            bars = store.get_prices(symbol)  # thread-safe list copy
            for _, price, vol in bars:
                _step(s, price, vol)
        except Exception:
            pass


# ── per-bar update (module-level for speed) ──────────────────────────────────

def _step(s: _State, price: float, volume: float):
    kf   = 2.0 / (_MACD_FAST + 1)
    ks   = 2.0 / (_MACD_SLOW + 1)
    ksig = 2.0 / (_MACD_SIG + 1)
    k50  = 2.0 / (_EMA50  + 1)
    k200 = 2.0 / (_EMA200 + 1)

    # ── RSI (Wilder smoothing) ────────────────────────────────────────────
    if s.rsi_last is not None:
        d = price - s.rsi_last
        g, l = max(d, 0.0), max(-d, 0.0)
        if s.rsi_n < _RSI_PERIOD:
            s.rsi_gain += g; s.rsi_loss += l
            s.rsi_n += 1
            if s.rsi_n == _RSI_PERIOD:
                s.rsi_gain /= _RSI_PERIOD
                s.rsi_loss /= _RSI_PERIOD
                s.rsi_val = _rsi(s)
        else:
            s.rsi_gain = (s.rsi_gain * (_RSI_PERIOD - 1) + g) / _RSI_PERIOD
            s.rsi_loss = (s.rsi_loss * (_RSI_PERIOD - 1) + l) / _RSI_PERIOD
            s.rsi_val = _rsi(s)
    s.rsi_last = price

    # ── MACD ─────────────────────────────────────────────────────────────
    s.mf_n += 1
    s.mf = (price / _MACD_FAST) + s.mf if s.mf_n <= _MACD_FAST \
           else price * kf + s.mf * (1 - kf)

    s.ms_n += 1
    s.ms = (price / _MACD_SLOW) + s.ms if s.ms_n <= _MACD_SLOW \
           else price * ks + s.ms * (1 - ks)

    if s.mf_n > _MACD_FAST and s.ms_n > _MACD_SLOW:
        mv = s.mf - s.ms
        s.macd_line = mv
        if s.msig_n < _MACD_SIG:
            s.msig_buf.append(mv); s.msig_n += 1
            if s.msig_n == _MACD_SIG:
                s.msig = sum(s.msig_buf) / _MACD_SIG
                s.msig_buf = []
        else:
            s.msig = mv * ksig + s.msig * (1 - ksig)
        if s.msig_n >= _MACD_SIG:
            s.macd_hist = s.macd_line - s.msig

    # ── EMA 50 / 200 ─────────────────────────────────────────────────────
    s.e50_n += 1
    s.e50 = (price / _EMA50) + s.e50 if s.e50_n <= _EMA50 \
            else price * k50 + s.e50 * (1 - k50)

    s.e200_n += 1
    s.e200 = (price / _EMA200) + s.e200 if s.e200_n <= _EMA200 \
             else price * k200 + s.e200 * (1 - k200)

    if s.e50_n > _EMA50 and s.e200_n > _EMA200 and s.e200 != 0:
        s.ema_cross = max(-1.0, min(1.0, (s.e50 - s.e200) / s.e200 / 0.05))

    # ── Bollinger (last 20 prices) ────────────────────────────────────────
    s.pw.append(price)
    if len(s.pw) >= 20:
        win = list(s.pw)[-20:]
        mid = sum(win) / 20
        std = (sum((x - mid) ** 2 for x in win) / 20) ** 0.5
        upper = mid + 2.0 * std
        lower = mid - 2.0 * std
        bw = upper - lower
        s.boll_upper = upper; s.boll_mid = mid; s.boll_lower = lower
        s.boll_pct_b = (price - lower) / bw if bw != 0 else 0.5

    # ── Volume window ─────────────────────────────────────────────────────
    s.vw.append(volume)


def _rsi(s: _State) -> Optional[float]:
    if s.rsi_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + s.rsi_gain / s.rsi_loss), 2)


def _values(s: _State) -> dict:
    pw = s.pw; vw = s.vw
    cur = pw[-1] if pw else 0.0

    # momentum_5m: compare to 5 bars ago
    mom5 = None
    if len(pw) >= 6:
        past = pw[-6]
        if past:
            mom5 = max(-1.0, min(1.0, (cur - past) / past / 0.10))

    # momentum_1h: compare to 60 bars ago (oldest in the 61-bar window)
    mom60 = None
    if len(pw) == _PRICE_WIN:
        past = pw[0]
        if past:
            mom60 = max(-1.0, min(1.0, (cur - past) / past / 0.10))

    # volume_surge: latest vs 20-bar average
    vol_score = None
    if len(vw) == _VOL_WIN:
        vols = list(vw)
        avg = sum(vols[:20]) / 20
        if avg:
            vol_score = max(-1.0, min(1.0, (vols[20] / avg - 1.0) / 2.0))

    return {
        'rsi':         s.rsi_val,
        'macd_hist':   s.macd_hist,
        'macd_line':   s.macd_line,
        'boll_pct_b':  s.boll_pct_b,
        'ema_cross':   s.ema_cross,
        'momentum_5m': mom5,
        'momentum_1h': mom60,
        'vol_score':   vol_score,
    }


# Global singleton
indicator_cache = IndicatorCache()
