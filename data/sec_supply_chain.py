"""
SEC EDGAR Supply Chain Graph — free EDGAR EFTS full-text search.
Parses 10-K/10-Q filings for supply chain risk language.
Maps supplier risks to downstream affected tickers.
When a supplier warns = predict downstream impact before they announce.
Refreshes every 2 hours.
"""
import threading
import time
import requests
from core.data_store import store

_EDGAR_SEARCH = 'https://efts.sec.gov/LATEST/search-index'
_TIMEOUT = 20
_REFRESH = 7200  # 2 hours

_HEADERS = {'User-Agent': 'MarketGod/6.0 contact@marketgod.io'}

# Supplier → customers who depend on them
_SUPPLY_CHAIN_MAP = {
    'TSMC':          ['AAPL', 'NVDA', 'AMD', 'AVGO', 'QCOM', 'INTC'],
    'Samsung':       ['AAPL', 'NVDA', 'AMD', 'MU'],
    'ASML':          ['NVDA', 'AAPL', 'AMD', 'MU', 'INTC', 'TSLA'],
    'CATL':          ['TSLA', 'NIO', 'RIVN', 'GM', 'F'],
    'Foxconn':       ['AAPL', 'MSFT'],
    'Qualcomm':      ['AAPL', 'GOOGL', 'META'],
    'Broadcom':      ['AAPL', 'GOOGL', 'META', 'MSFT'],
    'Panasonic':     ['TSLA'],
    'LG Energy':     ['RIVN', 'GM', 'F'],
    'SK Hynix':      ['AAPL', 'NVDA', 'AMD'],
}

# Stress keywords in filings
_STRESS_WORDS = [
    'shortage', 'supply chain disruption', 'supply constraint',
    'inability to source', 'procurement challenges', 'sourcing delays',
    'material shortage', 'component shortage', 'supplier concentration risk',
    'single source', 'sole source', 'supply disruption',
]

_POSITIVE_WORDS = [
    'supply chain resilience', 'diversified sourcing', 'inventory buffer',
    'strategic stockpile', 'supply chain improvement',
]


def _search_edgar(query: str, form_types: str = '10-K,10-Q') -> list[dict]:
    """Search SEC EDGAR full-text and return recent filing hits."""
    try:
        params = {
            'q':       f'"{query}"',
            'forms':   form_types,
            'dateRange': 'custom',
            'startdt': '2025-01-01',
        }
        resp = requests.get(_EDGAR_SEARCH, params=params,
                            headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get('hits', {}).get('hits', [])
        return hits
    except Exception:
        return []


def _score_supplier_stress(supplier_name: str) -> int:
    """
    Search for a supplier name combined with stress keywords.
    Returns 0-100 stress score.
    """
    stress_hits = 0
    for kw in _STRESS_WORDS[:4]:  # limit API calls
        hits = _search_edgar(f'{supplier_name} {kw}')
        stress_hits += len(hits)
        time.sleep(0.5)

    # Score: 0 hits = 0, 10+ hits = 100
    return min(100, stress_hits * 10)


def _refresh_loop():
    while True:
        # ticker → {stress_score, drivers, signal}
        result: dict[str, dict] = {}
        alert_events: list[dict] = []

        try:
            for supplier, downstream in _SUPPLY_CHAIN_MAP.items():
                stress = _score_supplier_stress(supplier)
                if stress > 0:
                    for ticker in downstream:
                        if ticker not in result:
                            result[ticker] = {
                                'stress': 0,
                                'drivers': [],
                                'signal': 0.0,
                            }
                        # Accumulate stress from multiple suppliers
                        result[ticker]['stress'] = min(100, result[ticker]['stress'] + stress // 2)
                        result[ticker]['drivers'].append({
                            'supplier': supplier,
                            'stress':   stress,
                        })

                    if stress > 60:
                        alert_events.append({
                            'supplier': supplier,
                            'stress':   stress,
                            'affected': downstream,
                            'message':  f'{supplier} supply stress ({stress}) → impacts {", ".join(downstream[:3])}',
                        })

                time.sleep(1)

            # Convert stress to signals
            for ticker, data in result.items():
                s = data['stress']
                # High stress = negative signal (-1 to 0)
                # Low stress = neutral (0)
                data['signal'] = round(max(-1.0, -(s - 30) / 70), 3) if s > 30 else 0.0

            print(f'[SupplyChain] {len(result)} tickers affected, '
                  f'{len(alert_events)} alerts')
        except Exception as e:
            print(f'[SupplyChain] error: {e}')

        with store._lock:
            store.altdata['supply_chain'] = result
            # Merge alerts into the altdata alerts list
            existing = store.altdata.get('alerts', [])
            new_alerts = [a for a in alert_events
                          if not any(x.get('supplier') == a['supplier'] for x in existing[-20:])]
            store.altdata['alerts'] = (existing + new_alerts)[-50:]

        time.sleep(_REFRESH)


def start_sec_supply_chain():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='supply_chain')
    t.start()
