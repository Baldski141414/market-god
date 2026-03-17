"""
Opportunity Ranker — ranks all assets by signal score every 30 seconds.
Publishes top-10 opportunities via event bus.
Also fires EVT_SIGNAL_READY for every asset it scans so the paper trader
sees a full-universe sweep across all 134+ assets on every cycle.
"""
import threading
import time
from core.event_bus import bus, EVT_RANK_UPDATE, EVT_SIGNAL_READY
from core.data_store import store
from core.config import RANK_REFRESH_SECS, TOP_OPPORTUNITIES
from signals.engine import calculate_signal


def _rank_loop():
    while True:
        try:
            # Recompute signals for ALL symbols with price data on every sweep.
            # This ensures the paper trader sees a full 134+ asset scan every
            # RANK_REFRESH_SECS, not just assets that happened to get a price tick.
            prices = store.latest_prices
            all_signals: dict = {}
            for sym in list(prices.keys()):
                try:
                    result = calculate_signal(sym)
                    store.set_signal(sym, result)
                    all_signals[sym] = result
                    bus.publish(EVT_SIGNAL_READY, result)
                except Exception:
                    pass

            # Sort by score descending
            ranked = sorted(
                [v for v in all_signals.values() if v.get('score') is not None],
                key=lambda x: x['score'],
                reverse=True,
            )

            top = ranked[:TOP_OPPORTUNITIES]

            # Enrich with latest price data
            opportunities = []
            for item in top:
                sym = item['symbol']
                latest = store.get_latest(sym) or {}
                opportunities.append({
                    'symbol': sym,
                    'score': item['score'],
                    'signal': item['signal'],
                    'price': latest.get('price', 0),
                    'change_pct': latest.get('change_pct', 0),
                    'components': item.get('components', {}),
                    'rsi': item.get('rsi'),
                    'ts': item.get('ts', 0),
                })

            store.set_opportunities(opportunities)
            bus.publish(EVT_RANK_UPDATE, opportunities)

        except Exception as e:
            print(f'[Ranker] error: {e}')

        time.sleep(RANK_REFRESH_SECS)


def start_ranker():
    t = threading.Thread(target=_rank_loop, daemon=True, name='ranker')
    t.start()
    print('[Ranker] started')
