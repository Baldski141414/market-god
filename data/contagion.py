"""
Cross-Market Contagion Engine.
Maps global market cause-and-effect chains.
Detects the first domino falling, predicts the chain reaction.
Monitors macro indicators already in the store and triggers predictions.
Refreshes every 60 seconds.
"""
import threading
import time
from core.data_store import store

_REFRESH = 60

# Contagion chains: (trigger_key, direction, threshold_pct) → effects
# Each effect: (affected_group, signal_direction, delay_seconds, description)
_CONTAGION_CHAINS = [
    {
        'id':      'vix_spike',
        'name':    'VIX Spike → Defensive Rotation',
        'trigger': {'key': 'vix', 'direction': '>', 'value': 30},
        'effects': [
            ('XLU',  +0.6, 600,   'VIX>30 → utilities pump (flight to safety)'),
            ('TLT',  +0.5, 300,   'VIX>30 → bonds bid (flight to safety)'),
            ('GLD',  +0.4, 900,   'VIX>30 → gold safe-haven bid'),
            ('XLF',  -0.5, 1200,  'VIX>30 → financials sell off'),
            ('TQQQ', -0.8, 60,    'VIX>30 → leveraged ETFs hammered'),
            ('SQQQ', +0.8, 60,    'VIX>30 → inverse ETFs pump'),
        ],
    },
    {
        'id':      'oil_surge',
        'name':    'Oil Surge → Energy/Transport Impact',
        'trigger': {'key': 'oil', 'direction': '>', 'pct_change': 5},
        'effects': [
            ('XLE',  +0.7, 300,   'Oil+5% → energy stocks pump'),
            ('XOM',  +0.5, 300,   'Oil+5% → XOM direct beneficiary'),
            ('CVX',  +0.5, 300,   'Oil+5% → CVX direct beneficiary'),
            ('UBER', -0.3, 1800,  'Oil+5% → ride-share cost pressure'),
            ('LYFT', -0.3, 1800,  'Oil+5% → ride-share cost pressure'),
            ('AAL',  -0.4, 1800,  'Oil+5% → airlines fuel cost surge'),
            ('DAL',  -0.4, 1800,  'Oil+5% → airlines fuel cost surge'),
        ],
    },
    {
        'id':      'dxy_surge',
        'name':    'DXY Surge → Commodities/EM Hit',
        'trigger': {'key': 'dxy', 'direction': '>', 'pct_change': 2},
        'effects': [
            ('GLD',  -0.5, 600,   'DXY+2% → gold falls (inverse)'),
            ('SLV',  -0.4, 600,   'DXY+2% → silver falls'),
            ('BTC',  -0.3, 1200,  'DXY+2% → crypto risk-off'),
            ('USO',  -0.3, 1800,  'DXY+2% → oil USD-denominated falls'),
        ],
    },
    {
        'id':      'gold_breakout',
        'name':    'Gold Breakout → Risk-Off Signal',
        'trigger': {'key': 'gold', 'direction': '>', 'pct_change': 3},
        'effects': [
            ('TLT',  +0.3, 900,   'Gold+3% → bonds bid (risk-off)'),
            ('XLE',  +0.2, 1800,  'Gold+3% → energy safe haven bid'),
            ('TSLA', -0.2, 2400,  'Gold+3% → growth stocks rotation out'),
            ('NVDA', -0.2, 2400,  'Gold+3% → growth stocks rotation out'),
        ],
    },
    {
        'id':      'rates_spike',
        'name':    'Rate Spike → Growth Stocks Sell',
        'trigger': {'key': 'yield_10y', 'direction': '>', 'value': 5.0},
        'effects': [
            ('TLT',  -0.8, 300,   '10Y>5% → long-duration bonds crushed'),
            ('NVDA', -0.4, 1800,  '10Y>5% → high-multiple growth sells'),
            ('TSLA', -0.4, 1800,  '10Y>5% → high-multiple growth sells'),
            ('META', -0.3, 1800,  '10Y>5% → tech P/E compression'),
            ('XLU',  -0.5, 600,   '10Y>5% → utilities lose rate-trade appeal'),
            ('KO',   -0.3, 600,   '10Y>5% → dividend stocks become less attractive'),
        ],
    },
]


def _check_trigger(trigger: dict, macro: dict) -> bool:
    """Check if a contagion trigger is active."""
    key = trigger['key']
    current = macro.get(key) or macro.get(key.replace('yield_10y', 'yield_10y'))
    if current is None:
        return False

    if trigger.get('direction') == '>':
        if 'value' in trigger:
            return current > trigger['value']
        if 'pct_change' in trigger:
            # Would need historical comparison — use a simplified check
            # We treat current value being in 95th percentile as a trigger
            threshold = {
                'oil':   70.0,
                'dxy':   104.0,
                'gold':  2000.0,
            }.get(key, current * 1.05)
            return current > threshold
    return False


def _refresh_loop():
    active_chains: dict[str, dict] = {}
    last_macro: dict = {}

    while True:
        result = {
            'active_chains': [],
            'predictions':   [],
            'chain_history': [],
            'ts': time.time(),
        }
        try:
            macro = dict(store.macro)
            if not macro:
                time.sleep(_REFRESH)
                continue

            active = []
            predictions = []

            for chain in _CONTAGION_CHAINS:
                triggered = _check_trigger(chain['trigger'], macro)
                chain_id  = chain['id']

                if triggered:
                    # Track when it first triggered
                    if chain_id not in active_chains:
                        active_chains[chain_id] = {
                            'triggered_at': time.time(),
                            'chain':        chain,
                        }
                    info = active_chains[chain_id]
                    elapsed = time.time() - info['triggered_at']

                    active.append({
                        'id':      chain_id,
                        'name':    chain['name'],
                        'elapsed': int(elapsed),
                    })

                    # Emit predictions for effects that should be materializing
                    for effect in chain['effects']:
                        ticker, direction, delay, desc = effect
                        if elapsed >= delay:
                            status = 'MATERIALIZING'
                        elif elapsed >= delay * 0.5:
                            status = 'PENDING'
                        else:
                            status = 'COMING'
                        predictions.append({
                            'chain':     chain['name'],
                            'ticker':    ticker,
                            'direction': 'BULLISH' if direction > 0 else 'BEARISH',
                            'strength':  abs(direction),
                            'delay_min': delay // 60,
                            'status':    status,
                            'desc':      desc,
                        })
                else:
                    # Remove expired chain
                    active_chains.pop(chain_id, None)

            # Sort predictions by strength
            predictions.sort(key=lambda x: -x['strength'])

            result = {
                'active_chains': active,
                'predictions':   predictions[:20],
                'ts':            time.time(),
            }

            if active:
                print(f'[Contagion] {len(active)} chains active: {[c["id"] for c in active]}')

        except Exception as e:
            print(f'[Contagion] error: {e}')

        with store._lock:
            store.altdata['contagion'] = result

        last_macro = dict(macro) if 'macro' in dir() else {}
        time.sleep(_REFRESH)


def start_contagion():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='contagion')
    t.start()
