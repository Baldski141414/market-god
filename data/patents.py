"""
USPTO Patent Intelligence — PatentsView free API, no key needed.
Tracks patent filing velocity for major tech companies.
Spike in core-area filings = product announcement coming.
Refreshes every 6 hours (patent data updates slowly).
"""
import threading
import time
import requests
from datetime import datetime, timedelta
from core.data_store import store

_PATENTSVIEW_URL = 'https://api.patentsview.org/patents/query'
_TIMEOUT = 20
_REFRESH = 21600  # 6 hours

# Maps stock ticker → assignee name as it appears in USPTO
_TICKER_ASSIGNEES = {
    'AAPL':  'Apple Inc',
    'NVDA':  'Nvidia Corporation',
    'MSFT':  'Microsoft Corporation',
    'GOOGL': 'Google LLC',
    'TSLA':  'Tesla Inc',
    'META':  'Meta Platforms',
    'AMZN':  'Amazon Technologies',
    'AMD':   'Advanced Micro Devices',
    'INTC':  'Intel Corporation',
    'QCOM':  'QUALCOMM Incorporated',
    'AVGO':  'Broadcom',
    'ORCL':  'Oracle International',
    'CRM':   'Salesforce',
    'ADBE':  'Adobe Inc',
}

# Core business IPC codes per company (signals relevant innovation)
_TICKER_IPC = {
    'AAPL':  ['G06F', 'H04N', 'H04W', 'G06T'],   # Computing, display, wireless, graphics
    'NVDA':  ['G06T', 'G06F', 'H01L'],             # Graphics, computing, semiconductor
    'MSFT':  ['G06F', 'G06Q', 'H04L'],             # Computing, business methods, networking
    'GOOGL': ['G06F', 'H04L', 'G06N'],             # Computing, networking, AI/ML
    'TSLA':  ['B60L', 'H01M', 'B60K'],             # EV drive, batteries, powertrain
    'META':  ['G06F', 'G06T', 'H04N'],             # Computing, graphics, display
    'AMZN':  ['G06Q', 'G06F', 'B65G'],             # E-commerce, computing, logistics
    'AMD':   ['G06F', 'H01L', 'G06T'],             # Computing, semiconductor, graphics
}


def _fetch_recent_patents(assignee: str, days_back: int = 90) -> dict:
    """Fetch recent patent grants for an assignee via PatentsView."""
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    try:
        payload = {
            'q': {
                '_and': [
                    {'_gte': {'patent_date': start_date}},
                    {'_text_phrase': {'assignee_organization': assignee}},
                ]
            },
            'f': ['patent_id', 'patent_date', 'patent_title', 'ipc_main_group'],
            'o': {'page': 1, 'per_page': 50},
            's': [{'patent_date': 'desc'}],
        }
        resp = requests.post(_PATENTSVIEW_URL, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        patents = data.get('patents') or []
        return {
            'count':      len(patents),
            'total_found': data.get('total_patent_count', len(patents)),
            'recent':     patents[:10],  # keep top 10 for display
        }
    except Exception as e:
        print(f'[Patents] error for {assignee}: {e}')
        return {}


def _refresh_loop():
    # Track last-cycle counts for velocity calculation
    prev_counts: dict[str, int] = {}

    while True:
        result: dict[str, dict] = {}
        try:
            for ticker, assignee in _TICKER_ASSIGNEES.items():
                data = _fetch_recent_patents(assignee)
                count = data.get('count', 0)
                prev = prev_counts.get(ticker, count)
                velocity = count - prev  # change vs last scan

                result[ticker] = {
                    'assignee':       assignee,
                    'recent_filings': count,
                    'velocity':       velocity,   # +N = filing spike
                    'recent':         data.get('recent', []),
                    'signal':         min(1.0, count / 20.0),  # 20+ filings = max bullish
                }
                prev_counts[ticker] = count
                time.sleep(2)  # be polite

            print(f'[Patents] scanned {len(result)} companies')
        except Exception as e:
            print(f'[Patents] loop error: {e}')

        with store._lock:
            store.altdata['patents'] = result

        time.sleep(_REFRESH)


def start_patents():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='patents')
    t.start()
