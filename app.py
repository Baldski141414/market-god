import os
import json
import time
import re
import threading
import traceback
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta

import feedparser
import requests
import yfinance as yf
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from flask_socketio import SocketIO

try:
    import anthropic as _anthropic_lib
    _claude_client = _anthropic_lib.Anthropic()
    _claude_available = True
    print('[CLAUDE] Anthropic client initialized')
except Exception as _ce:
    _claude_client = None
    _claude_available = False
    print(f'[CLAUDE] Unavailable: {_ce}')

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading',
                    logger=False, engineio_logger=False)

# ── Configuration ──────────────────────────────────────────────────────────────
STOCKS      = ['AAPL', 'TSLA', 'NVDA', 'AMD', 'SPY', 'QQQ', 'GME', 'MSFT']
CRYPTO_IDS  = ['bitcoin', 'ethereum', 'solana', 'ripple']
CRYPTO_MAP  = {'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL', 'ripple': 'XRP'}

TV_SYMBOLS = {
    'AAPL': 'NASDAQ:AAPL', 'TSLA': 'NASDAQ:TSLA',
    'NVDA': 'NASDAQ:NVDA', 'AMD':  'NASDAQ:AMD',
    'SPY':  'AMEX:SPY',    'QQQ':  'NASDAQ:QQQ',
    'GME':  'NYSE:GME',    'MSFT': 'NASDAQ:MSFT',
    'BTC':  'BINANCE:BTCUSDT', 'ETH': 'BINANCE:ETHUSDT',
    'SOL':  'BINANCE:SOLUSDT', 'XRP': 'BINANCE:XRPUSDT',
}

NEWS_FEEDS = [
    ('Reuters Business', 'https://feeds.reuters.com/reuters/businessNews'),
    ('Reuters Top News', 'https://feeds.reuters.com/reuters/topNews'),
    ('CNBC Finance',     'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664'),
    ('CNBC Markets',     'https://www.cnbc.com/id/20910258/device/rss/rss.html'),
]

GEO_FEEDS = [
    ('Reuters World',    'https://feeds.reuters.com/reuters/worldNews'),
    ('Reuters Politics', 'https://feeds.reuters.com/Reuters/PoliticsNews'),
    ('CNBC World',       'https://www.cnbc.com/id/100727362/device/rss/rss.html'),
]

SEC_HEADERS     = {'User-Agent': 'MarketGod/4.0 market@example.com'}
WHALE_ALERT_KEY = os.environ.get('WHALE_ALERT_API_KEY', '')
_cik_cache: dict = {}

GEO_HIGH = [
    'war', 'attack', 'invasion', 'missile', 'nuclear', 'explosion', 'terrorism',
    'conflict', 'military', 'crisis', 'sanctions', 'embargo', 'coup',
    'assassination', 'escalation', 'airstrikes', 'blockade',
]
GEO_MED = [
    'tension', 'dispute', 'protest', 'unrest', 'tariff', 'trade war',
    'recession', 'collapse', 'default', 'supply chain', 'disruption',
    'warning', 'instability', 'deteriorating', 'inflation concerns',
]
GEO_LOW = [
    'deal', 'agreement', 'treaty', 'ceasefire', 'peace', 'negotiation',
    'summit', 'cooperation', 'alliance', 'stabilize', 'recovery',
]

# ── Cache ──────────────────────────────────────────────────────────────────────
_cache = {
    'stocks': {}, 'crypto': {}, 'fear_greed': {},
    'reddit': {}, 'trends': {}, 'macro': {}, 'news': [],
    'signals': {},
    'insider': {}, 'short_interest': {}, 'fed_sentiment': {},
    'earnings': {}, 'options': {}, 'smart_money': {},
    'congress': {}, 'whale': {}, 'dark_pool': {}, 'geo_risk': {},
    'last_updated': None,
}
_ttl: dict[str, float] = {}
_lock = threading.Lock()

INTERVALS = {
    'stocks': 55, 'crypto': 55, 'fear_greed': 60,
    'news': 120, 'reddit': 180, 'macro': 300, 'trends': 300,
    'insider': 3600, 'short_interest': 1800, 'fed_sentiment': 600,
    'earnings': 3600, 'options': 300,
    'congress': 3600, 'whale': 120, 'dark_pool': 3600, 'geo_risk': 300,
}

_claude_signals: dict = {}
_claude_ttl:     dict = {}
CLAUDE_TTL = 300
_claude_lock = threading.Lock()


def _should_update(key: str) -> bool:
    return time.time() - _ttl.get(key, 0) > INTERVALS.get(key, 300)


def _set(key: str, data):
    with _lock:
        _cache[key] = data
        _ttl[key]   = time.time()
        _cache['last_updated'] = datetime.now().isoformat()


# ── Existing Fetchers ──────────────────────────────────────────────────────────

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
               'rocket','squeeze','pump','hodl','bullish','green','rip'}
    BEARISH = {'puts','short','sell','bear','crash','bag','loss',
               'dump','red','tank','bearish','collapse','drill'}
    all_tickers = set(STOCKS) | set(CRYPTO_MAP.values())
    try:
        headers = {'User-Agent': 'MarketGod:v4.0 (educational dashboard)'}
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
                    s['m'] += 1; s['b'] += bull; s['e'] += bear
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
            return {'error': 'No trend data'}
        result = {}
        for kw in kws:
            if kw not in df.columns:
                continue
            vals     = [int(v) for v in df[kw].tolist()]
            recent   = sum(vals[-7:])  / 7   if len(vals) >= 7  else vals[-1]
            previous = sum(vals[-14:-7]) / 7 if len(vals) >= 14 else (vals[0] or 1)
            trend    = round((recent - previous) / max(previous, 1) * 100, 1)
            result[kw] = {'current': vals[-1], 'trend': trend, 'up': trend > 0, 'sparkline': vals[-14:]}
        return result
    except Exception as e:
        return {'error': str(e)[:120]}


