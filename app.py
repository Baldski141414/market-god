"""
app.py — Flask server + background polling engine for Market God.

Run:
    python app.py
Dashboard: http://localhost:5000
"""
import datetime as dt
import logging
import threading
import time

from flask import Flask, jsonify, render_template

from config import AVG_VOL_REFRESH, LOG_FILE, POLL_INTERVAL, WATCHLIST
from data_fetcher import fetch_bars, get_30day_avg_volume, get_vix
from managers import PortfolioManager
from signal_engine import evaluate_signal

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
logger = logging.getLogger("market_god")

# ── App & shared state ─────────────────────────────────────────────────────────
app = Flask(__name__)
pm = PortfolioManager()
_lock = threading.Lock()

_state: dict = {
    "signals": {},
    "prices": {},
    "vix": None,
    "last_update": None,
    "engine_status": "starting",
    "trade_log": [],
}


# ── Background engine ──────────────────────────────────────────────────────────

def _polling_loop():
    """Main scan loop — runs every POLL_INTERVAL seconds."""
    logger.info(f"Engine started. Watchlist: {WATCHLIST}")
    avg_volumes: dict[str, float] = {}
    last_vol_refresh: float = 0.0

    while True:
        loop_start = time.time()
        try:
            # Refresh 30-day average volumes periodically
            if loop_start - last_vol_refresh > AVG_VOL_REFRESH:
                for sym in WATCHLIST:
                    v = get_30day_avg_volume(sym)
                    if v:
                        avg_volumes[sym] = v
                last_vol_refresh = loop_start
                logger.info(f"Avg volumes refreshed for {len(avg_volumes)} symbols")

            vix = get_vix()
            prices: dict[str, float] = {}
            signals: dict[str, dict] = {}
            new_events: list[dict] = []

            for symbol in WATCHLIST:
                df = fetch_bars(symbol)
                if df is None:
                    continue

                price = float(df.iloc[-1]["close"])
                prices[symbol] = price

                result = evaluate_signal(
                    symbol=symbol,
                    df=df,
                    avg_daily_volume=avg_volumes.get(symbol, 0.0),
                    vix=vix,
                    has_position=symbol in pm.portfolio.positions,
                    mock_catalyst=False,
                )

                signals[symbol] = {
                    "symbol": symbol,
                    "signal": result.signal,
                    "quality_score": result.quality_score,
                    "conditions_met": result.conditions_met,
                    "conditions": result.conditions,
                    "bonuses": result.bonuses,
                    "reason": result.reason,
                    "price": result.price,
                    "timestamp": result.timestamp,
                }

                if result.signal:
                    err = pm.open_position(symbol, price, result.reason, prices)
                    new_events.append({
                        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "type": "BUY" if err is None else "SIGNAL_SKIP",
                        "symbol": symbol,
                        "price": price,
                        "score": result.quality_score,
                        "reason": err if err else result.reason,
                    })
                    if err is None:
                        logger.info(f"BUY executed: {symbol} @ ${price:.2f}  score={result.quality_score}")
                    else:
                        logger.info(f"Signal skipped ({symbol}): {err}")

            # Trailing stops then exit checks
            pm.update_trailing_stops(prices)
            for ex in pm.check_exits(prices):
                new_events.append({
                    "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "type": "SELL",
                    "symbol": ex["symbol"],
                    "price": ex["price"],
                    "reason": ex["reason"],
                })

            with _lock:
                _state["signals"] = signals
                _state["prices"] = prices
                _state["vix"] = vix
                _state["last_update"] = dt.datetime.now(dt.timezone.utc).isoformat()
                _state["engine_status"] = "running"
                if new_events:
                    _state["trade_log"] = (new_events + _state["trade_log"])[:100]

        except Exception as exc:
            logger.error(f"Engine error: {exc}", exc_info=True)
            with _lock:
                _state["engine_status"] = f"error: {exc}"

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        prices = dict(_state.get("prices", {}))
        vix = _state.get("vix")
        last_update = _state.get("last_update")
        engine_status = _state.get("engine_status")
        signals = dict(_state.get("signals", {}))
        trade_log = list(_state.get("trade_log", []))

    positions = {}
    for sym, pos in pm.portfolio.positions.items():
        price = prices.get(sym, pos.entry_price)
        pnl = (price - pos.entry_price) * pos.shares
        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price else 0
        positions[sym] = {
            "symbol": sym,
            "shares": round(pos.shares, 4),
            "entry_price": pos.entry_price,
            "current_price": round(price, 2),
            "stop_loss": round(pos.stop_loss, 2),
            "take_profit": round(pos.take_profit, 2),
            "trailing_active": pos.trailing_active,
            "highest_price": round(pos.highest_price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "entry_time": pos.entry_time,
            "entry_reason": pos.entry_reason,
        }

    equity = pm.total_equity(prices)
    pnl_total = equity - 10_000.0
    ok, block_reason = pm.can_trade(prices)

    return jsonify({
        "portfolio": {
            "cash": round(pm.portfolio.cash, 2),
            "total_equity": round(equity, 2),
            "total_pnl": round(pnl_total, 2),
            "total_pnl_pct": round(pnl_total / 10_000.0 * 100, 2),
            "n_positions": len(positions),
            "loss_streak": pm.portfolio.loss_streak,
            "paused_until": pm.portfolio.paused_until,
            "is_paused": pm.is_paused(),
            "can_trade": ok,
            "block_reason": block_reason,
        },
        "positions": positions,
        "signals": signals,
        "vix": round(vix, 2) if vix else None,
        "last_update": last_update,
        "engine_status": engine_status,
        "trade_log": trade_log,
        "recent_trades": list(reversed(pm.portfolio.trades[-30:])),
        "stats": pm.stats(),
    })


@app.route("/api/close/<symbol>", methods=["POST"])
def api_close(symbol: str):
    sym = symbol.upper()
    with _lock:
        prices = dict(_state.get("prices", {}))
    pos = pm.portfolio.positions.get(sym)
    if not pos:
        return jsonify({"error": f"No open position for {sym}"}), 404
    price = prices.get(sym, pos.entry_price)
    pm.close_position(sym, price, "Manual close via dashboard")
    with _lock:
        _state["trade_log"] = ([{
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "type": "SELL",
            "symbol": sym,
            "price": price,
            "reason": "Manual close",
        }] + _state["trade_log"])[:100]
    return jsonify({"ok": True, "symbol": sym, "price": price})


@app.route("/api/logs")
def api_logs():
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-200:]})
    except FileNotFoundError:
        return jsonify({"lines": []})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bg = threading.Thread(target=_polling_loop, daemon=True, name="engine")
    bg.start()
    logger.info("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
