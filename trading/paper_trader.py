"""
Paper Trading Engine — Maximum Aggression Mode.

Speed: signal evaluation every 1s, stop checks every 1s, instant triggers fire in <100ms.
Order execution is always async (background thread) — never blocks the signal loop.

Instant trigger rules (fire immediately, bypass score threshold):
  1. Price up 1%+ in 5 min + volume 2x average
  2. Whale accumulation >$50M in last 5 min
  3. Congress purchase filed today
  4. Short interest >15% + price moving up 0.5%
  5. 52-week high break + volume surge 2x
  6. Options flow 10x normal volume

Position sizing:
  Crypto: 15% per trade, max 8 positions
  Stocks: 20% per trade, max 10 positions

Risk/Reward:
  Stop-loss: -4%  |  Take-profit: +15%  |  Trailing stop: +8% activation

Trade log records: WHY it fired, which trigger, latency in milliseconds.
"""
import time
import datetime
import threading
from zoneinfo import ZoneInfo

from core.event_bus import bus, EVT_SIGNAL_READY, EVT_TRADE
from core.data_store import store
from core.config import (
    CRYPTO_SYMBOLS,
    MAX_POSITIONS, MIN_SIGNAL_TO_BUY, STOCK_POSITION_PCT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_TRIGGER_PCT,
    CRYPTO_MIN_SIGNAL_TO_BUY, CRYPTO_POSITION_PCT,
    CRYPTO_STOP_LOSS_PCT, CRYPTO_TAKE_PROFIT_PCT, CRYPTO_TRAILING_TRIGGER,
    MAX_CRYPTO_POSITIONS,
    INSTANT_PRICE_UP_PCT, INSTANT_VOLUME_MULT, INSTANT_WHALE_USD,
    INSTANT_SHORT_PCT, INSTANT_SHORT_PRICE_UP, INSTANT_OPTIONS_MULT,
)
import trading.alpaca as alpaca

_ET   = ZoneInfo('America/New_York')
_lock = threading.Lock()

# Track symbols that already have pending instant-trigger buy so we don't double-fire
_instant_fired: dict[str, float] = {}   # symbol -> timestamp of last instant trigger


# ── Instant Trigger Detectors ──────────────────────────────────────────────

def _check_price_volume_surge(symbol: str) -> bool:
    """Price up 1%+ in last 5 minutes AND volume 2x average."""
    history = store.get_prices(symbol)
    if len(history) < 5:
        return False
    now = time.time()
    five_min_ago = now - 300
    recent = [(ts, p, v) for ts, p, v in history if ts >= five_min_ago]
    if len(recent) < 2:
        return False
    price_then = recent[0][1]
    price_now  = recent[-1][1]
    if price_then <= 0:
        return False
    price_change = (price_now - price_then) / price_then
    if price_change < INSTANT_PRICE_UP_PCT:
        return False
    # Volume check: recent avg vs 20-bar rolling avg
    all_vols    = [v for _, _, v in history if v > 0]
    recent_vols = [v for _, _, v in recent if v > 0]
    if not all_vols or not recent_vols:
        return False
    avg_vol    = sum(all_vols[-20:]) / min(20, len(all_vols))
    recent_avg = sum(recent_vols) / len(recent_vols)
    return avg_vol > 0 and recent_avg >= avg_vol * INSTANT_VOLUME_MULT


def _check_whale_trigger(symbol: str) -> bool:
    """Whale accumulation >$50M in last 5 minutes."""
    if symbol not in CRYPTO_SYMBOLS:
        return False
    now = time.time()
    cutoff = now - 300
    recent_whales = [
        w for w in store.whale
        if w.get('symbol') == symbol and w.get('ts', 0) >= cutoff
    ]
    total_usd = sum(w.get('usd_value', 0) for w in recent_whales)
    return total_usd >= INSTANT_WHALE_USD


def _check_congress_trigger(symbol: str) -> bool:
    """Congress purchase filed today."""
    today = datetime.date.today().isoformat()
    for trade in store.congress:
        if (trade.get('ticker') == symbol
                and 'purchase' in trade.get('type', '').lower()
                and trade.get('date', '') == today):
            return True
    return False


