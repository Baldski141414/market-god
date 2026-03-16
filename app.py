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

SEC_HEADERS = {'User-Agent': 'MarketGod/3.0 market@example.com'}
_cik_cache: dict = {}

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache = {
    'stocks': {}, 'crypto': {}, 'fear_greed': {},
    'reddit': {}, 'trends': {}, 'macro': {}, 'news': [],
    'signals': {},
    'insider': {}, 'short_interest': {}, 'fed_sentiment': {},
    'earnings': {}, 'options': {}, 'smart_money': {},
    'last_updated': None,
}
_ttl: dict[str, float] = {}
_lock = threading.Lock()

INTERVALS = {
    'stocks': 55, 'crypto': 55, 'fear_greed': 60,
    'news': 120, 'reddit': 180, 'macro': 300, 'trends': 300,
    'insider': 3600, 'short_interest': 1800, 'fed_sentiment': 600,
    'earnings': 3600, 'options': 300,
}


def _should_update(key: str) -> bool:
    return time.time() - _ttl.get(key, 0) > INTERVALS.get(key, 300)


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
        headers = {'User-Agent': 'MarketGod:v3.0 (market dashboard; educational)'}
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


# ── SEC EDGAR — Insider Activity ──────────────────────────────────────────────

def _get_cik_map() -> dict:
    global _cik_cache
    if _cik_cache:
        return _cik_cache
    try:
        r = requests.get(
            'https://www.sec.gov/files/company_tickers.json',
            headers=SEC_HEADERS, timeout=15
        )
        for v in r.json().values():
            _cik_cache[v['ticker'].upper()] = str(v['cik_str']).zfill(10)
        print(f'[EDGAR] Loaded {len(_cik_cache)} CIK mappings')
    except Exception as e:
        print(f'[EDGAR] CIK map error: {e}')
    return _cik_cache


def _parse_form4_xml(cik_int: int, accession_no: str, primary_doc: str) -> list:
    acc_nodash = accession_no.replace('-', '')
    xml_url = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary_doc}'
    try:
        r = requests.get(xml_url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)

        owner_name    = root.findtext('.//rptOwnerName', '').strip()
        is_officer    = root.findtext('.//isOfficer', '0').strip() == '1'
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
                transactions.append({
                    'direction': direction,
                    'shares':    round(shares),
                    'price':     round(price, 2),
                    'value':     round(shares * price),
                    'owner':     owner_name,
                    'title':     role,
                    'date':      txn_date,
                    'code':      code,
                })
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
            r = requests.get(
                f'https://data.sec.gov/submissions/CIK{cik}.json',
                headers=SEC_HEADERS, timeout=12
            )
            sub = r.json()
            recent       = sub.get('filings', {}).get('recent', {})
            forms        = recent.get('form', [])
            dates        = recent.get('filingDate', [])
            accessions   = recent.get('accessionNumber', [])
            primary_docs = recent.get('primaryDocument', [])

            form4_queue = []
            for i, form in enumerate(forms[:100]):
                if form == '4' and i < len(dates) and dates[i] >= thirty_days_ago:
                    form4_queue.append({
                        'date':        dates[i],
                        'accession':   accessions[i] if i < len(accessions) else '',
                        'primary_doc': primary_docs[i] if i < len(primary_docs) else 'form4.xml',
                    })

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

            result[ticker] = {
                'transactions':  all_txns[:5],
                'buy_count':     len(buys),
                'sell_count':    len(sells),
                'net_buys':      net,
                'signal':        'bullish' if net > 0 else ('bearish' if net < 0 else 'neutral'),
                'form4_count':   len(form4_queue),
            }
        except Exception as e:
            result[ticker] = {
                'transactions': [], 'net_buys': 0,
                'signal': 'neutral', 'error': str(e)[:80]
            }
    return result


# ── Short Interest (FINRA via Yahoo Finance / yfinance) ───────────────────────

