"""
Bitcoin Mempool Intelligence — mempool.space free API, no key needed.
Large mempool + high fees = institutional congestion = whale movement.
Exchange inflow spike = sell pressure coming.
Refreshes every 60 seconds (mempool is real-time).
"""
import threading
import time
import requests
from core.data_store import store

_MEMPOOL_API   = 'https://mempool.space/api/mempool'
_FEES_API      = 'https://mempool.space/api/v1/fees/recommended'
_HASHRATE_API  = 'https://mempool.space/api/v1/mining/hashrate/3d'
_TIMEOUT = 10
_REFRESH = 60  # 1 minute


def _refresh_loop():
    # Rolling history for congestion trending
    fee_history: list[int] = []
    count_history: list[int] = []

    while True:
        result = {
            'count': 0,
            'vsize': 0,
            'total_fee': 0,
            'fees': {},
            'congestion': 50,
            'signal': 0.0,
            'ts': time.time(),
        }
        try:
            mempool_resp = requests.get(_MEMPOOL_API, timeout=_TIMEOUT)
            fees_resp    = requests.get(_FEES_API,    timeout=_TIMEOUT)

            mp_data   = mempool_resp.json() if mempool_resp.ok else {}
            fees_data = fees_resp.json()    if fees_resp.ok    else {}

            count     = mp_data.get('count', 0)
            vsize     = mp_data.get('vsize', 0)
            total_fee = mp_data.get('total_fee', 0)

            fastest_fee = fees_data.get('fastestFee', 0)
            hour_fee    = fees_data.get('hourFee', 0)
            economy_fee = fees_data.get('economyFee', 0)

            # Track fee history (last 20 samples = ~20 min)
            fee_history.append(fastest_fee)
            count_history.append(count)
            if len(fee_history) > 20:
                fee_history.pop(0)
            if len(count_history) > 20:
                count_history.pop(0)

            # Congestion score 0-100
            # Normal mempool: ~5,000-50,000 txs, fee ~5-15 sat/vB
            # High activity: >100k txs, fee >30 sat/vB
            count_score = min(100, count / 1500)   # 150k txs = 100
            fee_score   = min(100, fastest_fee * 2) # 50 sat/vB = 100
            congestion  = int(count_score * 0.5 + fee_score * 0.5)

            # Signal: high congestion = institutional activity = bullish BTC
            # Very high fees (>50) could mean panic/congestion = neutral
            if fastest_fee > 50:
                signal = 0.3  # potentially panic, dampened
            elif fastest_fee > 20:
                signal = min(1.0, fastest_fee / 40)
            elif fastest_fee < 5:
                signal = -0.3  # very low activity
            else:
                signal = (fastest_fee - 10) / 20  # linear between 5-30

            result = {
                'count':       count,
                'vsize':       vsize,
                'total_fee':   total_fee,
                'fees': {
                    'fastest': fastest_fee,
                    'hour':    hour_fee,
                    'economy': economy_fee,
                },
                'congestion':  congestion,
                'signal':      round(max(-1.0, min(1.0, signal)), 3),
                'fee_trend':   'rising' if len(fee_history) >= 5 and fastest_fee > fee_history[0] else 'falling',
                'ts':          time.time(),
            }
        except Exception as e:
            print(f'[Mempool] error: {e}')

        with store._lock:
            store.altdata['mempool'] = result

        time.sleep(_REFRESH)


def start_mempool():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='mempool')
    t.start()