def _check_short_squeeze(symbol: str) -> bool:
    """Short interest >15% AND price moving up 0.5%."""
    dark_pool = store.altdata.get('dark_pool') or {}
    dp = dark_pool.get(symbol) or {}
    short_pct = dp.get('short_pct', 0)
    if short_pct < INSTANT_SHORT_PCT:
        return False
    history = store.get_prices(symbol)
    if len(history) < 2:
        return False
    now = time.time()
    recent = [(ts, p, v) for ts, p, v in history if ts >= now - 300]
    if len(recent) < 2:
        return False
    price_change = (recent[-1][1] - recent[0][1]) / recent[0][1] if recent[0][1] > 0 else 0
    return price_change >= INSTANT_SHORT_PRICE_UP


def _check_52wk_high(symbol: str) -> bool:
    """52-week high break + volume surge 2x."""
    history = store.get_prices(symbol)
    if len(history) < 50:
        return False
    prices  = [p for _, p, _ in history]
    volumes = [v for _, _, v in history]
    current_price = prices[-1]
    year_high     = max(prices)
    # Breaking or at 52-week high (within 0.1%)
    if current_price < year_high * 0.999:
        return False
    all_vols = [v for v in volumes if v > 0]
    if not all_vols or volumes[-1] == 0:
        return False
    avg_vol = sum(all_vols[-20:]) / min(20, len(all_vols))
    return avg_vol > 0 and volumes[-1] >= avg_vol * INSTANT_VOLUME_MULT


def _check_options_flow(symbol: str) -> bool:
    """Options flow 10x normal volume."""
    opt = store.options.get(symbol) or {}
    calls_vol = opt.get('calls_vol', 0)
    puts_vol  = opt.get('puts_vol', 0)
    total_vol = calls_vol + puts_vol
    if total_vol == 0:
        return False
    # We compare against a rolling baseline stored in opt; if unavailable use a
    # fixed floor: any total_vol >= 10x the typical threshold (100k contracts = 10x)
    baseline = opt.get('avg_total_vol', 0)
    if baseline > 0:
        return total_vol >= baseline * INSTANT_OPTIONS_MULT
    # Fallback: very high absolute volume signals unusual activity
    return total_vol >= 500_000  # half-million contracts = clearly unusual


def _detect_instant_trigger(symbol: str) -> str | None:
    """
    Check all instant trigger conditions.
    Returns trigger name string if any fires, else None.
    """
    if _check_price_volume_surge(symbol):
        return 'price_volume_surge'
    if _check_whale_trigger(symbol):
        return 'whale_accumulation'
    if _check_congress_trigger(symbol):
        return 'congress_purchase'
    if _check_short_squeeze(symbol):
        return 'short_squeeze'
    if _check_52wk_high(symbol):
        return '52wk_high_break'
    if _check_options_flow(symbol):
        return 'options_flow_10x'
    return None


# ── Portfolio helpers ──────────────────────────────────────────────────────

def _win_rate() -> float:
    trades = store.portfolio.get('trades', [])
    closed = [t for t in trades if t.get('pnl') is not None]
    if not closed:
        return 0.55
    wins = sum(1 for t in closed if t['pnl'] > 0)
    return wins / len(closed)


def _portfolio_value() -> float:
    p = store.portfolio
    equity = sum(
        pos['shares'] * (store.get_latest(sym) or {}).get('price', pos['avg_price'])
        for sym, pos in p['positions'].items()
    )
    return p['cash'] + equity


def _crypto_position_count(p: dict) -> int:
    return sum(1 for sym in p['positions'] if sym in CRYPTO_SYMBOLS)


# ── Trade execution ────────────────────────────────────────────────────────

