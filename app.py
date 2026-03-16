import os
import json
import time
import re
import threading
import traceback
from collections import defaultdict
from datetime import datetime

import feedparser
import requests
import yfinance as yf
from flask import Flask, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Configuration ─────────────────────────────────────────────────────────────
STOCKS = ['AAPL', 'TSLA', 'NVDA', 'AMD', 'SPY', 'QQQ', 'GME', 'MSFT']

CRYPTO_IDS  = ['bitcoin', 'ethereum', 'solana', 'ripple']
CRYPTO_MAP  = {'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL', 'ripple': 'XRP'}

TV_SYMBOLS = {
    'AAPL':  'NASDAQ:AAPL',  'TSLA': 'NASDAQ:TSLA',
    'NVDA':  'NASDAQ:NVDA',  'AMD':  'NASDAQ:AMD',
    'SPY':   'AMEX:SPY',     'QQQ':  'NASDAQ:QQQ',
    'GME':   'NYSE:GME',     'MSFT': 'NASDAQ:MSFT',
    'BTC':   'BINANCE:BTCUSDT', 'ETH': 'BINANCE:ETHUSDT',
    'SOL':   'BINANCE:SOLUSDT', 'XRP': 'BINANCE:XRPUSDT',
}

NEWS_FEEDS = [
    ('Reuters Business',  'https://feeds.reuters.com/reuters/businessNews'),
    ('Reuters Top News',  'https://feeds.reuters.com/reuters/topNews'),
    ('CNBC Finance',      'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664'),
    ('CNBC Markets',      'https://www.cnbc.com/id/20910258/device/rss/rss.html'),
]

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache = {
    'stocks': {}, 'crypto': {}, 'fear_greed': {},
    'reddit': {}, 'trends': {}, 'macro': {}, 'news': [],
    'last_updated': None,
}
_ttl: dict[str, float] = {}
_lock = threading.Lock()

INTERVALS = {
    'stocks': 55, 'crypto': 55, 'fear_greed': 60,
    'news': 120, 'reddit': 180, 'macro': 300, 'trends': 300,
}


def _should_update(key: str) -> bool:
    return time.time() - _ttl.get(key, 0) > INTERVALS[key]


def _set(key: str, data):
    with _lock:
        _cache[key] = data
        _ttl[key] = time.time()
        _cache['last_updated'] = datetime.now().isoformat()


# ── Data Fetchers ─────────────────────────────────────────────────────────────

def fetch_stocks() -> dict:
    data = {}
    for ticker in STOCKS:
        try:
            fi   = yf.Ticker(ticker).fast_info
            prev = fi.previous_close or 0
            curr = fi.last_price     or 0
            chg  = curr - prev
            pct  = (chg / prev * 100) if prev else 0
            data[ticker] = {
                'price':  round(curr, 2),
                'change': round(chg,  2),
                'pct':    round(pct,  2),
                'tv':     TV_SYMBOLS.get(ticker, f'NASDAQ:{ticker}'),
            }
        except Exception as e:
            data[ticker] = {'price': 0, 'change': 0, 'pct': 0, 'error': str(e)[:60]}
    return data


def fetch_crypto() -> dict:
    try:
        ids = ','.join(CRYPTO_IDS)
        url = (
            f'https://api.coingecko.com/api/v3/simple/price'
            f'?ids={ids}&vs_currencies=usd'
            f'&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true'
        )
        raw = requests.get(url, timeout=10).json()
        result = {}
        for cid in CRYPTO_IDS:
            sym = CRYPTO_MAP[cid]
            d   = raw.get(cid, {})
            result[sym] = {
                'price': d.get('usd', 0),
                'pct':   round(d.get('usd_24h_change', 0), 2),
                'mcap':  d.get('usd_market_cap', 0),
                'vol':   d.get('usd_24h_vol', 0),
                'tv':    TV_SYMBOLS.get(sym, f'BINANCE:{sym}USDT'),
            }
        return result
    except Exception as e:
        return {'error': str(e)[:120]}


def fetch_fear_greed() -> dict:
    try:
        data = requests.get('https://api.alternative.me/fng/?limit=30', timeout=10).json()['data']
        return {
            'value':   int(data[0]['value']),
            'label':   data[0]['value_classification'],
            'history': [{'v': int(d['value']), 'l': d['value_classification']} for d in data],
        }
    except Exception as e:
        return {'value': 50, 'label': 'Neutral', 'history': [], 'error': str(e)[:80]}


def fetch_reddit_sentiment() -> dict:
    BULLISH = {'moon','calls','buy','bull','long','gains','tendies','yolo',
               'rocket','squeeze','pump','hodl','bullish','green','calls','rip'}
    BEARISH = {'puts','short','sell','bear','crash','bag','loss',
               'dump','red','tank','bearish','collapse','drill'}
    all_tickers = set(STOCKS) | set(CRYPTO_MAP.values())

    try:
        headers = {'User-Agent': 'MarketGod:v2.0 (market dashboard; educational)'}
        r = requests.get(
            'https://www.reddit.com/r/wallstreetbets/hot.json?limit=100',
            headers=headers, timeout=15,
        )
        posts = r.json()['data']['children']

        sentiment: dict = defaultdict(lambda: {'m': 0, 'b': 0, 'e': 0, 'posts': []})

        for post in posts:
            p    = post['data']
            text = (p['title'] + ' ' + (p.get('selftext') or '')).upper()
            wlwr = set(text.lower().split())
            bull = len(wlwr & BULLISH)
            bear = len(wlwr & BEARISH)

            for t in all_tickers:
                if re.search(r'\b' + re.escape(t) + r'\b', text):
                    s = sentiment[t]
                    s['m'] += 1
                    s['b'] += bull
                    s['e'] += bear
                    if len(s['posts']) < 3:
                        s['posts'].append({
                            'title': p['title'][:90],
                            'score': p['score'],
                            'url':   'https://reddit.com' + p['permalink'],
                        })

        result = {}
        for ticker, d in sentiment.items():
            total = d['b'] + d['e']
            score = round(d['b'] / total * 100, 1) if total else 50
            result[ticker] = {'mentions': d['m'], 'score': score, 'posts': d['posts']}

        return dict(sorted(result.items(), key=lambda x: x[1]['mentions'], reverse=True)[:16])
    except Exception as e:
        return {'error': str(e)[:120]}


