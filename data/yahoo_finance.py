"""
Yahoo Finance poller.
Fetches prices for all stock tickers every 30 seconds.
Rate-limited: max 5 tickers per batch, 0.5s delay between batches, with retry logic.
Also pre-loads 5-minute historical data for technical indicators.
"""
import threading
import time
import yfinance as yf
from core.config import ALL_STOCK_TICKERS, YAHOO_REFRESH_SECS
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

# Rate-limit settings per user request
_BATCH        = 5    # max tickers per request
_BATCH_DELAY  = 0.5  # seconds between batches
_MAX_RETRIES  = 3
_RETRY_DELAY  = 2.0  # seconds between retries


def _download_with_retry(tickers: list[str], period: str, interval: str):
    """Download yfinance data with retries and exponential back-off."""
    for attempt in range(_MAX_RETRIES):
        try:
            raw = yf.download(
                tickers=' '.join(tickers),
                period=period,
                interval=interval,
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=False,   # avoid internal threading that triggers rate-limits
            )
            return raw
        except Exception as e:
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_DELAY * (attempt + 1)
                print(f'[Yahoo] download error (attempt {attempt+1}/{_MAX_RETRIES}): {e} — retrying in {wait}s')
                time.sleep(wait)
            else:
                print(f'[Yahoo] download failed after {_MAX_RETRIES} attempts: {e}')
    return None


def _seed_history(tickers: list[str]):
    """Load 5-min bars so indicators have data before live ticks arrive."""
    print(f'[Yahoo] seeding history for {len(tickers)} tickers...')
    for i in range(0, len(tickers), _BATCH):
        batch = tickers[i:i + _BATCH]
        raw = _download_with_retry(batch, period='5d', interval='5m')
        if raw is not None and not raw.empty:
            for sym in batch:
                try:
                    if len(batch) == 1:
                        df = raw
                    else:
                        df = raw[sym] if sym in raw.columns.get_level_values(0) else None
                    if df is None or df.empty:
                        continue
                    df = df.dropna(subset=['Close'])
                    for row in df.itertuples():
                        ts = row.Index.timestamp() if hasattr(row.Index, 'timestamp') else time.time()
                        store.push_price(sym, float(row.Close), float(row.Volume or 0), ts)
                except Exception:
                    pass
        time.sleep(_BATCH_DELAY)
    print('[Yahoo] history seed complete')


# yfinance symbol → internal display symbol for crypto
_CRYPTO_SEED_MAP = {
    'BTC-USD': 'BTC',
    'ETH-USD': 'ETH',
    'SOL-USD': 'SOL',
    'XRP-USD': 'XRP',
}


def _seed_crypto_history():
    """Seed 5-min historical bars for crypto via yfinance so charts have real data."""
    print('[Yahoo] seeding crypto history...')
    for yf_sym, display_sym in _CRYPTO_SEED_MAP.items():
        raw = _download_with_retry([yf_sym], period='1d', interval='5m')
        if raw is not None and not raw.empty:
            try:
                df = raw.dropna(subset=['Close'])
                for row in df.itertuples():
                    ts = row.Index.timestamp() if hasattr(row.Index, 'timestamp') else time.time()
                    store.push_price(display_sym, float(row.Close), float(row.Volume or 0), ts)
            except Exception as e:
                print(f'[Yahoo] crypto seed error for {yf_sym}: {e}')
        time.sleep(_BATCH_DELAY)
    print('[Yahoo] crypto history seed complete')


def _refresh_loop():
    # Seed historical data once at startup
    _seed_crypto_history()
    _seed_history(ALL_STOCK_TICKERS)

    while True:
        start = time.time()
        for i in range(0, len(ALL_STOCK_TICKERS), _BATCH):
            batch = ALL_STOCK_TICKERS[i:i + _BATCH]
            for attempt in range(_MAX_RETRIES):
                try:
                    tickers_obj = yf.Tickers(' '.join(batch))
                    for sym in batch:
                        try:
                            info  = tickers_obj.tickers[sym].fast_info
                            price = float(info.last_price or 0)
                            prev  = float(info.previous_close or 0)
                            if price > 0:
                                chg = (price - prev) / prev * 100 if prev else 0
                                vol = float(getattr(info, 'three_month_average_volume', 0) or 0)
                                store.push_price(sym, price, vol)
                                bus.publish(EVT_PRICE_TICK, {
                                    'symbol': sym, 'price': price,
                                    'volume': vol, 'change_pct': chg,
                                    'ts': time.time(), 'source': 'yahoo',
                                })
                        except Exception:
                            pass
                    break  # success — no retry needed
                except Exception as e:
                    if attempt < _MAX_RETRIES - 1:
                        wait = _RETRY_DELAY * (attempt + 1)
                        print(f'[Yahoo] refresh error (attempt {attempt+1}/{_MAX_RETRIES}): {e} — retrying in {wait}s')
                        time.sleep(wait)
                    else:
                        print(f'[Yahoo] refresh failed for batch {batch}: {e}')
            time.sleep(_BATCH_DELAY)

        elapsed   = time.time() - start
        sleep_for = max(0, YAHOO_REFRESH_SECS - elapsed)
        time.sleep(sleep_for)


def start_yahoo():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='yahoo-finance')
    t.start()
    print('[Yahoo] poller starting...')
