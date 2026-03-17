"""Central configuration for Market God 5.0"""

# Binance WebSocket assets (real-time 100ms ticks)
BINANCE_SYMBOLS = ['btcusdt', 'ethusdt', 'solusdt', 'xrpusdt']
BINANCE_DISPLAY = {'btcusdt': 'BTC', 'ethusdt': 'ETH', 'solusdt': 'SOL', 'xrpusdt': 'XRP'}

# Crypto symbols — real-time Kraken ticks, trade 24/7
CRYPTO_SYMBOLS = frozenset({'BTC', 'ETH', 'SOL', 'XRP'})

# All stock tickers to track
ALL_STOCK_TICKERS = [
    'AAPL','MSFT','NVDA','AMZN','META','GOOGL','BRK-B','LLY','AVGO','JPM',
    'TSLA','WMT','V','UNH','XOM','MA','ORCL','COST','HD','PG','JNJ','ABBV',
    'BAC','NFLX','CRM','CVX','MRK','AMD','KO','PEP','CSCO','TMO','ACN','MCD',
    'ADBE','ABT','DHR','TXN','CAT','VZ','NEE','MS','INTC','IBM','GE','RTX',
    'HON','AMGN','QCOM','SPGI','BLK','AXP','T','GS','BKNG','SYK','LMT','DE',
    'ISRG','MDT','CVS','CI','GILD','ADI','PLD','MU','REGN','NOW','SCHW',
    'VRTX','ZTS','ETN','BSX','LRCX','KLAC','SNPS','UNP','ADP','SO','DUK',
    'PH','MCO','CDNS','EMR','CMG','TJX','AON','ICE','USB','PNC','TFC',
    'COF','ALLY','PYPL','ABNB','UBER','LYFT','DASH','SNAP','PINS',
    'RBLX','U','DKNG','PENN','MSTR','COIN','HOOD','SOFI','GME','AMC','PLTR',
    'NIO','RIVN','LCID','F','GM','STLA','TM','HMC','SPY','QQQ','IWM','DIA',
    'GLD','TLT','SLV','USO','XLE','XLF','XLK','XLV','ARKK','SQQQ','TQQQ',
]

# Refresh intervals (seconds)
YAHOO_REFRESH_SECS = 30
COINGECKO_REFRESH_SECS = 60
SLOW_DATA_REFRESH_SECS = 300  # Reddit, SEC, Congress, Whale, Options, Macro, Fear/Greed

# Price history buffer length (for technical indicators)
PRICE_HISTORY_LEN = 500

# Signal weights — must sum to 1.0
DEFAULT_WEIGHTS = {
    # Technical (0.50)
    'momentum_5m':      0.06,
    'momentum_1h':      0.08,
    'volume_surge':     0.09,
    'rsi':              0.06,
    'macd':             0.08,
    'bollinger':        0.05,
    'ema_cross':        0.08,
    # Auxiliary (0.26)
    'options_flow':     0.08,
    'fear_greed':       0.04,
    'reddit':           0.03,
    'whale':            0.04,
    'congress_insider': 0.04,
    'macro':            0.03,
    # Alternative data (0.24) — new
    'dark_pool':        0.05,
    'prediction_market':0.05,
    'supply_chain':     0.04,
    'earnings_nlp':     0.04,
    'shipping_bdi':     0.03,
    'geopolitical':     0.02,
    'patent':           0.01,
}

# Crypto-optimized weights (sum = 1.0)
CRYPTO_WEIGHTS = {
    # Technical (0.58)
    'momentum_5m':      0.12,
    'momentum_1h':      0.12,
    'volume_surge':     0.17,
    'rsi':              0.09,
    'macd':             0.09,
    'bollinger':        0.0,
    'ema_cross':        0.0,
    # Auxiliary (0.28)
    'options_flow':     0.12,
    'fear_greed':       0.04,
    'reddit':           0.0,
    'whale':            0.12,
    'congress_insider': 0.0,
    'macro':            0.0,
    # Alternative data for crypto (0.13)
    'dark_pool':        0.0,
    'prediction_market':0.05,
    'supply_chain':     0.0,
    'earnings_nlp':     0.0,
    'shipping_bdi':     0.0,
    'geopolitical':     0.04,
    'patent':           0.0,
    'mempool':          0.04,   # crypto-only: Bitcoin mempool activity
}

# Stock paper trading
STARTING_CASH        = 100_000.0
MAX_POSITIONS        = 20
MIN_SIGNAL_TO_BUY    = 75
STOP_LOSS_PCT        = -0.03
TAKE_PROFIT_PCT      = 0.08
TRAILING_TRIGGER_PCT = 0.04

# Crypto paper trading overrides (tighter, faster, 24/7)
CRYPTO_MIN_SIGNAL_TO_BUY = 60    # lower confidence threshold
CRYPTO_POSITION_PCT       = 0.05  # 5% of portfolio per trade (vs Kelly for stocks)
CRYPTO_STOP_LOSS_PCT      = -0.02 # -2%
CRYPTO_TAKE_PROFIT_PCT    = 0.05  # +5%
CRYPTO_TRAILING_TRIGGER   = 0.025 # trailing activates at +2.5% profit
MAX_CRYPTO_POSITIONS      = 5

# Opportunity ranker
SIGNAL_ALERT_THRESHOLD = 85
TOP_OPPORTUNITIES      = 10
RANK_REFRESH_SECS      = 30
