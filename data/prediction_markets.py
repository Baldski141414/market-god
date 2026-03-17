"""
Prediction Market Intelligence — Polymarket Gamma API (free, no key).
Real money = more accurate than analyst forecasts.
Fed rate decision odds, earnings beat/miss probabilities.
Refreshes every 5 minutes.
"""
import threading
import time
import re
import requests
from core.config import SLOW_DATA_REFRESH_SECS
from core.data_store import store

_POLYMARKET_URL = 'https://gamma-api.polymarket.com/markets'
_TIMEOUT = 15
_REFRESH = SLOW_DATA_REFRESH_SECS

# Keywords to classify markets
_FED_KEYWORDS    = ['fed', 'federal reserve', 'rate cut', 'rate hike', 'fomc', 'interest rate']
_EQUITY_MAP = {
    'nvda':   'NVDA', 'nvidia':   'NVDA',
    'aapl':   'AAPL', 'apple':    'AAPL',
    'tsla':   'TSLA', 'tesla':    'TSLA',
    'msft':   'MSFT', 'microsoft':'MSFT',
    'googl':  'GOOGL','google':   'GOOGL', 'alphabet': 'GOOGL',
    'meta':   'META',
    'amzn':   'AMZN', 'amazon':   'AMZN',
    'spy':    'SPY',  'sp500':    'SPY',  's&p':     'SPY',
    'btc':    'BTC',  'bitcoin':  'BTC',
    'eth':    'ETH',  'ethereum': 'ETH',
    'crypto': 'BTC',
}


def _classify_market(question: str) -> tuple[str, str | None]:
    """Returns (category, ticker_or_None)."""
    q = question.lower()
    if any(kw in q for kw in _FED_KEYWORDS):
        return 'fed', 'TLT'  # Fed decisions affect TLT/rates
    for kw, ticker in _EQUITY_MAP.items():
        if kw in q:
            return 'equity', ticker
    if any(kw in q for kw in ['recession', 'gdp', 'cpi', 'inflation', 'nfp', 'unemployment']):
        return 'macro', None
    return 'other', None


def _parse_yes_probability(market: dict) -> float | None:
    """Extract Yes probability from market, returns 0-100."""
    try:
        outcomes       = market.get('outcomes') or []
        outcome_prices = market.get('outcomePrices') or []
        if not outcomes or not outcome_prices:
            return None
        for i, outcome in enumerate(outcomes):
            if str(outcome).lower() in ('yes', 'true', '1'):
                p = float(outcome_prices[i])
                return round(p * 100, 1)  # Polymarket uses 0-1 scale
    except Exception:
        pass
    return None


def _refresh_loop():
    while True:
        result = {
            'markets':       [],
            'by_ticker':     {},  # ticker -> {bull_pct, question, volume}
            'fed_cut_prob':  50.0,
            'recession_prob': 20.0,
            'ts': time.time(),
        }
        try:
            resp = requests.get(
                _POLYMARKET_URL,
                params={'active': 'true', 'limit': 200},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            markets = resp.json()

            parsed_markets = []
            by_ticker: dict[str, list] = {}
            fed_probs = []
            recession_probs = []

            for mkt in markets:
                question = mkt.get('question') or ''
                volume   = float(mkt.get('volume') or 0)
                if volume < 1000:  # skip tiny markets
                    continue

                yes_prob = _parse_yes_probability(mkt)
                if yes_prob is None:
                    continue

                category, ticker = _classify_market(question)

                entry = {
                    'question': question[:120],
                    'yes_prob': yes_prob,
                    'volume':   round(volume),
                    'category': category,
                    'ticker':   ticker,
                }
                parsed_markets.append(entry)

                if ticker:
                    if ticker not in by_ticker:
                        by_ticker[ticker] = []
                    by_ticker[ticker].append(entry)

                if category == 'fed':
                    fed_probs.append(yes_prob)
                elif category == 'macro' and 'recession' in question.lower():
                    recession_probs.append(yes_prob)

            # Aggregate per-ticker: weighted avg of yes_prob by volume
            ticker_signals: dict[str, dict] = {}
            for ticker, mkts in by_ticker.items():
                total_vol = sum(m['volume'] for m in mkts)
                if total_vol == 0:
                    continue
                bull_pct = sum(m['yes_prob'] * m['volume'] for m in mkts) / total_vol
                ticker_signals[ticker] = {
                    'bull_pct':  round(bull_pct, 1),
                    'signal':    round((bull_pct - 50) / 50, 3),
                    'markets':   len(mkts),
                    'top_question': sorted(mkts, key=lambda x: -x['volume'])[0]['question'],
                }

            # Sort by volume
            parsed_markets.sort(key=lambda x: -x['volume'])

            result = {
                'markets':       parsed_markets[:30],
                'by_ticker':     ticker_signals,
                'fed_cut_prob':  round(sum(fed_probs) / len(fed_probs), 1) if fed_probs else 50.0,
                'recession_prob': round(sum(recession_probs) / len(recession_probs), 1) if recession_probs else 20.0,
                'ts':            time.time(),
            }
            print(f'[PredMkts] {len(parsed_markets)} markets, '
                  f'fed_cut={result["fed_cut_prob"]}%, '
                  f'tickers={len(ticker_signals)}')
        except Exception as e:
            print(f'[PredMkts] error: {e}')

        with store._lock:
            store.altdata['prediction_markets'] = result

        time.sleep(_REFRESH)


def start_prediction_markets():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='prediction_markets')
    t.start()
