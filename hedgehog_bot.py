"""
Roostoo Hedgehog Bot  —  inspired by FrankfurtHedgehogs
=========================================================
Adapted strategies:

  1. StaticTrader  → Market Making on BTC/USD
                     Post LIMIT orders inside the spread to capture bid-ask.
                     Cancel & repost every tick.

  2. EtfTrader     → Running Premium Spread Arb
                     Track log(A) - β·log(B) spread using Welford's online
                     running mean + std (same technique as n_hist_samples ETF
                     premium tracking). Trade when deviation > threshold.

  3. DynamicTrader → Price Velocity Signal (proxy for "informed trader")
                     Short-window EMA of price change as informed signal.
                     Amplifies spread signal when momentum agrees, suppresses
                     when momentum disagrees.

  4. InkTrader     → Strong Velocity Bet
                     If velocity exceeds HIGH_VEL_THR, take a full directional
                     position independent of spread signal.

Risk:
  - 25% drawdown circuit breaker
  - ATR-based dynamic stop-loss + take-profit
  - Trailing stop (locks in profits)
  - Cooldown per asset (min 3 ticks between entries)
  - Max 2 concurrent positions

Pairs : BTC/ETH, SOL/ETH, BNB/ETH
Assets: BTC, ETH, SOL, BNB

Run:
  export $(cat .env | xargs) && python3 hedgehog_bot.py
"""

import os, time, hmac, hashlib, logging, json, math, requests
from collections import deque
from datetime import datetime, timezone

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BASE_URL          = os.getenv("ROOSTOO_BASE_URL",   "https://mock-api.roostoo.com")
API_KEY           = os.getenv("ROOSTOO_API_KEY",    "YOUR_API_KEY_HERE")
SECRET_KEY        = os.getenv("ROOSTOO_SECRET_KEY", "YOUR_SECRET_KEY_HERE")

LOOP_INTERVAL_SEC = 12
MAX_POSITIONS     = 2
MIN_TRADE_USD     = 400.0
MAX_DRAWDOWN_PCT  = 0.25
COOLDOWN_TICKS    = 3

# ── Market Making (StaticTrader) ────────────────────
MM_SPREAD_PCT     = 0.0015     # post orders ±0.15% from last price
MM_ASSET          = "BTC"      # which asset to market-make
MM_SIZE_USD       = 1000.0     # notional per MM order

# ── Spread Arb (EtfTrader) ──────────────────────────
PAIRS             = [("BTC","ETH"), ("SOL","ETH"), ("BNB","ETH")]
ASSETS            = list(dict.fromkeys(a for p in PAIRS for a in p))
SPREAD_THRESHOLD  = 2.0        # z-score to enter arb (like BASKET_THRESHOLDS)
SPREAD_EXIT       = 0.3        # z-score to exit
SPREAD_WINDOW     = 60         # min samples before trading
KALMAN_DELTA      = 1e-4
KALMAN_NOISE_OBS  = 1e-3

# ── Velocity Signal (DynamicTrader / InkTrader) ──────
VEL_WINDOW        = 5          # short window for price velocity EMA
VEL_THRESHOLD     = 0.003      # velocity above this = strong informed signal (0.3%)
HIGH_VEL_THR      = 0.008      # above this → full directional bet (InkTrader mode)
VEL_SIGNAL_WEIGHT = 0.35       # how much velocity modifies spread score

# ── Position Sizing ──────────────────────────────────
POSITION_SIZE_PCT = 0.20       # base size (20% of portfolio)
ATR_PERIOD        = 14
ATR_STOP_MULT     = 5.0
ATR_TP_MULT       = 8.0
TRAIL_MULT        = 3.0

WARMUP_CANDLES    = 80
LOG_FILE          = "logs/hedgehog_trades.jsonl"
RETRY_ATTEMPTS    = 3

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/hedgehog_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("hedgehog_bot")


# ─────────────────────────────────────────────
#  API CLIENT
# ─────────────────────────────────────────────
def _timestamp():
    return str(int(time.time() * 1000))