def _try_buy(symbol: str, price: float, signal: dict, trigger: str = 'signal', tick_ts: float = None):
    """
    Attempt to open a position.
    trigger: human-readable reason this trade fired.
    tick_ts: timestamp of the price tick that caused this (for latency calc).
    """
    if price <= 0:
        return
    is_crypto = symbol in CRYPTO_SYMBOLS
    fired_ts  = time.time()
    latency_ms = round((fired_ts - tick_ts) * 1000, 1) if tick_ts else None

    def _execute(p):
        # Position-limit checks
        if is_crypto:
            if _crypto_position_count(p) >= MAX_CRYPTO_POSITIONS:
                return
        else:
            if len(p['positions']) - _crypto_position_count(p) >= MAX_POSITIONS:
                return
        if symbol in p['positions']:
            return
        if p['cash'] < 50:
            return

        pv      = _portfolio_value()
        pct     = CRYPTO_POSITION_PCT if is_crypto else STOCK_POSITION_PCT
        invest  = pv * pct
        invest  = min(invest, p['cash'] * 0.95)
        invest  = max(invest, 50)
        shares  = invest / price

        p['positions'][symbol] = {
            'shares':        shares,
            'avg_price':     price,
            'buy_time':      fired_ts,
            'highest_price': price,
            'cost_basis':    invest,
        }
        p['cash'] -= invest
        p['trades'].append({
            'id':          len(p['trades']) + 1,
            'ts':          fired_ts,
            'symbol':      symbol,
            'action':      'BUY',
            'price':       price,
            'shares':      shares,
            'value':       invest,
            'score':       signal.get('score'),
            'signal':      signal.get('signal'),
            'trigger':     trigger,
            'latency_ms':  latency_ms,
            'pnl':         None,
        })
        bus.publish(EVT_TRADE, {
            'action':     'BUY',
            'symbol':     symbol,
            'price':      price,
            'shares':     shares,
            'trigger':    trigger,
            'latency_ms': latency_ms,
        })
        # Mirror to Alpaca — async, never blocks signal loop
        threading.Thread(
            target=alpaca.submit_order,
            args=(symbol, 'buy'),
            kwargs={'notional': invest},
            daemon=True,
        ).start()

    store.update_portfolio(_execute)


def _try_sell(symbol: str, price: float, reason: str = 'signal'):
    """Close a position."""
    if price <= 0:
        return

    def _execute(p):
        pos = p['positions'].get(symbol)
        if not pos:
            return
        sell_shares = pos['shares']
        proceeds    = sell_shares * price
        pnl         = proceeds - pos['cost_basis']
        p['cash']  += proceeds
        del p['positions'][symbol]
        p['trades'].append({
            'id':      len(p['trades']) + 1,
            'ts':      time.time(),
            'symbol':  symbol,
            'action':  'SELL',
            'price':   price,
            'shares':  sell_shares,
            'value':   proceeds,
            'pnl':     round(pnl, 2),
            'pnl_pct': round(pnl / pos['cost_basis'] * 100, 2) if pos['cost_basis'] else 0,
            'reason':  reason,
            'trigger': reason,
            'score':   None,
            'signal':  None,
        })
        bus.publish(EVT_TRADE, {'action': 'SELL', 'symbol': symbol,
                                'price': price, 'pnl': pnl})
        # Mirror to Alpaca — async
        threading.Thread(
            target=alpaca.submit_order,
            args=(symbol, 'sell'),
            kwargs={'qty': sell_shares},
            daemon=True,
        ).start()

    store.update_portfolio(_execute)


