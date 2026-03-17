"""
Earnings Call NLP — SEC EDGAR 8-K filings, free no key.
Score management language for confidence vs uncertainty markers.
CEO says "headwinds" 3x more = bearish. Guidance raise = bullish.
Refreshes every 4 hours.
"""
import threading
import time
import re
import requests
from core.config import ALL_STOCK_TICKERS
from core.data_store import store

_EDGAR_SEARCH  = 'https://efts.sec.gov/LATEST/search-index'
_EDGAR_DOC_URL = 'https://www.sec.gov'
_TIMEOUT = 20
_REFRESH = 14400  # 4 hours

_HEADERS = {'User-Agent': 'MarketGod/6.0 contact@marketgod.io'}

# Confidence/bullish language markers
_BULLISH_TERMS = [
    'record revenue', 'exceeded expectations', 'raised guidance', 'raised outlook',
    'raised full year', 'strong demand', 'accelerating growth', 'beat estimates',
    'record-breaking', 'outperformed', 'momentum', 'confident', 'robust demand',
    'multiple expansion', 'market share gains', 'operating leverage', 'ahead of plan',
    'stronger than expected', 'raised our', 'increasing guidance',
]

# Uncertainty/bearish language markers
_BEARISH_TERMS = [
    'headwinds', 'challenging', 'uncertain', 'softness', 'softer demand',
    'lowered guidance', 'lowered outlook', 'below expectations', 'disappointed',
    'miss', 'shortfall', 'weakness', 'difficult environment', 'pressured margins',
    'macro uncertainty', 'slowing demand', 'reduced outlook', 'cautious',
    'deteriorating', 'inventory correction', 'pricing pressure',
]

# CIK map for major companies (static — CIK doesn't change)
_TICKER_CIK = {
    'AAPL':  '320193',
    'MSFT':  '789019',
    'NVDA':  '1045810',
    'TSLA':  '1318605',
    'META':  '1326801',
    'GOOGL': '1652044',
    'AMZN':  '1018724',
    'AMD':   '2488',
    'INTC':  '50863',
    'NFLX':  '1065280',
    'JPM':   '19617',
    'BAC':   '70858',
    'GS':    '886982',
    'COIN':  '1679788',
    'PLTR':  '1321655',
}


def _fetch_latest_8k_text(cik: str) -> str | None:
    """Fetch the most recent 8-K filing text for a company."""
    try:
        # Get filing list
        url = f'https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json'
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        filings = data.get('filings', {}).get('recent', {})
        forms   = filings.get('form', [])
        acc_nos = filings.get('accessionNumber', [])
        dates   = filings.get('filingDate', [])

        # Find most recent 8-K
        for i, form in enumerate(forms):
            if form == '8-K' and i < len(acc_nos):
                acc = acc_nos[i].replace('-', '')
                # Fetch the filing index
                idx_url = f'https://www.sec.gov/Archives/edgar/full-index/{dates[i][:4]}/{dates[i][5:7]}/{acc}-index.json'
                # Simpler: use the EDGAR viewer
                filing_url = (f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany'
                              f'&CIK={cik}&type=8-K&dateb=&owner=include&count=1&search_text=&output=atom')
                feed_resp = requests.get(filing_url, headers=_HEADERS, timeout=_TIMEOUT)
                if not feed_resp.ok:
                    break
                # Extract text filing link
                text = feed_resp.text
                # Look for htm link in filing index
                import re
                links = re.findall(r'https://www\.sec\.gov/Archives/edgar/data/\d+/[\d]+/[^"]+\.htm', text)
                if links:
                    doc_resp = requests.get(links[0], headers=_HEADERS, timeout=_TIMEOUT)
                    if doc_resp.ok:
                        # Strip HTML tags
                        raw = re.sub(r'<[^>]+>', ' ', doc_resp.text)
                        return raw[:8000]  # first 8000 chars
                break
    except Exception:
        pass
    return None


def _score_text(text: str) -> float:
    """Score text for bullish/bearish language. Returns -1 to +1."""
    if not text:
        return 0.0
    text_lower = text.lower()
    bull_count = sum(1 for t in _BULLISH_TERMS if t in text_lower)
    bear_count = sum(1 for t in _BEARISH_TERMS if t in text_lower)
    total = bull_count + bear_count
    if total == 0:
        return 0.0
    net = (bull_count - bear_count) / total
    # Amplify if strong signal
    return round(max(-1.0, min(1.0, net * 1.5)), 3)


def _refresh_loop():
    while True:
        result: dict[str, dict] = {}
        try:
            for ticker, cik in _TICKER_CIK.items():
                text = _fetch_latest_8k_text(cik)
                sentiment = _score_text(text)
                result[ticker] = {
                    'sentiment': sentiment,
                    'has_data':  text is not None,
                }
                time.sleep(2)

            print(f'[EarningsNLP] scored {len(result)} companies')
        except Exception as e:
            print(f'[EarningsNLP] error: {e}')

        with store._lock:
            store.altdata['earnings_nlp'] = result

        time.sleep(_REFRESH)


def start_earnings_nlp():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='earnings_nlp')
    t.start()