def _sign(payload):
    payload["timestamp"] = _timestamp()
    sorted_keys  = sorted(payload.keys())
    total_params = "&".join(f"{k}={payload[k]}" for k in sorted_keys)
    sig = hmac.new(SECRET_KEY.encode(), total_params.encode(), hashlib.sha256).hexdigest()
    return {"RST-API-KEY": API_KEY, "MSG-SIGNATURE": sig,
            "Content-Type": "application/x-www-form-urlencoded"}, total_params


def _request(method, path, payload=None, signed=False):
    url, payload = BASE_URL + path, payload or {}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            if signed:
                headers, total_params = _sign(dict(payload))
                r = (requests.get(f"{url}?{total_params}", headers=headers, timeout=10)
                     if method == "GET"
                     else requests.post(url, headers=headers, data=total_params, timeout=10))
            else:
                p = {**payload, "timestamp": _timestamp()}
                r = (requests.get(url, params=p, timeout=10)
                     if method == "GET"
                     else requests.post(url, data=p, timeout=10))
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.warning(f"HTTP {e.response.status_code if e.response else '?'} {method} {path} (attempt {attempt+1})")
        except requests.exceptions.RequestException as e:
            log.warning(f"Request error {method} {path} (attempt {attempt+1}): {e}")
        if attempt < RETRY_ATTEMPTS - 1:
            time.sleep(2 ** attempt)
    log.error(f"All attempts failed: {method} {path}")
    return None


def get_exchange_info():
    d = _request("GET", "/v3/exchangeInfo")
    return d.get("TradePairs", {}) if d else {}

def get_ticker_all():
    d = _request("GET", "/v3/ticker")
    return d.get("Data", {}) if (d and d.get("Success")) else {}

def get_balance():
    d = _request("GET", "/v3/balance", signed=True)
    return d.get("SpotWallet", {}) if (d and d.get("Success")) else {}

def place_order(pair, side, quantity, price=None, entry_price=None):
    payload = {
        "pair":     pair,
        "side":     side.upper(),
        "type":     "LIMIT" if price else "MARKET",
        "quantity": str(quantity),
    }
    if price:
        payload["price"] = str(round(price, 2))
    resp = _request("POST", "/v3/place_order", payload, signed=True)
    _log_trade(pair, side, quantity, price, resp, entry_price)
    return resp or {}

def cancel_all_orders():
    return _request("POST", "/v3/cancel_order", {}, signed=True) or {}


# ─────────────────────────────────────────────
#  BINANCE WARM-UP
# ─────────────────────────────────────────────
BINANCE_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT",
               "SOL": "SOLUSDT", "BNB": "BNBUSDT"}

def fetch_binance_closes(asset, limit=WARMUP_CANDLES):
    sym = BINANCE_MAP.get(asset)
    if not sym:
        return []
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": sym, "interval": "1m", "limit": limit},
                         timeout=10)
        r.raise_for_status()
        return [float(c[4]) for c in r.json()]
    except Exception as e:
        log.warning(f"Binance warm-up failed {asset}: {e}")
        return []


# ─────────────────────────────────────────────
#  TRADE LOGGER
# ─────────────────────────────────────────────
def _log_trade(pair, side, qty, price, resp, entry_price=None):
    detail       = (resp or {}).get("OrderDetail", {})
    filled_price = float(detail.get("FilledAverPrice") or 0)
    commission   = float(detail.get("CommissionChargeValue") or 0)
    filled_qty   = float(detail.get("FilledQuantity") or qty)
    realised_pnl = None
    if side.upper() == "SELL" and entry_price and entry_price > 0 and filled_price > 0:
        realised_pnl = (filled_price - entry_price) * filled_qty - commission
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "pair": pair, "side": side,
        "quantity": qty, "price": price, "order_id": detail.get("OrderID"),
        "status": detail.get("Status"), "filled_qty": filled_qty,
        "filled_price": filled_price, "entry_price": entry_price,
        "realised_pnl": realised_pnl, "commission": commission,
        "api_success": (resp or {}).get("Success", False),
        "err_msg": (resp or {}).get("ErrMsg", ""),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    pnl_str = f"  P&L=${realised_pnl:+.2f}" if realised_pnl is not None else ""
    log.info(f"TRADE {side:4s} {qty} {pair} @ {'MARKET' if not price else price} "
             f"-> {detail.get('Status','ERROR')} (ID={detail.get('OrderID')}){pnl_str}")


# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def _ema(prices, period):
    if not prices:
        return 0.0
    if len(prices) < period:
        return sum(prices) / len(prices)
    k, val = 2.0 / (period + 1), prices[0]
    for p in prices[1:]:
        val = p * k + val * (1 - k)
    return val

def _atr(prices, period=ATR_PERIOD):
    if len(prices) < 2:
        return 0.0
    trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return _ema(trs, period)


# ─────────────────────────────────────────────
#  RUNNING PREMIUM TRACKER  (EtfTrader-style)
#  Uses Welford's online algorithm — same idea as:
#    n_hist_samples / running mean_premium in EtfTrader
# ─────────────────────────────────────────────
class RunningPremiumTracker:
    """
    Tracks log(A) - beta*log(B) spread using:
    1. Kalman filter for adaptive beta (hedge ratio)
    2. Welford's online algorithm for running mean + variance
       (mirrors how EtfTrader tracks mean_premium with n_hist_samples)

    spread_deviation = current_spread - running_mean
    z_score = spread_deviation / running_std
    """
    def __init__(self, min_samples=SPREAD_WINDOW):
        # Kalman state
        self.beta   = 1.0
        self.P      = 1.0

        # Welford online stats (like EtfTrader's mean_premium + n)
        self.n      = 0
        self.mean   = 0.0
        self.M2     = 0.0     # sum of squared deviations (for variance)

        self.last_z = 0.0
        self.min_samples = min_samples

    def update(self, price_a, price_b):
        if price_a <= 0 or price_b <= 0:
            return
        la, lb = math.log(price_a), math.log(price_b)

        # Kalman update for beta
        P_pred = self.P + KALMAN_DELTA
        H      = lb
        S      = H * P_pred * H + KALMAN_NOISE_OBS
        K      = P_pred * H / S
        inn    = la - H * self.beta
        self.beta += K * inn
        self.P     = (1 - K * H) * P_pred

        spread = la - self.beta * lb

        # Welford's online mean + variance (like EtfTrader n_hist_samples)
        self.n  += 1
        delta    = spread - self.mean
        self.mean += delta / self.n           # running mean
        delta2   = spread - self.mean
        self.M2  += delta * delta2            # running sum of squares

        # z-score
        if self.n >= self.min_samples and self.M2 > 0:
            std = math.sqrt(self.M2 / self.n)
            self.last_z = (spread - self.mean) / std if std > 1e-10 else 0.0
        else:
            self.last_z = 0.0

    @property
    def ready(self):
        return self.n >= self.min_samples

    @property
    def zscore(self):
        return self.last_z


# ─────────────────────────────────────────────
#  VELOCITY TRACKER  (DynamicTrader/InkTrader proxy)
#  "Olivia" → large informed trader
#  Here: strong price velocity = informed signal
# ─────────────────────────────────────────────
class VelocityTracker:
    """
    Tracks short-window EMA of log-returns to detect "informed" momentum.
    Equivalent to watching for Olivia's trades in DynamicTrader.

    velocity > HIGH_VEL_THR  → strong bullish (InkTrader: go full long)
    velocity < -HIGH_VEL_THR → strong bearish (InkTrader: go full short)
    |velocity| < VEL_THRESHOLD → neutral (no informed signal)
    """
    def __init__(self, window=VEL_WINDOW):
        self.prices = deque(maxlen=window + 2)
        self.window = window

    def update(self, price):
        self.prices.append(price)

    @property
    def velocity(self):
        """EMA of recent log-returns. Units: fractional change per tick."""
        if len(self.prices) < 3:
            return 0.0
        log_rets = []
        prices = list(self.prices)
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                log_rets.append(math.log(prices[i] / prices[i-1]))
        return _ema(log_rets, self.window)

    @property
    def direction(self):
        """Returns LONG (+1), SHORT (-1), or NEUTRAL (0)."""
        v = self.velocity
        if v > HIGH_VEL_THR:
            return 1
        if v < -HIGH_VEL_THR:
            return -1
        if v > VEL_THRESHOLD:
            return 1
        if v < -VEL_THRESHOLD:
            return -1
        return 0


