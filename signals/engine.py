"""
Signal Engine — pure math, zero API calls.
Computes a 0-100 confidence score per asset in <10ms.
Triggered by price tick events (event-driven, no polling).

Crypto:  recalculates on every Kraken tick (no debounce), 24/7
Stocks:  recalculates on every Yahoo tick (market hours only)
All indicators are served from the incremental IndicatorCache (O(1) per tick).
"""
import time
import datetime
import threading
from zoneinfo import ZoneInfo

from core.event_bus import bus, EVT_PRICE_TICK, EVT_SIGNAL_READY, EVT_ALERT
from core.data_store import store
from core.config import (
    SIGNAL_ALERT_THRESHOLD, CRYPTO_SYMBOLS, CRYPTO_WEIGHTS,
)
from signals.indicator_cache import indicator_cache
from signals.weights import get_weights

_ET   = ZoneInfo('America/New_York')
_lock = threading.Lock()
_last_calc: dict[str, float] = {}

# Stocks get a small safety floor so a burst of simultaneous Yahoo ticks
# for the same symbol doesn't fire redundant calculations.
_STOCK_MIN_RECALC_MS = 100


def _is_market_hours() -> bool:
    """Return True if NYSE is currently open."""
    now = datetime.datetime.now(_ET)
    if now.weekday() >= 5:          # Sat / Sun
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t


def _component_score(symbol: str) -> dict[str, float]:
    """
    Compute each signal component as a value in [-1, +1].
    Technical indicators come from the fast incremental cache (O(1)).
    Auxiliary signals are looked up from the data store.
    """
    ind = indicator_cache.get(symbol)
    scores: dict[str, float] = {}

    # ── Technical indicators (from cache) ───────────────────────────────
    scores['momentum_5m'] = ind.get('momentum_5m') or 0.0
    scores['momentum_1h'] = ind.get('momentum_1h') or 0.0

    vs = ind.get('vol_score')
    scores['volume_surge'] = vs if vs is not None else 0.0

    rsi_val = ind.get('rsi')
    if rsi_val is not None:
        if rsi_val >= 70:
            scores['rsi'] = -0.5 - (rsi_val - 70) / 60
        elif rsi_val <= 30:
            scores['rsi'] = 0.5 + (30 - rsi_val) / 60
        else:
            scores['rsi'] = (rsi_val - 50) / 50 * 0.4
    else:
        scores['rsi'] = 0.0

    hist = ind.get('macd_hist')
    if hist is not None:
        latest = store.get_latest(symbol)
        price_ref = (latest or {}).get('price', 1) or 1
        scores['macd'] = max(-1.0, min(1.0, hist / (price_ref * 0.01)))
    else:
        scores['macd'] = 0.0

    pct_b = ind.get('boll_pct_b')
    if pct_b is not None:
        scores['bollinger'] = max(-1.0, min(1.0, (0.5 - pct_b) * 2))
    else:
        scores['bollinger'] = 0.0

    scores['ema_cross'] = ind.get('ema_cross') or 0.0

    # ── Auxiliary data ───────────────────────────────────────────────────
    opt = store.options.get(symbol, {})
    pc  = opt.get('pc_ratio', 1.0)
    if pc < 0.7:
        scores['options_flow'] = min(1.0, (1.0 - pc) / 0.6)
    elif pc > 1.3:
        scores['options_flow'] = max(-1.0, -(pc - 1.0) / 1.0)
    else:
        scores['options_flow'] = 0.0

    fg = store.fear_greed.get('value', 50)
    scores['fear_greed'] = (fg - 50) / 50

    reddit = store.reddit.get(symbol, {})
    scores['reddit'] = (reddit.get('bull_pct', 50) - 50) / 50

    sym_whales = [w for w in store.whale if w.get('symbol') == symbol]
    scores['whale'] = min(1.0, len(sym_whales) * 0.25)

    insider   = store.insider.get(symbol, {})
    buys      = insider.get('buys', 0)
    sells     = insider.get('sells', 0)
    net_ins   = (buys - sells) / max(buys + sells, 1)
    congress  = store.congress
    cb = sum(1 for t in congress if t.get('ticker') == symbol and 'purchase' in t.get('type','').lower())
    cs = sum(1 for t in congress if t.get('ticker') == symbol and 'sale'     in t.get('type','').lower())
    net_cong  = (cb - cs) / max(cb + cs, 1)
    scores['congress_insider'] = max(-1.0, min(1.0, (net_ins + net_cong) / 2))

    regime = store.macro.get('regime', 'NEUTRAL')
    vix    = store.macro.get('vix', 20)
    if regime == 'RISK_ON':
        scores['macro'] = 0.5
    elif regime == 'RISK_OFF':
        scores['macro'] = -0.8
    elif regime == 'RATE_PRESSURE':
        scores['macro'] = -0.4
    else:
        scores['macro'] = max(-1.0, min(1.0, (20 - vix) / 20))

    # ── Alternative Data (from altdata store) ───────────────────
    alt = store.altdata

    # Geopolitical risk (GDELT)
    geo = alt.get('gdelt') or {}
    geo_risk = geo.get('global_risk', 50)
    ticker_risks = geo.get('affected_tickers') or {}
    if symbol in ticker_risks:
        # Symbol is specifically flagged — use max regional risk
        max_regional = max((r['risk'] for r in ticker_risks[symbol]), default=geo_risk)
        scores['geopolitical'] = -(max_regional - 50) / 50
    else:
        scores['geopolitical'] = -(geo_risk - 50) / 50

    # Patent activity (USPTO/PatentsView)
    patents = alt.get('patents') or {}
    pat = patents.get(symbol) or {}
    scores['patent'] = float(pat.get('signal', 0.0))

    # Shipping / Baltic Dry Index (FRED)
    shipping = alt.get('shipping') or {}
    scores['shipping_bdi'] = float(shipping.get('macro_signal', 0.0))

    # Prediction markets (Polymarket)
    pred_mkts = alt.get('prediction_markets') or {}
    by_ticker = pred_mkts.get('by_ticker') or {}
    if symbol in by_ticker:
        scores['prediction_market'] = float(by_ticker[symbol].get('signal', 0.0))
    else:
        # Use fed cut probability as macro signal when no direct ticker match
        fed_prob = pred_mkts.get('fed_cut_prob', 50.0)
        scores['prediction_market'] = (fed_prob - 50) / 100  # small default

    # Dark pool activity (FINRA RegSho)
    dark_pool = alt.get('dark_pool') or {}
    dp = dark_pool.get(symbol) or {}
    scores['dark_pool'] = float(dp.get('signal', 0.0))

    # Supply chain stress (SEC EDGAR)
    supply_chain = alt.get('supply_chain') or {}
    sc = supply_chain.get(symbol) or {}
    scores['supply_chain'] = float(sc.get('signal', 0.0))

    # Earnings call NLP (SEC 8-K)
    enlp = alt.get('earnings_nlp') or {}
    en = enlp.get(symbol) or {}
    scores['earnings_nlp'] = float(en.get('sentiment', 0.0))

    # Mempool (Bitcoin only — weight=0 for stocks)
    mp = alt.get('mempool') or {}
    scores['mempool'] = float(mp.get('signal', 0.0))

    return scores


