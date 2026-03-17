# Roostoo Trading Bot

## Project Overview

A fully autonomous crypto trading bot for the Roostoo Mock Exchange hackathon.

**Strategy:** Multi-Signal Momentum + Mean-Reversion Hybrid
**Key features:**
- EMA crossover trend filter blended with Rate-of-Change momentum
- Fractional Kelly position sizing, volatility-adjusted per asset
- Drawdown circuit breaker, concentration cap, minimum order guard
- Binance historical OHLCV warm-up so indicators are ready from tick 1
- Structured JSONL trade log (every field the judges expect)
- Exponential backoff retry on every API call

---

## Architecture

```
bot.py
├── API Client         _request()  — signed/unsigned, exponential backoff
├── Market Data        get_ticker_all(), fetch_binance_closes()
├── Signal Engine      compute_signals()  — EMA crossover + ROC momentum
├── Position Sizing    kelly_usd()        — fractional Kelly, vol-adjusted
├── Risk Manager       circuit breaker, concentration cap, min-notional guard
├── Execution          place_order(), cancel_all_orders()
├── Performance        PerformanceTracker — Sharpe, Sortino, Calmar, DD
└── Logger             _log_trade()  → logs/bot_trades.jsonl + logs/bot.log
```

**Tech stack:** Python 3.10+, `requests` only (zero extra dependencies)

---

## Strategy Explanation

### Entry conditions
| Condition | Threshold |
|---|---|
| EMA fast (5 ticks) > EMA slow (20 ticks) | Bullish |
| Composite score = `0.6 × EMA_signal + 0.4 × ROC_norm` | |
| `abs(score) > 0.30` | Trade fires |

### Exit conditions
- Opposite signal fires (score flips sign + exceeds threshold)
- Drawdown circuit breaker halts all trading

### Position sizing
```
f* = abs(score) / vol²       # raw Kelly fraction
f  = min(f* × 0.25, 0.20)   # capped at 20% of portfolio
notional = portfolio_value × f
```

### Risk management
1. **25% drawdown circuit breaker** — cancels all pending orders, skips new trades
2. **20% max concentration** per coin
3. **5% USD buffer** always kept in reserve
4. **$15 minimum notional** — prevents fee-burning micro-trades
5. **Market orders only** — guaranteed fills, no stale limit order buildup

### Data warm-up
On startup, the bot fetches 60 × 1-min Binance candles per pair (Roostoo prices mirror Binance per FAQ). This ensures all EMA and ROC indicators are fully primed before the first live trade.

---

## Setup & Deployment (AWS EC2)

### 1. Launch EC2
- AMI: **Ubuntu 22.04 LTS**
- Instance type: `t3.micro`
- Region: **ap-southeast-2 (Sydney)** — required by competition
- Security group: outbound HTTPS (443) open

### 2. Install
```bash
sudo apt update && sudo apt install -y python3-pip screen git
git clone https://github.com/YOUR_TEAM/roostoo-bot.git
cd roostoo-bot
pip3 install -r requirements.txt
```

### 3. Configure credentials (never hardcode)
```bash
export ROOSTOO_API_KEY="your_competition_api_key"
export ROOSTOO_SECRET_KEY="your_competition_secret_key"
```
Or put them in a `.env` file (add `.env` to `.gitignore`):
```
ROOSTOO_API_KEY=xxx
ROOSTOO_SECRET_KEY=yyy
```
Then load with: `export $(cat .env | xargs)`

### 4. Run in background
```bash
screen -S bot
python3 bot.py
# Detach: Ctrl+A then D
# Reattach: screen -r bot
```

### 5. Monitor
```bash
tail -f logs/bot.log              # live operational log
cat logs/bot_trades.jsonl         # full trade history (JSON Lines)
```

---

## File Structure
```
roostoo-bot/
├── bot.py                  # single-file bot (all logic)
├── requirements.txt
├── README.md
├── .env.example            # template (never commit real keys)
├── .gitignore
└── logs/
    ├── bot.log             # human-readable operational log
    └── bot_trades.jsonl    # structured trade log (one JSON object per line)
```

---

## Rate Limit Budget

Roostoo allows **30 calls/minute** total.

| Call | Per tick | Per minute (12 s loop) |
|---|---|---|
| `GET /v3/ticker` | 1 | 5 |
| `GET /v3/balance` | 1 | 5 |
| `POST /v3/place_order` | 0–1 | 0–5 |
| **Total** | **2–3** | **10–15** |

Leaves comfortable headroom for retries and ad-hoc queries.

---

## Trade Log Fields (logs/bot_trades.jsonl)

Each line is a JSON object:
```json
{
  "timestamp":    "2025-03-21T20:05:01.123Z",
  "pair":         "BTC/USD",
  "side":         "BUY",
  "quantity":     0.002,
  "price":        null,
  "order_id":     142,
  "status":       "FILLED",
  "filled_qty":   0.002,
  "filled_price": 84312.5,
  "unit_change":  168.625,
  "commission":   0.020235,
  "api_success":  true,
  "err_msg":      ""
}
```

---

## Tuning Parameters

| Parameter | Default | Effect |
|---|---|---|
| `EMA_FAST` | 5 | Faster = more responsive |
| `EMA_SLOW` | 20 | Slower = stronger trend filter |
| `MOMENTUM_WINDOW` | 10 | ROC lookback ticks |
| `KELLY_FRACTION` | 0.25 | Trade size aggressiveness |
| `MAX_POSITION_PCT` | 0.20 | Concentration cap |
| `MAX_DRAWDOWN_PCT` | 0.25 | Circuit-breaker |
| `SIGNAL_THRESHOLD` | 0.30 | Minimum signal to trade |
| `LOOP_INTERVAL_SEC` | 12 | Seconds between ticks |

---

## Competition Timeline

| Event | Date |
|---|---|
| Round 1 starts | Mar 21, 8:00 PM |
| First trade deadline | Mar 22, 8:00 PM |
| Round 2 starts | Apr 4, 8:00 PM |