def fetch_short_interest() -> dict:
    result = {}
    for ticker in STOCKS:
        try:
            info       = yf.Ticker(ticker).info
            short_pct  = info.get('shortPercentOfFloat')
            short_ratio = info.get('shortRatio')
            if short_pct is not None:
                short_pct = round(float(short_pct) * 100, 1)
            result[ticker] = {
                'short_pct':    short_pct,
                'short_ratio':  round(float(short_ratio), 1) if short_ratio else None,
                'squeeze_alert': bool(short_pct and short_pct > 20),
                'high_short':   bool(short_pct and short_pct > 15),
            }
        except Exception:
            result[ticker] = {
                'short_pct': None, 'short_ratio': None,
                'squeeze_alert': False, 'high_short': False,
            }
    return result


# ── Federal Reserve NLP ───────────────────────────────────────────────────────

def fetch_fed_sentiment() -> dict:
    HAWKISH = [
        'raise rates', 'rate hike', 'tighten', 'restrictive', 'combat inflation',
        'above target', 'overheat', 'aggressive', 'inflation remains elevated',
        'further increases', 'higher for longer', 'premature to cut',
        'inflation elevated', 'price stability', 'not yet achieved',
    ]
    DOVISH = [
        'cut rates', 'rate cut', 'easing', 'accommodative', 'support growth',
        'below target', 'patient', 'gradual', 'labor market concerns',
        'downside risk', 'pause', 'disinflation', 'progress on inflation',
        'cooling', 'moderated', 'reduce rates',
    ]

    fed_feeds = [
        'https://www.federalreserve.gov/feeds/press_all.xml',
        'https://www.federalreserve.gov/feeds/speeches.xml',
    ]

    all_text = ''
    articles = []
    for url in fed_feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                text = (entry.get('title', '') + ' ' + entry.get('summary', '')).lower()
                all_text += ' ' + text
                articles.append({
                    'title': entry.get('title', '')[:100],
                    'date':  entry.get('published', ''),
                })
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

    return {
        'sentiment':     sentiment,
        'score':         score,
        'hawkish':       hawkish_hits,
        'dovish':        dovish_hits,
        'market_impact': 'bearish' if sentiment == 'hawkish' else ('bullish' if sentiment == 'dovish' else 'neutral'),
        'articles':      articles[:5],
    }


# ── Earnings Call Sentiment ───────────────────────────────────────────────────

def fetch_earnings_sentiment() -> dict:
    CONFIDENT = {
        'beat', 'exceeded', 'raised', 'strong', 'record', 'growth', 'above',
        'outperform', 'positive', 'momentum', 'accelerating', 'robust',
        'solid', 'upside', 'surpass', 'raised guidance',
    }
    CAUTIOUS = {
        'miss', 'below', 'cut', 'concern', 'weak', 'lowered', 'warning',
        'headwind', 'slowdown', 'disappoint', 'risk', 'uncertainty',
        'decline', 'pressure', 'challenging', 'missed estimates',
    }

    result = {}
    for ticker in STOCKS:
        try:
            t    = yf.Ticker(ticker)
            news = t.news or []

            def _news_text(n):
                if isinstance(n, dict):
                    title = n.get('title', '')
                    content = n.get('content', {})
                    if isinstance(content, dict):
                        summary = content.get('summary', '') or content.get('title', '')
                    else:
                        summary = n.get('summary', '') or ''
                    return (title + ' ' + summary).lower()
                return ''

            earnings_kw = {'earnings','beat','miss','revenue','profit','guidance','eps','quarterly','results'}
            e_news = [n for n in news if any(w in _news_text(n) for w in earnings_kw)]

            score, sentiment, conf_h, caut_h = 50, 'neutral', 0, 0
            if e_news:
                text   = ' '.join(_news_text(n) for n in e_news[:5])
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

            result[ticker] = {
                'sentiment':         sentiment,
                'score':             score,
                'confident_signals': conf_h,
                'cautious_signals':  caut_h,
                'news_count':        len(e_news),
                'next_earnings':     next_earnings,
            }
        except Exception:
            result[ticker] = {
                'sentiment': 'neutral', 'score': 50,
                'confident_signals': 0, 'cautious_signals': 0,
                'news_count': 0, 'next_earnings': None,
            }
    return result