def _check_stops():
    """Check stop-loss / take-profit / trailing stop for all open positions."""
    positions = dict(store.portfolio.get('positions', {}))
    for symbol, pos in positions.items():
        is_crypto  = symbol in CRYPTO_SYMBOLS
        stop_pct   = CRYPTO_STOP_LOSS_PCT   if is_crypto else STOP_LOSS_PCT
        tp_pct     = CRYPTO_TAKE_PROFIT_PCT if is_crypto else TAKE_PROFIT_PCT
        trail_trig = CRYPTO_TRAILING_TRIGGER if is_crypto else TRAILING_TRIGGER_PCT

        latest = store.get_latest(symbol)
        if not latest:
            continue
        price = latest.get('price', 0)
        if price <= 0:
            continue

        avg     = pos['avg_price']
        pnl_pct = (price - avg) / avg

        # Update trailing high
        def _update_high(p, sym=symbol):
            pos_inner = p['positions'].get(sym)
            if pos_inner and price > pos_inner.get('highest_price', 0):
                pos_inner['highest_price'] = price
        store.update_portfolio(_update_high)

        highest    = store.portfolio['positions'].get(symbol, {}).get('highest_price', avg)
        trail_drop = (price - highest) / highest if highest > 0 else 0

        if pnl_pct <= stop_pct:
            _try_sell(symbol, price, 'stop_loss')
        elif pnl_pct >= tp_pct:
            _try_sell(symbol, price, 'take_profit')
        elif pnl_pct >= trail_trig and trail_drop <= -0.015:
            _try_sell(symbol, price, 'trailing_stop')


def _on_signal(data: dict):
    """React to signal events — checks threshold + instant triggers."""
    if not data:
        return

    symbol    = data.get('symbol')
    score     = data.get('score', 0)
    signal    = data.get('signal', '')
    is_crypto = symbol in CRYPTO_SYMBOLS
    min_score = CRYPTO_MIN_SIGNAL_TO_BUY if is_crypto else MIN_SIGNAL_TO_BUY
    tick_ts   = data.get('ts', time.time())

    latest = store.get_latest(symbol)
    if not latest:
        return
    price = latest.get('price', 0)

    with _lock:
        # ── Instant trigger check (bypasses score threshold) ────────────
        now = time.time()
        last_instant = _instant_fired.get(symbol, 0)
        if now - last_instant > 60:  # cooldown: 60s per symbol for instant triggers
            trigger = _detect_instant_trigger(symbol)
            if trigger and symbol not in store.portfolio.get('positions', {}):
                _instant_fired[symbol] = now
                _try_buy(symbol, price, data, trigger=f'instant_{trigger}', tick_ts=tick_ts)
                return  # instant trigger fired — skip normal signal logic

        # ── Normal threshold logic ──────────────────────────────────────
        if score >= min_score and signal in ('Strong Buy', 'Buy'):
            _try_buy(symbol, price, data, trigger='signal', tick_ts=tick_ts)
        elif signal in ('Sell', 'Strong Sell'):
            _try_sell(symbol, price, 'signal')

        _check_stops()

        if symbol == 'SPY' and store.portfolio.get('spy_basis') is None and price > 0:
            def _set_spy(p):
                p['spy_basis']      = price
                p['spy_basis_time'] = time.time()
            store.update_portfolio(_set_spy)


def _trader_loop():
    """
    Background loop: check stops + instant triggers every 1 second.
    Runs independently of signal events for maximum speed.
    """
    while True:
        try:
            _check_stops()
            # Scan for instant triggers on all symbols with prices
            prices = dict(store.latest_prices)
            now    = time.time()
            for symbol, latest in prices.items():
                price = latest.get('price', 0)
                if price <= 0:
                    continue
                if symbol in store.portfolio.get('positions', {}):
                    continue
                last_instant = _instant_fired.get(symbol, 0)
                if now - last_instant <= 60:
                    continue
                trigger = _detect_instant_trigger(symbol)
                if trigger:
                    _instant_fired[symbol] = now
                    cached_signal = store.get_signal(symbol) or {}
                    _try_buy(symbol, price, cached_signal,
                             trigger=f'instant_{trigger}', tick_ts=now)
        except Exception as e:
            print(f'[PaperTrader] loop error: {e}')
        time.sleep(1)


def start_paper_trader():
    bus.subscribe(EVT_SIGNAL_READY, _on_signal)
    t = threading.Thread(target=_trader_loop, daemon=True, name='trader-loop')
    t.start()
    print(
        '[PaperTrader] started — crypto>=45 (24/7, 15% pos, -4%/+15%), '
        'stocks>=55 (20% pos, -4%/+15%), instant triggers ON, 1s loop'
    )