def fetch_trends() -> dict:
    try:
        from pytrends.request import TrendReq
        pt  = TrendReq(hl='en-US', tz=300, timeout=(10, 25))
        kws = ['AAPL stock', 'TSLA stock', 'NVDA stock', 'Bitcoin', 'Ethereum']
        pt.build_payload(kws, timeframe='now 7-d', geo='US')
        df = pt.interest_over_time()
        if df.empty:
            return {'error': 'No trend data returned'}
        result = {}
        for kw in kws:
            if kw not in df.columns:
                continue
            vals = [int(v) for v in df[kw].tolist()]
            recent   = sum(vals[-7:]) / 7   if len(vals) >= 7  else vals[-1]
            previous = sum(vals[-14:-7]) / 7 if len(vals) >= 14 else (vals[0] or 1)
            trend    = round((recent - previous) / max(previous, 1) * 100, 1)
            result[kw] = {
                'current':    vals[-1],
                'trend':      trend,
                'up':         trend > 0,
                'sparkline':  vals[-14:],
            }
        return result
    except Exception as e:
        return {'error': str(e)[:120]}


def fetch_macro() -> dict:
    macro: dict = {}

    # BLS public API v1 – no API key required
    try:
        payload = json.dumps({
            'seriesid':  ['CUUR0000SA0', 'LNS14000000'],
            'startyear': str(datetime.now().year - 1),
            'endyear':   str(datetime.now().year),
        })
        resp = requests.post(
            'https://api.bls.gov/publicAPI/v1/timeseries/data/',
            data=payload,
            headers={'Content-type': 'application/json'},
            timeout=15,
        )
        for series in resp.json().get('Results', {}).get('series', []):
            sid = series['seriesID']
            pts = series['data']
            if not pts:
                continue
            latest = pts[0]
            if sid == 'CUUR0000SA0':
                yoy = None
                if len(pts) >= 12:
                    yoy = round(
                        (float(latest['value']) - float(pts[12]['value']))
                        / float(pts[12]['value']) * 100, 2
                    )
                macro['cpi'] = {
                    'value':  float(latest['value']),
                    'yoy':    yoy,
                    'period': f"{latest['periodName']} {latest['year']}",
                    'label':  'CPI Inflation (YoY %)',
                }
            elif sid == 'LNS14000000':
                macro['unemployment'] = {
                    'value':  float(latest['value']),
                    'period': f"{latest['periodName']} {latest['year']}",
                    'label':  'Unemployment Rate',
                }
    except Exception as e:
        macro['bls_error'] = str(e)[:80]

    # yfinance – market rates & indicators (no API key)
    yf_tickers = [
        ('^TNX',      'treasury10y', '10Y Treasury Yield'),
        ('^IRX',      'treasury3m',  '3M T-Bill Rate'),
        ('^VIX',      'vix',         'VIX Volatility'),
        ('GC=F',      'gold',        'Gold ($/oz)'),
        ('CL=F',      'oil',         'Crude Oil ($/bbl)'),
        ('DX-Y.NYB',  'dxy',         'US Dollar Index'),
    ]
    for sym, key, label in yf_tickers:
        try:
            fi   = yf.Ticker(sym).fast_info
            curr = fi.last_price     or 0
            prev = fi.previous_close or 0
            pct  = round((curr - prev) / prev * 100, 2) if prev else 0
            macro[key] = {'value': round(curr, 2), 'pct': pct, 'label': label}
        except Exception:
            pass

    return macro


def fetch_news() -> list:
    articles = []
    for source, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:6]:
                summary = (e.get('summary') or '')
                # strip HTML tags from summary
                summary = re.sub(r'<[^>]+>', '', summary)[:180]
                articles.append({
                    'source':    source,
                    'title':     e.get('title', ''),
                    'link':      e.get('link',  ''),
                    'summary':   summary,
                    'published': e.get('published', ''),
                })
        except Exception:
            pass
    return articles[:24]


# ── Background Refresh Loop ───────────────────────────────────────────────────
FETCHERS = {
    'stocks':     fetch_stocks,
    'crypto':     fetch_crypto,
    'fear_greed': fetch_fear_greed,
    'reddit':     fetch_reddit_sentiment,
    'trends':     fetch_trends,
    'macro':      fetch_macro,
    'news':       fetch_news,
}


def _refresh_loop():
    while True:
        for key, fn in FETCHERS.items():
            if _should_update(key):
                try:
                    _set(key, fn())
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] ✓ {key}')
                except Exception:
                    print(f'[ERROR] {key}:\n{traceback.format_exc()}')
        time.sleep(5)


threading.Thread(target=_refresh_loop, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', tv_symbols=json.dumps(TV_SYMBOLS))


@app.route('/api/data')
def api_data():
    with _lock:
        return jsonify({k: v for k, v in _cache.items()})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)