# ── Options Unusual Activity ──────────────────────────────────────────────────

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
                            unusual_calls.append({
                                'strike': round(float(row['strike']), 1),
                                'exp':    exp_date,
                                'volume': int(vol),
                                'oi':     int(oi),
                                'ratio':  round(vol / oi, 1),
                                'type':   'CALL',
                            })
                    for _, row in puts.iterrows():
                        oi, vol = float(row['openInterest']), float(row['volume'])
                        if vol > 500 and oi > 0 and vol / oi > 3:
                            unusual_puts.append({
                                'strike': round(float(row['strike']), 1),
                                'exp':    exp_date,
                                'volume': int(vol),
                                'oi':     int(oi),
                                'ratio':  round(vol / oi, 1),
                                'type':   'PUT',
                            })
                except Exception:
                    pass

            unusual_calls.sort(key=lambda x: x['volume'], reverse=True)
            unusual_puts.sort(key=lambda x: x['volume'],  reverse=True)

            pcr = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else None
            n_calls, n_puts = len(unusual_calls), len(unusual_puts)
            signal = 'bullish' if n_calls > n_puts + 1 else ('bearish' if n_puts > n_calls + 1 else 'neutral')

            result[ticker] = {
                'unusual_calls':  unusual_calls[:3],
                'unusual_puts':   unusual_puts[:3],
                'put_call_ratio': pcr,
                'total_call_vol': total_call_vol,
                'total_put_vol':  total_put_vol,
                'signal':         signal,
                'has_unusual':    bool(unusual_calls or unusual_puts),
            }
        except Exception:
            result[ticker] = {
                'unusual_calls': [], 'unusual_puts': [], 'put_call_ratio': None,
                'signal': 'neutral', 'has_unusual': False,
            }
    return result


# ── Master AI Score (0-100) ───────────────────────────────────────────────────