def fetch_macro() -> dict:
    macro: dict = {}
    try:
        payload = json.dumps({
            'seriesid':  ['CUUR0000SA0', 'LNS14000000'],
            'startyear': str(datetime.now().year - 1),
            'endyear':   str(datetime.now().year),
        })
        resp = requests.post(
            'https://api.bls.gov/publicAPI/v1/timeseries/data/',
            data=payload, headers={'Content-type': 'application/json'}, timeout=15,
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
                    yoy = round((float(latest['value']) - float(pts[12]['value'])) / float(pts[12]['value']) * 100, 2)
                macro['cpi'] = {'value': float(latest['value']), 'yoy': yoy,
                                'period': f"{latest['periodName']} {latest['year']}", 'label': 'CPI Inflation (YoY %)'}
            elif sid == 'LNS14000000':
                macro['unemployment'] = {'value': float(latest['value']),
                                         'period': f"{latest['periodName']} {latest['year']}", 'label': 'Unemployment Rate'}
    except Exception as e:
        macro['bls_error'] = str(e)[:80]

    for sym, key, label in [
        ('^TNX', 'treasury10y', '10Y Treasury Yield'),
        ('^IRX', 'treasury3m',  '3M T-Bill Rate'),
        ('^VIX', 'vix',         'VIX Volatility'),
        ('GC=F', 'gold',        'Gold ($/oz)'),
        ('CL=F', 'oil',         'Crude Oil ($/bbl)'),
        ('DX-Y.NYB', 'dxy',     'US Dollar Index'),
    ]:
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
                summary = re.sub(r'<[^>]+>', '', e.get('summary') or '')[:180]
                articles.append({
                    'source': source, 'title': e.get('title', ''),
                    'link': e.get('link', ''), 'summary': summary,
                    'published': e.get('published', ''),
                })
        except Exception:
            pass
    return articles[:24]


# ── SEC EDGAR Insider Activity ─────────────────────────────────────────────────

def _get_cik_map() -> dict:
    global _cik_cache
    if _cik_cache:
        return _cik_cache
    try:
        r = requests.get('https://www.sec.gov/files/company_tickers.json', headers=SEC_HEADERS, timeout=15)
        for v in r.json().values():
            _cik_cache[v['ticker'].upper()] = str(v['cik_str']).zfill(10)
        print(f'[EDGAR] Loaded {len(_cik_cache)} CIK mappings')
    except Exception as e:
        print(f'[EDGAR] CIK error: {e}')
    return _cik_cache


def _parse_form4_xml(cik_int: int, accession_no: str, primary_doc: str) -> list:
    acc_nodash = accession_no.replace('-', '')
    url = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary_doc}'
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        owner_name    = root.findtext('.//rptOwnerName', '').strip()
        is_director   = root.findtext('.//isDirector', '0').strip() == '1'
        officer_title = root.findtext('.//officerTitle', '').strip()
        role = officer_title or ('Director' if is_director else 'Insider')
        transactions = []
        for txn in root.findall('.//nonDerivativeTransaction'):
            try:
                code     = txn.findtext('.//transactionCode', '').strip()
                acq_disp = txn.findtext('.//transactionAcquiredDisposedCode/value', '').strip()
                shares   = float(txn.findtext('.//transactionShares/value', '0') or 0)
                price    = float(txn.findtext('.//transactionPricePerShare/value', '0') or 0)
                txn_date = txn.findtext('.//transactionDate/value', '').strip()
                direction = 'BUY' if acq_disp == 'A' or code == 'P' else 'SELL'
                transactions.append({'direction': direction, 'shares': round(shares), 'price': round(price, 2),
                                     'value': round(shares * price), 'owner': owner_name,
                                     'title': role, 'date': txn_date, 'code': code})
            except Exception:
                pass
        return transactions
    except Exception:
        return []


def fetch_insider_activity() -> dict:
    cik_map = _get_cik_map()
    if not cik_map:
        return {'error': 'CIK map unavailable'}
    result = {}
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    for ticker in STOCKS:
        cik = cik_map.get(ticker)
        if not cik:
            result[ticker] = {'transactions': [], 'net_buys': 0, 'signal': 'neutral'}
            continue
        try:
            cik_int = int(cik)
            r = requests.get(f'https://data.sec.gov/submissions/CIK{cik}.json', headers=SEC_HEADERS, timeout=12)
            sub          = r.json()
            recent       = sub.get('filings', {}).get('recent', {})
            forms        = recent.get('form', [])
            dates        = recent.get('filingDate', [])
            accessions   = recent.get('accessionNumber', [])
            primary_docs = recent.get('primaryDocument', [])
            form4_queue  = []
            for i, form in enumerate(forms[:100]):
                if form == '4' and i < len(dates) and dates[i] >= thirty_days_ago:
                    form4_queue.append({'date': dates[i],
                                        'accession': accessions[i] if i < len(accessions) else '',
                                        'primary_doc': primary_docs[i] if i < len(primary_docs) else 'form4.xml'})
            all_txns = []
            for f4 in form4_queue[:3]:
                if not f4['accession']:
                    continue
                txns = _parse_form4_xml(cik_int, f4['accession'], f4['primary_doc'])
                for t in txns:
                    t['filing_date'] = f4['date']
                all_txns.extend(txns)
                time.sleep(0.15)
            buys  = [t for t in all_txns if t['direction'] == 'BUY']
            sells = [t for t in all_txns if t['direction'] == 'SELL']
            net   = len(buys) - len(sells)
            result[ticker] = {'transactions': all_txns[:5], 'buy_count': len(buys),
                              'sell_count': len(sells), 'net_buys': net,
                              'signal': 'bullish' if net > 0 else ('bearish' if net < 0 else 'neutral'),
                              'form4_count': len(form4_queue)}
        except Exception as e:
            result[ticker] = {'transactions': [], 'net_buys': 0, 'signal': 'neutral', 'error': str(e)[:80]}
    return result


def fetch_short_interest() -> dict:
    result = {}
    for ticker in STOCKS:
        try:
            info      = yf.Ticker(ticker).info
            short_pct = info.get('shortPercentOfFloat')
            short_rt  = info.get('shortRatio')
            if short_pct is not None:
                short_pct = round(float(short_pct) * 100, 1)
            result[ticker] = {'short_pct': short_pct,
                              'short_ratio': round(float(short_rt), 1) if short_rt else None,
                              'squeeze_alert': bool(short_pct and short_pct > 20),
                              'high_short':   bool(short_pct and short_pct > 15)}
        except Exception:
            result[ticker] = {'short_pct': None, 'short_ratio': None, 'squeeze_alert': False, 'high_short': False}
    return result


def fetch_fed_sentiment() -> dict:
    HAWKISH = ['raise rates','rate hike','tighten','restrictive','combat inflation',
               'above target','overheat','aggressive','inflation remains elevated',
               'further increases','higher for longer','premature to cut']
    DOVISH  = ['cut rates','rate cut','easing','accommodative','support growth',
               'below target','patient','gradual','labor market concerns',
               'downside risk','pause','disinflation','progress on inflation',
               'cooling','moderated','reduce rates']
    fed_feeds = ['https://www.federalreserve.gov/feeds/press_all.xml',
                 'https://www.federalreserve.gov/feeds/speeches.xml']
    all_text = ''
    articles = []
    for url in fed_feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                text = (entry.get('title', '') + ' ' + entry.get('summary', '')).lower()
                all_text += ' ' + text
                articles.append({'title': entry.get('title', '')[:100], 'date': entry.get('published', '')})
        except Exception:
            pass
    if not all_text.strip():
        return {'sentiment': 'neutral', 'score': 50, 'hawkish': 0, 'dovish': 0, 'articles': []}
    hawkish_hits = sum(1 for p in HAWKISH if p in all_text)
    dovish_hits  = sum(1 for p in DOVISH  if p in all_text)
    total        = hawkish_hits + dovish_hits
    if total == 0:
        sentiment, score = 'neutral', 50
    elif hawkish_hits > dovish_hits:
        sentiment = 'hawkish'
        score = round(50 + (hawkish_hits - dovish_hits) / total * 50)
    else:
        sentiment = 'dovish'
        score = round(50 - (dovish_hits - hawkish_hits) / total * 50)
    return {'sentiment': sentiment, 'score': score, 'hawkish': hawkish_hits, 'dovish': dovish_hits,
            'market_impact': 'bearish' if sentiment == 'hawkish' else ('bullish' if sentiment == 'dovish' else 'neutral'),
            'articles': articles[:5]}


