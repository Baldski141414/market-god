"""
Market God 5.0 — Entry Point
Starts all background threads then launches Flask/SocketIO server.
"""
import threading
import time
import os

# ── Data layer ────────────────────────────────────────────────────────────
from data.binance_ws   import start_binance
from data.yahoo_finance import start_yahoo
from data.coingecko    import start_coingecko
from data.fear_greed   import start_fear_greed
from data.reddit       import start_reddit
from data.macro        import start_macro
from data.options      import start_options
from data.congress     import start_congress
from data.whale        import start_whale
from data.insider      import start_insider

# ── Signal layer ──────────────────────────────────────────────────────────
from signals.engine    import start_signal_engine
from signals.ranker    import start_ranker

# ── Trading & Learning ────────────────────────────────────────────────────
from trading.paper_trader import start_paper_trader
from learning.adaptive    import start_adaptive_learning

# ── API ───────────────────────────────────────────────────────────────────
from api.routes import create_app


def start_all_services():
    print('=' * 60)
    print('  MARKET GOD 5.0 — STARTING ALL SERVICES')
    print('=' * 60)

    # Signal engine must be up before data flows in
    start_signal_engine()
    start_paper_trader()

    # Real-time data (Binance WebSocket — instant)
    start_binance()

    # Periodic pollers
    start_coingecko()
    start_fear_greed()
    start_reddit()
    start_macro()
    start_options()
    start_congress()
    start_whale()
    start_insider()

    # Yahoo Finance (seeds history first, then polls every 30s)
    start_yahoo()

    # Opportunity ranker
    start_ranker()

    # Adaptive learning
    start_adaptive_learning()

    print('[App] All services started')


app, socketio = create_app()

if __name__ == '__main__':
    start_all_services()
    port = int(os.environ.get('PORT', 5000))
    print(f'[App] Server starting on port {port}')
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
else:
    # Gunicorn/production: start services on first worker init
    start_all_services()