def calc_ai_score(sym: str, pct: float, fg_value: int, reddit: dict,
                  insider: dict, short_interest: dict, fed: dict,
                  earnings: dict, options: dict) -> dict:
    """
    0-100 institutional AI score.
    Baseline 50 = neutral.
      Momentum:        ±25
      Fear & Greed:    ±15
      Reddit WSB:      ±10
      Insider (EDGAR): ±15
      Short squeeze:   ±10
      Fed NLP:         ±10
      Earnings:        ±10
      Options flow:    ± 5
    """
    score   = 50
    reasons = []

    # Price momentum
    if pct >= 5:
        score += 25; reasons.append(f'▲{pct:.1f}% strong move')
    elif pct >= 3:
        score += 15; reasons.append(f'▲{pct:.1f}% momentum')
    elif pct >= 1:
        score += 8;  reasons.append(f'▲{pct:.1f}% momentum')
    elif pct <= -5:
        score -= 25; reasons.append(f'▼{abs(pct):.1f}% strong drop')
    elif pct <= -3:
        score -= 15; reasons.append(f'▼{abs(pct):.1f}% drop')
    elif pct <= -1:
        score -= 8;  reasons.append(f'▼{abs(pct):.1f}% drop')

    # Fear & Greed
    if fg_value >= 75:
        score += 15; reasons.append(f'F&G={fg_value} extreme greed')
    elif fg_value >= 60:
        score += 8;  reasons.append(f'F&G={fg_value} greed')
    elif fg_value <= 25:
        score -= 15; reasons.append(f'F&G={fg_value} extreme fear')
    elif fg_value <= 40:
        score -= 8;  reasons.append(f'F&G={fg_value} fear')

    # Reddit WSB
    rd = reddit.get(sym, {}) if isinstance(reddit, dict) else {}
    if isinstance(rd, dict) and rd.get('mentions', 0) >= 2:
        rs = rd.get('score', 50)
        if rs >= 75:
            score += 10; reasons.append(f'WSB {rs:.0f}% bull')
        elif rs >= 60:
            score += 5;  reasons.append(f'WSB {rs:.0f}% bull')
        elif rs <= 25:
            score -= 10; reasons.append(f'WSB {rs:.0f}% bear')
        elif rs <= 40:
            score -= 5;  reasons.append(f'WSB {rs:.0f}% bear')

    # Insider activity — SEC EDGAR Form 4
    ins = insider.get(sym, {}) if isinstance(insider, dict) else {}
    if isinstance(ins, dict) and 'net_buys' in ins:
        nb = ins['net_buys']
        if nb > 0:
            boost = min(15, nb * 5)
            score += boost; reasons.append(f'Insider buy ×{nb}')
        elif nb < 0:
            drop = min(15, abs(nb) * 5)
            score -= drop;  reasons.append(f'Insider sell ×{abs(nb)}')

    # Short squeeze — FINRA via yfinance
    si = short_interest.get(sym, {}) if isinstance(short_interest, dict) else {}
    if isinstance(si, dict):
        sp = si.get('short_pct') or 0
        if sp > 25 and pct > 0:
            score += 10; reasons.append(f'{sp:.0f}% short → squeeze alert')
        elif sp > 15 and pct > 0:
            score += 5;  reasons.append(f'{sp:.0f}% short interest')
        elif sp > 20 and pct < 0:
            score -= 5;  reasons.append(f'{sp:.0f}% shorts adding pressure')

    # Fed NLP
    if isinstance(fed, dict):
        fs = fed.get('sentiment', 'neutral')
        if fs == 'dovish':
            score += 10; reasons.append('Fed dovish → rate cuts')
        elif fs == 'hawkish':
            score -= 10; reasons.append('Fed hawkish → rate hikes')

    # Earnings sentiment
    earn = earnings.get(sym, {}) if isinstance(earnings, dict) else {}
    if isinstance(earn, dict):
        es = earn.get('sentiment', 'neutral')
        if es == 'confident':
            score += 10; reasons.append('Earnings strong/beat')
        elif es == 'cautious':
            score -= 10; reasons.append('Earnings miss/warn')

    # Options unusual flow
    opts = options.get(sym, {}) if isinstance(options, dict) else {}
    if isinstance(opts, dict):
        os_ = opts.get('signal', 'neutral')
        if os_ == 'bullish':
            score += 5; reasons.append('Smart money calls')
        elif os_ == 'bearish':
            score -= 5; reasons.append('Smart money puts')

    score = max(0, min(100, score))

    if score >= 70:   label = 'Strong Buy'
    elif score >= 58: label = 'Buy'
    elif score <= 30: label = 'Strong Sell'
    elif score <= 42: label = 'Sell'
    else:             label = 'Hold'

    return {'label': label, 'score': score, 'reasons': reasons}


# ── Smart Money Summary ───────────────────────────────────────────────────────

