"""
Roostoo AI Trading Bot  —  v2 (FAQ-hardened)
=============================================
Strategy  : Momentum + RSI Mean-Reversion Hybrid
Indicators: EMA crossover (5/20), Rate-of-Change (10), RSI (14), Volatility-adjusted sizing
Risk      : 25% drawdown circuit-breaker, 20% max concentration, 5% USD buffer
Rate limit: 30 calls/minute budget -> ~12 s loop, ~3 API calls per tick
Data warm-up: pulls Binance 1-min OHLCV to pre-fill price history before trading

Run:
  export ROOSTOO_API_KEY=...
  export ROOSTOO_SECRET_KEY=...
  python3 bot.py
"""

import os, time, hmac, hashlib, logging, json, math, requests
from collections import defaultdict, deque
from datetime import datetime, timezone

# ─────────────────────────────────────────────
#  CONFIG  (override via environment variables)
# ─────────────────────────────────────────────
BASE_URL           = os.getenv("ROOSTOO_BASE_URL",   "https://mock-api.roostoo.com")
API_KEY            = os.getenv("ROOSTOO_API_KEY",    "YOUR_API_KEY_HERE")
SECRET_KEY         = os.getenv("ROOSTOO_SECRET_KEY", "YOUR_SECRET_KEY_HERE")

LOOP_INTERVAL_SEC  = 12       # ~5 ticks/min; stays well under 30 calls/min budget
MAX_OPEN_POSITIONS = 5        # max number of coins held at once
POSITION_SIZE_PCT  = 0.20     # buy exactly 20% of portfolio per position (~$10k)
MIN_TRADE_USD      = 500.0    # minimum notional — no tiny positions
MAX_DRAWDOWN_PCT   = 0.20     # circuit-breaker threshold
STOP_LOSS_PCT      = 0.02     # exit position if down 2%
TAKE_PROFIT_PCT    = 0.04     # exit position if up 4%
EMA_FAST           = 5        # fast EMA period (ticks)
EMA_SLOW           = 20       # slow EMA period (ticks)
MOMENTUM_WINDOW    = 10       # ROC lookback (ticks)
RSI_PERIOD         = 14       # RSI lookback period
RSI_BUY_MAX        = 45       # only buy when RSI < this (not overbought)
RSI_SELL_MIN       = 55       # only sell when RSI > this (not oversold)
PRICE_HISTORY_MAX  = 300      # rolling price buffer length per pair
SIGNAL_THRESHOLD   = 0.30     # minimum |score| to trigger a new buy
WARMUP_CANDLES     = 60       # Binance 1-min candles to pre-fill history
LOG_FILE           = "logs/bot_trades.jsonl"
RETRY_ATTEMPTS     = 3        # exponential backoff retries per API call

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("roostoo_bot")


# ─────────────────────────────────────────────
#  API CLIENT  (with exponential backoff)
# ─────────────────────────────────────────────
def _timestamp() -> str:
    return str(int(time.time() * 1000))


