# Roostoo Trading Bot v4  —  Regime-Adaptive

## Project Overview

A fully autonomous, market-regime-adaptive crypto trading bot for the Roostoo Mock Exchange.

**Strategy:** Dual-mode regime-adaptive trading
- **MOMENTUM mode** (bullish): trend following with RSI pullback entry
- **REVERSION mode** (bearish): oversold bounce trades with quick exits

**Core features:**
- Auto-detects market regime from BTC EMA + breadth analysis
- Regime-specific parameters for position sizing, stops, and exits
- ATR-based dynamic stop-loss, take-profit, and trailing stops
- **Volatility Gate** — skips trades in low-volatility periods (ATR < 0.3% of price)
- **Minimum hold period** — enforces 5-tick minimum before any exit
- Spread reversion guard (exits on extreme bid-ask spreads)
- Kelly criterion position sizing adjusted for volatility
- 15% drawdown circuit breaker with automatic order cancellation
- Binance historical warm-up (60 × 1-min candles per pair)
- Structured JSONL trade log with full execution details

---

## Architecture

```
bot.py
├── API Client           _request()  — signed/unsigned, exponential backoff
├── Market Data          get_ticker_all(), fetch_binance_ohlcv()
├── Regime Detector      detect_regime()      — BTC EMA + breadth analysis
├── Signal Engine        compute_signals()    — EMA + ROC + RSI composite
├── Position Sizing      kelly_position_size() — ATR-adjusted Kelly
├── Risk Manager         circuit breaker, concentration cap, min-notional guard
├── Execution            place_order(), cancel_all_orders()
├── Exit Logic           ATR stops, trailing stops, spread guards
├── Performance          PerformanceTracker — Sharpe, Sortino, Calmar, Drawdown
└── Logger               _log_trade() → logs/bot_trades.jsonl + logs/bot.log
```

**Tech stack:** Python 3.10+, `requests` only (zero extra dependencies)

---

## Strategy Explanation

### Regime Detection

Every tick, the bot detects market regime from:

1. **BTC Trend** (EMA crossover on BTC/USD)
   - EMA(5) > EMA(20) → Bullish signal
   - EMA(5) ≤ EMA(20) → Bearish signal

2. **Market Breadth** (% of whitelist pairs in uptrend)
   - If ≥40% of pairs have EMA(3) > EMA(10) → Bullish
   - If <40% → Bearish

**Regime Logic:**
- **MOMENTUM mode:** BTC bullish OR breadth bullish
- **REVERSION mode:** BTC bearish AND breadth bearish

---

### Entry Logic (Regime-Dependent)

| Parameter | MOMENTUM | REVERSION |
|---|---|---|
| **RSI threshold** | RSI < 45 (pullback) | RSI < 25 (extreme oversold) |
| **Signal threshold** | 0.35+ | 0.35+ |
| **Max positions** | 3 | 2 (conservative) |
| **Pair filter** | Not already held | Not already held |

**Entry workflow:**
1. Detect regime
2. Compute EMA + ROC + RSI signals for available pairs
3. Select highest-scoring pair above threshold
4. Calculate ATR-adjusted position size via Kelly criterion
5. Place market BUY order with entry price tracking

---

### Exit Logic (ATR-Based Dynamic Levels)

For each open position, the bot continuously monitors four exit conditions:

| Exit Type | Trigger | Formula |
|---|---|---|
| **Spread Reversion** | Bid-ask spread > 0.5% | Exits immediately |
| **ATR Stop-Loss** | Price ≤ entry - SL_mult × ATR | High: 1.5× ATR, Low: 1.0× ATR |
| **ATR Take-Profit** | Price ≥ entry + TP_mult × ATR | High: 3.0× ATR, Low: 1.5× ATR |
| **Trailing Stop** | Price ≤ peak - Trail_mult × ATR | High: 0.5× ATR, Low: 0.3× ATR |

**Regime-specific parameters:**

| Parameter | MOMENTUM | REVERSION |
|---|---|---|
| SL multiple | 1.5× ATR | 1.0× ATR (tighter) |
| TP multiple | 3.0× ATR | 1.5× ATR (quicker exit) |
| Trail multiple | 0.5× ATR | 0.3× ATR (lock in faster) |

---

### Position Sizing

```
signal_strength = composite_score (EMA + ROC + RSI)
atr_volatility = ATR(7)
base_kelly = min(signal_strength / (atr_volatility²) × 0.25, 0.20)
position_usd = portfolio_value × base_kelly
position_qty = position_usd / current_price
```

Adjustments:
- Capped at **20% of portfolio** max per trade
- Minimum **$5,000 notional** per trade
- 5% USD buffer always reserved

---

### Risk Management

1. **15% drawdown circuit breaker** — halts all new trades, cancels pending orders
2. **Max 3 positions in MOMENTUM** / **2 positions in REVERSION**
3. **Spread reversion guard** — exits if bid-ask spreads spike
4. **ATR-based stops** — prevent runaway losses
5. **Trailing stops** — lock in gains as price moves favorably

---

### Volatility Gate

The bot implements a **volatility gate** to avoid trading during low-liquidity periods:

**Gate Logic:**
- **ATR Threshold:** Skips trading if ATR < 0.3% of current price
  - Rationale: In flat markets, ATR-based stops may exit too easily; avoid whipsaws
  - Market must show sufficient intrabar movement to trade
  
- **Minimum Hold Period:** Once in a trade, holds for at least **5 ticks (~40 seconds)**
  - Prevents rapid in-and-out exits due to brief volatility spikes
  - Allows positions time to develop before exiting
  
**Benefit:** Reduces false signals and chop-induced losses in sideways markets

---

### Data Warm-Up

On startup, the bot:
1. Fetches 60 × 1-min Binance OHLCV candles per whitelisted pair
2. Populates price history for all technical indicators
3. Calculates initial EMA, RSI, ATR values
4. Ensures indicators are fully primed before first live trade

This prevents indicator "warmup bias" and ensures consistent signal quality from tick 1.

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
