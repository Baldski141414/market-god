"""
Market God 6.0 — Entry Point
Starts all background threads then launches Flask/SocketIO server.
"""
import threading
import time
import os

# ── Data layer ────────────────────────────────────────────────────────────
from data.binance_ws    import start_binance
from data.yahoo_finance import start_yahoo
from data.coingecko     import start_coingecko
from data.fear_greed    import start_fear_greed
from data.reddit        import start_reddit
from data.macro         import start_macro
from data.options       import start_options
from data.congress      import start_congress
from data.whale         import start_whale
from data.insider       import start_insider

# ── Alternative data layer (new) ─────────────────────────────────────────
from data.gdelt               import start_gdelt
from data.patents             import start_patents
from data.shipping            import start_shipping
from data.mempool             import start_mempool
from data.prediction_markets  import start_prediction_markets
from data.dark_pool           import start_dark_pool
from data.sec_supply_chain    import start_sec_supply_chain
from data.earnings_nlp        import start_earnings_nlp
from data.contagion           import start_contagion

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
    print('  MARKET GOD 6.0 — STARTING ALL SERVICES')
    print('=' * 60)

    # Signal engine must be up before data flows in
    start_signal_engine()
    start_paper_trader()

    # Real-time data (Kraken WebSocket — instant)
    start_binance()

    # Periodic pollers (core)
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

    # Alternative data layer — all run in background, cache-heavy, zero cost
    start_gdelt()               # GDELT geopolitical risk (10 min)
    start_mempool()             # Bitcoin mempool (60 sec)
    start_prediction_markets()  # Polymarket odds (5 min)
    start_shipping()            # BDI via FRED (1 hour)
    start_dark_pool()           # FINRA dark pool (4 hours)
    start_patents()             # USPTO patents (6 hours)
    start_earnings_nlp()        # SEC 8-K NLP (4 hours)
    start_sec_supply_chain()    # SEC supply chain (2 hours)
    start_contagion()           # Contagion engine (60 sec, uses cached macro)

    # Opportunity ranker
    start_ranker()

    # Adaptive learning
    start_adaptive_learning()

    print('[App] All services started — Market God 6.0')


app, socketio = create_app()

if __name__ == '__main__':
    start_all_services()
    port = int(os.environ.get('PORT', 5000))
    print(f'[App] Server starting on port {port}')
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
else:
    # Gunicorn/production: start services on first worker init
    start_all_services()