def fetch_earnings_sentiment() -> dict:
    CONFIDENT = {'beat','exceeded','raised','strong','record','growth','above','outperform',
                 'positive','momentum','accelerating','robust','solid','upside','surpass'}
    CAUTIOUS  = {'miss','below','cut','concern','weak','lowered','warning','headwind',
                 'slowdown','disappoint','risk','uncertainty','decline','pressure','challenging'}
    result = {}
    for ticker in STOCKS:
        try:
            t    = yf.Ticker(ticker)
            news = t.news or []
            def _ntext(n):
                if isinstance(n, dict):
                    title   = n.get('title', '')
                    content = n.get('content', {})
                    summary = content.get('summary', '') if isinstance(content, dict) else n.get('summary', '')
                    return (title + ' ' + (summary or '')).lower()
                return ''
            earnings_kw = {'earnings','beat','miss','revenue','profit','guidance','eps','quarterly'}
            e_news = [n for n in news if any(w in _ntext(n) for w in earnings_kw)]
            score, sentiment, conf_h, caut_h = 50, 'neutral', 0, 0
            if e_news:
                text   = ' '.join(_ntext(n) for n in e_news[:5])
                conf_h = sum(1 for w in CONFIDENT if w in text)
                caut_h = sum(1 for w in CAUTIOUS  if w in text)
                tot    = conf_h + caut_h
                if tot > 0:
                    score     = round(conf_h / tot * 100)
                    sentiment = 'confident' if score >= 60 else ('cautious' if score <= 40 else 'mixed')
            next_earnings = None
            try:
                cal = t.calendar
                if cal is not None and not cal.empty:
                    col = cal.columns[0]
                    next_earnings = str(col.date()) if hasattr(col, 'date') else str(col)[:10]
            except Exception:
                pass
            result[ticker] = {'sentiment': sentiment, 'score': score, 'confident_signals': conf_h,
                              'cautious_signals': caut_h, 'news_count': len(e_news), 'next_earnings': next_earnings}
        except Exception:
            result[ticker] = {'sentiment': 'neutral', 'score': 50, 'confident_signals': 0,
                              'cautious_signals': 0, 'news_count': 0, 'next_earnings': None}
    return result


def fetch_options_activity() -> dict:
    result = {}
    for ticker in STOCKS:
        try:
            t         = yf.Ticker(ticker)
            exp_dates = (t.options or [])[:2]
            unusual_calls, unusual_puts = [], []
            total_call_vol = total_put_vol = 0
            for exp_date in exp_dates:
                try:
                    chain = t.option_chain(exp_date)
                    calls = chain.calls.dropna(subset=['volume', 'openInterest'])
                    puts  = chain.puts.dropna(subset=['volume', 'openInterest'])
                    total_call_vol += int(calls['volume'].sum())
                    total_put_vol  += int(puts['volume'].sum())
                    for _, row in calls.iterrows():
                        oi, vol = float(row['openInterest']), float(row['volume'])
                        if vol > 500 and oi > 0 and vol / oi > 3:
                            unusual_calls.append({'strike': round(float(row['strike']), 1), 'exp': exp_date,
                                                  'volume': int(vol), 'oi': int(oi), 'ratio': round(vol/oi, 1), 'type': 'CALL'})
                    for _, row in puts.iterrows():
                        oi, vol = float(row['openInterest']), float(row['volume'])
                        if vol > 500 and oi > 0 and vol / oi > 3:
                            unusual_puts.append({'strike': round(float(row['strike']), 1), 'exp': exp_date,
                                                 'volume': int(vol), 'oi': int(oi), 'ratio': round(vol/oi, 1), 'type': 'PUT'})
                except Exception:
                    pass
            unusual_calls.sort(key=lambda x: x['volume'], reverse=True)
            unusual_puts.sort(key=lambda x: x['volume'], reverse=True)
            pcr    = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else None
            n_c, n_p = len(unusual_calls), len(unusual_puts)
            signal = 'bullish' if n_c > n_p + 1 else ('bearish' if n_p > n_c + 1 else 'neutral')
            result[ticker] = {'unusual_calls': unusual_calls[:3], 'unusual_puts': unusual_puts[:3],
                              'put_call_ratio': pcr, 'total_call_vol': total_call_vol,
                              'total_put_vol': total_put_vol, 'signal': signal,
                              'has_unusual': bool(unusual_calls or unusual_puts)}
        except Exception:
            result[ticker] = {'unusual_calls': [], 'unusual_puts': [], 'put_call_ratio': None,
                              'signal': 'neutral', 'has_unusual': False}
    return result


# ── NEW: Congress Trading Tracker ──────────────────────────────────────────────

def fetch_congress_trades() -> dict:
    all_tickers     = set(STOCKS) | set(CRYPTO_MAP.values())
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    trades = []

    hdrs = {'User-Agent': 'MarketGod/4.0 market@example.com'}

    try:
        r = requests.get(
            'https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json',
            timeout=30, headers=hdrs,
        )
        for t in r.json():
            ticker = t.get('ticker', '').upper().replace('$', '').strip()
            date   = (t.get('transaction_date') or '')
            if ticker in all_tickers and date >= thirty_days_ago:
                trades.append({
                    'chamber':    'House',
                    'politician': t.get('representative', 'Unknown'),
                    'ticker':     ticker,
                    'type':       t.get('type', ''),
                    'amount':     t.get('amount', ''),
                    'date':       date,
                    'party':      t.get('party', ''),
                    'state':      t.get('state', ''),
                })
        print(f'[CONGRESS] House: {len(trades)} recent trades for tracked tickers')
    except Exception as e:
        print(f'[CONGRESS] House error: {e}')

    senate_count = 0
    try:
        r = requests.get(
            'https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json',
            timeout=30, headers=hdrs,
        )
        for t in r.json():
            ticker = t.get('ticker', '').upper().replace('$', '').strip()
            date   = (t.get('transaction_date') or '')
            if ticker in all_tickers and date >= thirty_days_ago:
                trades.append({
                    'chamber':    'Senate',
                    'politician': t.get('senator', t.get('name', 'Unknown')),
                    'ticker':     ticker,
                    'type':       t.get('type', ''),
                    'amount':     t.get('amount', ''),
                    'date':       date,
                    'party':      t.get('party', ''),
                    'state':      t.get('state', ''),
                })
                senate_count += 1
        print(f'[CONGRESS] Senate: {senate_count} recent trades for tracked tickers')
    except Exception as e:
        print(f'[CONGRESS] Senate error: {e}')

    trades.sort(key=lambda x: x.get('date', ''), reverse=True)
    by_ticker: dict = defaultdict(list)
    for t in trades:
        by_ticker[t['ticker']].append(t)

    return {
        'trades':      trades[:60],
        'by_ticker':   {k: v[:5] for k, v in by_ticker.items()},
        'total':       len(trades),
        'last_updated': datetime.now().isoformat(),
    }


