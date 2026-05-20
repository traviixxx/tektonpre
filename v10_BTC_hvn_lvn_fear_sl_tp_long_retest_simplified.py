#!/usr/bin/env python3
"""
BTC HVN/LVN zone-touch trader (SIM or LIVE Binance Futures).
Volume profile logic aligned with v18_advanced_v2 (4h/1d wide profile).
Price poll: 3s | Profile refresh: 3min
Logs/state: BTCUSD/BTC_hvn_lvn_* | Engine: BTCUSDT

LIVE: export BTC_LIVE_TRADE=true BINANCE_API_KEY=... BINANCE_ED25519_PRIVATE_KEY_PATH=...
      export BTC_ORDER_USDT=1000   # margin (Binance "Cost") USDT
      export BTC_LEVERAGE=40       # position notional ≈ 1000 × 40 = 40,000 USDT
      export BTC_ENTRY_ORDER_TYPE=LIMIT
      export BTC_LIMIT_ENTRY_OFFSET=0
      export BTC_ENTRY_ORDER_TIMEOUT_SEC=90
      # chỉ notional 1000 (không × lev): export BTC_SIZING_MODE=notional
      # Fear SL/TP risk modifier:
      export BTC_FEAR_ENABLED=true
      export BTC_FEAR_SOURCE=API      # API | ENV | OFF
      export BTC_FEAR_INDEX=50        # dùng khi BTC_FEAR_SOURCE=ENV
      export CMC_PRO_API_KEY=...      # CoinMarketCap Fear & Greed API key

      # Experimental LONG reclaim-retest edge:
      # Khi bật, Long retest được ưu tiên trước max-inverse tại level=max.
      export BTC_ENABLE_LONG_BULLISH_4H_MAX_EDGE=true
      # Optional, mặc định true:
      export BTC_LONG_RETEST_REQUIRE_ACCEPTANCE=true
"""

import base64
import json
import math
import os
import shlex
import time
import requests
from urllib.parse import quote, urlencode

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_SHELL_ENV_KEYS = set(os.environ)


def _load_dotenv_file(path, override=False):
    """Load KEY=value or export KEY=value lines without requiring python-dotenv."""
    if not path or not os.path.isfile(path):
        return False

    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()

            try:
                parts = shlex.split(line, comments=True, posix=True)
            except ValueError:
                parts = [line]

            for part in parts:
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                key = key.strip()
                if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                    continue
                if not override and key in os.environ:
                    continue
                os.environ[key] = value
    return True


def _load_dotenv():
    candidates = [
        os.path.join(_SCRIPT_DIR, ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(_REPO_ROOT, ".env"),
    ]
    loaded = []
    seen = set()
    for path in candidates:
        path = os.path.abspath(path)
        if path in seen:
            continue
        seen.add(path)
        if _load_dotenv_file(path, override=False):
            loaded.append(path)
    return loaded


ENV_FILES_LOADED = _load_dotenv()


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name, default, *fallback_names):
    env_names = (name, *fallback_names)
    for env_name in env_names:
        if env_name not in _SHELL_ENV_KEYS:
            continue
        raw = os.getenv(env_name)
        if raw is not None and raw.strip() != "":
            return float(raw)
    for env_name in env_names:
        raw = os.getenv(env_name)
        if raw is not None and raw.strip() != "":
            return float(raw)
    return float(default)

# =========================
# CONFIG
# =========================
SYMBOL = os.getenv("BTC_SYMBOL", "BTCUSDT")
FUTURES_BASE = "https://fapi.binance.com"

ZONE_TOUCH_TOLERANCE = env_float("BTC_ZONE_TOUCH_TOLERANCE", "15.0", "ZONE_TOUCH_TOLERANCE")
TP_LVN_POINTS = env_float("BTC_TP_LVN_POINTS", "150.0", "TP_LVN_POINTS")
TP_HVN_POINTS = env_float("BTC_TP_HVN_POINTS", "230.0", "TP_HVN_POINTS")
SL_POINTS = env_float("BTC_SL_POINTS", "260.0", "SL_POINTS")
PRICE_POLL_SEC = float(os.getenv("PRICE_POLL_SEC", "3"))
PROFILE_REFRESH_SEC = float(os.getenv("PROFILE_REFRESH_SEC", "180"))
SCAN_STATUS_SEC = float(os.getenv("SCAN_STATUS_SEC", str(PRICE_POLL_SEC)))

# =========================
# STRATEGY FILTERS
# =========================
# Giữ edge đã audit: LIVE 4h SHORT min only có winrate tốt nhất.
# STRICT_EDGE_ONLY=true sẽ chỉ cho phép nhóm edge này và max-inverse/rejection rule.
STRICT_EDGE_ONLY = env_bool("BTC_STRICT_EDGE_ONLY", True)
ENABLE_MAX_INVERSE = env_bool("BTC_ENABLE_MAX_INVERSE", True)
ENABLE_REJECTION_FOLLOW_TREND = env_bool("BTC_ENABLE_REJECTION_FOLLOW_TREND", True)
ENABLE_SHORT_BEARISH_4H_MIN_EDGE = env_bool("BTC_ENABLE_SHORT_BEARISH_4H_MIN_EDGE", True)
# Experimental bullish reclaim/retest edge. Default OFF so it cannot change current edge unless enabled.
# Khi bật, edge này LUÔN được xét trước max-inverse tại level=max.
ENABLE_LONG_BULLISH_4H_MAX_EDGE = env_bool("BTC_ENABLE_LONG_BULLISH_4H_MAX_EDGE", False)
# Require last closed 15m candle to still accept the level as support.
# Giữ mặc định true để Long retest không bị quá dễ.
LONG_RETEST_REQUIRE_ACCEPTANCE = env_bool("BTC_LONG_RETEST_REQUIRE_ACCEPTANCE", True)
MAX_RETEST_IMPULSE_POINTS = float(os.getenv("BTC_MAX_RETEST_IMPULSE_POINTS", "500.0"))
RETEST_LOOKBACK_CANDLES = int(os.getenv("BTC_RETEST_LOOKBACK_CANDLES", "8"))
REJECTION_LOOKBACK_CANDLES = int(os.getenv("BTC_REJECTION_LOOKBACK_CANDLES", "3"))
REJECTION_WICK_RATIO = float(os.getenv("BTC_REJECTION_WICK_RATIO", "1.2"))

MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
STARTING_BALANCE = float(os.getenv("SIM_STARTING_BALANCE", "10000.0"))

STATE_PATH = os.path.join(
    _SCRIPT_DIR,
    os.getenv("BTC_HVN_LVN_STATE", "BTC_hvn_lvn_trade_state.json"),
)
TRADE_LOG_PATH = os.path.join(
    _SCRIPT_DIR,
    os.getenv("BTC_HVN_LVN_LOG", "BTC_hvn_lvn_trades.jsonl"),
)

PRICE_DECIMALS = int(os.getenv("PRICE_DECIMALS", "1"))

LIVE_TRADE = env_bool("BTC_LIVE_TRADE", False)
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
ED25519_KEY_PATH = os.getenv(
    "BINANCE_ED25519_PRIVATE_KEY_PATH",
    os.path.expanduser("~/.ssh/binance"),
)
# Position sizing: margin (Cost) × leverage → notional, qty = notional / price
ORDER_USDT = float(os.getenv("BTC_ORDER_USDT", os.getenv("BTC_MARGIN_USDT", "400")))
LEVERAGE = int(os.getenv("BTC_LEVERAGE", "20"))
# margin (default): notional = ORDER_USDT * LEVERAGE | notional: ORDER_USDT only
SIZING_MODE = os.getenv("BTC_SIZING_MODE", "margin").strip().lower()
_ORDER_QTY_RAW = os.getenv("BTC_ORDER_QTY", "").strip()
ORDER_QTY_FIXED = float(_ORDER_QTY_RAW) if _ORDER_QTY_RAW else None
API_TIMEOUT = float(os.getenv("BINANCE_API_TIMEOUT", "15"))
RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))

# LIVE entry mode. Giữ nguyên sizing ORDER_USDT × LEVERAGE, chỉ đổi cách vào lệnh.
# LIMIT_ENTRY_OFFSET:
#   LONG  -> BUY LIMIT = signal_entry - offset
#   SHORT -> SELL LIMIT = signal_entry + offset
# ENTRY_ORDER_TIMEOUT_SEC=0 nghĩa là không tự cancel lệnh chờ.
ENTRY_ORDER_TYPE = os.getenv("BTC_ENTRY_ORDER_TYPE", "LIMIT").strip().upper()
LIMIT_ENTRY_OFFSET = float(os.getenv("BTC_LIMIT_ENTRY_OFFSET", "0"))
ENTRY_ORDER_TIMEOUT_SEC = float(os.getenv("BTC_ENTRY_ORDER_TIMEOUT_SEC", "90"))

# SL/TP bracket behavior
# AUTO: try closePosition first, then quantity reduce-only/positionSide fallback.
# CLOSE_POSITION: only closePosition=true.
# QTY: only quantity-based bracket.
BRACKET_MODE = os.getenv("BTC_BRACKET_MODE", "AUTO").strip().upper()
BRACKET_CLOSE_POSITION = env_bool("BTC_BRACKET_CLOSE_POSITION", True)
BRACKET_WORKING_TYPE = os.getenv("BTC_BRACKET_WORKING_TYPE", "MARK_PRICE").strip().upper()
POSITION_SIDE_MODE = os.getenv("BINANCE_POSITION_SIDE_MODE", "AUTO").strip().upper()  # AUTO | ONEWAY | HEDGE
BRACKET_RETRY_ATTEMPTS = max(1, int(os.getenv("BTC_BRACKET_RETRY", "1")))
BRACKET_RETRY_SLEEP_SEC = float(os.getenv("BTC_BRACKET_RETRY_SLEEP_SEC", os.getenv("BTC_BRACKET_RETRY_SEC", "3")))
BRACKET_RETRY_SEC = float(os.getenv("BTC_BRACKET_RETRY_SEC", str(BRACKET_RETRY_SLEEP_SEC)))
POSITION_STATUS_SEC = float(os.getenv("BTC_POSITION_STATUS_SEC", str(PRICE_POLL_SEC)))
# Binance USD-M now requires conditional TP/SL via Algo Order API on some accounts/API versions.
BRACKET_API = os.getenv("BTC_BRACKET_API", "ALGO").strip().upper()  # ALGO or ORDER