def calculate_signal(symbol: str) -> dict:
    """
    Compute full signal for a symbol.
    Returns {score: 0-100, signal: str, components: dict, ts: float}
    """
    is_crypto = symbol in CRYPTO_SYMBOLS
    weights   = CRYPTO_WEIGHTS if is_crypto else get_weights()
    components = _component_score(symbol)

    raw   = sum(components.get(k, 0) * w for k, w in weights.items())
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
        'symbol':     symbol,
        'score':      score,
        'signal':     signal,
        'components': {k: round(v, 3) for k, v in components.items()},
        'rsi':        indicator_cache.get(symbol).get('rsi'),
        'ts':         time.time(),
    }


def _on_price_tick(data: dict):
    """Event handler: called on every price tick."""
    symbol = data.get('symbol')
    if not symbol:
        return

    price  = data.get('price', 0)
    volume = data.get('volume', 0)

    # Always keep the indicator cache current (O(1) per tick)
    if price > 0:
        indicator_cache.update(symbol, price, volume)

    is_crypto = symbol in CRYPTO_SYMBOLS

    # Stocks only recalculate during market hours
    if not is_crypto and not _is_market_hours():
        return

    now = time.time() * 1000  # ms
    # Crypto: no debounce — every Kraken tick fires a signal update
    # Stocks: 100ms safety floor (Yahoo already throttles to 30s)
    min_ms = 0 if is_crypto else _STOCK_MIN_RECALC_MS

    with _lock:
        last = _last_calc.get(symbol, 0)
        if now - last < min_ms:
            return
        _last_calc[symbol] = now

    result = calculate_signal(symbol)
    store.set_signal(symbol, result)
    bus.publish(EVT_SIGNAL_READY, result)

    if result['score'] >= SIGNAL_ALERT_THRESHOLD:
        bus.publish(EVT_ALERT, {
            'symbol':  symbol,
            'score':   result['score'],
            'signal':  result['signal'],
            'message': f"{symbol} hit {result['score']:.0f} confidence — {result['signal']}",
        })


def start_signal_engine():
    """Subscribe to price ticks and start processing."""
    bus.subscribe(EVT_PRICE_TICK, _on_price_tick)
    print('[SignalEngine] started — event-driven, incremental cache, crypto real-time')
