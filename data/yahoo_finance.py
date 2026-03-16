"""
Yahoo Finance poller.
Fetches prices for all stock tickers every 30 seconds using batch download.
Also pre-loads 5-minute historical data for technical indicators.
"""
import threading
import time
import yfinance as yf
from core.config import ALL_STOCK_TICKERS, YAHOO_REFRESH_SECS
from core.event_bus import bus, EVT_PRICE_TICK
from core.data_store import store

# Batch size for yfinance downloads
_BATCH = 50


def _fetch_batch(tickers: list[str]):
    try:
        raw = yf.download(
            tickers=' '.join(tickers),
            period='1d',
            interval='5m',
            group_by='ticker',
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        return raw
    except Exception as e:
        print(f'[Yahoo] batch error: {e}')
        return None


def _seed_history(tickers: list[str]):
    """Load 5-min bars so indicators have data before live ticks arrive."""
    print(f'[Yahoo] seeding history for {len(tickers)} tickers...')
    for i in range(0, len(tickers), _BATCH):
        batch = tickers[i:i + _BATCH]
        try:
            raw = yf.download(
                tickers=' '.join(batch),
                period='5d',
                interval='5m',
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw is None or raw.empty:
                continue
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
        except Exception as e:
            print(f'[Yahoo] seed batch error: {e}')
        time.sleep(0.5)
    print('[Yahoo] history seed complete')


def _refresh_loop():
    # Seed historical data once at startup
    _seed_history(ALL_STOCK_TICKERS)

    while True:
        start = time.time()
        for i in range(0, len(ALL_STOCK_TICKERS), _BATCH):
            batch = ALL_STOCK_TICKERS[i:i + _BATCH]
            try:
                tickers_obj = yf.Tickers(' '.join(batch))
                for sym in batch:
                    try:
                        info = tickers_obj.tickers[sym].fast_info
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
            except Exception as e:
                print(f'[Yahoo] refresh error: {e}')
            time.sleep(0.2)

        elapsed = time.time() - start
        sleep_for = max(0, YAHOO_REFRESH_SECS - elapsed)
        time.sleep(sleep_for)


def start_yahoo():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='yahoo-finance')
    t.start()
    print('[Yahoo] poller starting...')
