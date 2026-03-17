"""
Opportunity Ranker — ranks all assets by signal score every 30 seconds.
Publishes top-10 opportunities via event bus.

Crypto signals are kept fresh by real-time Kraken ticks, so the ranker
only re-scans stock symbols.  Crypto is included in the final ranking
via its cached signal values.
"""
import threading
import time
from core.event_bus import bus, EVT_RANK_UPDATE, EVT_SIGNAL_READY
from core.data_store import store
from core.config import RANK_REFRESH_SECS, TOP_OPPORTUNITIES, CRYPTO_SYMBOLS
from signals.engine import calculate_signal


def _rank_loop():
    while True:
        try:
            prices     = store.latest_prices
            all_signals: dict = {}

            for sym in list(prices.keys()):
                if sym in CRYPTO_SYMBOLS:
                    # Crypto: use cached signal — real-time ticks keep it fresh
                    cached = store.get_signal(sym)
                    if cached:
                        all_signals[sym] = cached
                    continue
                # Stocks: recompute and publish EVT_SIGNAL_READY so paper trader
                # evaluates any symbols that may have missed their price tick.
                try:
                    result = calculate_signal(sym)
                    store.set_signal(sym, result)
                    all_signals[sym] = result
                    bus.publish(EVT_SIGNAL_READY, result)
                except Exception:
                    pass

            ranked = sorted(
                [v for v in all_signals.values() if v.get('score') is not None],
                key=lambda x: x['score'],
                reverse=True,
            )

            opportunities = []
            for item in ranked[:TOP_OPPORTUNITIES]:
                sym    = item['symbol']
                latest = store.get_latest(sym) or {}
                opportunities.append({
                    'symbol':     sym,
                    'score':      item['score'],
                    'signal':     item['signal'],
                    'price':      latest.get('price', 0),
                    'change_pct': latest.get('change_pct', 0),
                    'components': item.get('components', {}),
                    'rsi':        item.get('rsi'),
                    'ts':         item.get('ts', 0),
                })

            store.set_opportunities(opportunities)
            bus.publish(EVT_RANK_UPDATE, opportunities)

        except Exception as e:
            print(f'[Ranker] error: {e}')

        time.sleep(RANK_REFRESH_SECS)


def start_ranker():
    t = threading.Thread(target=_rank_loop, daemon=True, name='ranker')
    t.start()
    print('[Ranker] started — rescans every 1s, crypto uses real-time cache')