# ── NEW: Whale Movements ───────────────────────────────────────────────────────

def _fetch_blockchain_whales() -> dict:
    txns = []
    try:
        r = requests.get(
            'https://api.blockchair.com/bitcoin/transactions'
            '?s=output_total(desc)&limit=10&q=output_total(100000000..)',
            timeout=12, headers={'User-Agent': 'MarketGod/4.0'},
        )
        for tx in (r.json().get('data') or [])[:10]:
            val = tx.get('output_total_usd') or 0
            if val >= 1_000_000:
                txns.append({'symbol': 'BTC', 'amount': round((tx.get('output_total') or 0) / 1e8, 2),
                             'amount_usd': round(val), 'from': 'bitcoin network', 'to': 'bitcoin network',
                             'blockchain': 'bitcoin', 'timestamp': 0, 'hash': (tx.get('hash') or '')[:16]})
    except Exception:
        pass
    try:
        r = requests.get(
            'https://api.blockchair.com/ethereum/transactions'
            '?s=value_usd(desc)&limit=10&q=value_usd(1000000..)',
            timeout=12, headers={'User-Agent': 'MarketGod/4.0'},
        )
        for tx in (r.json().get('data') or [])[:10]:
            val = tx.get('value_usd') or 0
            if val >= 1_000_000:
                txns.append({'symbol': 'ETH', 'amount': round((tx.get('value') or 0) / 1e18, 4),
                             'amount_usd': round(val),
                             'from': (tx.get('sender') or 'unknown')[:20],
                             'to':   (tx.get('recipient') or 'unknown')[:20],
                             'blockchain': 'ethereum', 'timestamp': 0, 'hash': (tx.get('hash') or '')[:16]})
    except Exception:
        pass
    txns.sort(key=lambda x: x['amount_usd'], reverse=True)
    return {'transactions': txns[:15], 'total': len(txns), 'source': 'blockchair'}


def fetch_whale_movements() -> dict:
    if not WHALE_ALERT_KEY:
        try:
            return _fetch_blockchain_whales()
        except Exception:
            return {'transactions': [], 'error': 'Set WHALE_ALERT_API_KEY for live data', 'source': 'none'}
    try:
        since = int(time.time()) - 3600
        url   = (f'https://api.whale-alert.io/v1/transactions'
                 f'?api_key={WHALE_ALERT_KEY}&min_value=1000000&start={since}&limit=100')
        r    = requests.get(url, timeout=15)
        data = r.json()
        sym_map = {'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'xrp': 'XRP',
                   'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL', 'ripple': 'XRP'}
        tracked = set(CRYPTO_MAP.values())
        txns = []
        for t in data.get('transactions', []):
            sym = sym_map.get(t.get('symbol', '').lower(), t.get('symbol', '').upper())
            if sym not in tracked:
                continue
            txns.append({'symbol': sym, 'amount': round(t.get('amount', 0), 2),
                         'amount_usd': round(t.get('amount_usd', 0)),
                         'from': (t.get('from', {}).get('owner') or 'unknown')[:30],
                         'to':   (t.get('to',   {}).get('owner') or 'unknown')[:30],
                         'blockchain': t.get('blockchain', ''), 'timestamp': t.get('timestamp', 0),
                         'hash': (t.get('hash') or '')[:16]})
        txns.sort(key=lambda x: x['amount_usd'], reverse=True)
        return {'transactions': txns[:20], 'total': len(txns), 'source': 'whale-alert'}
    except Exception as e:
        return {'transactions': [], 'error': str(e)[:120], 'source': 'whale-alert'}


# ── NEW: Dark Pool Data ────────────────────────────────────────────────────────

def fetch_dark_pool() -> dict:
    try:
        r = requests.get(
            'https://api.finra.org/data/group/otcMarket/name/weeklySummary',
            params={'limit': 50,
                    'fields': 'issueSymbolIdentifier,marketParticipantId,totalWeeklyShareQuantity,totalWeeklyTradeCount,weekStartDate'},
            headers={'Accept': 'application/json', 'User-Agent': 'MarketGod/4.0'},
            timeout=15,
        )
        data    = r.json()
        tracked = set(STOCKS) | set(CRYPTO_MAP.values())
        result  = []
        for item in (data if isinstance(data, list) else []):
            sym = (item.get('issueSymbolIdentifier') or '').upper()
            if sym in tracked:
                result.append({'symbol': sym,
                                'venue':  item.get('marketParticipantId', 'ATS'),
                                'shares': item.get('totalWeeklyShareQuantity', 0),
                                'trades': item.get('totalWeeklyTradeCount', 0),
                                'week':   item.get('weekStartDate', '')})
        result.sort(key=lambda x: (x.get('shares') or 0), reverse=True)
        return {'trades': result[:20], 'source': 'FINRA ATS', 'count': len(result)}
    except Exception as e:
        return {'trades': [], 'error': str(e)[:120], 'source': 'FINRA ATS'}


# ── NEW: Geopolitical Risk NLP ─────────────────────────────────────────────────

