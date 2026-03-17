"""
Baltic Dry Index + Global Shipping Stress — FRED free API.
BDI leads economic activity by 6 months.
Rising BDI = global growth incoming = risk-on.
Crashing BDI = recession warning = risk-off.
Refreshes every hour (FRED updates daily).
"""
import threading
import time
import requests
from core.data_store import store

_FRED_BDI_URL  = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=BDIY'
_FRED_CASS_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=CASSSHIPM'  # Cass Freight Shipments
_TIMEOUT = 15
_REFRESH = 3600  # 1 hour


def _parse_fred_csv(url: str) -> list[tuple[str, float]]:
    """Download a FRED CSV and return list of (date_str, value)."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = []
        for line in resp.text.strip().splitlines()[1:]:  # skip header
            parts = line.split(',')
            if len(parts) == 2:
                date_str = parts[0].strip()
                val_str  = parts[1].strip()
                if val_str and val_str != '.':
                    try:
                        rows.append((date_str, float(val_str)))
                    except ValueError:
                        pass
        return rows
    except Exception as e:
        print(f'[Shipping] FRED fetch error {url}: {e}')
        return []


def _compute_signal(series: list[tuple[str, float]]) -> dict:
    """Compute trend signal from a FRED time series."""
    if not series:
        return {'latest': None, 'change_pct': 0.0, 'signal': 0.0}

    # Use last 2 valid data points for change
    valid = [(d, v) for d, v in series if v and v > 0]
    if not valid:
        return {'latest': None, 'change_pct': 0.0, 'signal': 0.0}

    latest_date, latest_val = valid[-1]

    # 30-day change (approx 22 trading days)
    lookback = max(0, len(valid) - 22)
    prev_val = valid[lookback][1]
    change_pct = (latest_val - prev_val) / prev_val * 100 if prev_val else 0.0

    # Signal: -1 to +1 (20% swing = full signal)
    signal = max(-1.0, min(1.0, change_pct / 20.0))

    return {
        'latest':     round(latest_val, 2),
        'latest_date': latest_date,
        'change_pct': round(change_pct, 2),
        'signal':     round(signal, 3),
    }


def _refresh_loop():
    while True:
        result = {
            'bdi': {},
            'cass_freight': {},
            'macro_signal': 0.0,
            'regime': 'NEUTRAL',
            'ts': time.time(),
        }
        try:
            bdi_series   = _parse_fred_csv(_FRED_BDI_URL)
            cass_series  = _parse_fred_csv(_FRED_CASS_URL)

            bdi_data  = _compute_signal(bdi_series)
            cass_data = _compute_signal(cass_series)

            # Combine into macro signal
            bdi_sig  = bdi_data.get('signal', 0.0)
            cass_sig = cass_data.get('signal', 0.0)
            macro_signal = round(bdi_sig * 0.6 + cass_sig * 0.4, 3)

            if macro_signal > 0.3:
                regime = 'GROWTH'
            elif macro_signal < -0.3:
                regime = 'CONTRACTION'
            else:
                regime = 'NEUTRAL'

            result = {
                'bdi':          bdi_data,
                'cass_freight': cass_data,
                'macro_signal': macro_signal,
                'regime':       regime,
                'ts':           time.time(),
            }
            print(f'[Shipping] BDI={bdi_data.get("latest")} '
                  f'chg={bdi_data.get("change_pct")}% regime={regime}')
        except Exception as e:
            print(f'[Shipping] error: {e}')

        with store._lock:
            store.altdata['shipping'] = result

        time.sleep(_REFRESH)


def start_shipping():
    t = threading.Thread(target=_refresh_loop, daemon=True, name='shipping')
    t.start()