def _sign(payload: dict):
    """
    Mutates payload (adds timestamp), returns (headers, total_params_str).
    Sorts keys alphabetically, builds HMAC-SHA256 signature per Roostoo spec.
    """
    payload["timestamp"] = _timestamp()
    sorted_keys  = sorted(payload.keys())
    total_params = "&".join(f"{k}={payload[k]}" for k in sorted_keys)
    sig = hmac.new(
        SECRET_KEY.encode(),
        total_params.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "RST-API-KEY":   API_KEY,
        "MSG-SIGNATURE": sig,
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    return headers, total_params


def _request(method: str, path: str, payload: dict = None, signed: bool = False):
    """
    Central request dispatcher with exponential backoff retry.
    Roostoo rate limit: 30 calls/minute total.

    For signed GETs: append total_params directly to the URL so the
    query string the server receives exactly matches what we signed.
    For signed POSTs: send total_params as the raw request body.
    """
    url     = BASE_URL + path
    payload = payload or {}

    for attempt in range(RETRY_ATTEMPTS):
        try:
            if signed:
                # Fresh copy each attempt so timestamp is regenerated
                headers, total_params = _sign(dict(payload))
                if method == "GET":
                    # Append signed query string directly — do NOT let
                    # requests re-encode it, which would break the signature
                    r = requests.get(f"{url}?{total_params}", headers=headers, timeout=10)
                else:
                    r = requests.post(url, headers=headers, data=total_params, timeout=10)
            else:
                p = dict(payload)
                p["timestamp"] = _timestamp()
                if method == "GET":
                    r = requests.get(url, params=p, timeout=10)
                else:
                    r = requests.post(url, data=p, timeout=10)

            r.raise_for_status()
            return r.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            body   = e.response.text[:200] if e.response else ""
            log.warning(f"HTTP {status} on {method} {path} (attempt {attempt+1}): {body}")
        except requests.exceptions.RequestException as e:
            log.warning(f"Request error on {method} {path} (attempt {attempt+1}): {e}")

        if attempt < RETRY_ATTEMPTS - 1:
            wait = 2 ** attempt      # 1 s, 2 s, 4 s
            log.info(f"Retrying in {wait}s...")
            time.sleep(wait)

    log.error(f"All {RETRY_ATTEMPTS} attempts failed for {method} {path}")
    return None


# ─────────────────────────────────────────────
#  ROOSTOO API WRAPPERS
# ─────────────────────────────────────────────
def get_exchange_info() -> dict:
    data = _request("GET", "/v3/exchangeInfo")
    return data.get("TradePairs", {}) if data else {}


def get_ticker_all() -> dict:
    data = _request("GET", "/v3/ticker")
    return data.get("Data", {}) if (data and data.get("Success")) else {}


def get_balance() -> dict:
    data = _request("GET", "/v3/balance", signed=True)
    return data.get("SpotWallet", {}) if (data and data.get("Success")) else {}


def place_order(pair: str, side: str, quantity: float, price: float = None, entry_price: float = None) -> dict:
    payload = {
        "pair":     pair,
        "side":     side.upper(),
        "type":     "LIMIT" if price else "MARKET",
        "quantity": str(quantity),
    }
    if price:
        payload["price"] = str(price)
    resp = _request("POST", "/v3/place_order", payload, signed=True)
    _log_trade(pair, side, quantity, price, resp, entry_price=entry_price)
    return resp or {}


def cancel_all_orders() -> dict:
    return _request("POST", "/v3/cancel_order", {}, signed=True) or {}


# ─────────────────────────────────────────────
#  BINANCE WARM-UP  (OHLCV pre-fill)
#  Roostoo prices mirror Binance (per FAQ Q15)
# ─────────────────────────────────────────────
BINANCE_SYMBOL_MAP = {
    "BTC/USD":  "BTCUSDT",
    "ETH/USD":  "ETHUSDT",
    "BNB/USD":  "BNBUSDT",
    "SOL/USD":  "SOLUSDT",
    "XRP/USD":  "XRPUSDT",
    "ADA/USD":  "ADAUSDT",
    "DOGE/USD": "DOGEUSDT",
    "AVAX/USD": "AVAXUSDT",
    "LINK/USD": "LINKUSDT",
    "DOT/USD":  "DOTUSDT",
}

def fetch_binance_closes(pair: str, limit: int = WARMUP_CANDLES) -> list:
    """Fetch 1-min close prices from Binance public endpoint (no auth needed)."""
    sym = BINANCE_SYMBOL_MAP.get(pair)
    if not sym:
        return []
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": sym, "interval": "1m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return [float(c[4]) for c in r.json()]   # index 4 = close price
    except Exception as e:
        log.warning(f"Binance warm-up failed for {pair}: {e}")
        return []


# ─────────────────────────────────────────────
#  TRADE LOGGER  (structured JSONL per FAQ Q38)
# ─────────────────────────────────────────────
def _log_trade(pair: str, side: str, qty: float, price, resp: dict, entry_price: float = None):
    detail      = (resp or {}).get("OrderDetail", {})
    filled_price = float(detail.get("FilledAverPrice") or 0)
    commission   = float(detail.get("CommissionChargeValue") or 0)
    filled_qty   = float(detail.get("FilledQuantity") or qty)

    # Real P&L = (sell_price - avg_buy_price) * qty - commission
    # Only meaningful for SELL orders where we know the entry price
    if side.upper() == "SELL" and entry_price and entry_price > 0 and filled_price > 0:
        realised_pnl = (filled_price - entry_price) * filled_qty - commission
    else:
        realised_pnl = None

    entry = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "pair":         pair,
        "side":         side,
        "quantity":     qty,
        "price":        price,
        "order_id":     detail.get("OrderID"),
        "status":       detail.get("Status"),
        "filled_qty":   filled_qty,
        "filled_price": filled_price,
        "entry_price":  entry_price,        # avg buy price (for sells)
        "realised_pnl": realised_pnl,       # true profit/loss in USD
        "commission":   commission,
        "api_success":  (resp or {}).get("Success", False),
        "err_msg":      (resp or {}).get("ErrMsg", ""),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    pnl_str = f"  P&L=${realised_pnl:+.2f}" if realised_pnl is not None else ""
    log.info(
        f"TRADE {side:4s} {qty} {pair} @ {'MARKET' if not price else price} "
        f"-> {detail.get('Status', 'ERROR')}  (ID={detail.get('OrderID')}){pnl_str}"
    )


# ─────────────────────────────────────────────
#  TECHNICAL INDICATORS
# ─────────────────────────────────────────────
def ema(prices: list, period: int) -> float:
    if not prices:
        return 0.0
    if len(prices) < period:
        return sum(prices) / len(prices)
    k, val = 2.0 / (period + 1), prices[0]
    for p in prices[1:]:
        val = p * k + val * (1 - k)
    return val


def rate_of_change(prices: list, window: int) -> float:
    if len(prices) < window + 1:
        return 0.0
    past = prices[-1 - window]
    return (prices[-1] - past) / (past + 1e-9)


def volatility(prices: list, window: int = 20) -> float:
    tail = prices[-window:] if len(prices) >= window else prices
    if len(tail) < 2:
        return 1.0
    log_rets = [math.log(tail[i] / tail[i-1]) for i in range(1, len(tail)) if tail[i-1] > 0]
    if not log_rets:
        return 1.0
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean)**2 for r in log_rets) / max(len(log_rets), 1)
    return math.sqrt(var) + 1e-9


def rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0  # neutral — not enough data
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────
#  PERFORMANCE TRACKER
# ─────────────────────────────────────────────
class PerformanceTracker:
    def __init__(self, initial_value: float):
        self.initial = initial_value
        self.peak    = initial_value
        self.curve   = [initial_value]
        self.returns = []

    def update(self, val: float):
        if self.curve:
            prev = self.curve[-1]
            if prev > 0:
                self.returns.append((val - prev) / prev)
        self.curve.append(val)
        self.peak = max(self.peak, val)

    @property
    def total_return(self):
        return (self.curve[-1] / self.initial - 1) if self.initial else 0

    @property
    def drawdown(self):
        return (self.peak - self.curve[-1]) / self.peak if self.peak else 0

    def _mean_std(self, data):
        if len(data) < 2:
            return 0.0, 0.0
        m = sum(data) / len(data)
        s = math.sqrt(sum((x - m)**2 for x in data) / len(data))
        return m, s

    @property
    def sharpe(self):
        m, s = self._mean_std(self.returns)
        return (m / s) * math.sqrt(len(self.returns)) if s > 0 else 0.0

    @property
    def sortino(self):
        if not self.returns:
            return 0.0
        m   = sum(self.returns) / len(self.returns)
        neg = [r for r in self.returns if r < 0]
        if not neg:
            return float("inf")
        ds = math.sqrt(sum(r**2 for r in neg) / len(neg))
        return (m / ds) * math.sqrt(len(self.returns)) if ds > 0 else 0.0

    @property
    def calmar(self):
        dd = self.drawdown
        return self.total_return / dd if dd > 1e-9 else float("inf")

    def report(self):
        log.info(
            f"[PERF] PortVal=${self.curve[-1]:,.2f}  "
            f"Return={self.total_return:+.2%}  "
            f"DD={self.drawdown:.2%}  "
            f"Sharpe={self.sharpe:.3f}  "
            f"Sortino={self.sortino:.3f}  "
            f"Calmar={self.calmar:.3f}"
        )


# ─────────────────────────────────────────────
#  PORTFOLIO VALUE
# ─────────────────────────────────────────────
def portfolio_value(wallet: dict, tickers: dict) -> float:
    total = wallet.get("USD", {}).get("Free", 0) + wallet.get("USD", {}).get("Lock", 0)
    for coin, bal in wallet.items():
        if coin == "USD":
            continue
        price  = tickers.get(f"{coin}/USD", {}).get("LastPrice", 0)
        total += (bal.get("Free", 0) + bal.get("Lock", 0)) * price
    return total