def fetch_geopolitical_risk() -> dict:
    headlines = []
    for source, url in GEO_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:12]:
                headlines.append({'source': source, 'title': e.get('title', ''),
                                  'published': e.get('published', ''), 'link': e.get('link', '')})
        except Exception:
            pass

    if not headlines:
        return {'score': 50, 'level': 'MODERATE', 'headlines': [], 'asset_impact': {}}

    all_text  = ' '.join(h['title'].lower() for h in headlines)
    high_hits = sum(1 for kw in GEO_HIGH if kw in all_text)
    med_hits  = sum(1 for kw in GEO_MED  if kw in all_text)
    low_hits  = sum(1 for kw in GEO_LOW  if kw in all_text)
    score     = max(0, min(100, 40 + high_hits * 6 + med_hits * 2 - low_hits * 3))

    if   score >= 75: level = 'CRITICAL'
    elif score >= 60: level = 'HIGH'
    elif score >= 40: level = 'MODERATE'
    elif score >= 25: level = 'LOW'
    else:             level = 'MINIMAL'

    SAFE_HAVENS = {'BTC', 'ETH'}
    RISK_OFF    = {'TSLA', 'NVDA', 'AMD', 'GME', 'SOL', 'XRP'}
    asset_impact = {}
    for sym in STOCKS + list(CRYPTO_MAP.values()):
        if sym in SAFE_HAVENS:
            asset_impact[sym] = 'bullish' if score > 60 else 'neutral'
        elif sym in RISK_OFF:
            asset_impact[sym] = 'bearish' if score > 60 else 'neutral'
        else:
            asset_impact[sym] = 'bearish' if score > 75 else 'neutral'

    flagged = []
    for h in headlines:
        t  = h['title'].lower()
        hs = sum(6 for kw in GEO_HIGH if kw in t) + sum(2 for kw in GEO_MED if kw in t)
        if hs > 0:
            h['risk_score'] = hs
            flagged.append(h)
    flagged.sort(key=lambda x: x.get('risk_score', 0), reverse=True)

    return {
        'score':            score,
        'level':            level,
        'high_risk_hits':   high_hits,
        'medium_risk_hits': med_hits,
        'low_risk_hits':    low_hits,
        'headlines':        flagged[:8],
        'asset_impact':     asset_impact,
    }


# ── Rule-based fallback scorer ─────────────────────────────────────────────────

def calc_ai_score(sym: str, pct: float, fg_value: int, reddit: dict,
                  insider: dict, short_interest: dict, fed: dict,
                  earnings: dict, options: dict) -> dict:
    score, reasons = 50, []
    if   pct >= 5:  score += 25; reasons.append(f'▲{pct:.1f}% strong move')
    elif pct >= 3:  score += 15; reasons.append(f'▲{pct:.1f}% momentum')
    elif pct >= 1:  score += 8;  reasons.append(f'▲{pct:.1f}% momentum')
    elif pct <= -5: score -= 25; reasons.append(f'▼{abs(pct):.1f}% strong drop')
    elif pct <= -3: score -= 15; reasons.append(f'▼{abs(pct):.1f}% drop')
    elif pct <= -1: score -= 8;  reasons.append(f'▼{abs(pct):.1f}% drop')

    if   fg_value >= 75: score += 15; reasons.append(f'F&G={fg_value} extreme greed')
    elif fg_value >= 60: score += 8;  reasons.append(f'F&G={fg_value} greed')
    elif fg_value <= 25: score -= 15; reasons.append(f'F&G={fg_value} extreme fear')
    elif fg_value <= 40: score -= 8;  reasons.append(f'F&G={fg_value} fear')

    rd = reddit.get(sym, {}) if isinstance(reddit, dict) else {}
    if isinstance(rd, dict) and rd.get('mentions', 0) >= 2:
        rs = rd.get('score', 50)
        if   rs >= 75: score += 10; reasons.append(f'WSB {rs:.0f}% bull')
        elif rs >= 60: score += 5;  reasons.append(f'WSB {rs:.0f}% bull')
        elif rs <= 25: score -= 10; reasons.append(f'WSB {rs:.0f}% bear')
        elif rs <= 40: score -= 5;  reasons.append(f'WSB {rs:.0f}% bear')

    ins = insider.get(sym, {}) if isinstance(insider, dict) else {}
    if isinstance(ins, dict) and 'net_buys' in ins:
        nb = ins['net_buys']
        if nb > 0:
            score += min(15, nb * 5); reasons.append(f'Insider buy ×{nb}')
        elif nb < 0:
            score -= min(15, abs(nb) * 5); reasons.append(f'Insider sell ×{abs(nb)}')

    si = short_interest.get(sym, {}) if isinstance(short_interest, dict) else {}
    if isinstance(si, dict):
        sp = si.get('short_pct') or 0
        if   sp > 25 and pct > 0: score += 10; reasons.append(f'{sp:.0f}% short → squeeze')
        elif sp > 15 and pct > 0: score += 5;  reasons.append(f'{sp:.0f}% short interest')
        elif sp > 20 and pct < 0: score -= 5;  reasons.append(f'{sp:.0f}% shorts adding pressure')

    if isinstance(fed, dict):
        fs = fed.get('sentiment', 'neutral')
        if   fs == 'dovish':  score += 10; reasons.append('Fed dovish → rate cuts')
        elif fs == 'hawkish': score -= 10; reasons.append('Fed hawkish → rate hikes')

    earn = earnings.get(sym, {}) if isinstance(earnings, dict) else {}
    if isinstance(earn, dict):
        es = earn.get('sentiment', 'neutral')
        if   es == 'confident': score += 10; reasons.append('Earnings strong/beat')
        elif es == 'cautious':  score -= 10; reasons.append('Earnings miss/warn')

    opts = options.get(sym, {}) if isinstance(options, dict) else {}
    if isinstance(opts, dict):
        os_ = opts.get('signal', 'neutral')
        if   os_ == 'bullish': score += 5; reasons.append('Smart money calls')
        elif os_ == 'bearish': score -= 5; reasons.append('Smart money puts')

    score = max(0, min(100, score))
    if   score >= 70: label = 'Strong Buy'
    elif score >= 58: label = 'Buy'
    elif score <= 30: label = 'Strong Sell'
    elif score <= 42: label = 'Sell'
    else:             label = 'Hold'

    return {'label': label, 'score': score, 'reasons': reasons}


# ── NEW: Claude AI Signal Engine ───────────────────────────────────────────────

