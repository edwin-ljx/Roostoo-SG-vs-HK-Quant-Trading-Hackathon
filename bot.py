"""
Roostoo Trading Bot  —  v4 (Regime-Adaptive)
=============================================
Strategy  : Dual-mode regime-adaptive trading
Regime    : AUTO-DETECTED each tick from BTC trend + market breadth
  MOMENTUM mode  (bullish market) — trend following, RSI pullback entry
  REVERSION mode (bearish market) — oversold bounces, quick exits
Indicators: EMA crossover (3/10), ROC (5), RSI (14), ATR (7)
Sizing    : Kelly criterion adjusted for ATR volatility
Exit logic: ATR stop-loss, ATR take-profit, trailing stop, spread guard
Risk      : Max 3 positions, 15% drawdown circuit-breaker

Run:
  export ROOSTOO_API_KEY=...
  export ROOSTOO_SECRET_KEY=...
  python3 bot.py
"""

import os, time, hmac, hashlib, logging, json, math, requests
from collections import defaultdict, deque
from datetime import datetime, timezone

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BASE_URL           = os.getenv("ROOSTOO_BASE_URL",   "https://mock-api.roostoo.com")
API_KEY            = os.getenv("ROOSTOO_API_KEY",    "YOUR_API_KEY_HERE")
SECRET_KEY         = os.getenv("ROOSTOO_SECRET_KEY", "YOUR_SECRET_KEY_HERE")

LOOP_INTERVAL_SEC  = 8
MAX_OPEN_POSITIONS = 3
MIN_TRADE_USD      = 5000.0
MAX_DRAWDOWN_PCT   = 0.15
MIN_PRICE          = 0.01

# Shared indicator settings
EMA_FAST           = 3
EMA_SLOW           = 10
MOMENTUM_WINDOW    = 5
RSI_PERIOD         = 14
ATR_PERIOD         = 7
SPREAD_REVERT_PCT  = 0.005

# ── VOLATILITY GATE ───────────────────────────
MIN_ATR_PCT        = 0.001    # lowered from 0.003 — 0.1% minimum movement
MIN_HOLD_TICKS     = 3        # lowered from 5 — 24s minimum hold

# ── MOMENTUM MODE (bullish market) ────────────
MOM_SIGNAL_THRESHOLD = 0.35   # lowered from 0.55 — easier to trigger
MOM_RSI_BUY_MAX      = 55     # raised from 40 — more entries allowed
MOM_ATR_SL_MULT      = 2.0    # stop-loss  = entry - 2.0 × ATR
MOM_ATR_TP_MULT      = 4.0    # take-profit = entry + 4.0 × ATR
MOM_TRAIL_MULT       = 1.0    # trailing stop = peak - 1.0 × ATR

# ── REVERSION MODE (bearish market) ───────────
REV_RSI_STRONG       = 25     # raised from 20 — more opportunities
REV_RSI_MEDIUM       = 35     # raised from 30
REV_RSI_WEAK         = 45     # raised from 38 — catches more bounces
REV_ATR_SL_MULT      = 1.0    # stop-loss
REV_ATR_TP_MULT      = 1.5    # take-profit
REV_TRAIL_MULT       = 0.5    # trailing stop
REV_MAX_POSITIONS    = 2      # conservative in bear market

# ── REGIME DETECTOR ───────────────────────────
REGIME_EMA_FAST      = 5
REGIME_EMA_SLOW      = 20
REGIME_BREADTH_MIN   = 0.30   # lowered from 0.40 — 30% of pairs in uptrend = MOMENTUM

# Kelly position sizing
KELLY_FRACTION     = 0.25
MAX_POSITION_PCT   = 0.20
MIN_POSITION_PCT   = 0.05

# Whitelist — liquid pairs only
WHITELIST = {
    "BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD",
    "XRP/USD", "ADA/USD", "AVAX/USD", "LINK/USD",
    "DOGE/USD", "DOT/USD", "LTC/USD", "UNI/USD",
    "AAVE/USD", "FET/USD", "TAO/USD", "SUI/USD",
    "NEAR/USD", "TRX/USD", "TON/USD", "APT/USD"
}

PRICE_HISTORY_MAX  = 300
WARMUP_CANDLES     = 100      # increased from 60 — better indicator warmup
LOG_FILE           = "logs/bot_trades.jsonl"
ENTRY_FILE         = "logs/entry_prices.json"
RETRY_ATTEMPTS     = 3

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
#  API CLIENT
# ─────────────────────────────────────────────
def _timestamp() -> str:
    return str(int(time.time() * 1000))


