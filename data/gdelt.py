"""
GDELT Geopolitical Risk Engine — free, no API key.
Scores global geopolitical risk 0-100 per region.
Maps regional risk to affected ticker watchlist.
Refreshes every 10 minutes (data updates every 15 min on GDELT).
"""
import threading
import time
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_GDELT_ART_URL = 'https://api.gdeltproject.org/api/v2/doc/doc'
_TIMEOUT = 15
_REFRESH = SLOW_DATA_REFRESH_SECS * 2  # 10 min — GDELT data is ~15 min delayed

# Region → affected tickers
_REGION_TICKERS = {
    'taiwan':      ['NVDA', 'AAPL', 'AMD', 'AVGO', 'AMAT', 'LRCX', 'KLAC', 'QCOM'],
    'middle_east': ['XOM', 'CVX', 'USO', 'XLE', 'CL=F'],
    'russia':      ['XOM', 'CVX', 'LNG', 'WEAT'],
    'china':       ['AAPL', 'NVDA', 'TSLA', 'BA', 'NIO'],
    'europe':      ['JPM', 'BAC', 'GS', 'MS', 'C'],
    'korea':       ['AAPL', 'AVGO', 'MU', 'INTC'],
}

# Keywords mapped to regions
_REGION_QUERIES = {
    'taiwan':      'Taiwan strait China military invasion',
    'middle_east': 'Iran Israel Gaza Middle East conflict oil',
    'russia':      'Russia Ukraine war sanctions energy',
    'china':       'China economy slowdown trade war tariff',
    'europe':      'Europe financial crisis bank contagion',
    'korea':       'North Korea missile nuclear Korea',
}


def _count_articles(query: str) -> int:
    """Query GDELT for article count in last 24h on a topic."""
    try:
        params = {
            'query': query,
            'mode': 'artlist',
            'format': 'json',
            'timespan': '24h',
            'maxrecords': '50',
        }
        resp = requests.get(_GDELT_ART_URL, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return 0
        data = resp.json()
        return len(data.get('articles') or [])
    except Exception:
        return 0


def _refresh_loop():
    # Baseline article counts per region (computed on first run)
    baseline: dict[str, float] = {}
    run = 0

    while True:
        result = {'regions': {}, 'affected_tickers': {}, 'global_risk': 50, 'ts': time.time()}
        try:
            region_counts: dict[str, int] = {}
            for region, query in _REGION_QUERIES.items():
                count = _count_articles(query)
                region_counts[region] = count
                time.sleep(1)  # be polite to GDELT

            # On first run, set baselines
            if run == 0:
                for region, count in region_counts.items():
                    baseline[region] = max(count, 1)

            # Compute risk scores relative to baseline
            region_risks: dict[str, int] = {}
            all_risks = []
            for region, count in region_counts.items():
                base = baseline.get(region, max(count, 1))
                # Risk = how elevated vs baseline (capped at 100)
                ratio = count / base if base > 0 else 1.0
                risk = min(100, int(50 * ratio))
                region_risks[region] = risk
                all_risks.append(risk)
                # Slow-update baseline
                baseline[region] = base * 0.95 + count * 0.05

            # Global risk = weighted average with peak risk driving it up
            if all_risks:
                avg_risk = sum(all_risks) / len(all_risks)
                peak_risk = max(all_risks)
                global_risk = int(avg_risk * 0.6 + peak_risk * 0.4)
            else:
                global_risk = 50

            # Map to tickers
            ticker_risks: dict[str, list] = {}
            for region, risk in region_risks.items():
                if risk > 55:
                    for ticker in _REGION_TICKERS.get(region, []):
                        if ticker not in ticker_risks:
                            ticker_risks[ticker] = []
                        ticker_risks[ticker].append({
                            'region': region,
                            'risk': risk,
                            'label': f'{region.replace("_"," ").title()} tension ({risk})',
                        })

            result = {
                'regions': region_risks,
                'affected_tickers': ticker_risks,
                'global_risk': global_risk,
                'ts': time.time(),
            }
            print(f'[GDELT] global_risk={global_risk}, regions={region_risks}')
        except Exception as e:
            print(f'[GDELT] error: {e}')

        with store._lock:
            store.altdata['gdelt'] = result

        run += 1
        time.sleep(_REFRESH)


def start_gdelt():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='gdelt')
    t.start()