# =========================
# FEAR INDEX RISK MODIFIER
# =========================
# Fear Index không dùng Binance API. Mặc định lấy từ CoinMarketCap giống v18_advanced_v2.py.
# Nếu muốn không gọi internet ngoài Binance: export BTC_FEAR_SOURCE=ENV và export BTC_FEAR_INDEX=50
FEAR_ENABLED = env_bool("BTC_FEAR_ENABLED", True)
FEAR_SOURCE = os.getenv("BTC_FEAR_SOURCE", "API").strip().upper()  # API | ENV | OFF
FEAR_INDEX_OVERRIDE = os.getenv("BTC_FEAR_INDEX", "").strip()
FEAR_API_URL = os.getenv("BTC_FEAR_API_URL", "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest")
CMC_PRO_API_KEY = os.getenv("CMC_PRO_API_KEY", "4085f6546c2e4690941b74d721ab7aec").strip()
FEAR_CACHE_SEC = float(os.getenv("BTC_FEAR_CACHE_SEC", "1800"))  # v18 uses 30m cache.
FEAR_API_TIMEOUT = float(os.getenv("BTC_FEAR_API_TIMEOUT", "5"))
FEAR_DEFAULT_INDEX = int(os.getenv("BTC_FEAR_DEFAULT_INDEX", "50"))
# qty adjust giữ risk hợp lý khi SL bị widen/narrow theo fear. Tắt bằng BTC_FEAR_ADJUST_QTY=false.
FEAR_ADJUST_QTY = env_bool("BTC_FEAR_ADJUST_QTY", True)

# Binance private-call optimization: set leverage once, then cache success.
# Nếu leverage fail thì abort entry, không đặt lệnh với qty sai và không spam Binance.
ABORT_ON_LEVERAGE_FAIL = env_bool("BTC_ABORT_ON_LEVERAGE_FAIL", True)
LEVERAGE_RETRY_SEC = float(os.getenv("BTC_LEVERAGE_RETRY_SEC", "300"))

ACTIVE_TRADE = None
TRADE_HISTORY = []
TRADE_COUNTER = 0
CONSECUTIVE_LOSSES = 0
SIM_BALANCE = STARTING_BALANCE

_volume_zone_cache = None
_trend_cache = "neutral"
_trend_15m_cache = 0.0
_last_profile_ts = 0.0
_last_scan_status_ts = 0.0
_exchange_info_cache = None
_ed25519_private_key = None
_fear_cache = {"ts": 0.0, "index": None, "source": None, "label": None}
_leverage_configured = False
_next_leverage_retry_ts = 0.0


def round_price(value):
    return round(float(value), PRICE_DECIMALS)


# =========================
# BINANCE FUTURES (Ed25519)
# =========================
def load_ed25519_private_key():
    global _ed25519_private_key
    if _ed25519_private_key is not None:
        return _ed25519_private_key
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem_path = ED25519_KEY_PATH
    if not pem_path or not os.path.isfile(pem_path):
        return None
    with open(pem_path, "rb") as f:
        _ed25519_private_key = load_pem_private_key(f.read(), password=None)
    return _ed25519_private_key


def live_trading_ready():
    return LIVE_TRADE and bool(API_KEY) and os.path.isfile(ED25519_KEY_PATH)


def live_config_missing():
    missing = []
    if LIVE_TRADE and not API_KEY:
        missing.append("BINANCE_API_KEY")
    if LIVE_TRADE and not os.path.isfile(ED25519_KEY_PATH):
        missing.append(f"Ed25519 PEM ({ED25519_KEY_PATH})")
    return missing


def signed_query(params=None):
    private_key = load_ed25519_private_key()
    if private_key is None:
        raise ValueError("Ed25519 private key not configured")

    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    query = urlencode(params)
    signature = base64.b64encode(private_key.sign(query.encode("utf-8"))).decode("ascii")
    return f"{query}&signature={quote(signature, safe='')}"


def futures_request(method, path, params=None, signed=False):
    url = f"{FUTURES_BASE}{path}"
    headers = {"X-MBX-APIKEY": API_KEY} if API_KEY else {}
    params = dict(params or {})

    if signed:
        if not API_KEY:
            return None, "BINANCE_API_KEY required for signed request"
        try:
            query = signed_query(params)
        except ValueError as exc:
            return None, str(exc)
        url = f"{url}?{query}"
        r = requests.request(method, url, headers=headers, timeout=API_TIMEOUT)
    else:
        r = requests.request(method, url, headers=headers, params=params, timeout=API_TIMEOUT)

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        return None, body
    return body, None


def get_exchange_info(symbol):
    global _exchange_info_cache
    if _exchange_info_cache:
        return _exchange_info_cache
    data, err = futures_request("GET", "/fapi/v1/exchangeInfo")
    if err:
        return None
    for item in data.get("symbols", []):
        if item.get("symbol") == symbol:
            _exchange_info_cache = item
            return item
    return None


def _precision_from_filter(filters, name, key):
    for f in filters:
        if f.get("filterType") == name:
            return float(f.get(key, 0))
    return None


def format_price(symbol, price):
    info = get_exchange_info(symbol) or {}
    tick = _precision_from_filter(info.get("filters", []), "PRICE_FILTER", "tickSize") or 0.01
    prec = max(0, int(round(-math.log10(tick)))) if tick > 0 else PRICE_DECIMALS
    return f"{float(price):.{prec}f}"


def format_qty(symbol, qty):
    info = get_exchange_info(symbol) or {}
    step = _precision_from_filter(info.get("filters", []), "LOT_SIZE", "stepSize") or 0.01
    prec = max(0, int(round(-math.log10(step)))) if step > 0 else 1
    q = max(float(qty), step)
    # Dùng floor để không vô tình tăng notional so với amount/leverage mong muốn.
    steps = max(1, math.floor((q / step) + 1e-12))
    q = steps * step
    return f"{q:.{prec}f}"


def fetch_order(symbol, order_id):
    return futures_request(
        "GET",
        "/fapi/v1/order",
        {"symbol": symbol, "orderId": int(order_id)},
        signed=True,
    )


def cancel_order(symbol, order_id):
    return futures_request(
        "DELETE",
        "/fapi/v1/order",
        {"symbol": symbol, "orderId": int(order_id)},
        signed=True,
    )


def place_futures_order(params):
    return futures_request("POST", "/fapi/v1/order", params, signed=True)


def place_futures_algo_order(params):
    """Place USD-M Futures conditional TP/SL using the Algo Order endpoint.

    Binance error -4120 on /fapi/v1/order means conditional types such as
    STOP_MARKET / TAKE_PROFIT_MARKET must be sent to /fapi/v1/algoOrder.
    Algo endpoint uses triggerPrice instead of stopPrice and requires
    algoType=CONDITIONAL.
    """
    return futures_request("POST", "/fapi/v1/algoOrder", params, signed=True)


def fetch_algo_order(symbol, algo_id):
    return futures_request(
        "GET",
        "/fapi/v1/algoOrder",
        {"symbol": symbol, "algoId": int(algo_id)},
        signed=True,
    )


def cancel_algo_order(symbol, algo_id):
    return futures_request(
        "DELETE",
        "/fapi/v1/algoOrder",
        {"symbol": symbol, "algoId": int(algo_id)},
        signed=True,
    )


def get_position_rows(symbol):
    data, err = futures_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    if err or not isinstance(data, list):
        return [], err
    return [row for row in data if row.get("symbol") == symbol], None


def get_active_position(symbol, side=None):
    """Return active position row + abs qty. Supports One-way and Hedge mode."""
    rows, err = get_position_rows(symbol)
    if err:
        return None, 0.0, err

    expected_side = None
    if side:
        expected_side = "LONG" if side.upper() == "LONG" else "SHORT"

    best_row = None
    best_qty = 0.0
    for row in rows:
        try:
            amt = float(row.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            continue
        qty = abs(amt)
        if qty <= 0:
            continue

        pos_side = row.get("positionSide", "BOTH")
        if expected_side and pos_side in ("LONG", "SHORT") and pos_side != expected_side:
            continue
        if expected_side and pos_side == "BOTH":
            if expected_side == "LONG" and amt < 0:
                continue
            if expected_side == "SHORT" and amt > 0:
                continue

        if qty > best_qty:
            best_row = row
            best_qty = qty

    return best_row, best_qty, None


def bracket_position_side(trade, position_row):
    if POSITION_SIDE_MODE == "HEDGE":
        return "LONG" if trade["side"].upper() == "LONG" else "SHORT"
    if POSITION_SIDE_MODE == "ONEWAY":
        return None
    if position_row and position_row.get("positionSide") in ("LONG", "SHORT"):
        return position_row.get("positionSide")
    return None


def stop_price_valid(side, order_kind, stop_price, current_price):
    # Avoid Binance -2021: Order would immediately trigger.
    side = side.upper()
    stop_price = float(stop_price)
    current_price = float(current_price)
    if side == "LONG":
        return stop_price < current_price if order_kind == "SL" else stop_price > current_price
    return stop_price > current_price if order_kind == "SL" else stop_price < current_price


def has_open_position(symbol):
    _, qty, _ = get_active_position(symbol)
    return qty > 0


def set_symbol_leverage(symbol=SYMBOL, leverage=LEVERAGE):
    body, err = futures_request(
        "POST",
        "/fapi/v1/leverage",
        {"symbol": symbol, "leverage": int(leverage)},
        signed=True,
    )
    if body is None:
        return False, err
    return True, body


def ensure_symbol_leverage_once(symbol=SYMBOL, leverage=LEVERAGE):
    """Cache successful leverage setup to reduce Binance private API calls."""
    global _leverage_configured, _next_leverage_retry_ts

    if _leverage_configured:
        return True, {"cached": True, "leverage": int(leverage)}

    now = time.time()
    if _next_leverage_retry_ts and now < _next_leverage_retry_ts:
        wait = round(_next_leverage_retry_ts - now, 1)
        return False, {
            "stage": "set_leverage_cooldown",
            "reason": f"previous set leverage failed; retry after {wait}s",
            "configured_leverage": int(leverage),
        }

    ok, result = set_symbol_leverage(symbol, leverage)
    if ok:
        _leverage_configured = True
        _next_leverage_retry_ts = 0.0
        return True, result

    _next_leverage_retry_ts = now + LEVERAGE_RETRY_SEC
    return False, {
        "stage": "set_leverage",
        "error": result,
        "reason": "abort_entry_to_avoid_wrong_qty" if ABORT_ON_LEVERAGE_FAIL else "leverage_failed_continue_disabled",
        "configured_leverage": int(leverage),
        "retry_sec": LEVERAGE_RETRY_SEC,
    }


def target_notional_usdt():
    if SIZING_MODE in ("margin", "cost"):
        return ORDER_USDT * LEVERAGE
    return ORDER_USDT


def calc_order_qty(price):
    if ORDER_QTY_FIXED is not None:
        return format_qty(SYMBOL, ORDER_QTY_FIXED), target_notional_usdt(), "fixed_qty"

    p = float(price)
    if p <= 0:
        raise ValueError(f"invalid price for qty sizing: {price}")

    notional = target_notional_usdt()
    qty = format_qty(SYMBOL, notional / p)
    mode = "margin_x_lev" if SIZING_MODE in ("margin", "cost") else "notional"
    return qty, notional, mode


# =========================
# HTTP / MARKET
# =========================
def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=15)
        return r.json()
    except Exception:
        return None