def build_smart_money_summary(insider: dict, short_interest: dict,
                               options: dict, stocks: dict) -> dict:
    all_pcts = {sym: d.get('pct', 0) for sym, d in (stocks or {}).items() if isinstance(d, dict)}
    all_prices = {sym: d.get('price', 0) for sym, d in (stocks or {}).items() if isinstance(d, dict)}

    insider_buys, insider_sells = [], []
    for ticker, ins in (insider or {}).items():
        if not isinstance(ins, dict):
            continue
        for t in ins.get('transactions', [])[:2]:
            entry = {
                'ticker': ticker,
                'owner':  t.get('owner', 'Insider')[:30],
                'title':  t.get('title', '')[:30],
                'value':  t.get('value', 0),
                'shares': t.get('shares', 0),
                'price':  t.get('price', 0),
                'date':   t.get('filing_date', t.get('date', '')),
            }
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
            squeeze.append({
                'ticker':       ticker,
                'short_pct':    sp,
                'short_ratio':  si.get('short_ratio'),
                'current_pct':  all_pcts.get(ticker, 0),
                'squeeze_alert': si.get('squeeze_alert', False),
                'price':        all_prices.get(ticker, 0),
            })
    squeeze.sort(key=lambda x: x['short_pct'], reverse=True)

    unusual_opts = []
    for ticker, opts in (options or {}).items():
        if not isinstance(opts, dict) or not opts.get('has_unusual'):
            continue
        for item, otype in [(opts.get('unusual_calls', []), 'CALL'), (opts.get('unusual_puts', []), 'PUT')]:
            if item:
                unusual_opts.append({
                    'ticker': ticker,
                    'type':   otype,
                    'strike': item[0].get('strike'),
                    'volume': item[0].get('volume'),
                    'ratio':  item[0].get('ratio'),
                    'exp':    item[0].get('exp'),
                    'signal': 'bullish' if otype == 'CALL' else 'bearish',
                })
    unusual_opts.sort(key=lambda x: x.get('volume') or 0, reverse=True)

    return {
        'insider_buys':      insider_buys[:5],
        'insider_sells':     insider_sells[:5],
        'squeeze_candidates': squeeze[:5],
        'unusual_options':   unusual_opts[:6],
        'alert_count':       len(insider_buys) + len(squeeze) + len(unusual_opts),
    }


# ── Paper Trading ─────────────────────────────────────────────────────────────
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paper_trading.json')
STARTING_CASH  = 100_000.0
MAX_POSITIONS  = 12
POSITION_SIZE  = 0.08
_portfolio_lock = threading.Lock()
_paper_ttl      = 0.0
PAPER_INTERVAL  = 65


def _default_portfolio() -> dict:
    return {
        'cash':           STARTING_CASH,
        'positions':      {},
        'trades':         [],
        'spy_basis':      None,
        'spy_basis_time': None,
        'created':        datetime.now().isoformat(),
    }


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
    prices: dict = {}
    pcts:   dict = {}
    with _lock:
        for sym, d in _cache.get('stocks', {}).items():
            if isinstance(d, dict) and d.get('price'):
                prices[sym] = d['price']
                pcts[sym]   = d.get('pct', 0)
        for sym, d in (_cache.get('crypto') or {}).items():
            if isinstance(d, dict) and d.get('price') and sym != 'error':
                prices[sym] = d['price']
                pcts[sym]   = d.get('pct', 0)
    return prices, pcts