# ─────────────────────────────────────────────
#  SIGNAL ENGINE
# ─────────────────────────────────────────────
def compute_signals(price_history: dict, pairs: list) -> dict:
    """
    Returns {pair: score}.  score > 0 = bullish, < 0 = bearish.

    Entry logic:
      BUY  : EMA fast > EMA slow  AND  RSI < RSI_BUY_MAX  (uptrend + not overbought)
      SELL : EMA fast < EMA slow  AND  RSI > RSI_SELL_MIN (downtrend + not oversold)

    Score = 0.6 * EMA_signal + 0.4 * ROC_norm
    RSI acts as a gate — if RSI condition fails, score is forced to 0 (no trade).
    """
    signals = {}
    for pair in pairs:
        prices = list(price_history[pair])
        if len(prices) < EMA_SLOW + 1:
            signals[pair] = 0.0
            continue

        ema_fast   = ema(prices, EMA_FAST)
        ema_slow   = ema(prices, EMA_SLOW)
        ema_signal = 1.0 if ema_fast > ema_slow else -1.0
        roc        = rate_of_change(prices, MOMENTUM_WINDOW)
        roc_norm   = math.copysign(min(abs(roc) * 100, 1.0), roc)
        raw_score  = 0.6 * ema_signal + 0.4 * roc_norm

        # RSI gate — only allow trade if RSI confirms timing
        rsi_val = rsi(prices, RSI_PERIOD)
        if raw_score > 0 and rsi_val >= RSI_BUY_MAX:
            # Bullish signal but RSI too high — overbought, skip
            signals[pair] = 0.0
        elif raw_score < 0 and rsi_val <= RSI_SELL_MIN:
            # Bearish signal but RSI too low — oversold, skip
            signals[pair] = 0.0
        else:
            signals[pair] = raw_score

        log.debug(f"{pair}: EMA={ema_signal:+.0f}  ROC={roc:.4f}  RSI={rsi_val:.1f}  score={signals[pair]:+.3f}")

    return signals