def _sign(payload: dict):
    payload["timestamp"] = _timestamp()
    sorted_keys  = sorted(payload.keys())
    total_params = "&".join(f"{k}={payload[k]}" for k in sorted_keys)
    sig = hmac.new(
        SECRET_KEY.encode(), total_params.encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "RST-API-KEY":   API_KEY,
        "MSG-SIGNATURE": sig,
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    return headers, total_params


def _request(method: str, path: str, payload: dict = None, signed: bool = False):
    url     = BASE_URL + path
    payload = payload or {}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            if signed:
                headers, total_params = _sign(dict(payload))
                if method == "GET":
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
            wait = 2 ** attempt
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
    if not data or not data.get("Success"):
        return {}
    return data.get("SpotWallet") or data.get("Wallet") or {}


def place_order(pair: str, side: str, quantity: float,
                price: float = None, entry_price: float = None) -> dict:
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
#  BINANCE WARM-UP
# ─────────────────────────────────────────────
BINANCE_SYMBOL_MAP = {
    "BTC/USD":  "BTCUSDT",  "ETH/USD":  "ETHUSDT",
    "BNB/USD":  "BNBUSDT",  "SOL/USD":  "SOLUSDT",
    "XRP/USD":  "XRPUSDT",  "ADA/USD":  "ADAUSDT",
    "AVAX/USD": "AVAXUSDT", "LINK/USD": "LINKUSDT",
    "DOGE/USD": "DOGEUSDT", "DOT/USD":  "DOTUSDT",
    "LTC/USD":  "LTCUSDT",  "UNI/USD":  "UNIUSDT",
    "AAVE/USD": "AAVEUSDT", "FET/USD":  "FETUSDT",
    "TAO/USD":  "TAOUSSDT", "SUI/USD":  "SUIUSDT",
    "NEAR/USD": "NEARUSDT", "TRX/USD":  "TRXUSDT",
    "TON/USD":  "TONUSDT",  "APT/USD":  "APTUSDT",
}


def fetch_binance_ohlcv(pair: str, limit: int = WARMUP_CANDLES) -> list:
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
        # Return list of (high, low, close) tuples for ATR calculation
        return [(float(c[2]), float(c[3]), float(c[4])) for c in r.json()]
    except Exception as e:
        log.warning(f"Binance warm-up failed for {pair}: {e}")
        return []


# ─────────────────────────────────────────────
#  TRADE LOGGER
# ─────────────────────────────────────────────
def _log_trade(pair: str, side: str, qty: float, price,
               resp: dict, entry_price: float = None, reason: str = ""):
    detail       = (resp or {}).get("OrderDetail", {})
    filled_price = float(detail.get("FilledAverPrice") or 0)
    commission   = float(detail.get("CommissionChargeValue") or 0)
    filled_qty   = float(detail.get("FilledQuantity") or qty)

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
        "entry_price":  entry_price,
        "realised_pnl": realised_pnl,
        "commission":   commission,
        "exit_reason":  reason,
        "api_success":  (resp or {}).get("Success", False),
        "err_msg":      (resp or {}).get("ErrMsg", ""),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    pnl_str = f"  P&L=${realised_pnl:+.2f}" if realised_pnl is not None else ""
    reason_str = f"  [{reason}]" if reason else ""
    log.info(
        f"TRADE {side:4s} {qty} {pair} @ {'MARKET' if not price else price} "
        f"-> {detail.get('Status', 'ERROR')}  "
        f"(ID={detail.get('OrderID')}){pnl_str}{reason_str}"
    )


# ─────────────────────────────────────────────
#  ENTRY PRICE PERSISTENCE
# ─────────────────────────────────────────────
def load_entry_prices() -> dict:
    if os.path.exists(ENTRY_FILE):
        try:
            with open(ENTRY_FILE) as f:
                data = json.load(f)
            log.info(f"Loaded entry prices: {data}")
            return data
        except Exception:
            pass
    return {}


def save_entry_prices(ep: dict):
    with open(ENTRY_FILE, "w") as f:
        json.dump(ep, f, indent=2)


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


def rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(hlc_history: list, period: int = ATR_PERIOD) -> float:
    """
    Average True Range from list of (high, low, close) tuples.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    if len(hlc_history) < period + 1:
        # Fallback: use high-low range only
        if len(hlc_history) < 2:
            return hlc_history[-1][0] * 0.01 if hlc_history else 0.0
        trs = [h - l for h, l, c in hlc_history[-period:]]
        return sum(trs) / len(trs) if trs else 0.0

    tail = hlc_history[-(period + 1):]
    trs  = []
    for i in range(1, len(tail)):
        h, l, c  = tail[i]
        prev_c   = tail[i-1][2]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def kelly_position_size(score: float, atr_val: float,
                        price: float, port_val: float) -> float:
    """
    Kelly fraction adjusted for ATR volatility.
    - Higher ATR  → smaller position (more volatile = more risk)
    - Lower ATR   → larger position (calm market = less risk)

    f = (edge / odds) × Kelly_fraction
    edge  ≈ abs(score)
    odds  ≈ ATR / price  (normalised volatility)
    """
    if price <= 0 or atr_val <= 0:
        return port_val * MIN_POSITION_PCT

    norm_vol = atr_val / price          # ATR as % of price
    if norm_vol <= 0:
        return port_val * MIN_POSITION_PCT

    raw_kelly = abs(score) / norm_vol   # Kelly fraction
    fraction  = raw_kelly * KELLY_FRACTION

    # Clamp between floor and ceiling
    fraction = max(MIN_POSITION_PCT, min(fraction, MAX_POSITION_PCT))
    return port_val * fraction


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
#  REGIME DETECTOR
# ─────────────────────────────────────────────
def detect_regime(price_history: dict, tickers: dict) -> str:
    """
    Detects current market regime by looking at two things:

    1. BTC trend — the market leader
       MOMENTUM if BTC fast EMA > slow EMA (BTC is in uptrend)
       REVERSION if BTC fast EMA < slow EMA (BTC is in downtrend)

    2. Market breadth — % of whitelisted pairs in uptrend
       MOMENTUM if >40% of pairs have fast EMA > slow EMA
       REVERSION if <=40% — majority of market is bearish

    Both must agree for MOMENTUM. If either is bearish → REVERSION.
    """
    # 1. BTC trend
    btc_prices = list(price_history.get("BTC/USD", []))
    btc_regime = "REVERSION"
    if len(btc_prices) >= REGIME_EMA_SLOW + 1:
        btc_fast = ema(btc_prices, REGIME_EMA_FAST)
        btc_slow = ema(btc_prices, REGIME_EMA_SLOW)
        btc_regime = "MOMENTUM" if btc_fast > btc_slow else "REVERSION"

    # 2. Market breadth — count pairs in uptrend
    uptrend_count = 0
    total_count   = 0
    for pair in WHITELIST:
        prices = list(price_history.get(pair, []))
        if len(prices) < REGIME_EMA_SLOW + 1:
            continue
        total_count += 1
        fast = ema(prices, REGIME_EMA_FAST)
        slow = ema(prices, REGIME_EMA_SLOW)
        if fast > slow:
            uptrend_count += 1

    breadth = uptrend_count / total_count if total_count > 0 else 0
    breadth_regime = "MOMENTUM" if breadth >= REGIME_BREADTH_MIN else "REVERSION"

    # Both must agree for MOMENTUM
    regime = "MOMENTUM" if btc_regime == "MOMENTUM" and breadth_regime == "MOMENTUM" else "REVERSION"

    log.info(
        f"[REGIME] {regime}  |  "
        f"BTC={btc_regime}  |  "
        f"Breadth={breadth:.0%} ({uptrend_count}/{total_count} pairs in uptrend)"
    )
    return regime
# ─────────────────────────────────────────────
#  SIGNAL ENGINE  (dual-mode)
# ─────────────────────────────────────────────
def compute_signals(price_history: dict, hlc_history: dict,
                    pairs: list, tickers: dict, regime: str) -> dict:
    """
    Returns {pair: (score, atr_val)}.

    MOMENTUM mode — trend following:
      Entry: EMA fast > slow AND RSI < 45 AND score > 0.35
      Logic: buy into uptrend on slight pullback

    REVERSION mode — mean reversion:
      Entry: RSI < 25 (extremely oversold)
      Logic: buy oversold bounces regardless of trend direction
    """
    signals = {}

    for pair in pairs:
        if pair not in WHITELIST:
            continue

        prices = list(price_history[pair])
        hlc    = list(hlc_history[pair])

        if len(prices) < REGIME_EMA_SLOW + 1:
            continue

        cur_price = tickers.get(pair, {}).get("LastPrice", 0)
        if cur_price < MIN_PRICE:
            continue

        rsi_val = rsi(prices, RSI_PERIOD)
        atr_val = atr(hlc, ATR_PERIOD)

        # ── VOLATILITY GATE ──────────────────────────
        # Skip if market is too quiet — ATR too small relative to price
        atr_pct = atr_val / cur_price if cur_price > 0 else 0
        if atr_pct < MIN_ATR_PCT:
            log.debug(f"  SKIP {pair}: ATR too low ({atr_pct:.4%} < {MIN_ATR_PCT:.4%})")
            continue

        if regime == "MOMENTUM":
            # Trend must be up
            ema_fast   = ema(prices, EMA_FAST)
            ema_slow   = ema(prices, EMA_SLOW)
            if ema_fast <= ema_slow:
                continue

            # RSI pullback gate
            if rsi_val >= MOM_RSI_BUY_MAX:
                continue

            roc      = rate_of_change(prices, MOMENTUM_WINDOW)
            roc_norm = math.copysign(min(abs(roc) * 100, 1.0), roc)
            score    = 0.6 * 1.0 + 0.4 * roc_norm

            if score >= MOM_SIGNAL_THRESHOLD:
                signals[pair] = (score, atr_val)
                log.info(
                    f"  [MOM] {pair}: score={score:+.3f}  "
                    f"RSI={rsi_val:.1f}  ATR={atr_val:.4f}  ATR%={atr_pct:.3%}"
                )

        elif regime == "REVERSION":
            prices_list = list(prices)
            if len(prices_list) < 2:
                continue

            rsi_val    = rsi(prices_list, RSI_PERIOD)
            prev_price = prices_list[-2]
            bouncing   = cur_price > prev_price  # price turned up — confirmation

            # Tier the signal by RSI depth + bounce confirmation
            if rsi_val < REV_RSI_STRONG and bouncing:
                score = 1.0    # strongest — RSI < 20 and bouncing
            elif rsi_val < REV_RSI_MEDIUM and bouncing:
                score = 0.65   # medium — RSI < 30 and bouncing
            elif rsi_val < REV_RSI_WEAK and bouncing:
                score = 0.35   # weak — RSI < 38 and bouncing
            else:
                continue       # not oversold enough or not bouncing yet

            signals[pair] = (score, atr_val)
            log.info(
                f"  [REV] {pair}: score={score:.2f}  "
                f"RSI={rsi_val:.1f}  bouncing={bouncing}  "
                f"ATR={atr_val:.4f}  ATR%={atr_pct:.3%}"
            )

    return signals


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Roostoo Trading Bot v4  —  Regime-Adaptive")
    log.info("=" * 60)

    exchange_info   = get_exchange_info()
    tradeable_pairs = [p for p, info in exchange_info.items() if info.get("CanTrade")]
    whitelist_pairs = [p for p in tradeable_pairs if p in WHITELIST]
    log.info(f"Whitelist pairs: {whitelist_pairs}")

    price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=PRICE_HISTORY_MAX))
    hlc_history:   dict[str, deque] = defaultdict(lambda: deque(maxlen=PRICE_HISTORY_MAX))

    # Warm up from Binance
    log.info(f"Warming up from Binance ({WARMUP_CANDLES} x 1-min candles)...")
    for pair in whitelist_pairs:
        ohlcv = fetch_binance_ohlcv(pair, limit=WARMUP_CANDLES)
        if ohlcv:
            for h, l, c in ohlcv:
                price_history[pair].append(c)
                hlc_history[pair].append((h, l, c))
            log.info(f"  {pair}: {len(ohlcv)} candles  last=${ohlcv[-1][2]:.4f}  "
                     f"ATR={atr(list(hlc_history[pair]), ATR_PERIOD):.4f}")
        else:
            log.warning(f"  {pair}: no warm-up data")

    init_wallet  = get_balance()
    init_tickers = get_ticker_all()
    init_val     = portfolio_value(init_wallet, init_tickers)
    log.info(f"Initial portfolio value: ${init_val:,.2f}")

    tracker      = PerformanceTracker(init_val)
    entry_prices = load_entry_prices()
    trail_peaks: dict[str, float] = {
        coin: float(entry_prices.get(coin, 0)) for coin in entry_prices
    }
    entry_ticks: dict[str, int] = {coin: 0 for coin in entry_prices}

    tick = 0

    while True:
        tick_start = time.time()
        try:
            tick += 1
            log.info(f"\n{'─'*50}\nTick {tick}")

            # 1. Tickers
            tickers = get_ticker_all()
            if not tickers:
                log.warning("No ticker data — skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            for pair in whitelist_pairs:
                if pair in tickers:
                    d   = tickers[pair]
                    c   = d["LastPrice"]
                    bid = d.get("MaxBid", c)
                    ask = d.get("MinAsk", c)
                    price_history[pair].append(c)
                    hlc_history[pair].append((ask, bid, c))

            # 2. Balance
            wallet = get_balance()
            if not wallet:
                log.warning("Could not fetch balance — skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            usd_free = wallet.get("USD", {}).get("Free", 0)
            port_val = portfolio_value(wallet, tickers)
            tracker.update(port_val)
            tracker.report()

            # 3. Circuit breaker
            if tracker.drawdown > MAX_DRAWDOWN_PCT:
                log.warning(
                    f"CIRCUIT BREAKER: drawdown {tracker.drawdown:.2%} > "
                    f"{MAX_DRAWDOWN_PCT:.2%}. Cancelling all. Halting."
                )
                cancel_all_orders()
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # 4. Detect regime
            regime = detect_regime(price_history, tickers)

            # Use regime-specific params
            max_pos   = REV_MAX_POSITIONS if regime == "REVERSION" else MAX_OPEN_POSITIONS
            sl_mult   = REV_ATR_SL_MULT   if regime == "REVERSION" else MOM_ATR_SL_MULT
            tp_mult   = REV_ATR_TP_MULT   if regime == "REVERSION" else MOM_ATR_TP_MULT
            trail_mult = REV_TRAIL_MULT   if regime == "REVERSION" else MOM_TRAIL_MULT

            # 5. Open positions
            open_positions = {
                coin: bal.get("Free", 0)
                for coin, bal in wallet.items()
                if coin != "USD" and bal.get("Free", 0) > 0.000001
            }
            log.info(
                f"[{regime}] Open positions "
                f"({len(open_positions)}/{max_pos}): "
                f"{list(open_positions.keys()) or 'none'}"
            )

            # ── 6. EXIT LOGIC ────────────────────────────────────
            for coin, qty in list(open_positions.items()):
                pair      = f"{coin}/USD"
                ticker_d  = tickers.get(pair, {})
                cur_price = ticker_d.get("LastPrice", 0)
                bid       = ticker_d.get("MaxBid", cur_price)
                ask       = ticker_d.get("MinAsk", cur_price)
                entry     = float(entry_prices.get(coin, cur_price))

                if cur_price <= 0 or entry <= 0:
                    continue

                if coin not in trail_peaks or cur_price > trail_peaks[coin]:
                    trail_peaks[coin] = cur_price

                peak     = trail_peaks[coin]
                atr_val  = atr(list(hlc_history[pair]), ATR_PERIOD)
                pnl_pct  = (cur_price - entry) / entry

                sl_price   = entry - sl_mult   * atr_val
                tp_price   = entry + tp_mult   * atr_val
                trail_sl   = peak  - trail_mult * atr_val

                spread_pct  = (ask - bid) / cur_price if cur_price > 0 and bid > 0 else 0
                spread_exit = spread_pct > SPREAD_REVERT_PCT

                # Minimum hold time — don't exit on noise right after entry
                ticks_held  = tick - entry_ticks.get(coin, 0)
                if ticks_held < MIN_HOLD_TICKS and not spread_exit:
                    log.info(
                        f"  HOLD {coin}: min hold not reached "
                        f"({ticks_held}/{MIN_HOLD_TICKS} ticks)  pnl={pnl_pct:+.2%}"
                    )
                    continue

                exit_reason = None
                if spread_exit:
                    exit_reason = f"spread_reversion ({spread_pct:.3%})"
                elif cur_price <= sl_price:
                    exit_reason = f"ATR_stop_loss ({pnl_pct:.2%})"
                elif cur_price >= tp_price:
                    exit_reason = f"ATR_take_profit ({pnl_pct:.2%})"
                elif cur_price <= trail_sl and pnl_pct > 0:
                    exit_reason = f"trailing_stop (peak=${peak:.4f})"

                if exit_reason:
                    log.info(
                        f"EXIT [{regime}] {coin}: {exit_reason}  "
                        f"entry=${entry:.4f}  now=${cur_price:.4f}  "
                        f"SL=${sl_price:.4f}  TP=${tp_price:.4f}  "
                        f"trail=${trail_sl:.4f}"
                    )
                    amt_prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
                    sell_qty = round(qty, amt_prec)
                    resp = place_order(pair, "SELL", sell_qty,
                                      entry_price=entry)
                    if resp.get("Success"):
                        entry_prices.pop(coin, None)
                        trail_peaks.pop(coin, None)
                        save_entry_prices(entry_prices)
                        log.info(f"EXIT filled: {sell_qty} {coin}")
                    continue

                log.info(
                    f"  HOLD [{regime}] {coin}: pnl={pnl_pct:+.2%}  "
                    f"SL=${sl_price:.4f}  TP=${tp_price:.4f}  "
                    f"trail=${trail_sl:.4f}  ATR={atr_val:.4f}"
                )

            # ── 7. ENTRY LOGIC ───────────────────────────────────
            if len(open_positions) >= max_pos:
                log.info(f"At max positions ({max_pos}) for {regime} mode. No new buys.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            if usd_free < MIN_TRADE_USD:
                log.info(f"Insufficient USD (${usd_free:.2f}). No new buys.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            signals = compute_signals(
                price_history, hlc_history,
                [p for p in whitelist_pairs if p.split("/")[0] not in open_positions],
                tickers, regime
            )

            if not signals:
                # Debug — show why each pair was skipped
                for pair in [p for p in whitelist_pairs if p.split("/")[0] not in open_positions]:
                    prices_d = list(price_history.get(pair, []))
                    hlc_d    = list(hlc_history.get(pair, []))
                    if len(prices_d) < 2:
                        continue
                    cur_p   = tickers.get(pair, {}).get("LastPrice", 0)
                    atr_v   = atr(hlc_d, ATR_PERIOD)
                    atr_p   = atr_v / cur_p if cur_p > 0 else 0
                    rsi_v   = rsi(prices_d, RSI_PERIOD)
                    ema_f   = ema(prices_d, EMA_FAST)
                    ema_s   = ema(prices_d, EMA_SLOW)
                    bounce  = prices_d[-1] > prices_d[-2]
                    log.info(
                        f"  SKIP {pair}: RSI={rsi_v:.1f}  "
                        f"ATR%={atr_p:.4%}  EMA_up={ema_f>ema_s}  "
                        f"bouncing={bounce}  regime={regime}"
                    )
                log.info(f"No qualifying {regime} signals. Holding cash.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            best_pair        = max(signals, key=lambda p: signals[p][0])
            best_score, best_atr = signals[best_pair]
            coin             = best_pair.split("/")[0]
            cur_price        = tickers.get(best_pair, {}).get("LastPrice", 0)

            if cur_price <= 0:
                log.warning(f"Invalid price for {best_pair} — skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            trade_usd = kelly_position_size(best_score, best_atr, cur_price, port_val)
            trade_usd = min(trade_usd, usd_free * 0.95)
            quantity  = trade_usd / cur_price

            if quantity * cur_price < MIN_TRADE_USD:
                log.info(f"Position too small (${quantity*cur_price:.2f}). Skipping.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            amt_prec = exchange_info.get(best_pair, {}).get("AmountPrecision", 6)
            quantity = round(quantity, amt_prec)

            sl_price = cur_price - sl_mult * best_atr
            tp_price = cur_price + tp_mult * best_atr

            log.info(
                f"-> BUY [{regime}] {quantity} {best_pair} @ MARKET  "
                f"signal={best_score:+.3f}  ATR={best_atr:.4f}  "
                f"notional=${quantity*cur_price:.2f}  "
                f"SL=${sl_price:.4f}  TP=${tp_price:.4f}  "
                f"size={trade_usd/port_val:.1%} of portfolio"
            )

            resp = place_order(best_pair, "BUY", quantity)
            if resp.get("Success"):
                d = resp.get("OrderDetail", {})
                filled_price = float(d.get("FilledAverPrice") or cur_price)
                entry_prices[coin] = filled_price
                trail_peaks[coin]  = filled_price
                entry_ticks[coin]  = tick
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
