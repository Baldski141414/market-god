"""
Opportunity Ranker — ranks all assets by signal score every 30 seconds.
Publishes top-10 opportunities via event bus.
"""
import threading
import time
from core.event_bus import bus, EVT_RANK_UPDATE
from core.data_store import store
from core.config import RANK_REFRESH_SECS, TOP_OPPORTUNITIES
from signals.engine import calculate_signal


def _rank_loop():
    while True:
        try:
            all_signals = store.get_all_signals()
            # Also compute for any symbol with price data but no recent signal
            prices = store.latest_prices
            for sym in list(prices.keys()):
                if sym not in all_signals:
                    try:
                        result = calculate_signal(sym)
                        store.set_signal(sym, result)
                        all_signals[sym] = result
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
