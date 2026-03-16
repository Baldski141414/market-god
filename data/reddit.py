"""Reddit WSB + crypto subreddit sentiment via RSS (every 5 minutes)."""
import threading
import time
import re
from collections import defaultdict
import feedparser
from core.config import SLOW_DATA_REFRESH_SECS, ALL_STOCK_TICKERS, BINANCE_DISPLAY
from core.data_store import store

_FEEDS = [
    'https://www.reddit.com/r/wallstreetbets/new/.rss?limit=100',
    'https://www.reddit.com/r/CryptoCurrency/new/.rss?limit=50',
    'https://www.reddit.com/r/investing/new/.rss?limit=50',
]

_BULLISH = {'buy','bull','long','calls','moon','rocket','squeeze','yolo','ath','pump'}
_BEARISH = {'sell','bear','short','puts','crash','dump','rekt','bankrupt','bubble'}

_ALL_SYMS = set(ALL_STOCK_TICKERS) | set(v for v in BINANCE_DISPLAY.values())


def _analyse_posts(posts: list[str]) -> dict:
    mentions: dict[str, int] = defaultdict(int)
    bull: dict[str, int] = defaultdict(int)
    bear: dict[str, int] = defaultdict(int)

    for text in posts:
        words = re.findall(r'\b[A-Z]{2,5}\b', text.upper())
        lower = text.lower()
        is_bull = any(w in lower for w in _BULLISH)
        is_bear = any(w in lower for w in _BEARISH)
        for w in words:
            if w in _ALL_SYMS:
                mentions[w] += 1
                if is_bull: bull[w] += 1
                if is_bear: bear[w] += 1

    result = {}
    for sym, cnt in mentions.items():
        total_sentiment = bull[sym] + bear[sym]
        bull_pct = bull[sym] / total_sentiment * 100 if total_sentiment else 50
        result[sym] = {'mentions': cnt, 'bull_pct': round(bull_pct, 1)}
    return result


def _refresh_loop():
    while True:
        posts = []
        for url in _FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    posts.append(f"{entry.get('title','')} {entry.get('summary','')}")
            except Exception as e:
                print(f'[Reddit] feed error: {e}')
        if posts:
            store.set_reddit(_analyse_posts(posts))
        time.sleep(SLOW_DATA_REFRESH_SECS)


def start_reddit():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='reddit')
    t.start()