# ─────────────────────────────────────────────
#  POSITION SIZING  (fixed % of portfolio)
# ─────────────────────────────────────────────
def position_usd(port_val: float) -> float:
    return port_val * POSITION_SIZE_PCT


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Roostoo Trading Bot v2  —  Starting")
    log.info("=" * 60)

    # ── Bootstrap ──────────────────────────────────────────────
    exchange_info   = get_exchange_info()
    tradeable_pairs = [p for p, info in exchange_info.items() if info.get("CanTrade")]
    log.info(f"Tradeable pairs: {tradeable_pairs}")

    price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=PRICE_HISTORY_MAX))

    # Warm up from Binance historical candles (same prices as Roostoo per FAQ)
    log.info(f"Warming up from Binance ({WARMUP_CANDLES} x 1-min candles)...")
    for pair in tradeable_pairs:
        closes = fetch_binance_closes(pair, limit=WARMUP_CANDLES)
        if closes:
            price_history[pair].extend(closes)
            log.info(f"  {pair}: {len(closes)} candles loaded  last=${closes[-1]:.4f}")
        else:
            log.warning(f"  {pair}: no warm-up data — will build from live ticks")

    init_wallet  = get_balance()
    init_tickers = get_ticker_all()
    init_val     = portfolio_value(init_wallet, init_tickers)
    log.info(f"Initial portfolio value: ${init_val:,.2f}")

    tracker = PerformanceTracker(init_val)
    tick    = 0

    # ── Entry price persistence ─────────────────────────────────
    ENTRY_FILE = "logs/entry_prices.json"

    def load_entry_prices() -> dict:
        if os.path.exists(ENTRY_FILE):
            try:
                with open(ENTRY_FILE) as f:
                    data = json.load(f)
                log.info(f"Loaded entry prices from file: {data}")
                return data
            except Exception:
                pass
        return {}

    def save_entry_prices(ep: dict):
        with open(ENTRY_FILE, "w") as f:
            json.dump(ep, f)

    # ── Main loop ──────────────────────────────────────────────
    entry_prices = load_entry_prices()  # persists across restarts

    while True:
        tick_start = time.time()
        try:
            tick += 1
            log.info(f"\n{'─'*50}\nTick {tick}")

            # 1. Tickers  (1 call)
            tickers = get_ticker_all()
            if not tickers:
                log.warning("No ticker data — skipping tick.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue
            for pair in tradeable_pairs:
                if pair in tickers:
                    price_history[pair].append(tickers[pair]["LastPrice"])

            # 2. Balance  (1 call)
            wallet = get_balance()
            if not wallet:
                log.warning("Could not fetch balance — skipping tick.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            usd_free = wallet.get("USD", {}).get("Free", 0)
            port_val = portfolio_value(wallet, tickers)
            tracker.update(port_val)
            tracker.report()

            # 3. Circuit breaker
            if tracker.drawdown > MAX_DRAWDOWN_PCT:
                log.warning(
                    f"CIRCUIT BREAKER: drawdown {tracker.drawdown:.2%} > {MAX_DRAWDOWN_PCT:.2%}. "
                    "Cancelling all orders. No new trades."
                )
                cancel_all_orders()
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # 4. Count open positions
            open_positions = {
                coin: bal.get("Free", 0)
                for coin, bal in wallet.items()
                if coin != "USD" and bal.get("Free", 0) > 0
            }
            log.info(f"Open positions ({len(open_positions)}/{MAX_OPEN_POSITIONS}): "
                     f"{list(open_positions.keys()) or 'none'}")

            # 5. Check stop-loss / take-profit on existing positions
            for coin, qty in list(open_positions.items()):
                pair      = f"{coin}/USD"
                cur_price = tickers.get(pair, {}).get("LastPrice", 0)
                entry     = entry_prices.get(coin, cur_price)
                if cur_price <= 0 or entry <= 0:
                    continue
                pnl_pct = (cur_price - entry) / entry

                if pnl_pct <= -STOP_LOSS_PCT:
                    log.info(f"STOP-LOSS hit on {coin}: {pnl_pct:.2%} (entry=${entry:.4f} now=${cur_price:.4f})")
                    amt_prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
                    sell_qty = round(qty, amt_prec)
                    resp = place_order(pair, "SELL", sell_qty, entry_price=entry)
                    if resp.get("Success"):
                        entry_prices.pop(coin, None)
                        save_entry_prices(entry_prices)
                        log.info(f"Stop-loss SELL filled: {sell_qty} {coin}")
                    continue

                if pnl_pct >= TAKE_PROFIT_PCT:
                    log.info(f"TAKE-PROFIT hit on {coin}: {pnl_pct:.2%} (entry=${entry:.4f} now=${cur_price:.4f})")
                    amt_prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
                    sell_qty = round(qty, amt_prec)
                    resp = place_order(pair, "SELL", sell_qty, entry_price=entry)
                    if resp.get("Success"):
                        entry_prices.pop(coin, None)
                        save_entry_prices(entry_prices)
                        log.info(f"Take-profit SELL filled: {sell_qty} {coin}")
                    continue

                log.info(f"  {coin}: entry=${entry:.4f}  now=${cur_price:.4f}  pnl={pnl_pct:+.2%}")

            # 6. Signals — only look for new buys if under position limit
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                log.info(f"At max positions ({MAX_OPEN_POSITIONS}). No new buys.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            if usd_free < MIN_TRADE_USD:
                log.info(f"Insufficient USD (${usd_free:.2f}). No new buys.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            signals = compute_signals(price_history, tradeable_pairs)

            # Only consider BUY signals for pairs we don't already hold
            buy_signals = {
                pair: score for pair, score in signals.items()
                if score > SIGNAL_THRESHOLD
                and pair.split("/")[0] not in open_positions
            }

            if not buy_signals:
                log.info("No qualifying buy signals. Holding.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # Pick strongest buy signal
            best_pair  = max(buy_signals, key=buy_signals.get)
            best_score = buy_signals[best_pair]
            coin       = best_pair.split("/")[0]
            cur_price  = tickers.get(best_pair, {}).get("LastPrice", 0)

            if cur_price <= 0:
                log.warning(f"Invalid price for {best_pair} — skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # 7. Fixed position sizing — 20% of portfolio
            trade_usd = min(position_usd(port_val), usd_free * 0.95)
            quantity  = trade_usd / cur_price

            if quantity * cur_price < MIN_TRADE_USD:
                log.info(f"Order too small (${quantity*cur_price:.2f}). Skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            amt_prec = exchange_info.get(best_pair, {}).get("AmountPrecision", 6)
            quantity = round(quantity, amt_prec)

            log.info(
                f"-> BUY {quantity} {best_pair} @ MARKET  "
                f"signal={best_score:+.3f}  notional=${quantity*cur_price:.2f}  "
                f"SL=${cur_price*(1-STOP_LOSS_PCT):.4f}  TP=${cur_price*(1+TAKE_PROFIT_PCT):.4f}"
            )

            # 8. Execute  (1 call)
            resp = place_order(best_pair, "BUY", quantity)
            if resp.get("Success"):
                d = resp.get("OrderDetail", {})
                filled_price = d.get("FilledAverPrice") or cur_price
                entry_prices[coin] = float(filled_price)
                save_entry_prices(entry_prices)
                log.info(
                    f"BUY OK — ID={d.get('OrderID')}  "
                    f"FilledQty={d.get('FilledQuantity')}  "
                    f"AvgPx={filled_price}  "
                    f"Commission={d.get('CommissionChargeValue')}"
                )
            else:
                log.warning(f"BUY FAILED: {resp.get('ErrMsg', 'unknown')}")

        except KeyboardInterrupt:
            log.info("Bot interrupted by user.")
            break
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        elapsed = time.time() - tick_start
        time.sleep(max(0, LOOP_INTERVAL_SEC - elapsed))

    log.info("Final performance summary:")
    tracker.report()
    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