def run_paper_trades():
    prices, pcts = _snapshot_prices()
    if not prices:
        return

    with _lock:
        fg_value      = _cache.get('fear_greed', {}).get('value', 50)
        reddit        = _cache.get('reddit') or {}
        insider       = _cache.get('insider') or {}
        short_interest = _cache.get('short_interest') or {}
        fed           = _cache.get('fed_sentiment') or {}
        earnings      = _cache.get('earnings') or {}
        options       = _cache.get('options') or {}

    signals_out: dict = {}

    with _portfolio_lock:
        p = load_portfolio()

        if p.get('spy_basis') is None and 'SPY' in prices:
            p['spy_basis']      = prices['SPY']
            p['spy_basis_time'] = datetime.now().isoformat()

        total = sum(pos['shares'] * prices.get(sym, pos['avg_price'])
                    for sym, pos in p['positions'].items()) + p['cash']

        for sym, price in prices.items():
            pct    = pcts.get(sym, 0)
            signal = calc_ai_score(sym, pct, fg_value, reddit,
                                   insider, short_interest, fed, earnings, options)
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
                p['positions'][sym] = {
                    'shares':    round(shares, 8),
                    'avg_price': round(price,  8),
                    'buy_time':  datetime.now().isoformat(),
                }
                p['trades'].append({
                    'id':        len(p['trades']) + 1,
                    'timestamp': datetime.now().isoformat(),
                    'sym':       sym,
                    'action':    'BUY',
                    'price':     round(price, 4),
                    'shares':    round(shares, 6),
                    'value':     round(cost, 2),
                    'signal':    label,
                    'ai_score':  signal['score'],
                    'reasons':   signal['reasons'],
                    'pnl':       None,
                })

            elif label in ('Sell', 'Strong Sell') and in_pos:
                pos      = p['positions'][sym]
                shares   = pos['shares']
                proceeds = shares * price
                pnl      = proceeds - shares * pos['avg_price']
                p['cash'] += proceeds
                del p['positions'][sym]
                p['trades'].append({
                    'id':        len(p['trades']) + 1,
                    'timestamp': datetime.now().isoformat(),
                    'sym':       sym,
                    'action':    'SELL',
                    'price':     round(price, 4),
                    'shares':    round(shares, 6),
                    'value':     round(proceeds, 2),
                    'signal':    label,
                    'ai_score':  signal['score'],
                    'reasons':   signal['reasons'],
                    'pnl':       round(pnl, 2),
                })

        p['trades'] = p['trades'][-500:]
        save_portfolio(p)

    with _lock:
        _cache['signals'] = signals_out


