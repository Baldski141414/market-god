"""
Flask routes + SocketIO event handlers.
Real-time push via WebSocket, REST fallback for all data.
"""
import time
import json
import datetime
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from core.data_store import store
from core.event_bus import bus, EVT_SIGNAL_READY, EVT_ALERT, EVT_TRADE, EVT_RANK_UPDATE
from signals.engine import calculate_signal

socketio: SocketIO = None


def create_app():
    global socketio
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    CORS(app)
    socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading',
                        logger=False, engineio_logger=False)

    # ── REST endpoints ──────────────────────────────────────────────────────
    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/api/data')
    def api_data():
        return jsonify(store.snapshot())

    @app.route('/api/portfolio')
    def api_portfolio():
        p = store.get_portfolio()
        prices = store.latest_prices
        positions = []
        for sym, pos in p['positions'].items():
            latest = prices.get(sym, {})
            current = latest.get('price', pos['avg_price'])
            cost = pos['cost_basis']
            value = pos['shares'] * current
            positions.append({
                'symbol': sym,
                'shares': round(pos['shares'], 4),
                'avg_price': round(pos['avg_price'], 4),
                'current_price': round(current, 4),
                'value': round(value, 2),
                'pnl': round(value - cost, 2),
                'pnl_pct': round((value - cost) / cost * 100, 2) if cost else 0,
                'buy_time': pos.get('buy_time'),
            })

        trades = p.get('trades', [])
        total_value = p['cash'] + sum(pos['value'] for pos in positions)
        pnl = total_value - 100_000
        closed = [t for t in trades if t.get('pnl') is not None]
        wins = sum(1 for t in closed if t['pnl'] > 0)
        win_rate = wins / len(closed) * 100 if closed else 0

        # SPY comparison
        spy_return = 0
        if p.get('spy_basis'):
            spy_latest = store.get_latest('SPY')
            if spy_latest:
                spy_current = spy_latest.get('price', p['spy_basis'])
                spy_return = (spy_current - p['spy_basis']) / p['spy_basis'] * 100

        return jsonify({
            'cash': round(p['cash'], 2),
            'equity': round(sum(pos['value'] for pos in positions), 2),
            'total': round(total_value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / 100_000 * 100, 2),
            'win_rate': round(win_rate, 1),
            'positions': positions,
            'trades': list(reversed(trades[-50:])),
            'spy_return': round(spy_return, 2),
            'num_positions': len(positions),
            'num_trades': len(trades),
        })

    @app.route('/api/paper/reset', methods=['POST'])
    def api_reset():
        store.reset_portfolio()
        return jsonify({'ok': True})

    @app.route('/api/signal/<symbol>')
    def api_signal(symbol):
        symbol = symbol.upper()
        cached = store.get_signal(symbol)
        # Recompute if stale (>5s)
        if not cached or (time.time() - cached.get('ts', 0)) > 5:
            result = calculate_signal(symbol)
            store.set_signal(symbol, result)
            return jsonify(result)
        return jsonify(cached)

    @app.route('/api/opportunities')
    def api_opportunities():
        return jsonify(store.opportunities)

    @app.route('/api/chart/<symbol>')
    def api_chart(symbol):
        symbol = symbol.upper()
        history = store.get_prices(symbol)  # list of (ts, price, volume)

        now = time.time()
        cutoff = now - 10 * 3600  # last 10 hours

        recent = [(ts, price) for ts, price, _vol in history if ts >= cutoff]
        if not recent:
            recent = [(ts, price) for ts, price, _vol in history]
        if not recent:
            return jsonify({'labels': [], 'prices': [], 'symbol': symbol})

        # Resample into 5-minute bins; keep latest price per bucket
        bins: dict[int, tuple[float, float]] = {}
        for ts, price in recent:
            bucket = int(ts // 300) * 300
            if bucket not in bins or ts > bins[bucket][0]:
                bins[bucket] = (ts, price)

        sorted_bins = sorted(bins.items())
        labels = []
        prices = []
        for bucket_ts, (_ts, price) in sorted_bins:
            dt = datetime.datetime.utcfromtimestamp(bucket_ts)
            labels.append(dt.strftime('%H:%M'))
            prices.append(round(price, 6))

        return jsonify({'labels': labels, 'prices': prices, 'symbol': symbol})

    @app.route('/api/orderbook/<symbol>')
    def api_orderbook(symbol):
        symbol = symbol.upper()
        ob = store.order_book.get(symbol, {'bids': [], 'asks': []})
        return jsonify(ob)

    @app.route('/api/altdata')
    def api_altdata():
        """Return full alternative data snapshot."""
        with store._lock:
            return jsonify({
                'gdelt':              dict(store.altdata.get('gdelt') or {}),
                'patents':            dict(store.altdata.get('patents') or {}),
                'shipping':           dict(store.altdata.get('shipping') or {}),
                'mempool':            dict(store.altdata.get('mempool') or {}),
                'prediction_markets': dict(store.altdata.get('prediction_markets') or {}),
                'dark_pool':          dict(store.altdata.get('dark_pool') or {}),
                'supply_chain':       dict(store.altdata.get('supply_chain') or {}),
                'earnings_nlp':       dict(store.altdata.get('earnings_nlp') or {}),
                'contagion':          dict(store.altdata.get('contagion') or {}),
                'alerts':             list(store.altdata.get('alerts') or []),
                'ts':                 time.time(),
            })

    # ── WebSocket bridge — publish store events to all clients ─────────────
    def _push_signal(data):
        try:
            socketio.emit('signal', data)
        except Exception:
            pass

    def _push_alert(data):
        try:
            socketio.emit('alert', data)
        except Exception:
            pass

    def _push_trade(data):
        try:
            socketio.emit('trade', data)
        except Exception:
            pass

    def _push_ranks(data):
        try:
            socketio.emit('opportunities', data)
        except Exception:
            pass

    bus.subscribe(EVT_SIGNAL_READY, _push_signal)
    bus.subscribe(EVT_ALERT, _push_alert)
    bus.subscribe(EVT_TRADE, _push_trade)
    bus.subscribe(EVT_RANK_UPDATE, _push_ranks)

    # ── SocketIO events ─────────────────────────────────────────────────────
    @socketio.on('connect')
    def on_connect():
        # Send full snapshot to newly connected client
        emit('snapshot', store.snapshot())
        emit('portfolio', json.loads(json.dumps({'ok': True})))

    @socketio.on('subscribe_orderbook')
    def on_sub_ob(data):
        symbol = (data.get('symbol') or 'BTC').upper()
        ob = store.order_book.get(symbol, {'bids': [], 'asks': []})
        emit('orderbook', {'symbol': symbol, **ob})

    return app, socketio