# ─────────────────────────────────────────────
#  PORTFOLIO VALUE & PERFORMANCE
# ─────────────────────────────────────────────
def portfolio_value(wallet, tickers):
    total = wallet.get("USD", {}).get("Free", 0) + wallet.get("USD", {}).get("Lock", 0)
    for coin, bal in wallet.items():
        if coin == "USD":
            continue
        price = tickers.get(f"{coin}/USD", {}).get("LastPrice", 0)
        total += (bal.get("Free", 0) + bal.get("Lock", 0)) * price
    return total


class PerformanceTracker:
    def __init__(self, initial):
        self.initial = initial
        self.peak    = initial
        self.curve   = [initial]
        self.returns = []

    def update(self, val):
        if self.curve and self.curve[-1] > 0:
            self.returns.append((val - self.curve[-1]) / self.curve[-1])
        self.curve.append(val)
        self.peak = max(self.peak, val)

    @property
    def total_return(self):
        return self.curve[-1] / self.initial - 1 if self.initial else 0

    @property
    def drawdown(self):
        return (self.peak - self.curve[-1]) / self.peak if self.peak else 0

    @property
    def sharpe(self):
        if len(self.returns) < 2:
            return 0.0
        m = sum(self.returns) / len(self.returns)
        s = math.sqrt(sum((r - m) ** 2 for r in self.returns) / len(self.returns))
        return (m / s) * math.sqrt(len(self.returns)) if s > 0 else 0.0

    @property
    def sortino(self):
        if not self.returns:
            return 0.0
        m   = sum(self.returns) / len(self.returns)
        neg = [r for r in self.returns if r < 0]
        if not neg:
            return float("inf")
        ds  = math.sqrt(sum(r**2 for r in neg) / len(neg))
        return (m / ds) * math.sqrt(len(self.returns)) if ds > 0 else 0.0

    def report(self):
        log.info(
            f"[PERF] Val=${self.curve[-1]:,.2f}  "
            f"Ret={self.total_return:+.2%}  DD={self.drawdown:.2%}  "
            f"Sharpe={self.sharpe:.3f}  Sortino={self.sortino:.3f}"
        )


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("Roostoo Hedgehog Bot  (FrankfurtHedgehogs-inspired)")
    log.info(f"  MM asset    : {MM_ASSET}  spread={MM_SPREAD_PCT:.2%}")
    log.info(f"  Arb pairs   : {PAIRS}")
    log.info(f"  Z entry/exit: {SPREAD_THRESHOLD} / {SPREAD_EXIT}")
    log.info(f"  Vel thr/high: {VEL_THRESHOLD:.3%} / {HIGH_VEL_THR:.3%}")
    log.info("=" * 65)

    exchange_info = get_exchange_info()

    # ── Liquidate non-strategy assets ───────────────────────────
    log.info("Checking for non-strategy holdings...")
    liq_w = get_balance()
    liq_t = get_ticker_all()
    strategy_assets = set(ASSETS) | {"USD"}
    for coin, bal in liq_w.items():
        if coin in strategy_assets:
            continue
        qty = bal.get("Free", 0)
        if qty <= 0:
            continue
        pair  = f"{coin}/USD"
        price = liq_t.get(pair, {}).get("LastPrice", 0)
        if price <= 0:
            continue
        if qty * price < MIN_TRADE_USD:
            log.info(f"  Skipping {coin}: notional ${qty*price:.2f} below minimum")
            continue
        prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
        resp = place_order(pair, "SELL", round(qty, prec))
        log.info(f"  Liquidated {coin}: {'OK' if resp.get('Success') else 'FAIL'}")
        time.sleep(1)

    # ── Close any open strategy-asset positions ──────────────────
    log.info("Closing any open strategy-asset positions...")
    for coin in ASSETS:
        qty = liq_w.get(coin, {}).get("Free", 0)
        if qty <= 0:
            continue
        pair  = f"{coin}/USD"
        price = liq_t.get(pair, {}).get("LastPrice", 0)
        if price <= 0:
            log.warning(f"  No price for {pair} — skipping")
            continue
        if qty * price < MIN_TRADE_USD:
            log.info(f"  Skipping {coin}: notional ${qty*price:.2f} below minimum")
            continue
        prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
        resp = place_order(pair, "SELL", round(qty, prec))
        log.info(f"  {'OK' if resp.get('Success') else 'FAIL'}: closed {coin} position ({qty})")
        time.sleep(1)
    log.info("All positions cleared. Starting fresh.")

    # ── Warm-up ──────────────────────────────────────────────────
    price_history = {a: deque(maxlen=500) for a in ASSETS}
    vel_trackers  = {a: VelocityTracker(VEL_WINDOW) for a in ASSETS}
    prem_trackers = {p: RunningPremiumTracker(SPREAD_WINDOW) for p in PAIRS}

    log.info(f"Warming up ({WARMUP_CANDLES} Binance 1-min candles)...")
    closes = {}
    for asset in ASSETS:
        closes[asset] = fetch_binance_closes(asset, WARMUP_CANDLES)
        if closes[asset]:
            price_history[asset].extend(closes[asset])
            for p in closes[asset]:
                vel_trackers[asset].update(p)
            log.info(f"  {asset}: {len(closes[asset])} candles  last=${closes[asset][-1]:,.4f}")

    n = min((len(closes.get(a, [])) for a in ASSETS), default=0)
    for i in range(n):
        for (a, b) in PAIRS:
            if i < len(closes.get(a, [])) and i < len(closes.get(b, [])):
                prem_trackers[(a, b)].update(closes[a][i], closes[b][i])

    init_wallet  = get_balance()
    init_tickers = get_ticker_all()
    init_val     = portfolio_value(init_wallet, init_tickers)
    log.info(f"Starting portfolio: ${init_val:,.2f}")

    perf_tracker   = PerformanceTracker(init_val)
    entry_prices   = {}
    trailing_peaks = {}
    last_trade_tick = {}
    mm_order_ids   = []    # track open MM limit orders

    ENTRY_FILE = "logs/hedgehog_entry_prices.json"
    def load_ep():
        try:
            if os.path.exists(ENTRY_FILE):
                with open(ENTRY_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    def save_ep(ep):
        with open(ENTRY_FILE, "w") as f:
            json.dump(ep, f)

    entry_prices = load_ep()
    tick = 0

    while True:
        tick_start = time.time()
        try:
            tick += 1
            log.info(f"\n{'─'*55}\nTick {tick}")

            # ── 1. Market data ──────────────────────────────────
            tickers = get_ticker_all()
            if not tickers:
                log.warning("No ticker data.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            prices = {}
            for asset in ASSETS:
                p = tickers.get(f"{asset}/USD", {}).get("LastPrice", 0)
                if p > 0:
                    prices[asset] = p
                    price_history[asset].append(p)
                    vel_trackers[asset].update(p)

            for (a, b) in PAIRS:
                if a in prices and b in prices:
                    prem_trackers[(a, b)].update(prices[a], prices[b])

            # ── 2. Balance ──────────────────────────────────────
            wallet = get_balance()
            if not wallet:
                log.warning("No balance data.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            usd_free = wallet.get("USD", {}).get("Free", 0)
            port_val = portfolio_value(wallet, tickers)
            perf_tracker.update(port_val)
            perf_tracker.report()

            # ── 3. Circuit breaker ──────────────────────────────
            if perf_tracker.drawdown > MAX_DRAWDOWN_PCT:
                log.warning(f"CIRCUIT BREAKER: DD={perf_tracker.drawdown:.2%}. Halting.")
                cancel_all_orders()
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # ── 4. Sync open positions ──────────────────────────
            # Ignore dust (notional below MIN_TRADE_USD)
            open_coins = {
                coin for coin in ASSETS
                if coin != "USD"
                and wallet.get(coin, {}).get("Free", 0) * prices.get(coin, 1) >= MIN_TRADE_USD
            }
            for coin in open_coins:
                p = prices.get(coin, 0)
                if p > 0:
                    trailing_peaks[coin] = max(trailing_peaks.get(coin, p), p)

            # ── 5. Market Making (StaticTrader) ─────────────────
            # Cancel old MM orders each tick, repost at updated price
            cancel_all_orders()

            mm_price = prices.get(MM_ASSET, 0)
            mm_pair  = f"{MM_ASSET}/USD"
            mm_prec  = exchange_info.get(mm_pair, {}).get("AmountPrecision", 6)

            if mm_price > 0 and MM_ASSET not in open_coins and usd_free > MM_SIZE_USD * 2:
                bid_px  = mm_price * (1 - MM_SPREAD_PCT)
                ask_px  = mm_price * (1 + MM_SPREAD_PCT)
                mm_qty  = round(MM_SIZE_USD / mm_price, mm_prec)

                if mm_qty * mm_price >= MIN_TRADE_USD:
                    # BUY limit below market
                    resp_b = place_order(mm_pair, "BUY", mm_qty, price=bid_px)
                    # SELL limit above market — only if we hold BTC
                    btc_free = wallet.get(MM_ASSET, {}).get("Free", 0)
                    if btc_free >= mm_qty:
                        resp_s = place_order(mm_pair, "SELL", mm_qty, price=ask_px)
                    log.info(
                        f"  MM {MM_ASSET}: bid=${bid_px:,.2f}  ask=${ask_px:,.2f}  "
                        f"qty={mm_qty}  spread={MM_SPREAD_PCT:.2%}"
                    )

            # ── 6. Exit logic ───────────────────────────────────
            for coin in list(open_coins):
                pair      = f"{coin}/USD"
                cur_price = prices.get(coin, 0)
                entry     = float(entry_prices.get(coin, cur_price))
                peak      = trailing_peaks.get(coin, cur_price)
                if cur_price <= 0 or entry <= 0:
                    continue

                hist = list(price_history[coin])
                atr  = _atr(hist, ATR_PERIOD)
                pnl  = (cur_price - entry) / entry

                sl_price    = entry - ATR_STOP_MULT * atr
                tp_price    = entry + ATR_TP_MULT * atr
                trail_price = peak  - TRAIL_MULT * atr

                # Find pair z-score for this coin
                pair_key = next(((a, b) for (a, b) in PAIRS if a == coin or b == coin), None)
                z = prem_trackers[pair_key].zscore if pair_key else 0.0

                reason = None
                if cur_price <= sl_price:
                    reason = f"STOP-LOSS  ATR={atr:.2f}"
                elif cur_price >= tp_price:
                    reason = f"TAKE-PROFIT  ATR={atr:.2f}"
                elif cur_price <= trail_price and pnl > 0:
                    reason = f"TRAILING-STOP  peak=${peak:.2f}"
                elif abs(z) < SPREAD_EXIT:
                    reason = f"SPREAD-REVERT  z={z:+.3f}"

                if reason:
                    log.info(f"EXIT {coin} [{reason}]  pnl={pnl:+.2%}")
                    qty  = wallet.get(coin, {}).get("Free", 0)
                    prec = exchange_info.get(pair, {}).get("AmountPrecision", 6)
                    resp = place_order(pair, "SELL", round(qty, prec), entry_price=entry)
                    if resp.get("Success"):
                        entry_prices.pop(coin, None)
                        trailing_peaks.pop(coin, None)
                        save_ep(entry_prices)
                        open_coins.discard(coin)
                else:
                    log.info(
                        f"  Hold {coin}: entry=${entry:.4f}  now=${cur_price:.4f}  "
                        f"pnl={pnl:+.2%}  SL=${sl_price:.4f}  TP=${tp_price:.4f}  "
                        f"trail=${trail_price:.4f}  z={z:+.3f}"
                    )

            # ── 7. Entry scanning ───────────────────────────────
            if len(open_coins) >= MAX_POSITIONS:
                log.info(f"At max positions ({MAX_POSITIONS}).")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            if usd_free < MIN_TRADE_USD:
                log.info(f"Insufficient USD (${usd_free:.2f}).")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            candidates = []

            for (a, b) in PAIRS:
                pt = prem_trackers[(a, b)]
                if not pt.ready:
                    continue

                z     = pt.zscore
                vel_a = vel_trackers[a]
                vel_b = vel_trackers[b]
                dir_a = vel_a.direction
                dir_b = vel_b.direction
                v_a   = vel_a.velocity
                v_b   = vel_b.velocity

                log.info(
                    f"  [{a}/{b}] z={z:+.3f}  β={pt.beta:.4f}  "
                    f"vel_{a}={v_a:+.4f}({dir_a:+d})  vel_{b}={v_b:+.4f}({dir_b:+d})"
                )

                # ── InkTrader mode: strong velocity = full directional bet ──
                # If BTC velocity is very high, buy BTC regardless of spread
                for coin, vel_tracker in [(a, vel_a), (b, vel_b)]:
                    if coin in open_coins:
                        continue
                    if tick - last_trade_tick.get(coin, 0) < COOLDOWN_TICKS:
                        continue
                    v = vel_tracker.velocity
                    if abs(v) >= HIGH_VEL_THR:
                        if v > 0:
                            candidates.append((abs(v) * 1.5, coin, f"{coin}/USD", "INK_LONG"))
                        # Don't short in spot market
                        log.info(f"  InkTrader signal: {coin} vel={v:+.4f} (HIGH)")

                # ── EtfTrader spread arb ──────────────────────────────────
                # Blend spread z-score with velocity signal
                # (mirrors informed_thr_adj in EtfTrader's get_basket_orders)
                if z < -SPREAD_THRESHOLD and dir_a >= 0 and a not in open_coins:
                    if tick - last_trade_tick.get(a, 0) >= COOLDOWN_TICKS:
                        vel_boost = 1 + VEL_SIGNAL_WEIGHT * max(dir_a, 0)
                        score = abs(z) * vel_boost
                        candidates.append((score, a, f"{a}/USD", "ARB"))

                elif z > SPREAD_THRESHOLD and dir_b >= 0 and b not in open_coins:
                    if tick - last_trade_tick.get(b, 0) >= COOLDOWN_TICKS:
                        vel_boost = 1 + VEL_SIGNAL_WEIGHT * max(dir_b, 0)
                        score = abs(z) * vel_boost
                        candidates.append((score, b, f"{b}/USD", "ARB"))

            if not candidates:
                log.info("No qualifying signals.")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # Best signal wins
            candidates.sort(key=lambda x: x[0], reverse=True)
            score, coin, pair, signal_type = candidates[0]
            cur_price = prices.get(coin, 0)
            if cur_price <= 0:
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            hist     = list(price_history[coin])
            atr_val  = _atr(hist, ATR_PERIOD)
            trade_usd = min(port_val * POSITION_SIZE_PCT, usd_free * 0.95)
            prec      = exchange_info.get(pair, {}).get("AmountPrecision", 6)
            quantity  = round(trade_usd / cur_price, prec)

            if quantity * cur_price < MIN_TRADE_USD:
                log.info(f"Order too small (${quantity*cur_price:.2f}).")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            sl_px = cur_price - ATR_STOP_MULT * atr_val
            tp_px = cur_price + ATR_TP_MULT * atr_val
            log.info(
                f"ENTRY [{signal_type}] BUY {coin}  score={score:.3f}  "
                f"notional=${quantity*cur_price:.2f}  "
                f"SL=${sl_px:.4f}  TP=${tp_px:.4f}"
            )

            resp = place_order(pair, "BUY", quantity)
            if resp.get("Success"):
                d = resp.get("OrderDetail", {})
                filled = float(d.get("FilledAverPrice") or cur_price)
                entry_prices[coin]    = filled
                trailing_peaks[coin]  = filled
                last_trade_tick[coin] = tick
                save_ep(entry_prices)
                log.info(f"  Filled @ ${filled:,.4f}  qty={d.get('FilledQuantity')}")
            else:
                log.warning(f"  Order failed: {resp.get('ErrMsg','')}")

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        elapsed = time.time() - tick_start
        time.sleep(max(0, LOOP_INTERVAL_SEC - elapsed))

    log.info("Final performance:")
    perf_tracker.report()
    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