def get_portfolio_summary() -> dict:
    prices, _ = _snapshot_prices()

    with _portfolio_lock:
        p = load_portfolio()

    equity  = sum(pos['shares'] * prices.get(sym, pos['avg_price'])
                  for sym, pos in p['positions'].items())
    total   = p['cash'] + equity
    pnl     = total - STARTING_CASH
    pnl_pct = (pnl / STARTING_CASH) * 100

    closed   = [t for t in p['trades'] if t['action'] == 'SELL' and t.get('pnl') is not None]
    wins     = sum(1 for t in closed if t['pnl'] > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else None

    spy_comparison = None
    spy_curr = prices.get('SPY')
    if spy_curr and p.get('spy_basis'):
        spy_ret = (spy_curr - p['spy_basis']) / p['spy_basis'] * 100
        spy_comparison = {
            'spy_return': round(spy_ret, 2),
            'our_return': round(pnl_pct, 2),
            'vs_spy':     round(pnl_pct - spy_ret, 2),
        }

    positions_detail = {}
    for sym, pos in p['positions'].items():
        curr_price  = prices.get(sym, pos['avg_price'])
        curr_value  = pos['shares'] * curr_price
        cost_basis  = pos['shares'] * pos['avg_price']
        pos_pnl     = curr_value - cost_basis
        pos_pnl_pct = (pos_pnl / cost_basis * 100) if cost_basis else 0
        positions_detail[sym] = {
            'shares':     round(pos['shares'], 6),
            'avg_price':  round(pos['avg_price'], 4),
            'curr_price': round(curr_price, 4),
            'curr_value': round(curr_value, 2),
            'cost_basis': round(cost_basis, 2),
            'pnl':        round(pos_pnl, 2),
            'pnl_pct':    round(pos_pnl_pct, 2),
            'buy_time':   pos.get('buy_time'),
        }

    return {
        'cash':           round(p['cash'], 2),
        'total_value':    round(total, 2),
        'starting_value': STARTING_CASH,
        'pnl':            round(pnl, 2),
        'pnl_pct':        round(pnl_pct, 2),
        'win_rate':       win_rate,
        'trades_count':   len(p['trades']),
        'spy_comparison': spy_comparison,
        'positions':      positions_detail,
        'trades':         list(reversed(p['trades']))[:50],
        'created':        p.get('created'),
    }


# ── Background Refresh Loop ───────────────────────────────────────────────────
FETCHERS = {
    'stocks':        fetch_stocks,
    'crypto':        fetch_crypto,
    'fear_greed':    fetch_fear_greed,
    'reddit':        fetch_reddit_sentiment,
    'trends':        fetch_trends,
    'macro':         fetch_macro,
    'news':          fetch_news,
    'fed_sentiment': fetch_fed_sentiment,
    'short_interest': fetch_short_interest,
    'options':       fetch_options_activity,
    'earnings':      fetch_earnings_sentiment,
    'insider':       fetch_insider_activity,
}


def _refresh_loop():
    global _paper_ttl
    while True:
        for key, fn in FETCHERS.items():
            if _should_update(key):
                try:
                    _set(key, fn())
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] ✓ {key}')
                except Exception:
                    print(f'[ERROR] {key}:\n{traceback.format_exc()}')

        # Rebuild smart money summary whenever upstream data is fresh
        try:
            with _lock:
                sm = build_smart_money_summary(
                    _cache.get('insider', {}),
                    _cache.get('short_interest', {}),
                    _cache.get('options', {}),
                    _cache.get('stocks', {}),
                )
                _cache['smart_money'] = sm
        except Exception:
            pass

        base_ready = all(_ttl.get(k, 0) > 0 for k in ('stocks', 'crypto', 'fear_greed'))
        if base_ready and (time.time() - _paper_ttl > PAPER_INTERVAL):
            try:
                run_paper_trades()
                _paper_ttl = time.time()
                print(f'[{datetime.now().strftime("%H:%M:%S")}] ✓ paper_trades')
            except Exception:
                print(f'[ERROR] paper_trades:\n{traceback.format_exc()}')

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
    """60-day momentum signal backtest vs buy-and-hold for all tracked stocks."""
    results = {}
    for ticker in STOCKS:
        try:
            hist = yf.Ticker(ticker).history(period='65d').reset_index()
            if hist.empty or len(hist) < 10:
                continue

            hist['pct'] = hist['Close'].pct_change() * 100
            hist['sig'] = 'Hold'
            hist.loc[hist['pct'] >= 3,  'sig'] = 'Strong Buy'
            hist.loc[(hist['pct'] >= 1) & (hist['pct'] < 3),   'sig'] = 'Buy'
            hist.loc[hist['pct'] <= -3, 'sig'] = 'Strong Sell'
            hist.loc[(hist['pct'] <= -1) & (hist['pct'] > -3), 'sig'] = 'Sell'

            cash, shares = 10000.0, 0.0
            trades_log = []
            for _, row in hist.iterrows():
                price    = float(row['Close'])
                sig      = row['sig']
                date_str = str(row['Date'])[:10]
                if sig in ('Buy', 'Strong Buy') and shares == 0 and cash > 0:
                    shares = cash / price
                    cash   = 0.0
                    trades_log.append({'date': date_str, 'action': 'BUY',  'price': round(price, 2)})
                elif sig in ('Sell', 'Strong Sell') and shares > 0:
                    cash   = shares * price
                    shares = 0.0
                    trades_log.append({'date': date_str, 'action': 'SELL', 'price': round(price, 2)})

            final_val = cash + shares * float(hist['Close'].iloc[-1])
            start_p   = float(hist['Close'].iloc[0])
            end_p     = float(hist['Close'].iloc[-1])

            signal_ret = (final_val - 10000) / 10000 * 100
            bah_ret    = (end_p - start_p) / start_p * 100

            results[ticker] = {
                'signal_return':   round(signal_ret, 2),
                'buy_hold_return': round(bah_ret, 2),
                'alpha':           round(signal_ret - bah_ret, 2),
                'trades':          len(trades_log),
                'recent_trades':   trades_log[-3:],
                'start_price':     round(start_p, 2),
                'end_price':       round(end_p, 2),
                'days':            len(hist),
            }
        except Exception as e:
            results[ticker] = {'error': str(e)[:80]}

    return jsonify({
        'results':   results,
        'period':    '60 days',
        'generated': datetime.now().isoformat(),
    })


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)