def calc_ai_score_claude(sym: str, pct: float, fg_value: int,
                          reddit: dict, insider: dict, short_interest: dict,
                          fed: dict, earnings: dict, options: dict,
                          congress: dict, whale: dict, geo_risk: dict) -> dict:
    now = time.time()
    with _claude_lock:
        cached = _claude_signals.get(sym)
        if cached and (now - _claude_ttl.get(sym, 0)) < CLAUDE_TTL:
            return cached

    if not _claude_available:
        fb = calc_ai_score(sym, pct, fg_value, reddit, insider, short_interest, fed, earnings, options)
        fb.update({'source': 'rules', 'reasoning': 'Claude unavailable — rule-based fallback', 'confidence': 3})
        return fb

    rd  = (reddit  or {}).get(sym, {})
    ins = (insider or {}).get(sym, {})
    si  = (short_interest or {}).get(sym, {})
    ea  = (earnings or {}).get(sym, {})
    op  = (options  or {}).get(sym, {})
    cg  = (congress or {}).get('by_ticker', {}).get(sym, [])
    wh  = [t for t in (whale or {}).get('transactions', []) if t.get('symbol') == sym]
    geo_lvl    = (geo_risk or {}).get('level', 'MODERATE')
    geo_score  = (geo_risk or {}).get('score', 50)
    geo_impact = (geo_risk or {}).get('asset_impact', {}).get(sym, 'neutral')

    cg_summary = ', '.join(
        f"{t.get('type','?')} by {t.get('politician','?')}" for t in cg[:3]
    ) or 'none'
    context = (
        f"ASSET: {sym}\n"
        f"PRICE MOMENTUM: {pct:+.2f}% (24h)\n"
        f"FEAR & GREED: {fg_value}/100\n"
        f"REDDIT WSB: {rd.get('mentions',0)} mentions, {rd.get('score',50):.0f}% bullish\n"
        f"SEC INSIDER: net_buys={ins.get('net_buys',0)}, signal={ins.get('signal','neutral')}\n"
        f"SHORT INTEREST: {si.get('short_pct','N/A')}% of float, squeeze={si.get('squeeze_alert',False)}\n"
        f"FED POLICY: {fed.get('sentiment','neutral')} (hawkish={fed.get('hawkish',0)}, dovish={fed.get('dovish',0)})\n"
        f"EARNINGS: {ea.get('sentiment','neutral')} (confidence={ea.get('score',50)})\n"
        f"OPTIONS: {op.get('signal','neutral')}, PCR={op.get('put_call_ratio','N/A')}, unusual={op.get('has_unusual',False)}\n"
        f"CONGRESS TRADES (30d): {len(cg)} trades — {cg_summary}\n"
        f"WHALE MOVEMENTS: {len(wh)} large transfers (>$1M)\n"
        f"GEOPOLITICAL RISK: {geo_score}/100 ({geo_lvl}), impact on {sym}: {geo_impact}"
    )

    try:
        resp = _claude_client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=400,
            messages=[{
                'role': 'user',
                'content': (
                    'You are an institutional quantitative trading AI. Analyze ALL indicators '
                    'and generate a precise trading signal.\n\n'
                    f'{context}\n\n'
                    'Respond ONLY in this exact JSON format (no markdown):\n'
                    '{"score": <0-100 int>, "signal": "<Strong Buy|Buy|Hold|Sell|Strong Sell>", '
                    '"reasoning": "<2-3 sentences on key factors>", '
                    '"confidence": <1-5 int>, "key_catalyst": "<single most important driver>"}\n\n'
                    'Score: 70-100=Strong Buy, 58-69=Buy, 43-57=Hold, 31-42=Sell, 0-30=Strong Sell'
                ),
            }],
        )
        text = resp.content[0].text.strip()
        m    = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            score  = max(0, min(100, int(parsed.get('score', 50))))
            result = {
                'score':      score,
                'label':      parsed.get('signal', 'Hold'),
                'reasons':    [parsed['key_catalyst']] if parsed.get('key_catalyst') else [],
                'reasoning':  parsed.get('reasoning', ''),
                'confidence': int(parsed.get('confidence', 3)),
                'source':     'claude',
            }
            with _claude_lock:
                _claude_signals[sym] = result
                _claude_ttl[sym]     = now
            return result
    except Exception as e:
        print(f'[CLAUDE] {sym}: {e}')

    fb = calc_ai_score(sym, pct, fg_value, reddit, insider, short_interest, fed, earnings, options)
    fb.update({'source': 'rules', 'reasoning': 'Claude temporarily unavailable', 'confidence': 3})
    return fb


def refresh_all_claude_signals():
    with _lock:
        stocks  = dict(_cache.get('stocks', {}))
        crypto  = dict(_cache.get('crypto', {}))
        fg      = dict(_cache.get('fear_greed', {}))
        reddit  = dict(_cache.get('reddit', {}))
        insider = dict(_cache.get('insider', {}))
        si      = dict(_cache.get('short_interest', {}))
        fed     = dict(_cache.get('fed_sentiment', {}))
        earn    = dict(_cache.get('earnings', {}))
        opts    = dict(_cache.get('options', {}))
        cong    = dict(_cache.get('congress', {}))
        whale   = dict(_cache.get('whale', {}))
        geo     = dict(_cache.get('geo_risk', {}))

    fg_val   = fg.get('value', 50)
    all_syms = list(stocks.keys()) + [s for s in crypto if s != 'error']
    signals_out = {}

    for sym in all_syms:
        pct = stocks.get(sym, {}).get('pct', 0) if sym in stocks else crypto.get(sym, {}).get('pct', 0)
        sig = calc_ai_score_claude(sym, pct, fg_val, reddit, insider, si, fed, earn, opts, cong, whale, geo)
        signals_out[sym] = sig
        time.sleep(0.3)

    with _lock:
        _cache['signals'] = signals_out
    try:
        socketio.emit('signal_update', {'signals': signals_out})
    except Exception:
        pass
    print(f'[CLAUDE] Refreshed {len(signals_out)} signals')


# ── Smart Money Summary ────────────────────────────────────────────────────────

def build_smart_money_summary(insider, short_interest, options, stocks) -> dict:
    all_pcts   = {sym: d.get('pct', 0)   for sym, d in (stocks or {}).items() if isinstance(d, dict)}
    all_prices = {sym: d.get('price', 0) for sym, d in (stocks or {}).items() if isinstance(d, dict)}

    insider_buys, insider_sells = [], []
    for ticker, ins in (insider or {}).items():
        if not isinstance(ins, dict):
            continue
        for t in ins.get('transactions', [])[:2]:
            entry = {'ticker': ticker, 'owner': t.get('owner', 'Insider')[:30],
                     'title': t.get('title', '')[:30], 'value': t.get('value', 0),
                     'shares': t.get('shares', 0), 'price': t.get('price', 0),
                     'date': t.get('filing_date', t.get('date', ''))}
            if t.get('direction') == 'BUY' and t.get('value', 0) > 5000:
                insider_buys.append(entry)
            elif t.get('direction') == 'SELL' and t.get('value', 0) > 5000:
                insider_sells.append(entry)

    squeeze = []
    for ticker, si in (short_interest or {}).items():
        if not isinstance(si, dict):
            continue
        sp = si.get('short_pct') or 0
        if sp > 10:
            squeeze.append({'ticker': ticker, 'short_pct': sp, 'short_ratio': si.get('short_ratio'),
                            'current_pct': all_pcts.get(ticker, 0),
                            'squeeze_alert': si.get('squeeze_alert', False), 'price': all_prices.get(ticker, 0)})
    squeeze.sort(key=lambda x: x['short_pct'], reverse=True)

    unusual_opts = []
    for ticker, opts in (options or {}).items():
        if not isinstance(opts, dict) or not opts.get('has_unusual'):
            continue
        for item, otype in [(opts.get('unusual_calls', []), 'CALL'), (opts.get('unusual_puts', []), 'PUT')]:
            if item:
                unusual_opts.append({'ticker': ticker, 'type': otype,
                                     'strike': item[0].get('strike'), 'volume': item[0].get('volume'),
                                     'ratio': item[0].get('ratio'), 'exp': item[0].get('exp'),
                                     'signal': 'bullish' if otype == 'CALL' else 'bearish'})
    unusual_opts.sort(key=lambda x: x.get('volume') or 0, reverse=True)

    return {'insider_buys': insider_buys[:5], 'insider_sells': insider_sells[:5],
            'squeeze_candidates': squeeze[:5], 'unusual_options': unusual_opts[:6],
            'alert_count': len(insider_buys) + len(squeeze) + len(unusual_opts)}


