"""SEC Form 4 insider trades (every 5 minutes)."""
import threading
import time
import requests
import feedparser
from collections import defaultdict
from core.config import SLOW_DATA_REFRESH_SECS, ALL_STOCK_TICKERS
from core.data_store import store

_SEC_FEED = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom'


def _refresh_loop():
    while True:
        try:
            feed = feedparser.parse(_SEC_FEED)
            by_ticker: dict[str, dict] = defaultdict(lambda: {'buys': 0, 'sells': 0})
            for entry in feed.entries:
                title = entry.get('title', '').upper()
                for sym in ALL_STOCK_TICKERS:
                    if sym in title:
                        summary = entry.get('summary', '').lower()
                        if 'purchase' in summary or 'buy' in summary:
                            by_ticker[sym]['buys'] += 1
                        elif 'sale' in summary or 'sell' in summary:
                            by_ticker[sym]['sells'] += 1
            store.set_insider(dict(by_ticker))
        except Exception as e:
            print(f'[Insider] error: {e}')
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_insider():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='insider')
    t.start()