def get_klines(symbol, tf="15m", limit=20):
    url = f"{FUTURES_BASE}/fapi/v1/klines"
    data = safe_get(url, {"symbol": symbol, "interval": tf, "limit": limit})
    if not isinstance(data, list):
        return [], []
    closes = [float(x[4]) for x in data]
    vols = [float(x[5]) for x in data]
    return closes, vols


def get_klines_ohlc(symbol, tf="15m", limit=20):
    url = f"{FUTURES_BASE}/fapi/v1/klines"
    data = safe_get(url, {"symbol": symbol, "interval": tf, "limit": limit})
    if not isinstance(data, list):
        return []
    rows = []
    for x in data:
        try:
            rows.append({
                "open_time": int(x[0]),
                "open": float(x[1]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "volume": float(x[5]),
                "close_time": int(x[6]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def get_mark_price(symbol):
    url = f"{FUTURES_BASE}/fapi/v1/premiumIndex"
    data = safe_get(url, {"symbol": symbol})
    if not isinstance(data, dict):
        return None
    try:
        return float(data["markPrice"])
    except (TypeError, ValueError, KeyError):
        return None


def get_last_price(symbol):
    url = f"{FUTURES_BASE}/fapi/v1/ticker/price"
    data = safe_get(url, {"symbol": symbol})
    if not isinstance(data, dict):
        return None
    try:
        return float(data["price"])
    except (TypeError, ValueError, KeyError):
        return None


def fetch_price(symbol):
    return get_mark_price(symbol) or get_last_price(symbol)


# =========================
# VOLUME PROFILE (from v18_advanced_v2)
# =========================
def pip_size_for_symbol(symbol):
    if symbol.startswith("BTC"):
        return 0.001
    if symbol.startswith("ETH"):
        return 0.01
    if symbol.startswith("XAU"):
        return 0.01
    if symbol.endswith("USDT"):
        return 0.01
    return 0.0001


def empty_volume_profile():
    return {
        "available": False,
        "poc": None,
        "value_area_low": None,
        "value_area_high": None,
        "high_volume_zones": [],
        "low_volume_zones": [],
    }


def volume_profile(prices, volumes, symbol, current_price, bins=32, value_area_ratio=0.7):
    if len(prices) < 10 or len(volumes) < 10:
        return empty_volume_profile()

    low = float(min(prices))
    high = float(max(prices))
    if high <= low:
        return empty_volume_profile()

    bucket_step = max((high - low) / bins, pip_size_for_symbol(symbol) * 20)
    bucket_count = max(int(math.ceil((high - low) / bucket_step)), 1)
    buckets = [
        {"low": low + idx * bucket_step, "high": low + (idx + 1) * bucket_step, "volume": 0.0}
        for idx in range(bucket_count)
    ]

    for price, volume in zip(prices, volumes):
        idx = min(int((float(price) - low) / bucket_step), bucket_count - 1)
        buckets[idx]["volume"] += float(volume)

    total_volume = sum(bucket["volume"] for bucket in buckets) + 1e-9
    nodes = []
    for bucket in buckets:
        center = (bucket["low"] + bucket["high"]) / 2.0
        nodes.append({
            "low": round(bucket["low"], PRICE_DECIMALS),
            "high": round(bucket["high"], PRICE_DECIMALS),
            "price": round(center, PRICE_DECIMALS),
            "volume": round(bucket["volume"], 3),
            "volume_share": round(bucket["volume"] / total_volume, 4),
        })

    active_nodes = [node for node in nodes if node["volume"] > 0]
    by_volume = sorted(active_nodes, key=lambda item: item["volume"], reverse=True)
    high_volume_zones = by_volume[:6]
    low_volume_zones = list(reversed(by_volume))[:6]

    value_area = []
    cumulative = 0.0
    for node in by_volume:
        value_area.append(node)
        cumulative += node["volume_share"]
        if cumulative >= value_area_ratio:
            break

    return {
        "available": True,
        "poc": high_volume_zones[0]["price"] if high_volume_zones else None,
        "value_area_low": min((node["low"] for node in value_area), default=None),
        "value_area_high": max((node["high"] for node in value_area), default=None),
        "high_volume_zones": high_volume_zones,
        "low_volume_zones": low_volume_zones,
    }


def volume_zone_map(symbol, current_price):
    p4h, v4h = get_klines(symbol, "4h", 180)
    p1d, v1d = get_klines(symbol, "1d", 180)

    profiles = {
        "4h": volume_profile(p4h, v4h, symbol, current_price, bins=32),
        "1d": volume_profile(p1d, v1d, symbol, current_price, bins=36),
    }

    primary_timeframe = "4h" if profiles["4h"]["available"] else ("1d" if profiles["1d"]["available"] else None)
    primary = profiles[primary_timeframe] if primary_timeframe else empty_volume_profile()

    high_zones = []
    low_zones = []
    for timeframe in ("4h", "1d"):
        profile = profiles[timeframe]
        for node in profile["high_volume_zones"][:4]:
            high_zones.append({"timeframe": timeframe, **node})
        for node in profile["low_volume_zones"][:4]:
            low_zones.append({"timeframe": timeframe, **node})

    return {
        "available": bool(primary_timeframe),
        "primary_timeframe": primary_timeframe,
        "poc": primary["poc"],
        "value_area_low": primary["value_area_low"],
        "value_area_high": primary["value_area_high"],
        "high_volume_zones": high_zones,
        "low_volume_zones": low_zones,
    }


def detect_trend(symbol):
    p15, _ = get_klines(symbol, "15m", 30)
    p1h, _ = get_klines(symbol, "1h", 30)
    if len(p15) < 6 or len(p1h) < 6:
        return "neutral", 0.0
    trend_15m = p15[-1] - p15[-5]
    trend_1h = p1h[-1] - p1h[-5]
    if trend_15m > 0 and trend_1h >= 0:
        return "bullish", trend_15m
    if trend_15m < 0 and trend_1h <= 0:
        return "bearish", trend_15m
    return "neutral", trend_15m


def opposite_side(side):
    return "SHORT" if str(side).upper() == "LONG" else "LONG"


def resolve_trend_side(trend, trend_15m, price, level_price):
    """Trend-following side gốc của script cũ."""
    if trend == "bullish":
        return "LONG"
    if trend == "bearish":
        return "SHORT"
    if trend_15m > 0:
        return "LONG"
    if trend_15m < 0:
        return "SHORT"
    return "LONG" if float(price) <= float(level_price) else "SHORT"


def candle_rejection_confirms_side(candle, side, level_price):
    """
    Rejection confirmation đơn giản từ candle 15m:
    - SHORT: wick quét lên/touch level rồi close lại dưới level, upper wick đủ lớn.
    - LONG: wick quét xuống/touch level rồi close lại trên level, lower wick đủ lớn.
    """
    side = side.upper()
    level = float(level_price)
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])
    body = max(abs(c - o), pip_size_for_symbol(SYMBOL) * 5)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if side == "SHORT":
        return h >= level and c < level and upper_wick >= body * REJECTION_WICK_RATIO
    return l <= level and c > level and lower_wick >= body * REJECTION_WICK_RATIO


def has_rejection_confirmation(side, level_price, tf="15m"):
    candles = get_klines_ohlc(SYMBOL, tf, max(REJECTION_LOOKBACK_CANDLES + 2, 5))
    if len(candles) < 2:
        return False
    # Bỏ candle hiện tại có thể chưa đóng; dùng các candle đã đóng gần nhất.
    closed = candles[:-1][-REJECTION_LOOKBACK_CANDLES:]
    return any(candle_rejection_confirms_side(c, side, level_price) for c in closed)


def retest_from_below(level_price, price, lookback=None):
    """SHORT bearish edge: giá từng ở dưới level và hiện retest lên vùng level từ bên dưới."""
    lookback = lookback or RETEST_LOOKBACK_CANDLES
    closes, _ = get_klines(SYMBOL, "15m", max(lookback + 2, 10))
    if len(closes) < 3:
        return False, "not_enough_15m_data"

    level = float(level_price)
    recent_closed = closes[:-1][-lookback:]
    was_below = any(c < level for c in recent_closed)
    now_at_or_below_level = float(price) <= level + ZONE_TOUCH_TOLERANCE
    if not was_below or not now_at_or_below_level:
        return False, "not_retest_from_below"

    recent_low = min(recent_closed)
    impulse_points = abs(float(price) - recent_low)
    if impulse_points > MAX_RETEST_IMPULSE_POINTS:
        return False, f"late_impulse_{impulse_points:.3f}pt_gt_{MAX_RETEST_IMPULSE_POINTS}"

    return True, f"retest_from_below_impulse_{impulse_points:.3f}pt"


def retest_from_above(level_price, price, lookback=None):
    """LONG bullish edge: giá từng ở trên level và hiện pullback/retest xuống vùng level từ bên trên.

    Đây là mirror có kiểm soát của retest_from_below(), dùng cho bullish reclaim-retest:
    - was_above: thị trường đã đóng trên level trong lookback.
    - now_at_or_above_level: giá hiện tại vẫn đang test gần level từ phía trên.
    - pullback_points: không được quá xa từ recent_high để tránh long late-pullback.
    - optional acceptance: nến 15m đã đóng gần nhất không được đóng sâu dưới level.
    """
    lookback = lookback or RETEST_LOOKBACK_CANDLES
    closes, _ = get_klines(SYMBOL, "15m", max(lookback + 2, 10))
    if len(closes) < 3:
        return False, "not_enough_15m_data"

    level = float(level_price)
    recent_closed = closes[:-1][-lookback:]
    was_above = any(c > level for c in recent_closed)
    now_at_or_above_level = float(price) >= level - ZONE_TOUCH_TOLERANCE
    if not was_above or not now_at_or_above_level:
        return False, "not_retest_from_above"

    last_closed = recent_closed[-1]
    if LONG_RETEST_REQUIRE_ACCEPTANCE and last_closed < level - ZONE_TOUCH_TOLERANCE:
        return False, f"long_retest_no_acceptance_close_{last_closed:.3f}_below_{level:.3f}"

    recent_high = max(recent_closed)
    pullback_points = abs(recent_high - float(price))
    if pullback_points > MAX_RETEST_IMPULSE_POINTS:
        return False, f"late_pullback_{pullback_points:.3f}pt_gt_{MAX_RETEST_IMPULSE_POINTS}"

    return True, f"retest_from_above_pullback_{pullback_points:.3f}pt"


def is_long_bullish_4h_max_candidate(price, trend, trend_side, zone, level_name, level_price):
    if not ENABLE_LONG_BULLISH_4H_MAX_EDGE:
        return False, "long_edge_disabled"
    if trend != "bullish":
        return False, "reject_long_not_bullish"
    if trend_side != "LONG":
        return False, "reject_long_trend_side_not_long"
    if zone.get("timeframe") != "4h":
        return False, "reject_long_not_4h"
    if level_name != "max":
        return False, "reject_long_not_max"
    return retest_from_above(level_price, price)


def resolve_side(price, trend, trend_15m, zone_kind, zone, level_name, level_price):
    trend_side = resolve_trend_side(trend, trend_15m, price, level_price)
    rejection_confirmed = False
    strategy_rule = "trend_follow"

    # Rule 1: vào tại max theo hướng nghịch đảo; nếu có rejection thì đi theo hướng thuận.
    if ENABLE_MAX_INVERSE and level_name == "max":
        if ENABLE_REJECTION_FOLLOW_TREND:
            rejection_confirmed = has_rejection_confirmation(trend_side, level_price)
        if rejection_confirmed:
            return trend_side, trend_side, True, "max_rejection_follow_trend"
        return opposite_side(trend_side), trend_side, False, "max_inverse_no_rejection"

    return trend_side, trend_side, rejection_confirmed, strategy_rule


def validate_strategy_signal(signal, price):
    """Hard gate theo audit mới."""
    side = signal["side"]
    trend = signal["trend"]
    zone_tf = signal["zone"].get("timeframe")
    level_name = signal["level_name"]

    # Priority 1: experimental LONG bullish 4h max reclaim/retest.
    # Khi BTC_ENABLE_LONG_BULLISH_4H_MAX_EDGE=true và setup confirm,
    # edge này đã được build trước max-inverse nên không bị flip sang SHORT.
    if signal.get("strategy_rule") == "long_bullish_4h_max_retest":
        signal["side"] = "LONG"
        signal["retest_confirmed"] = True
        signal["retest_reason"] = signal.get("long_retest_reason")
        signal["gate_reason"] = "long_bullish_4h_max_retest_edge"
        return True, signal["gate_reason"]

    # Rule 1: max inverse/rejection được phép đi qua gate riêng.
    if level_name == "max" and ENABLE_MAX_INVERSE:
        signal["gate_reason"] = signal.get("strategy_rule", "max_rule")
        return True, signal["gate_reason"]

    # Rule 2 + 3: giữ edge LIVE 4h SHORT min only.
    if ENABLE_SHORT_BEARISH_4H_MIN_EDGE and side == "SHORT":
        if trend != "bearish":
            return False, "reject_short_not_bearish"
        if zone_tf != "4h":
            return False, "reject_short_not_4h"
        if level_name != "min":
            return False, "reject_short_not_min"
        ok, reason = retest_from_below(signal["level_price"], price)
        if not ok:
            return False, reason
        signal["retest_confirmed"] = True
        signal["retest_reason"] = reason
        signal["gate_reason"] = "short_bearish_4h_min_edge"
        return True, signal["gate_reason"]

    if STRICT_EDGE_ONLY:
        return False, "reject_not_in_enabled_edge"

    signal["gate_reason"] = "legacy_allowed"
    return True, signal["gate_reason"]


# =========================
# DISPLAY
# =========================
def nearest_zones(zones, price, limit=5):
    return sorted(
        zones,
        key=lambda zone: abs(float(zone.get("price", price)) - price),
    )[:limit]


def print_wide_volume_profile(volume_zone, price):
    print("\n📦 WIDE VOLUME PROFILE:")
    print(
        f"basis={volume_zone.get('primary_timeframe')} | poc={volume_zone.get('poc')} | "
        f"value_area={volume_zone.get('value_area_low')} -> {volume_zone.get('value_area_high')}"
    )
    for title, key in (("🔥 HIGH VOLUME ZONES:", "high_volume_zones"), ("⚡ LOW VOLUME ZONES:", "low_volume_zones")):
        print(title)
        rows = nearest_zones(volume_zone.get(key, []), price, 5)
        if not rows:
            print("  - none")
            continue
        for zone in rows:
            print(
                f"  - {zone.get('timeframe')} | {zone.get('low')} -> {zone.get('high')} | "
                f"mid={zone.get('price')} | vol={zone.get('volume')} | share={zone.get('volume_share')}"
            )


# =========================
# ZONE TOUCH + TRADE BUILD
# =========================
def zone_level_points(zone):
    return [
        ("min", float(zone["low"])),
        ("max", float(zone["high"])),
        ("mid", float(zone.get("price", (zone["low"] + zone["high"]) / 2.0))),
    ]


def distance_to_level(price, level):
    return abs(float(price) - float(level))


def tp_distance(zone_kind):
    return TP_LVN_POINTS if zone_kind == "lvn" else TP_HVN_POINTS


def fetch_fear_index_from_api():
    """Fetch Crypto Fear & Greed Index from CoinMarketCap. This is not a Binance API call."""
    if not CMC_PRO_API_KEY:
        print(f"⚠️ CoinMarketCap Fear API skipped: missing CMC_PRO_API_KEY; fallback={FEAR_DEFAULT_INDEX}")
        return None, None

    try:
        r = requests.get(
            FEAR_API_URL,
            headers={"X-CMC_PRO_API_KEY": CMC_PRO_API_KEY},
            timeout=FEAR_API_TIMEOUT,
        )
        if r.status_code >= 400:
            print(f"⚠️ CoinMarketCap Fear API failed: HTTP {r.status_code}; fallback={FEAR_DEFAULT_INDEX}")
            return None, None
        data = r.json()
        row = data.get("data") if isinstance(data, dict) else None
        if not isinstance(row, dict):
            return None, None
        value = int(float(row.get("value")))
        label = str(row.get("value_classification") or "")
        return max(0, min(100, value)), label
    except Exception as exc:
        print(f"⚠️ CoinMarketCap Fear API failed: {exc}; fallback={FEAR_DEFAULT_INDEX}")
        return None, None


def get_fear_index(force=False):
    """Cached Fear Index. Called only when building a trade signal, not every scan loop."""
    now = time.time()

    if not FEAR_ENABLED or FEAR_SOURCE == "OFF":
        return None, "OFF", "disabled"

    if FEAR_SOURCE == "ENV" or FEAR_INDEX_OVERRIDE:
        try:
            value = int(FEAR_INDEX_OVERRIDE or FEAR_DEFAULT_INDEX)
        except ValueError:
            value = FEAR_DEFAULT_INDEX
        return max(0, min(100, value)), "ENV", fear_label(max(0, min(100, value)))

    if (
        not force
        and _fear_cache.get("index") is not None
        and now - float(_fear_cache.get("ts") or 0) < FEAR_CACHE_SEC
    ):
        return _fear_cache["index"], _fear_cache.get("source") or "CACHE", _fear_cache.get("label") or fear_label(_fear_cache["index"])

    value, label = fetch_fear_index_from_api()
    if value is None:
        value = FEAR_DEFAULT_INDEX
        label = fear_label(value)
        source = "DEFAULT"
    else:
        source = "COINMARKETCAP"
        label = label or fear_label(value)

    _fear_cache.update({"ts": now, "index": value, "source": source, "label": label})
    return value, source, label


def fear_label(index):
    if index is None:
        return "OFF"
    index = int(index)
    if index <= 20:
        return "EXTREME_FEAR"
    if index <= 40:
        return "FEAR"
    if index <= 60:
        return "NEUTRAL"
    if index <= 80:
        return "GREED"
    return "EXTREME_GREED"


def fear_risk_context(side):
    """Return SL/TP/qty multipliers by Fear Index regime.

    Fear Index is a risk modifier, not a side signal.
    It should not override HVN/LVN, trend, retest, or rejection gates.
    """
    idx, source, label = get_fear_index()
    side = str(side).upper()

    # Defaults when disabled/unavailable.
    ctx = {
        "fear_index": idx,
        "fear_source": source,
        "fear_label": label,
        "sl_mult": 1.0,
        "tp_mult": 1.0,
        "qty_mult": 1.0,
        "regime": "NEUTRAL_OR_DISABLED",
    }

    if idx is None:
        return ctx

    # Extreme fear: volatile downside + snapback risk.
    if idx <= 20:
        if side == "SHORT":
            ctx.update({"sl_mult": 1.25, "tp_mult": 1.15, "qty_mult": 1.0, "regime": "EXTREME_FEAR_SHORT_CONTINUATION"})
        else:
            ctx.update({"sl_mult": 1.30, "tp_mult": 0.80, "qty_mult": 1.0, "regime": "EXTREME_FEAR_LONG_COUNTER"})
        return ctx

    # Fear: short bias, but still high wick risk.
    if idx <= 40:
        if side == "SHORT":
            ctx.update({"sl_mult": 1.15, "tp_mult": 1.10, "qty_mult": 1.0, "regime": "FEAR_SHORT_BIAS"})
        else:
            ctx.update({"sl_mult": 1.15, "tp_mult": 0.90, "qty_mult": 1.0, "regime": "FEAR_LONG_CAUTION"})
        return ctx

    # Neutral.
    if idx <= 60:
        ctx.update({"regime": "NEUTRAL"})
        return ctx

    # Greed: long bias, but avoid over-sizing.
    if idx <= 80:
        if side == "LONG":
            ctx.update({"sl_mult": 1.10, "tp_mult": 1.10, "qty_mult": 1.0, "regime": "GREED_LONG_BIAS"})
        else:
            ctx.update({"sl_mult": 1.10, "tp_mult": 0.90, "qty_mult": 1.0, "regime": "GREED_SHORT_CAUTION"})
        return ctx

    # Extreme greed: both long-chase and counter-short are risky.
    if side == "LONG":
        ctx.update({"sl_mult": 1.25, "tp_mult": 0.90, "qty_mult": 1.0, "regime": "EXTREME_GREED_LONG_CHASE_RISK"})
    else:
        ctx.update({"sl_mult": 1.30, "tp_mult": 0.85, "qty_mult": 1.0, "regime": "EXTREME_GREED_SHORT_COUNTER"})
    return ctx


def apply_fear_to_distances(side, zone_kind):
    base_tp = tp_distance(zone_kind)
    base_sl = SL_POINTS
    ctx = fear_risk_context(side)
    tp_points = base_tp * float(ctx.get("tp_mult", 1.0))
    sl_points = base_sl * float(ctx.get("sl_mult", 1.0))
    return base_tp, base_sl, tp_points, sl_points, ctx


def bracket_levels(entry, side, zone_kind, sl_points=None, tp_points=None):
    """SL/TP từ entry thực (giá BTC, đơn vị USD)."""
    entry = float(entry)
    side = side.upper()
    tp_dist = float(tp_points if tp_points is not None else tp_distance(zone_kind))
    sl_dist = float(sl_points if sl_points is not None else SL_POINTS)
    if side == "LONG":
        return round_price(entry + tp_dist), round_price(entry - sl_dist), tp_dist, sl_dist
    return round_price(entry - tp_dist), round_price(entry + sl_dist), tp_dist, sl_dist


def limit_entry_price(signal, price):
    """
    Tính giá LIMIT entry theo signal.
    LONG  -> BUY LIMIT thấp hơn signal/current một offset.
    SHORT -> SELL LIMIT cao hơn signal/current một offset.
    offset=0 giữ đúng vùng touch đang phát hiện.
    """
    side = signal["side"].upper()
    base = float(signal.get("entry") or price)
    if side == "LONG":
        return round_price(base - LIMIT_ENTRY_OFFSET)
    return round_price(base + LIMIT_ENTRY_OFFSET)


def build_touch_signal(price, trend, trend_15m, zone_kind, zone, level_name, level_price):
    trend_side = resolve_trend_side(trend, trend_15m, price, level_price)
    long_ok, long_reason = is_long_bullish_4h_max_candidate(
        price, trend, trend_side, zone, level_name, level_price
    )
    if long_ok:
        # Priority: confirmed bullish reclaim-retest wins before max-inverse can flip max into SHORT.
        side = "LONG"
        rejection_confirmed = False
        strategy_rule = "long_bullish_4h_max_retest"
        long_retest_confirmed = True
        long_retest_reason = long_reason
    else:
        side, trend_side, rejection_confirmed, strategy_rule = resolve_side(
            price, trend, trend_15m, zone_kind, zone, level_name, level_price
        )
        long_retest_confirmed = False
        long_retest_reason = long_reason

    entry = float(price)

    base_tp_dist, base_sl_dist, tp_dist, sl_dist, fear_ctx = apply_fear_to_distances(side, zone_kind)
    tp, sl, tp_dist, sl_dist = bracket_levels(entry, side, zone_kind, sl_points=sl_dist, tp_points=tp_dist)

    return {
        "side": side,
        "trend_side": trend_side,
        "trend": trend,
        "zone_kind": zone_kind,
        "zone": zone,
        "level_name": level_name,
        "level_price": level_price,
        "entry": round_price(entry),
        "sl": sl,
        "tp": tp,
        "tp_points": tp_dist,
        "sl_points": sl_dist,
        "base_tp_points": base_tp_dist,
        "base_sl_points": base_sl_dist,
        "fear_index": fear_ctx.get("fear_index"),
        "fear_source": fear_ctx.get("fear_source"),
        "fear_label": fear_ctx.get("fear_label"),
        "fear_regime": fear_ctx.get("regime"),
        "fear_sl_mult": fear_ctx.get("sl_mult", 1.0),
        "fear_tp_mult": fear_ctx.get("tp_mult", 1.0),
        "fear_qty_mult": fear_ctx.get("qty_mult", 1.0),
        "touch_distance": round(distance_to_level(price, level_price), PRICE_DECIMALS),
        "rejection_confirmed": rejection_confirmed,
        "long_retest_confirmed": long_retest_confirmed,
        "long_retest_reason": long_retest_reason,
        "strategy_rule": strategy_rule,
    }


def scan_zone_touch(price, volume_zone, trend, trend_15m):
    if not volume_zone.get("available"):
        return None

    candidates = []
    rejected = []
    for zone_kind, zones in (
        ("hvn", volume_zone.get("high_volume_zones", [])),
        ("lvn", volume_zone.get("low_volume_zones", [])),
    ):
        for zone in zones:
            for level_name, level_price in zone_level_points(zone):
                dist = distance_to_level(price, level_price)
                if dist <= ZONE_TOUCH_TOLERANCE:
                    signal = build_touch_signal(
                        price, trend, trend_15m, zone_kind, zone, level_name, level_price
                    )
                    ok, reason = validate_strategy_signal(signal, price)
                    if ok:
                        candidates.append(signal)
                    else:
                        signal["reject_reason"] = reason
                        rejected.append(signal)

    if not candidates:
        if rejected:
            best_reject = min(rejected, key=lambda item: item["touch_distance"])
            print(
                f"⚠️ signal rejected | {best_reject['side']} {best_reject['zone_kind'].upper()} "
                f"{best_reject['level_name']}={best_reject['level_price']} "
                f"tf={best_reject['zone'].get('timeframe')} rule={best_reject.get('strategy_rule')} "
                f"reason={best_reject.get('reject_reason')}"
            )
        return None
    return min(candidates, key=lambda item: item["touch_distance"])


def nearest_touch_candidate(price, volume_zone):
    if not volume_zone.get("available"):
        return None
    best = None
    for zone_kind, zones in (
        ("hvn", volume_zone.get("high_volume_zones", [])),
        ("lvn", volume_zone.get("low_volume_zones", [])),
    ):
        for zone in zones:
            for level_name, level_price in zone_level_points(zone):
                dist = distance_to_level(price, level_price)
                row = {
                    "distance": dist,
                    "zone_kind": zone_kind,
                    "level_name": level_name,
                    "level_price": level_price,
                    "timeframe": zone.get("timeframe"),
                }
                if best is None or dist < best["distance"]:
                    best = row
    return best


def maybe_print_scan_status(price, volume_zone, cycle_ts):
    global _last_scan_status_ts
    now = time.time()
    if now - _last_scan_status_ts < SCAN_STATUS_SEC:
        return
    _last_scan_status_ts = now

    workflow = "no"
    if ACTIVE_TRADE:
        workflow = f"{ACTIVE_TRADE.get('status')}#{ACTIVE_TRADE.get('id')}"
        if ACTIVE_TRADE.get("mode") == "LIVE":
            workflow += f" bracket={ACTIVE_TRADE.get('bracket_status', 'NA')}"

    touch = nearest_touch_candidate(price, volume_zone)
    if not touch:
        print(f"[{cycle_ts}] 🔍 scan price={price:.{PRICE_DECIMALS}f} | workflow={workflow} | nearest touch: no HVN/LVN zones")
        return

    dist = touch["distance"]
    ready = dist <= ZONE_TOUCH_TOLERANCE
    print(
        f"[{cycle_ts}] 🔍 scan price={price:.{PRICE_DECIMALS}f} | workflow={workflow} | "
        f"nearest={dist:.{PRICE_DECIMALS}f}pt away "
        f"({touch['zone_kind'].upper()} {touch['level_name']}={touch['level_price']} "
        f"{touch.get('timeframe') or ''}) | need ≤{ZONE_TOUCH_TOLERANCE}pt | "
        f"{'✓ READY' if ready else 'waiting'}"
    )


# =========================
# TRADE (SIM + LIVE)
# =========================
def has_active_workflow():
    if ACTIVE_TRADE is None:
        return False
    return ACTIVE_TRADE.get("status") in ("PENDING_ENTRY", "OPEN")


def has_open_trade():
    return ACTIVE_TRADE is not None and ACTIVE_TRADE.get("status") == "OPEN"


def trade_base(signal, price, cycle_ts):
    return {
        "symbol": SYMBOL,
        "side": signal["side"],
        "trend": signal["trend"],
        "zone_kind": signal["zone_kind"],
        "level_name": signal["level_name"],
        "level_price": signal["level_price"],
        "trend_side": signal.get("trend_side"),
        "strategy_rule": signal.get("strategy_rule"),
        "gate_reason": signal.get("gate_reason"),
        "rejection_confirmed": signal.get("rejection_confirmed", False),
        "retest_confirmed": signal.get("retest_confirmed", False),
        "retest_reason": signal.get("retest_reason"),
        "long_retest_confirmed": signal.get("long_retest_confirmed", False),
        "long_retest_reason": signal.get("long_retest_reason"),
        "zone_timeframe": signal["zone"].get("timeframe"),
        "zone_low": signal["zone"].get("low"),
        "zone_high": signal["zone"].get("high"),
        "planned_entry": signal["entry"],
        "sl": signal["sl"],
        "tp": signal["tp"],
        "tp_points": signal["tp_points"],
        "sl_points": signal["sl_points"],
        "base_tp_points": signal.get("base_tp_points"),
        "base_sl_points": signal.get("base_sl_points"),
        "fear_index": signal.get("fear_index"),
        "fear_source": signal.get("fear_source"),
        "fear_label": signal.get("fear_label"),
        "fear_regime": signal.get("fear_regime"),
        "fear_sl_mult": signal.get("fear_sl_mult", 1.0),
        "fear_tp_mult": signal.get("fear_tp_mult", 1.0),
        "fear_qty_mult": signal.get("fear_qty_mult", 1.0),
        "signal_price": round_price(price),
        "opened_ts": cycle_ts,
        "opened_ts_ms": int(time.time() * 1000),
    }


def trade_hit(trade, price):
    side = trade["side"]
    if side == "LONG":
        if price >= trade["tp"]:
            return "TP_HIT", trade["tp"]
        if price <= trade["sl"]:
            return "SL_HIT", trade["sl"]
    else:
        if price <= trade["tp"]:
            return "TP_HIT", trade["tp"]
        if price >= trade["sl"]:
            return "SL_HIT", trade["sl"]
    return None, None


def open_sim_trade(signal, price, cycle_ts):
    global TRADE_COUNTER, ACTIVE_TRADE

    TRADE_COUNTER += 1
    ACTIVE_TRADE = {
        "id": TRADE_COUNTER,
        "mode": "SIM",
        "status": "OPEN",
        **trade_base(signal, price, cycle_ts),
        "entry": signal["entry"],
        "opened_price": round_price(price),
    }
    append_trade_log({"event": "OPEN", **ACTIVE_TRADE})
    print(
        f"\n🎯 SIM OPEN #{ACTIVE_TRADE['id']} | {ACTIVE_TRADE['side']} @ {ACTIVE_TRADE['entry']} | "
        f"{signal['zone_kind'].upper()} {signal['level_name']}={signal['level_price']} | "
        f"TP={ACTIVE_TRADE['tp']} (+{signal['tp_points']}pt) | SL={ACTIVE_TRADE['sl']} (-{signal['sl_points']}pt) | "
        f"fear={signal.get('fear_index')} {signal.get('fear_label')} sl×{signal.get('fear_sl_mult')} tp×{signal.get('fear_tp_mult')} | "
        f"trend={signal['trend']} | rule={signal.get('strategy_rule')} | gate={signal.get('gate_reason')}"
    )
    return ACTIVE_TRADE


def place_live_entry(signal, price):
    ok, lev_info = ensure_symbol_leverage_once()
    if not ok:
        if ABORT_ON_LEVERAGE_FAIL:
            return None, lev_info
        print(f"⚠️ set leverage {LEVERAGE}x failed: {lev_info} (continuing disabled by config)")

    order_side = "BUY" if signal["side"] == "LONG" else "SELL"

    # Giữ nguyên logic amount/leverage: notional = ORDER_USDT × LEVERAGE khi SIZING_MODE=margin.
    # Nếu LIMIT thì tính qty theo limit_price để notional sát hơn với giá entry dự kiến.
    entry_type = ENTRY_ORDER_TYPE if ENTRY_ORDER_TYPE in ("LIMIT", "MARKET") else "LIMIT"
    entry_price = limit_entry_price(signal, price) if entry_type == "LIMIT" else float(price)

    try:
        qty, notional, sizing_mode = calc_order_qty(entry_price)
    except ValueError as exc:
        return None, str(exc)

    qty_mult = float(signal.get("fear_qty_mult") or 1.0)
    if FEAR_ENABLED and FEAR_ADJUST_QTY and qty_mult > 0 and qty_mult != 1.0:
        raw_qty = float(qty)
        qty = format_qty(SYMBOL, raw_qty * qty_mult)
        notional = float(qty) * float(entry_price)
        sizing_mode = f"{sizing_mode}_fear_qty_{qty_mult:.2f}"

    if entry_type == "LIMIT":
        params = {
            "symbol": SYMBOL,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty,
            "price": format_price(SYMBOL, entry_price),
        }
    else:
        params = {
            "symbol": SYMBOL,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty,
        }

    meta = {
        "qty": qty,
        "notional_usdt": round(notional, 2),
        "sizing_mode": sizing_mode,
        "entry_order_type": entry_type,
        "limit_price": params.get("price"),
        "fear_qty_mult": qty_mult,
    }
    body, err = place_futures_order(params)
    if body is None:
        return None, err
    return body, meta


def build_bracket_order_params(trade, order_kind, order_type, stop_price, qty, position_side, mode):
    exit_side = "SELL" if trade["side"].upper() == "LONG" else "BUY"

    # /fapi/v1/algoOrder uses triggerPrice + algoType=CONDITIONAL.
    # Legacy /fapi/v1/order uses stopPrice. Keep both so we can switch by env.
    use_algo = BRACKET_API == "ALGO"
    params = {
        "symbol": SYMBOL,
        "side": exit_side,
        "type": order_type,
        "workingType": BRACKET_WORKING_TYPE,
    }
    if use_algo:
        params["algoType"] = "CONDITIONAL"
        params["triggerPrice"] = format_price(SYMBOL, stop_price)
    else:
        params["stopPrice"] = format_price(SYMBOL, stop_price)

    if position_side:
        params["positionSide"] = position_side

    if mode == "CLOSE_POSITION":
        params["closePosition"] = "true"
    else:
        params["quantity"] = qty
        # reduceOnly is invalid in Hedge Mode, but useful in One-way Mode.
        if not position_side:
            params["reduceOnly"] = "true"
    return params


def submit_bracket_order(params):
    if BRACKET_API == "ALGO":
        return place_futures_algo_order(params)
    return place_futures_order(params)


def bracket_response_id(body):
    if not isinstance(body, dict):
        return None
    # Algo endpoint returns algoId; regular order endpoint returns orderId.
    return body.get("algoId") or body.get("orderId")


def fetch_bracket_order(symbol, bracket_id):
    if BRACKET_API == "ALGO":
        return fetch_algo_order(symbol, bracket_id)
    return fetch_order(symbol, bracket_id)


def cancel_bracket_order(symbol, bracket_id):
    if BRACKET_API == "ALGO":
        return cancel_algo_order(symbol, bracket_id)
    return cancel_order(symbol, bracket_id)


def _bracket_order_filled(data):
    if not isinstance(data, dict):
        return False
    # Regular endpoint: status=FILLED. Algo endpoint may expose terminal algoStatus.
    return data.get("status") == "FILLED" or data.get("algoStatus") in ("TRIGGERED", "FINISHED", "FILLED")


def place_single_bracket_order(trade, order_kind, order_type, stop_price, qty, position_side, current_price):
    if not stop_price_valid(trade["side"], order_kind, stop_price, current_price):
        return None, {
            "stage": "validate",
            "reason": f"{order_kind.lower()}_would_immediately_trigger",
            "stop_price": format_price(SYMBOL, stop_price),
            "current_price": current_price,
            "side": trade.get("side"),
        }

    modes = []
    if BRACKET_MODE == "CLOSE_POSITION" and BRACKET_CLOSE_POSITION:
        modes = ["CLOSE_POSITION"]
    elif BRACKET_MODE == "QTY":
        modes = ["QTY"]
    elif BRACKET_MODE == "CLOSE_POSITION" and not BRACKET_CLOSE_POSITION:
        modes = ["QTY"]
    else:
        modes = ["CLOSE_POSITION", "QTY"] if BRACKET_CLOSE_POSITION else ["QTY"]

    last_err = None
    for mode in modes:
        params = build_bracket_order_params(
            trade, order_kind, order_type, stop_price, qty, position_side, mode
        )
        body, err = submit_bracket_order(params)
        if body is not None:
            return body, None
        last_err = {"mode": mode, "params": params, "error": err}
        print(f"⚠️ {order_kind} bracket failed mode={mode}: {err}")
    return None, last_err


def place_live_bracket(trade):
    # Đọc position thật sau khi FILLED để tránh qty mismatch/rounding/partial fill.
    position_row, position_qty, pos_err = get_active_position(SYMBOL, trade.get("side"))
    if pos_err:
        err = {"stage": "positionRisk", "error": pos_err}
        return None, err, None, err
    if position_qty <= 0:
        err = {"stage": "positionRisk", "error": "no_active_position_after_fill"}
        return None, err, None, err

    qty = format_qty(SYMBOL, position_qty)
    position_side = bracket_position_side(trade, position_row)
    current_price = fetch_price(SYMBOL) or trade.get("entry") or trade.get("signal_price")

    print(
        f"🛡️ BRACKET TRY #{trade.get('id')} | side={trade.get('side')} qty={qty} "
        f"posSide={position_side or 'BOTH'} current={current_price} "
        f"SL={trade['sl']} TP={trade['tp']} mode={BRACKET_MODE} api={BRACKET_API} workingType={BRACKET_WORKING_TYPE}"
    )

    sl_body = sl_err = tp_body = tp_err = None

    if not trade.get("sl_order_id"):
        sl_body, sl_err = place_single_bracket_order(
            trade, "SL", "STOP_MARKET", trade["sl"], qty, position_side, current_price
        )
    if not trade.get("tp_order_id"):
        tp_body, tp_err = place_single_bracket_order(
            trade, "TP", "TAKE_PROFIT_MARKET", trade["tp"], qty, position_side, current_price
        )

    return sl_body, sl_err, tp_body, tp_err


def _record_bracket_bodies(trade, sl_body, tp_body):
    if sl_body is not None and not trade.get("sl_order_id"):
        trade["sl_order_id"] = bracket_response_id(sl_body)
        trade["bracket_api"] = BRACKET_API
    if tp_body is not None and not trade.get("tp_order_id"):
        trade["tp_order_id"] = bracket_response_id(tp_body)
        trade["bracket_api"] = BRACKET_API


def place_live_bracket_with_retry(trade):
    last_sl_err = None
    last_tp_err = None

    for attempt in range(1, BRACKET_RETRY_ATTEMPTS + 1):
        if attempt > 1:
            print(
                f"🔁 Bracket retry attempt {attempt}/{BRACKET_RETRY_ATTEMPTS} "
                f"sleep={BRACKET_RETRY_SLEEP_SEC}s"
            )

        sl_body, sl_err, tp_body, tp_err = place_live_bracket(trade)
        _record_bracket_bodies(trade, sl_body, tp_body)
        if sl_err is not None:
            last_sl_err = sl_err
        if tp_err is not None:
            last_tp_err = tp_err

        if trade.get("sl_order_id") and trade.get("tp_order_id"):
            return None, None

        if attempt < BRACKET_RETRY_ATTEMPTS:
            time.sleep(BRACKET_RETRY_SLEEP_SEC)

    if trade.get("sl_order_id"):
        last_sl_err = None
    if trade.get("tp_order_id"):
        last_tp_err = None
    return last_sl_err, last_tp_err


def open_live_trade(signal, price, cycle_ts):
    global TRADE_COUNTER, ACTIVE_TRADE

    if has_open_position(SYMBOL):
        print(f"[{cycle_ts}] ⚠️ LIVE skip: đã có position {SYMBOL} trên sàn")
        return None

    body, meta_or_err = place_live_entry(signal, price)
    if body is None:
        print(f"[{cycle_ts}] ❌ LIVE entry failed: {meta_or_err}")
        return None

    sizing = meta_or_err
    TRADE_COUNTER += 1
    ACTIVE_TRADE = {
        "id": TRADE_COUNTER,
        "mode": "LIVE",
        "status": "PENDING_ENTRY",
        **trade_base(signal, price, cycle_ts),
        "quantity": sizing["qty"],
        "notional_usdt": sizing["notional_usdt"],
        "sizing_mode": sizing["sizing_mode"],
        "leverage": LEVERAGE,
        "entry_order_type": sizing.get("entry_order_type"),
        "limit_price": sizing.get("limit_price"),
        "pending_since_ms": int(time.time() * 1000),
        "entry_order_id": body.get("orderId"),
        "entry_order_status": body.get("status"),
    }
    append_trade_log({"event": "ORDER_PLACED", **ACTIVE_TRADE})
    print(
        f"\n📤 LIVE ENTRY ORDER #{ACTIVE_TRADE['id']} | {signal['side']} "
        f"type={sizing.get('entry_order_type')} limit={sizing.get('limit_price')} "
        f"qty={sizing['qty']} (~{sizing['notional_usdt']} USDT notional | {LEVERAGE}x | {sizing['sizing_mode']}) | "
        f"planned SL={signal['sl']} TP={signal['tp']} (sẽ tính lại từ giá FILLED) | "
        f"fear={signal.get('fear_index')} {signal.get('fear_label')} regime={signal.get('fear_regime')} "
        f"sl×{signal.get('fear_sl_mult')} tp×{signal.get('fear_tp_mult')} qty×{sizing.get('fear_qty_mult')} | "
        f"orderId={ACTIVE_TRADE['entry_order_id']} status={ACTIVE_TRADE['entry_order_status']} | "
        f"{signal['zone_kind'].upper()} {signal['level_name']} | rule={signal.get('strategy_rule')} | gate={signal.get('gate_reason')} — chờ FILLED rồi đặt SL/TP"
    )
    save_state()
    return ACTIVE_TRADE


def open_trade(signal, price, cycle_ts):
    if LIVE_TRADE:
        if not live_trading_ready():
            print(
                f"[{cycle_ts}] ⚠️ BTC_LIVE_TRADE=true nhưng thiếu BINANCE_API_KEY hoặc Ed25519 PEM — bỏ qua lệnh"
            )
            return None
        return open_live_trade(signal, price, cycle_ts)
    return open_sim_trade(signal, price, cycle_ts)


def apply_bracket_from_fill(trade):
    """Tính lại SL/TP từ giá khớp thực (không dùng planned_entry lúc signal)."""
    entry = float(trade.get("entry") or trade.get("planned_entry"))
    side = trade["side"]
    zone_kind = trade.get("zone_kind", "hvn")
    tp, sl, tp_dist, sl_dist = bracket_levels(
        entry, side, zone_kind, sl_points=trade.get("sl_points"), tp_points=trade.get("tp_points")
    )
    trade["entry"] = round_price(entry)
    trade["tp"] = tp
    trade["sl"] = sl
    trade["tp_points"] = tp_dist
    trade["sl_points"] = sl_dist
    return trade


def confirm_live_entry(trade, order_data, cycle_ts):
    executed_qty = float(order_data.get("executedQty") or 0)
    if executed_qty <= 0:
        print(f"[{cycle_ts}] ⚠️ Entry order chưa có executedQty — chưa đặt SL/TP")
        return

    trade["status"] = "OPEN"
    trade["entry_order_status"] = order_data.get("status")

    avg_price_raw = order_data.get("avgPrice")
    avg_price = float(avg_price_raw) if avg_price_raw and float(avg_price_raw) > 0 else float(trade["planned_entry"])
    trade["entry"] = round_price(avg_price)
    trade["filled_qty"] = order_data.get("executedQty")
    trade["quantity"] = format_qty(SYMBOL, executed_qty)
    trade["filled_ts"] = cycle_ts
    trade["confirmed"] = True

    apply_bracket_from_fill(trade)

    sl_err, tp_err = place_live_bracket_with_retry(trade)

    if trade.get("sl_order_id") and trade.get("tp_order_id"):
        trade["bracket_status"] = "PLACED"
        trade["bracket_error"] = None
    elif trade.get("sl_order_id") or trade.get("tp_order_id"):
        trade["bracket_status"] = "PARTIAL"
        trade["bracket_error"] = {"sl": sl_err, "tp": tp_err}
        trade["last_bracket_retry_ms"] = int(time.time() * 1000)
        print(f"[{cycle_ts}] ⚠️ Bracket partial SL={trade.get('sl_order_id')} TP={trade.get('tp_order_id')} err={trade['bracket_error']}")
    else:
        trade["bracket_status"] = "FAILED"
        trade["bracket_error"] = {"sl": sl_err, "tp": tp_err}
        trade["last_bracket_retry_ms"] = int(time.time() * 1000)
        print(f"[{cycle_ts}] ❌ Bracket failed SL={sl_err} TP={tp_err} — sẽ retry mỗi {BRACKET_RETRY_SEC}s khi position còn mở")

    append_trade_log({"event": "OPEN_CONFIRMED", **trade})
    print(
        f"\n✅ LIVE OPEN #{trade['id']} | {trade['side']} @ {trade['entry']} | "
        f"SL={trade['sl']} (-{trade['sl_points']}pt) | TP={trade['tp']} (+{trade['tp_points']}pt) | "
        f"fear={trade.get('fear_index')} {trade.get('fear_label')} sl×{trade.get('fear_sl_mult')} tp×{trade.get('fear_tp_mult')} | "
        f"qty={trade.get('quantity')} | SL order={trade.get('sl_order_id')} | TP order={trade.get('tp_order_id')} | "
        f"bracket={trade.get('bracket_status')}"
    )
    save_state()


def sync_pending_entry(cycle_ts):
    global ACTIVE_TRADE

    trade = ACTIVE_TRADE
    if not trade or trade.get("mode") != "LIVE" or trade.get("status") != "PENDING_ENTRY":
        return

    order_id = trade.get("entry_order_id")
    if not order_id:
        return

    data, err = fetch_order(SYMBOL, order_id)
    if data is None:
        print(f"[{cycle_ts}] ⚠️ Không đọc được entry order {order_id}: {err}")
        return

    status = data.get("status")
    trade["entry_order_status"] = status

    executed_qty = float(data.get("executedQty") or 0)
    orig_qty = float(data.get("origQty") or 0)

    print(
        f"[{cycle_ts}] ⏳ ENTRY CHECK #{trade.get('id')} | "
        f"orderId={order_id} status={status} filled={executed_qty}/{orig_qty} "
        f"type={trade.get('entry_order_type')} limit={trade.get('limit_price')}"
    )

    if status == "FILLED":
        confirm_live_entry(trade, data, cycle_ts)
        return

    if status in ("CANCELED", "REJECTED", "EXPIRED"):
        # Nếu order bị cancel/expired nhưng có partial fill, vẫn phải bảo vệ position đã mở.
        if executed_qty > 0:
            print(f"[{cycle_ts}] ⚠️ Entry {order_id} {status} nhưng đã partial fill {executed_qty}; đặt SL/TP cho phần đã khớp")
            confirm_live_entry(trade, data, cycle_ts)
            return
        print(f"[{cycle_ts}] ❌ Entry {order_id} {status} — không tính lệnh")
        append_trade_log({"event": "ENTRY_ABORTED", **trade, "final_status": status})
        ACTIVE_TRADE = None
        save_state()
        return

    pending_since = int(trade.get("pending_since_ms") or int(time.time() * 1000))
    age_sec = (int(time.time() * 1000) - pending_since) / 1000.0

    # LIMIT chưa fill đủ trong timeout: cancel phần còn lại.
    # Nếu đã partial fill thì sau cancel sẽ đặt bracket cho phần executedQty.
    if ENTRY_ORDER_TIMEOUT_SEC > 0 and age_sec >= ENTRY_ORDER_TIMEOUT_SEC:
        cancel_body, cancel_err = cancel_order(SYMBOL, order_id)
        print(
            f"[{cycle_ts}] ⏱️ Entry timeout {age_sec:.1f}s — cancel LIMIT order "
            f"{order_id}: {cancel_body or cancel_err}"
        )
        append_trade_log({
            "event": "ENTRY_TIMEOUT_CANCEL",
            **trade,
            "age_sec": round(age_sec, 1),
            "filled_before_cancel": executed_qty,
            "cancel_result": cancel_body or cancel_err,
        })

        if executed_qty > 0:
            # Dùng dữ liệu order trước cancel để tính bracket cho phần đã khớp.
            data["status"] = "PARTIALLY_FILLED_TIMEOUT_ACCEPTED"
            confirm_live_entry(trade, data, cycle_ts)
            return

        ACTIVE_TRADE = None
        save_state()
        return

    # PARTIALLY_FILLED chưa timeout: tiếp tục chờ fill đủ.
    save_state()


def _order_filled(data):
    return isinstance(data, dict) and data.get("status") == "FILLED"


def ensure_live_bracket(trade, cycle_ts):
    if not trade or trade.get("mode") != "LIVE" or trade.get("status") != "OPEN":
        return
    if trade.get("sl_order_id") and trade.get("tp_order_id"):
        trade["bracket_status"] = "PLACED"
        return

    now_ms = int(time.time() * 1000)
    last_ms = int(trade.get("last_bracket_retry_ms") or 0)
    if last_ms and (now_ms - last_ms) < BRACKET_RETRY_SEC * 1000:
        return

    trade["last_bracket_retry_ms"] = now_ms
    print(
        f"[{cycle_ts}] 🔁 Bracket retry #{trade.get('id')} | "
        f"SL={trade.get('sl_order_id')} TP={trade.get('tp_order_id')} status={trade.get('bracket_status')}"
    )
    sl_err, tp_err = place_live_bracket_with_retry(trade)

    if trade.get("sl_order_id") and trade.get("tp_order_id"):
        trade["bracket_status"] = "PLACED"
        trade["bracket_error"] = None
        print(f"[{cycle_ts}] ✅ Bracket retry OK | SL order={trade.get('sl_order_id')} TP order={trade.get('tp_order_id')}")
    elif trade.get("sl_order_id") or trade.get("tp_order_id"):
        trade["bracket_status"] = "PARTIAL"
        trade["bracket_error"] = {"sl": sl_err, "tp": tp_err}
        print(f"[{cycle_ts}] ⚠️ Bracket still PARTIAL | SL={trade.get('sl_order_id')} TP={trade.get('tp_order_id')} err={trade['bracket_error']}")
    else:
        trade["bracket_status"] = "FAILED"
        trade["bracket_error"] = {"sl": sl_err, "tp": tp_err}
        print(f"[{cycle_ts}] ❌ Bracket retry failed | err={trade['bracket_error']}")
    append_trade_log({"event": "BRACKET_RETRY", **trade})
    save_state()


def sync_open_live_trade(price, cycle_ts):
    trade = ACTIVE_TRADE
    if not trade or trade.get("mode") != "LIVE" or trade.get("status") != "OPEN":
        return None

    ensure_live_bracket(trade, cycle_ts)

    now_ms = int(time.time() * 1000)
    last_ms = int(trade.get("last_position_status_ms") or 0)
    if POSITION_STATUS_SEC > 0 and last_ms and (now_ms - last_ms) < POSITION_STATUS_SEC * 1000:
        return None
    trade["last_position_status_ms"] = now_ms

    sl_id = trade.get("sl_order_id")
    tp_id = trade.get("tp_order_id")

    if sl_id:
        sl_data, _ = fetch_bracket_order(SYMBOL, sl_id)
        if _bracket_order_filled(sl_data):
            if tp_id:
                cancel_bracket_order(SYMBOL, tp_id)
            close_live_trade(trade, "SL_HIT", float(sl_data.get("avgPrice") or trade["sl"]), cycle_ts)
            return trade

    if tp_id:
        tp_data, _ = fetch_bracket_order(SYMBOL, tp_id)
        if _bracket_order_filled(tp_data):
            if sl_id:
                cancel_bracket_order(SYMBOL, sl_id)
            close_live_trade(trade, "TP_HIT", float(tp_data.get("avgPrice") or trade["tp"]), cycle_ts)
            return trade

    if not has_open_position(SYMBOL):
        reason, hit_price = trade_hit(trade, float(price))
        if reason:
            close_live_trade(trade, reason, hit_price, cycle_ts)
            return trade

    return None


def close_live_trade(trade, reason, closed_price, cycle_ts):
    global ACTIVE_TRADE, CONSECUTIVE_LOSSES, SIM_BALANCE

    trade["status"] = "CLOSED"
    trade["close_reason"] = reason
    trade["closed_price"] = round_price(closed_price)
    trade["closed_ts"] = cycle_ts

    if reason == "TP_HIT":
        trade["outcome"] = "WIN"
        pnl = trade["tp_points"]
        CONSECUTIVE_LOSSES = 0
    else:
        trade["outcome"] = "LOSS"
        pnl = -trade["sl_points"]
        CONSECUTIVE_LOSSES += 1

    trade["pnl_points"] = round(pnl, PRICE_DECIMALS)
    SIM_BALANCE += pnl
    trade["sim_balance"] = round(SIM_BALANCE, PRICE_DECIMALS)

    TRADE_HISTORY.append(dict(trade))
    append_trade_log({"event": "CLOSE", **trade})
    print(
        f"📌 LIVE CLOSE #{trade['id']} | {trade['outcome']} | {reason} @ {closed_price} | "
        f"pnl={pnl:+.1f}pt | consecutive_losses={CONSECUTIVE_LOSSES}"
    )
    ACTIVE_TRADE = None
    save_state()


def close_sim_trade(trade, reason, closed_price, cycle_ts):
    global ACTIVE_TRADE, CONSECUTIVE_LOSSES, SIM_BALANCE

    trade["status"] = "CLOSED"
    trade["close_reason"] = reason
    trade["closed_price"] = round_price(closed_price)
    trade["closed_ts"] = cycle_ts

    if reason == "TP_HIT":
        trade["outcome"] = "WIN"
        pnl = trade["tp_points"]
        CONSECUTIVE_LOSSES = 0
    else:
        trade["outcome"] = "LOSS"
        pnl = -trade["sl_points"]
        CONSECUTIVE_LOSSES += 1

    SIM_BALANCE += pnl
    trade["pnl_points"] = round(pnl, PRICE_DECIMALS)
    trade["sim_balance"] = round(SIM_BALANCE, PRICE_DECIMALS)

    TRADE_HISTORY.append(dict(trade))
    append_trade_log({"event": "CLOSE", **trade})
    print(
        f"📌 SIM CLOSE #{trade['id']} | {trade['outcome']} | {reason} @ {closed_price} | "
        f"pnl={pnl:+.1f}pt | balance={SIM_BALANCE:.1f} | consecutive_losses={CONSECUTIVE_LOSSES}"
    )
    ACTIVE_TRADE = None
    save_state()


def update_active_trade(price, cycle_ts):
    if ACTIVE_TRADE is None:
        return None

    if ACTIVE_TRADE.get("mode") == "LIVE":
        if ACTIVE_TRADE.get("status") == "PENDING_ENTRY":
            sync_pending_entry(cycle_ts)
            return None
        if ACTIVE_TRADE.get("status") == "OPEN":
            return sync_open_live_trade(price, cycle_ts)
        return None

    if not has_open_trade():
        return None
    reason, hit_price = trade_hit(ACTIVE_TRADE, float(price))
    if reason:
        close_sim_trade(ACTIVE_TRADE, reason, hit_price, cycle_ts)
        return ACTIVE_TRADE
    return None


def winrate_stats():
    total = len(TRADE_HISTORY)
    wins = sum(1 for t in TRADE_HISTORY if t.get("outcome") == "WIN")
    losses = sum(1 for t in TRADE_HISTORY if t.get("outcome") == "LOSS")
    wr = round((wins / total) * 100.0, 2) if total else 0.0
    return {"total": total, "wins": wins, "losses": losses, "winrate": wr}


# =========================
# PERSISTENCE
# =========================
def append_trade_log(row):
    try:
        with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def save_state():
    payload = {
        "symbol": SYMBOL,
        "trade_counter": TRADE_COUNTER,
        "active_trade": ACTIVE_TRADE,
        "trade_history": TRADE_HISTORY[-500:],
        "sim_balance": SIM_BALANCE,
        "consecutive_losses_session": CONSECUTIVE_LOSSES,
        "state_path": STATE_PATH,
        "trade_log_path": TRADE_LOG_PATH,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_state():
    global TRADE_COUNTER, ACTIVE_TRADE, TRADE_HISTORY, SIM_BALANCE, CONSECUTIVE_LOSSES
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return

    TRADE_COUNTER = int(payload.get("trade_counter", 0))
    TRADE_HISTORY[:] = payload.get("trade_history", [])
    SIM_BALANCE = float(payload.get("sim_balance", STARTING_BALANCE))
    active = payload.get("active_trade")
    if isinstance(active, dict) and active.get("status") in ("PENDING_ENTRY", "OPEN"):
        ACTIVE_TRADE = active
    CONSECUTIVE_LOSSES = 0


def refresh_profile(symbol, price, force=False):
    global _volume_zone_cache, _trend_cache, _trend_15m_cache, _last_profile_ts

    now = time.time()
    if not force and _volume_zone_cache and (now - _last_profile_ts) < PROFILE_REFRESH_SEC:
        return _volume_zone_cache, _trend_cache, _trend_15m_cache, False

    _volume_zone_cache = volume_zone_map(symbol, price)
    _trend_cache, _trend_15m_cache = detect_trend(symbol)
    _last_profile_ts = now
    return _volume_zone_cache, _trend_cache, _trend_15m_cache, True


# =========================
# MAIN
# =========================
def run():
    global CONSECUTIVE_LOSSES

    load_state()
    missing = live_config_missing()
    if LIVE_TRADE and live_trading_ready():
        mode_label = "LIVE"
    elif LIVE_TRADE:
        mode_label = f"LIVE(misconfig: {', '.join(missing)})"
    else:
        mode_label = "SIM"
    print("=" * 70)
    print(f"BTC HVN/LVN {mode_label} | {SYMBOL} | poll={PRICE_POLL_SEC}s | profile={PROFILE_REFRESH_SEC}s")
    if ENV_FILES_LOADED:
        print(f"env={', '.join(ENV_FILES_LOADED)}")
    else:
        print("env=.env not found (using shell env/defaults)")
    print(
        f"touch ±{ZONE_TOUCH_TOLERANCE}pt | base TP LVN={TP_LVN_POINTS} HVN={TP_HVN_POINTS} | base SL={SL_POINTS}pt"
    )
    print(
        f"fear_enabled={FEAR_ENABLED} | fear_source={FEAR_SOURCE} | fear_api=coinmarketcap | fear_cache={FEAR_CACHE_SEC}s | "
        f"fear_qty_adjust={FEAR_ADJUST_QTY} | leverage_cached=true | abort_on_leverage_fail={ABORT_ON_LEVERAGE_FAIL}"
    )
    if LIVE_TRADE:
        ex = f" ~{calc_order_qty(80000)[0]} BTC @80000" if ORDER_QTY_FIXED is None else f" fixed_qty={ORDER_QTY_FIXED}"
        margin_note = (
            f"margin={ORDER_USDT:.0f} USDT × {LEVERAGE}x → notional={target_notional_usdt():.0f}"
            if SIZING_MODE in ("margin", "cost")
            else f"notional={target_notional_usdt():.0f} USDT"
        )
        print(
            f"sizing={SIZING_MODE} | {margin_note}{ex} | "
            f"entry_type={ENTRY_ORDER_TYPE} | limit_offset={LIMIT_ENTRY_OFFSET} | entry_timeout={ENTRY_ORDER_TIMEOUT_SEC}s | "
            f"bracket_mode={BRACKET_MODE} | close_position={BRACKET_CLOSE_POSITION} | "
            f"bracket_api={BRACKET_API} | bracket_working={BRACKET_WORKING_TYPE} | "
            f"bracket_retry={BRACKET_RETRY_ATTEMPTS}x/{BRACKET_RETRY_SLEEP_SEC}s | "
            f"pos_mode={POSITION_SIDE_MODE} | position_status={POSITION_STATUS_SEC}s | "
            f"api_key={'set' if API_KEY else 'MISSING — export BINANCE_API_KEY hoặc ./run_btc_live.sh'} | "
            f"pem={'OK' if os.path.isfile(ED25519_KEY_PATH) else 'MISSING'} ({ED25519_KEY_PATH})"
        )
    print(
        f"max_loss_streak={MAX_CONSECUTIVE_LOSSES} | scan_status={SCAN_STATUS_SEC}s | "
        f"strict_edge={STRICT_EDGE_ONLY} | max_inverse={ENABLE_MAX_INVERSE} | "
        f"rejection_follow_trend={ENABLE_REJECTION_FOLLOW_TREND} | "
        f"short_edge_4h_min={ENABLE_SHORT_BEARISH_4H_MIN_EDGE} | "
        f"long_edge_4h_max={ENABLE_LONG_BULLISH_4H_MAX_EDGE} "
        f"long_priority={'BEFORE_MAX_INVERSE' if ENABLE_LONG_BULLISH_4H_MAX_EDGE else 'OFF'} "
        f"acceptance={LONG_RETEST_REQUIRE_ACCEPTANCE} | "
        f"max_retest_impulse={MAX_RETEST_IMPULSE_POINTS}pt/{RETEST_LOOKBACK_CANDLES} candles"
    )
    print(f"balance={SIM_BALANCE:.1f} | closed_trades={len(TRADE_HISTORY)} (chỉ sau FILLED + đóng lệnh)")
    print(f"state={STATE_PATH}")
    print(f"log={TRADE_LOG_PATH}")
    if ACTIVE_TRADE:
        print(f"resume: #{ACTIVE_TRADE.get('id')} status={ACTIVE_TRADE.get('status')} mode={ACTIVE_TRADE.get('mode')}")
    print("=" * 70)

    while True:
        if CONSECUTIVE_LOSSES >= MAX_CONSECUTIVE_LOSSES:
            print(
                f"\n⛔ Dừng script: {MAX_CONSECUTIVE_LOSSES} lần thua liên tiếp. "
                "Chạy lại script để tiếp tục (counter reset)."
            )
            save_state()
            break

        cycle_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        price = fetch_price(SYMBOL)
        if price is None:
            print(f"[{cycle_ts}] Không lấy được giá {SYMBOL}, thử lại sau {PRICE_POLL_SEC}s")
            time.sleep(PRICE_POLL_SEC)
            continue

        price = float(price)
        update_active_trade(price, cycle_ts)

        volume_zone, trend, trend_15m, profile_refreshed = refresh_profile(
            SYMBOL, price, force=_last_profile_ts == 0.0
        )

        if profile_refreshed:
            print(f"\n[{cycle_ts}] price={price:.{PRICE_DECIMALS}f} | trend={trend} | trend_15m={trend_15m:+.3f}")
            print_wide_volume_profile(volume_zone, price)
            stats = winrate_stats()
            pending = ACTIVE_TRADE and ACTIVE_TRADE.get("status") == "PENDING_ENTRY"
            print(
                f"📈 WINRATE: {stats['winrate']}% | wins={stats['wins']} | losses={stats['losses']} | "
                f"total={stats['total']} | "
                f"workflow={'pending' if pending else ('open' if has_open_trade() else 'no')} | "
                f"balance={SIM_BALANCE:.1f}"
            )

        maybe_print_scan_status(price, volume_zone, cycle_ts)

        if not has_active_workflow() and SIM_BALANCE > 0:
            signal = scan_zone_touch(price, volume_zone, trend, trend_15m)
            if signal:
                open_trade(signal, price, cycle_ts)
                if ACTIVE_TRADE and ACTIVE_TRADE.get("mode") == "SIM":
                    save_state()

        time.sleep(PRICE_POLL_SEC)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n👋 Dừng thủ công.")
        save_state()
