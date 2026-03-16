"""
Signal Engine — pure math, zero API calls.
Computes a 0-100 confidence score per asset in <10ms.
Triggered by price tick events (event-driven, no polling).
"""
import time
import threading
from core.event_bus import bus, EVT_PRICE_TICK, EVT_SIGNAL_READY, EVT_ALERT
from core.data_store import store
from core.config import SIGNAL_ALERT_THRESHOLD, PRICE_HISTORY_LEN
from signals.indicators import rsi, macd, bollinger, ema_crossover, momentum, volume_surge
from signals.weights import get_weights

# Debounce: don't recalculate same symbol more often than this (ms)
_MIN_RECALC_MS = 100
_last_calc: dict[str, float] = {}
_lock = threading.Lock()


def _component_score(symbol: str) -> dict[str, float]:
    """
    Compute each signal component as a value in [-1, +1].
    Returns dict of component_name -> score.
    """
    prices  = store.get_close_series(symbol)
    volumes = store.get_volume_series(symbol)
    scores: dict[str, float] = {}

    # ── Technical indicators ────────────────────────────────────────────────
    # Momentum 5m (last 5 bars of 1m data = 5 min)
    m5 = momentum(prices, 5)
    scores['momentum_5m'] = m5 if m5 is not None else 0.0

    # Momentum 1h (last 60 bars)
    m60 = momentum(prices, 60)
    scores['momentum_1h'] = m60 if m60 is not None else 0.0

    # Volume surge
    vs = volume_surge(volumes, 20)
    scores['volume_surge'] = vs if vs is not None else 0.0

    # RSI — map 0-100 to -1 to +1 (overbought/oversold)
    rsi_val = rsi(prices, 14)
    if rsi_val is not None:
        if rsi_val >= 70:
            scores['rsi'] = -0.5 - (rsi_val - 70) / 60  # overbought = bearish
        elif rsi_val <= 30:
            scores['rsi'] = 0.5 + (30 - rsi_val) / 60   # oversold = bullish
        else:
            scores['rsi'] = (rsi_val - 50) / 50 * 0.4   # mild directional
    else:
        scores['rsi'] = 0.0

    # MACD
    macd_line, sig_line, hist = macd(prices)
    if hist is not None:
        price_ref = prices[-1] if prices else 1
        if price_ref != 0:
            norm = hist / (price_ref * 0.01)  # normalize by 1% of price
            scores['macd'] = max(-1.0, min(1.0, norm))
        else:
            scores['macd'] = 0.0
    else:
        scores['macd'] = 0.0

    # Bollinger band position
    upper, mid, lower, pct_b = bollinger(prices)
    if pct_b is not None:
        # pct_b < 0.2 = oversold (+1), pct_b > 0.8 = overbought (-1)
        scores['bollinger'] = max(-1.0, min(1.0, (0.5 - pct_b) * 2))
    else:
        scores['bollinger'] = 0.0

    # EMA 50/200 crossover
    ema_score = ema_crossover(prices, 50, 200)
    scores['ema_cross'] = ema_score if ema_score is not None else 0.0

    # ── Auxiliary data ──────────────────────────────────────────────────────
    # Options flow put/call ratio
    opt = store.options.get(symbol, {})
    pc = opt.get('pc_ratio', 1.0)
    if pc < 0.7:
        scores['options_flow'] = min(1.0, (1.0 - pc) / 0.6)
    elif pc > 1.3:
        scores['options_flow'] = max(-1.0, -(pc - 1.0) / 1.0)
    else:
        scores['options_flow'] = 0.0

    # Fear & Greed (macro sentiment, same for all assets)
    fg = store.fear_greed.get('value', 50)
    scores['fear_greed'] = (fg - 50) / 50  # -1 to +1

    # Reddit sentiment for this symbol
    reddit = store.reddit.get(symbol, {})
    bull_pct = reddit.get('bull_pct', 50)
    scores['reddit'] = (bull_pct - 50) / 50  # -1 to +1

    # Whale movements (BTC/ETH boost)
    whale_score = 0.0
    whales = store.whale
    sym_whales = [w for w in whales if w.get('symbol') == symbol]
    if sym_whales:
        # Recent large whale buys = slight positive
        whale_score = min(1.0, len(sym_whales) * 0.25)
    scores['whale'] = whale_score

    # Congress + insider combined
    insider = store.insider.get(symbol, {})
    buys = insider.get('buys', 0)
    sells = insider.get('sells', 0)
    net_insider = (buys - sells) / max(buys + sells, 1)

    congress = store.congress
    cong_buys = sum(1 for t in congress if t.get('ticker') == symbol and 'purchase' in t.get('type','').lower())
    cong_sells = sum(1 for t in congress if t.get('ticker') == symbol and 'sale' in t.get('type','').lower())
    net_cong = (cong_buys - cong_sells) / max(cong_buys + cong_sells, 1)

    scores['congress_insider'] = max(-1.0, min(1.0, (net_insider + net_cong) / 2))

    # Macro regime adjustment
    regime = store.macro.get('regime', 'NEUTRAL')
    vix = store.macro.get('vix', 20)
    if regime == 'RISK_ON':
        scores['macro'] = 0.5
    elif regime == 'RISK_OFF':
        scores['macro'] = -0.8
    elif regime == 'RATE_PRESSURE':
        scores['macro'] = -0.4
    else:
        scores['macro'] = max(-1.0, min(1.0, (20 - vix) / 20))

    return scores


def calculate_signal(symbol: str) -> dict:
    """
    Compute full signal for a symbol.
    Returns {score: 0-100, signal: str, components: dict, ts: float}
    """
    weights = get_weights()
    components = _component_score(symbol)

    # Weighted sum: each component is -1 to +1
    raw = sum(components.get(k, 0) * w for k, w in weights.items())

    # Map -1..+1 → 0..100
    score = round(max(0, min(100, raw * 50 + 50)), 1)

    if score >= 78:
        signal = 'Strong Buy'
    elif score >= 62:
        signal = 'Buy'
    elif score >= 42:
        signal = 'Hold'
    elif score >= 28:
        signal = 'Sell'
    else:
        signal = 'Strong Sell'

    return {
        'symbol': symbol,
        'score': score,
        'signal': signal,
        'components': {k: round(v, 3) for k, v in components.items()},
        'rsi': rsi(store.get_close_series(symbol), 14),
        'ts': time.time(),
    }


def _on_price_tick(data: dict):
    """Event handler: called on every price tick."""
    symbol = data.get('symbol')
    if not symbol:
        return

    now = time.time() * 1000  # ms
    with _lock:
        last = _last_calc.get(symbol, 0)
        if now - last < _MIN_RECALC_MS:
            return
        _last_calc[symbol] = now

    # Calculate signal (this must be fast)
    result = calculate_signal(symbol)
    store.set_signal(symbol, result)
    bus.publish(EVT_SIGNAL_READY, result)

    # Alert if threshold crossed
    if result['score'] >= SIGNAL_ALERT_THRESHOLD:
        bus.publish(EVT_ALERT, {
            'symbol': symbol,
            'score': result['score'],
            'signal': result['signal'],
            'message': f"{symbol} hit {result['score']:.0f} confidence — {result['signal']}",
        })


def start_signal_engine():
    """Subscribe to price ticks and start processing."""
    bus.subscribe(EVT_PRICE_TICK, _on_price_tick)
    print('[SignalEngine] started — event-driven mode')
