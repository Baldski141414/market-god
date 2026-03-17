"""
Dark Pool & Institutional Activity — FINRA RegSho daily short volume (free).
High short sale % → institutional dark pool positioning signal.
Spike in dark pool volume before earnings = someone knows something.
Refreshes every 4 hours (FINRA updates next trading day).
"""
import threading
import time
import datetime
import requests
from core.config import ALL_STOCK_TICKERS
from core.data_store import store

_FINRA_BASE = 'https://cdn.finra.org/equity/regsho/daily'
_TIMEOUT = 20
_REFRESH = 14400  # 4 hours


def _build_url(date: datetime.date) -> str:
    return f'{_FINRA_BASE}/CNMSshvol{date.strftime("%Y%m%d")}.txt'


def _fetch_finra_shvol(date: datetime.date) -> dict[str, dict] | None:
    """
    Download FINRA consolidated short sale volume file.
    Returns {symbol: {short_vol, total_vol, short_pct}} or None on failure.
    """
    url = _build_url(date)
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        result: dict[str, dict] = {}
        for line in resp.text.strip().splitlines():
            parts = line.split('|')
            if len(parts) < 5:
                continue
            symbol = parts[1].strip().upper()
            try:
                short_vol = int(parts[2])
                total_vol = int(parts[4])
                if total_vol == 0:
                    continue
                short_pct = round(short_vol / total_vol * 100, 2)
                result[symbol] = {
                    'short_vol':  short_vol,
                    'total_vol':  total_vol,
                    'short_pct':  short_pct,
                }
            except (ValueError, IndexError):
                pass
        return result if result else None
    except Exception as e:
        print(f'[DarkPool] fetch error for {date}: {e}')
        return None


def _refresh_loop():
    # Track 5-day baseline per symbol
    baselines: dict[str, float] = {}

    while True:
        result: dict[str, dict] = {}
        try:
            # Try today and previous 3 trading days
            today = datetime.date.today()
            raw = None
            for delta in range(4):
                check_date = today - datetime.timedelta(days=delta)
                if check_date.weekday() >= 5:  # skip weekends
                    continue
                raw = _fetch_finra_shvol(check_date)
                if raw:
                    print(f'[DarkPool] loaded FINRA data for {check_date}, {len(raw)} symbols')
                    break

            if not raw:
                print('[DarkPool] no FINRA data available')
                time.sleep(_REFRESH)
                continue

            # Filter to tracked symbols and compute signals
            for sym in ALL_STOCK_TICKERS:
                sym_clean = sym.replace('-', '.')  # FINRA uses BRK.B not BRK-B
                data = raw.get(sym_clean) or raw.get(sym)
                if not data:
                    continue

                short_pct = data['short_pct']
                # Update baseline (slow EMA)
                base = baselines.get(sym, short_pct)
                baselines[sym] = base * 0.85 + short_pct * 0.15

                # Spike: current short % vs 5-day baseline
                spike = short_pct - baselines[sym]

                # Signal interpretation:
                # High short_pct (>50%) = heavy dark pool = institutional positioning
                # Spike above baseline = someone building position quietly
                if short_pct > 50:
                    signal = min(1.0, (short_pct - 40) / 30)
                elif short_pct < 20:
                    signal = -0.2
                else:
                    signal = 0.0

                result[sym] = {
                    'short_pct':  short_pct,
                    'short_vol':  data['short_vol'],
                    'total_vol':  data['total_vol'],
                    'baseline':   round(baselines[sym], 2),
                    'spike':      round(spike, 2),
                    'signal':     round(signal, 3),
                }

            print(f'[DarkPool] scored {len(result)} tickers')
        except Exception as e:
            print(f'[DarkPool] error: {e}')

        with store._lock:
            store.altdata['dark_pool'] = result

        time.sleep(_REFRESH)


def start_dark_pool():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='dark_pool')
    t.start()