# ── Paper Trading ──────────────────────────────────────────────────────────────

PORTFOLIO_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paper_trading.json')
STARTING_CASH   = 100_000.0
MAX_POSITIONS   = 12
POSITION_SIZE   = 0.08
_portfolio_lock = threading.Lock()
_paper_ttl      = 0.0
PAPER_INTERVAL  = 5   # react within 5 seconds


def _default_portfolio() -> dict:
    return {'cash': STARTING_CASH, 'positions': {}, 'trades': [],
            'spy_basis': None, 'spy_basis_time': None, 'created': datetime.now().isoformat()}


def load_portfolio() -> dict:
    try:
        if os.path.exists(PORTFOLIO_FILE):
            with open(PORTFOLIO_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    p = _default_portfolio()
    save_portfolio(p)
    return p


def save_portfolio(p: dict):
    try:
        with open(PORTFOLIO_FILE, 'w') as f:
            json.dump(p, f, indent=2)
    except Exception as e:
        print(f'[PORTFOLIO] save error: {e}')


def _snapshot_prices() -> tuple[dict, dict]:
    prices, pcts = {}, {}
    with _lock:
        for sym, d in _cache.get('stocks', {}).items():
            if isinstance(d, dict) and d.get('price'):
                prices[sym] = d['price']; pcts[sym] = d.get('pct', 0)
        for sym, d in (_cache.get('crypto') or {}).items():
            if isinstance(d, dict) and d.get('price') and sym != 'error':
                prices[sym] = d['price']; pcts[sym] = d.get('pct', 0)
    return prices, pcts


def run_paper_trades():
    prices, pcts = _snapshot_prices()
    if not prices:
        return

    with _lock:
        signals        = dict(_cache.get('signals', {}))
        fg_value       = _cache.get('fear_greed', {}).get('value', 50)
        reddit         = _cache.get('reddit') or {}
        insider        = _cache.get('insider') or {}
        short_interest = _cache.get('short_interest') or {}
        fed            = _cache.get('fed_sentiment') or {}
        earnings       = _cache.get('earnings') or {}
        options        = _cache.get('options') or {}
        cong           = _cache.get('congress') or {}
        whale          = _cache.get('whale') or {}
        geo            = _cache.get('geo_risk') or {}

    signals_out: dict = {}

    with _portfolio_lock:
        p = load_portfolio()
        if p.get('spy_basis') is None and 'SPY' in prices:
            p['spy_basis']      = prices['SPY']
            p['spy_basis_time'] = datetime.now().isoformat()

        total = sum(pos['shares'] * prices.get(sym, pos['avg_price'])
                    for sym, pos in p['positions'].items()) + p['cash']

        for sym, price in prices.items():
            pct = pcts.get(sym, 0)
            # Use cached Claude signal if fresh, else compute rule-based fast
            if sym in signals:
                signal = signals[sym]
            else:
                signal = calc_ai_score(sym, pct, fg_value, reddit, insider, short_interest, fed, earnings, options)
                signal.update({'source': 'rules', 'confidence': 3})
            signals_out[sym] = signal
            label  = signal['label']
            in_pos = sym in p['positions']

            if label in ('Strong Buy', 'Buy') and not in_pos:
                if len(p['positions']) >= MAX_POSITIONS:
                    continue
                invest = min(total * POSITION_SIZE, p['cash'] * 0.90)
                if invest < 50:
                    continue
                shares = invest / price
                cost   = shares * price
                p['cash'] -= cost
                p['positions'][sym] = {'shares': round(shares, 8), 'avg_price': round(price, 8),
                                       'buy_time': datetime.now().isoformat()}
                p['trades'].append({'id': len(p['trades']) + 1, 'timestamp': datetime.now().isoformat(),
                                    'sym': sym, 'action': 'BUY', 'price': round(price, 4),
                                    'shares': round(shares, 6), 'value': round(cost, 2),
                                    'signal': label, 'ai_score': signal['score'],
                                    'reasons': signal.get('reasons', []),
                                    'source': signal.get('source', 'rules'), 'pnl': None})

            elif label in ('Sell', 'Strong Sell') and in_pos:
                pos      = p['positions'][sym]
                shares   = pos['shares']
                proceeds = shares * price
                pnl      = proceeds - shares * pos['avg_price']
                p['cash'] += proceeds
                del p['positions'][sym]
                p['trades'].append({'id': len(p['trades']) + 1, 'timestamp': datetime.now().isoformat(),
                                    'sym': sym, 'action': 'SELL', 'price': round(price, 4),
                                    'shares': round(shares, 6), 'value': round(proceeds, 2),
                                    'signal': label, 'ai_score': signal['score'],
                                    'reasons': signal.get('reasons', []),
                                    'source': signal.get('source', 'rules'), 'pnl': round(pnl, 2)})

        p['trades'] = p['trades'][-500:]
        save_portfolio(p)

    with _lock:
        _cache['signals'] = {**signals, **signals_out}


def get_portfolio_summary() -> dict:
    prices, _ = _snapshot_prices()
    with _portfolio_lock:
        p = load_portfolio()
    equity  = sum(pos['shares'] * prices.get(sym, pos['avg_price']) for sym, pos in p['positions'].items())
    total   = p['cash'] + equity
    pnl     = total - STARTING_CASH
    pnl_pct = pnl / STARTING_CASH * 100
    closed   = [t for t in p['trades'] if t['action'] == 'SELL' and t.get('pnl') is not None]
    wins     = sum(1 for t in closed if t['pnl'] > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else None
    spy_comparison = None
    spy_curr = prices.get('SPY')
    if spy_curr and p.get('spy_basis'):
        spy_ret = (spy_curr - p['spy_basis']) / p['spy_basis'] * 100
        spy_comparison = {'spy_return': round(spy_ret, 2), 'our_return': round(pnl_pct, 2),
                          'vs_spy': round(pnl_pct - spy_ret, 2)}
    positions_detail = {}
    for sym, pos in p['positions'].items():
        curr_price = prices.get(sym, pos['avg_price'])
        curr_value = pos['shares'] * curr_price
        cost_basis = pos['shares'] * pos['avg_price']
        pos_pnl    = curr_value - cost_basis
        positions_detail[sym] = {'shares': round(pos['shares'], 6), 'avg_price': round(pos['avg_price'], 4),
                                  'curr_price': round(curr_price, 4), 'curr_value': round(curr_value, 2),
                                  'cost_basis': round(cost_basis, 2), 'pnl': round(pos_pnl, 2),
                                  'pnl_pct': round(pos_pnl / cost_basis * 100 if cost_basis else 0, 2),
                                  'buy_time': pos.get('buy_time')}
    return {'cash': round(p['cash'], 2), 'total_value': round(total, 2), 'starting_value': STARTING_CASH,
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2), 'win_rate': win_rate,
            'trades_count': len(p['trades']), 'spy_comparison': spy_comparison,
            'positions': positions_detail, 'trades': list(reversed(p['trades']))[:50],
            'created': p.get('created')}


# ── Background Refresh Loop ────────────────────────────────────────────────────

FETCHERS = {
    'stocks':         fetch_stocks,
    'crypto':         fetch_crypto,
    'fear_greed':     fetch_fear_greed,
    'reddit':         fetch_reddit_sentiment,
    'trends':         fetch_trends,
    'macro':          fetch_macro,
    'news':           fetch_news,
    'fed_sentiment':  fetch_fed_sentiment,
    'short_interest': fetch_short_interest,
    'options':        fetch_options_activity,
    'earnings':       fetch_earnings_sentiment,
    'insider':        fetch_insider_activity,
    'congress':       fetch_congress_trades,
    'whale':          fetch_whale_movements,
    'dark_pool':      fetch_dark_pool,
    'geo_risk':       fetch_geopolitical_risk,
}

_claude_refresh_ttl = 0.0


def _refresh_loop():
    global _paper_ttl, _claude_refresh_ttl
    while True:
        for key, fn in FETCHERS.items():
            if _should_update(key):
                try:
                    data = fn()
                    _set(key, data)
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] ✓ {key}')
                    # Emit WebSocket events for real-time prices
                    if key in ('stocks', 'crypto'):
                        try:
                            socketio.emit('price_update', {key: data})
                        except Exception:
                            pass
                    elif key == 'geo_risk':
                        try:
                            socketio.emit('geo_risk_update', data)
                        except Exception:
                            pass
                    elif key == 'whale':
                        try:
                            socketio.emit('whale_update', data)
                        except Exception:
                            pass
                    elif key == 'congress':
                        try:
                            socketio.emit('congress_update', data)
                        except Exception:
                            pass
                except Exception:
                    print(f'[ERROR] {key}:\n{traceback.format_exc()}')

        # Rebuild smart money summary
        try:
            with _lock:
                sm = build_smart_money_summary(
                    _cache.get('insider', {}), _cache.get('short_interest', {}),
                    _cache.get('options', {}), _cache.get('stocks', {}),
                )
                _cache['smart_money'] = sm
        except Exception:
            pass

        # Refresh Claude signals every 5 minutes
        base_ready = all(_ttl.get(k, 0) > 0 for k in ('stocks', 'crypto', 'fear_greed'))
        if base_ready and (time.time() - _claude_refresh_ttl > CLAUDE_TTL):
            try:
                threading.Thread(target=refresh_all_claude_signals, daemon=True).start()
                _claude_refresh_ttl = time.time()
            except Exception:
                pass

        # Paper trades — every 5 seconds
        if base_ready and (time.time() - _paper_ttl > PAPER_INTERVAL):
            try:
                run_paper_trades()
                _paper_ttl = time.time()
            except Exception:
                print(f'[ERROR] paper_trades:\n{traceback.format_exc()}')

        time.sleep(2)


