# Market God — Clean Rebuild

Disciplined paper-trading bot. Quality over activity.

## Stack
- **Python 3.10+** / Flask
- **yfinance** — free market data (no API key required)
- **pandas_ta** — technical indicators
- **Tailwind CSS CDN** — dashboard

## Project Structure

```
config.py        — all constants (thresholds, risk params, circuit breakers)
data_fetcher.py  — yfinance OHLCV + VIX + 30-day avg volume
signal_engine.py — 6-condition evaluator + quality score
managers.py      — portfolio state, position sizing, risk controls
app.py           — Flask server + 2s polling engine
templates/index.html — dark dashboard
backtester.py    — historical validation
paper_trading.json   — persisted portfolio state (auto-created)
market_god.log       — full trade/signal log (auto-created)
```

## Signal Logic

6 conditions evaluated per symbol every 2 seconds:

| # | Condition | Notes |
|---|-----------|-------|
| 1 | **Momentum** | close > prev_close AND EMA(5) > EMA(20) |
| 2 | **Volume** | current bar > 1.5× 30-day daily avg / 78 |
| 3 | **RSI < 70** | RSI(14) not overbought |
| 4 | **Catalyst** | Mock (TODO: Unusual Whales / FMP) |
| 5 | **Regime** | VIX < 25 |
| 6 | **No position** | symbol not already in portfolio |

**Quality Score** = (conditions_met × 10) + bonuses

Bonuses: catalyst +2, strong volume (>2×) +1, strong EMA spread (>0.3%) +1

**Trade gate**: conditions_met ≥ 3 **AND** quality_score ≥ 35 (effectively needs 4 conditions)

## Risk Rules

- Max 5 simultaneous positions
- ≤15% of equity per position
- Risk 2% of portfolio per trade at 3% stop loss → position sized automatically
- Always keep ≥25% cash
- Hard stop loss: −3% | Take profit: +12%
- Trailing stop: activates at +6%, trails 5% below peak
- **Circuit breakers**: 3-loss streak → 30min pause | daily −5% → halt for day

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Start (paper trading, $10k fresh start)
python app.py
# Dashboard → http://localhost:5000
```

## Backtest

```bash
# Default: 7 symbols, last 59 days of 5m data
python backtester.py

# Custom
python backtester.py --symbols AAPL NVDA TSLA --days 45 --csv results.csv
```

Report includes: win rate, profit factor, total return, max drawdown, Sharpe, avg hold time.

## TODO (Real Catalyst Integration)

Replace `mock_catalyst=False` in `app.py` / `signal_engine.py` with:
- **Unusual Whales** — unusual options flow
- **FMP** — insider / congress trade disclosures
- **Benzinga** — real-time news catalyst