threading.Thread(target=_refresh_loop, daemon=True).start()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', tv_symbols=json.dumps(TV_SYMBOLS))


@app.route('/api/data')
def api_data():
    with _lock:
        return jsonify({k: v for k, v in _cache.items()})


@app.route('/api/portfolio')
def api_portfolio():
    return jsonify(get_portfolio_summary())


@app.route('/api/paper/reset', methods=['POST'])
def api_paper_reset():
    with _portfolio_lock:
        p = _default_portfolio()
        save_portfolio(p)
    with _lock:
        _cache['signals'] = {}
    global _paper_ttl
    _paper_ttl = 0.0
    return jsonify({'ok': True, 'message': 'Portfolio reset to $100,000'})


@app.route('/api/backtest')
def api_backtest():
    results = {}
    for ticker in STOCKS:
        try:
            hist = yf.Ticker(ticker).history(period='65d').reset_index()
            if hist.empty or len(hist) < 10:
                continue
            hist['pct'] = hist['Close'].pct_change() * 100
            hist['sig'] = 'Hold'
            hist.loc[hist['pct'] >= 3,  'sig'] = 'Strong Buy'
            hist.loc[(hist['pct'] >= 1) & (hist['pct'] < 3), 'sig'] = 'Buy'
            hist.loc[hist['pct'] <= -3, 'sig'] = 'Strong Sell'
            hist.loc[(hist['pct'] <= -1) & (hist['pct'] > -3), 'sig'] = 'Sell'
            cash, shares = 10000.0, 0.0
            trades_log   = []
            for _, row in hist.iterrows():
                price    = float(row['Close'])
                sig      = row['sig']
                date_str = str(row['Date'])[:10]
                if sig in ('Buy', 'Strong Buy') and shares == 0 and cash > 0:
                    shares = cash / price; cash = 0.0
                    trades_log.append({'date': date_str, 'action': 'BUY', 'price': round(price, 2)})
                elif sig in ('Sell', 'Strong Sell') and shares > 0:
                    cash = shares * price; shares = 0.0
                    trades_log.append({'date': date_str, 'action': 'SELL', 'price': round(price, 2)})
            final_val  = cash + shares * float(hist['Close'].iloc[-1])
            start_p    = float(hist['Close'].iloc[0])
            end_p      = float(hist['Close'].iloc[-1])
            signal_ret = (final_val - 10000) / 10000 * 100
            bah_ret    = (end_p - start_p) / start_p * 100
            results[ticker] = {'signal_return': round(signal_ret, 2), 'buy_hold_return': round(bah_ret, 2),
                               'alpha': round(signal_ret - bah_ret, 2), 'trades': len(trades_log),
                               'recent_trades': trades_log[-3:], 'start_price': round(start_p, 2),
                               'end_price': round(end_p, 2), 'days': len(hist)}
        except Exception as e:
            results[ticker] = {'error': str(e)[:80]}
    return jsonify({'results': results, 'period': '60 days', 'generated': datetime.now().isoformat()})


if __name__ == '__main__':
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)
