# -*- coding: utf-8 -*-
"""
NOBITEX EARLY MOVE RADAR - FINAL

هدف:
- اسکن کل بازار USDT نوبیتکس
- حفظ الگوریتم امتیازدهی PRE-MOVE فعلی
- FAST PRE-MOVE جداگانه
- قیمت دقیق از orderbook در لحظه هشدار
- فقط یک درخواست 15m برای هر بازار و ساخت 1H به صورت محلی
- Rate limit سراسری برای UDF
- جلوگیری از ادامه streak قدیمی
- کنترل پوشش بازار قبل از ارسال هشدار
- Telegram

این فایل با Python 3.10+ و کتابخانه استاندارد پایتون نوشته شده است.
"""

import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"

MAX_WORKERS = 4

# 380 x 15m ~= 95 hours. Enough for 90 closed 1H candles.
COUNTBACK_15M = 380

TOP_N = 5

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

# Nobitex UDF safety: keep average request rate under ~60/min.
UDF_MIN_INTERVAL = 1.05
_udf_rate_lock = threading.Lock()
_udf_next_request_at = 0.0

# A streak cannot continue if the previous scan is older than this.
MAX_STREAK_GAP_SECONDS = 25 * 60

# Do not send alerts when market coverage becomes too poor.
MIN_COVERAGE_FOR_ALERTS = 0.80

# Performance tracker clean-start schema.
TRACKING_SCHEMA_VERSION = 1
TRACKING_START_PATH = "nobitex_performance_tracking_start.json"

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    ""
).strip()

WORKSPACE = os.environ.get(
    "GITHUB_WORKSPACE",
    "."
)

STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_early_radar_state.json"
)

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json"
)

TRACKING_START_FULL_PATH = os.path.join(
    WORKSPACE,
    TRACKING_START_PATH
)

TELEGRAM_TEST_ON_START = False

EXCLUDED_SYMBOLS = {
    "USDCUSDT",
    "USDEUSDT",
    "DAIUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
    "WBTCUSDT",
}


# ============================================================
# HTTP HELPERS
# ============================================================

def _rate_limit_udf():
    global _udf_next_request_at

    with _udf_rate_lock:
        now = time.monotonic()
        wait = _udf_next_request_at - now
        if wait > 0:
            time.sleep(wait)
        _udf_next_request_at = time.monotonic() + UDF_MIN_INTERVAL


def http_get_json(url, params=None, udf=False):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    headers = {
        "User-Agent": "Nobitex-Early-Radar/3.0",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            if udf:
                _rate_limit_udf()

            req = urllib.request.Request(
                url,
                headers=headers,
                method="GET",
            )

            with urllib.request.urlopen(
                req,
                timeout=HTTP_TIMEOUT,
            ) as response:
                raw = response.read().decode("utf-8")

            return json.loads(raw)

        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.2 * (attempt + 1))

    raise last_error


def http_post_json(url, data):
    payload = json.dumps(data).encode("utf-8")

    headers = {
        "User-Agent": "Nobitex-Early-Radar/3.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )

            with urllib.request.urlopen(
                req,
                timeout=HTTP_TIMEOUT,
            ) as response:
                raw = response.read().decode("utf-8")

            return json.loads(raw)

        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.2 * (attempt + 1))

    raise last_error


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram disabled: secrets are not configured.")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        result = http_post_json(url, data)
        if result.get("ok"):
            print("Telegram message sent.")
            return True

        print("Telegram error:", result)
        return False

    except Exception as exc:
        print("Telegram exception:", exc)
        return False


def telegram_test():
    return telegram_send(
        "✅ اتصال رادار Early Move به تلگرام برقرار شد.\n"
        "Nobitex Early Move Radar فعال است."
    )


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def clamp(value, low, high):
    return max(low, min(high, value))


def median(values):
    clean = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]
    if not clean:
        return 0.0
    return statistics.median(clean)


def average(values):
    # Preserves the original algorithm behavior: zero values are ignored.
    clean = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]
    if not clean:
        return 0.0
    return sum(clean) / len(clean)


# ============================================================
# JSON STATE
# ============================================================

def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print("State load error:", path, exc)
        return default


def save_json(path, data):
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        return True
    except Exception as exc:
        print("State save error:", path, exc)
        return False


def ensure_tracking_start():
    existing = load_json(TRACKING_START_FULL_PATH, {})

    if (
        isinstance(existing, dict)
        and existing.get("schema_version") == TRACKING_SCHEMA_VERSION
        and safe_float(existing.get("started_at"), 0) > 0
    ):
        return int(existing["started_at"])

    started_at = int(time.time())
    payload = {
        "schema_version": TRACKING_SCHEMA_VERSION,
        "started_at": started_at,
        "created_at": started_at,
    }
    save_json(TRACKING_START_FULL_PATH, payload)
    print("Performance tracking start:", started_at)
    return started_at


# ============================================================
# ORDER BOOK SNAPSHOT
# ============================================================

def get_market_snapshot():
    data = http_get_json(
        BASE_URL + "/v3/orderbook/all"
    )

    result = {}

    if not isinstance(data, dict):
        return result

    for raw_symbol, book in data.items():
        symbol = str(raw_symbol).upper()

        if not symbol.endswith("USDT"):
            continue
        if symbol in EXCLUDED_SYMBOLS:
            continue
        if not isinstance(book, dict):
            continue

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid_value = 0.0
        ask_value = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            if bids:
                best_bid = safe_float(bids[0][0])
            if asks:
                best_ask = safe_float(asks[0][0])

            for row in bids[:20]:
                if len(row) >= 2:
                    bid_value += safe_float(row[0]) * safe_float(row[1])

            for row in asks[:20]:
                if len(row) >= 2:
                    ask_value += safe_float(row[0]) * safe_float(row[1])
        except Exception:
            pass

        flow = bid_value / ask_value if ask_value > 0 else 0.0

        if best_bid > 0 and best_ask > 0:
            live_price = (best_bid + best_ask) / 2.0
        elif best_bid > 0:
            live_price = best_bid
        else:
            live_price = best_ask

        result[symbol] = {
            "bid_value": bid_value,
            "ask_value": ask_value,
            "flow": flow,
            "price": live_price,
        }

    return result


# ============================================================
# 15M CANDLES
# ============================================================

def get_15m_candles(symbol):
    params = {
        "symbol": symbol,
        "resolution": "15",
        "countback": COUNTBACK_15M,
        "to": int(time.time()),
    }

    data = http_get_json(
        BASE_URL + "/market/udf/history",
        params=params,
        udf=True,
    )

    if not isinstance(data, dict):
        raise ValueError("INVALID_CANDLE_RESPONSE")

    if data.get("s") not in ("ok", "no_data"):
        raise ValueError("CANDLE_STATUS_" + str(data.get("s")))

    timestamps = [int(safe_float(x)) for x in data.get("t", [])]
    closes = [safe_float(x) for x in data.get("c", [])]
    volumes = [safe_float(x) for x in data.get("v", [])]

    n = min(len(timestamps), len(closes), len(volumes))
    timestamps = timestamps[:n]
    closes = closes[:n]
    volumes = volumes[:n]

    if len(closes) < 25:
        raise ValueError("CANDLES_TOO_SHORT")

    # Remove the current open 15m candle.
    now = int(time.time())
    if timestamps:
        if now - timestamps[-1] < 900:
            timestamps = timestamps[:-1]
            closes = closes[:-1]
            volumes = volumes[:-1]

    if len(closes) < 25:
        raise ValueError("CANDLES_TOO_SHORT_AFTER_REMOVE")

    return timestamps, closes, volumes


# ============================================================
# 15M -> 1H AGGREGATION
# ============================================================

def aggregate_15m_to_1h(timestamps, closes, volumes):
    buckets = {}

    for ts, close, volume in zip(timestamps, closes, volumes):
        hour_start = (int(ts) // 3600) * 3600
        buckets.setdefault(hour_start, []).append((int(ts), close, volume))

    hour_timestamps = []
    hour_closes = []
    hour_volumes = []

    now = int(time.time())

    for hour_start in sorted(buckets):
        rows = sorted(buckets[hour_start], key=lambda row: row[0])

        # We only use fully closed hours with four 15m candles.
        if len(rows) < 4:
            continue

        if now < hour_start + 3600:
            continue

        hour_timestamps.append(hour_start)
        hour_closes.append(rows[-1][1])
        hour_volumes.append(sum(row[2] for row in rows))

    if len(hour_closes) < 25:
        raise ValueError("HOURLY_AGGREGATION_TOO_SHORT")

    return hour_timestamps, hour_closes, hour_volumes


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    clean = [safe_float(x) for x in values]
    if not clean:
        return []
    if len(clean) < period:
        return [clean[-1]] * len(clean)

    alpha = 2.0 / (period + 1.0)
    result = [clean[0]]

    for value in clean[1:]:
        result.append(
            alpha * value
            + (1.0 - alpha) * result[-1]
        )

    return result


def rsi(values, period=14):
    values = [safe_float(x) for x in values]

    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    # IMPORTANT: zero gains/losses MUST remain in RSI calculation.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    n = min(len(fast), len(slow))
    if n == 0:
        return 0.0, 0.0

    line = [fast[i] - slow[i] for i in range(n)]
    signal = ema(line, 9)

    return line[-1], signal[-1]


def structure_score(closes):
    if len(closes) < 30:
        return 0

    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    price = closes[-1]

    score = 0

    if price > e9:
        score += 2
    if e9 > e21:
        score += 2
    if e21 > e50:
        score += 2

    recent = closes[-12:]
    if len(recent) >= 8:
        if average(recent[:6]) < average(recent[-6:]):
            score += 2

    return clamp(score, 0, 8)


def resistance_distance(closes):
    if len(closes) < 20:
        return 99.0

    price = closes[-1]
    lookback = closes[-25:-1]

    if not lookback or price <= 0:
        return 99.0

    resistance = max(lookback)
    if resistance <= 0:
        return 99.0

    distance = ((resistance - price) / price) * 100.0
    return max(0.0, distance)


def calm_score(closes):
    if len(closes) < 20:
        return 0

    recent = closes[-20:]
    returns = []

    for i in range(1, len(recent)):
        if recent[i - 1] != 0:
            returns.append(
                abs(
                    (recent[i] - recent[i - 1])
                    / recent[i - 1]
                ) * 100.0
            )

    if not returns:
        return 0

    avg_move = average(returns)

    if avg_move <= 0.35:
        return 10
    if avg_move <= 0.60:
        return 8
    if avg_move <= 0.90:
        return 5
    if avg_move <= 1.30:
        return 2
    return 0


def momentum_percent(closes, candles_back):
    if len(closes) <= candles_back:
        return 0.0

    old = closes[-1 - candles_back]
    new = closes[-1]

    if old == 0:
        return 0.0

    return ((new - old) / old) * 100.0


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    volume_ratio,
    volume_acceleration,
    rsi_value,
    structure,
    resistance,
    calm,
    m1h,
    m4h,
    m8h,
    m15,
    macd_positive,
    flow,
):
    score = 0

    # Volume ratio
    if 1.25 <= volume_ratio <= 4.0:
        score += 18
    elif 1.05 <= volume_ratio < 1.25:
        score += 10
    elif 4.0 < volume_ratio <= 6.0:
        score += 8
    elif 6.0 < volume_ratio <= 10.0:
        score += 2
    elif volume_ratio > 10.0:
        score -= 8

    # Volume acceleration
    if volume_acceleration >= 1.20:
        score += 10
    elif volume_acceleration >= 1.05:
        score += 7
    elif volume_acceleration >= 1.00:
        score += 3

    # RSI
    if 45 <= rsi_value <= 60:
        score += 15
    elif 60 < rsi_value <= 68:
        score += 10
    elif 40 <= rsi_value < 45:
        score += 7
    elif 35 <= rsi_value < 40:
        score += 3
    elif rsi_value > 72:
        score -= 10

    # Structure
    score += int(round(structure * 2.5))
    score = min(score, 100)

    # Resistance
    if 0 <= resistance <= 1:
        score += 15
    elif 1 < resistance <= 2:
        score += 12
    elif 2 < resistance <= 3:
        score += 8
    elif 3 < resistance <= 5:
        score += 4

    # Calm
    score += calm

    # Moderate momentum adds; large movement subtracts.
    if 0 <= m1h <= 2.5:
        score += 4

    if 0 <= m4h <= 6:
        score += 4

    if m1h > 4:
        score -= 8

    if m4h > 10:
        score -= 10

    if m8h > 15:
        score -= 8

    if m15 > 2:
        score -= 5

    # MACD
    if macd_positive:
        score += 2

    # Order flow
    if flow >= 1.20:
        score += 5
    elif flow >= 1.05:
        score += 3
    elif flow >= 0.95:
        score += 1
    elif 0 < flow < 0.80:
        score -= 5

    return int(clamp(round(score), 0, 100))


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(symbol, book):
    try:
        timestamps_15m, closes_15m, volumes_15m = get_15m_candles(symbol)
        _, closes_1h, volumes_1h = aggregate_15m_to_1h(
            timestamps_15m,
            closes_15m,
            volumes_15m,
        )

        book_price = safe_float(book.get("price"), 0.0)
        price = book_price if book_price > 0 else closes_15m[-1]

        # Momentum
        m15 = momentum_percent(closes_15m, 1)
        m1h = momentum_percent(closes_1h, 1)
        m4h = momentum_percent(closes_1h, 4)
        m8h = momentum_percent(closes_1h, 8)

        # 1H volume
        current_volume = volumes_1h[-1]
        previous_volumes = volumes_1h[-11:-1]
        med_volume = median(previous_volumes)
        volume_ratio = (
            current_volume / med_volume
            if med_volume > 0 else 0.0
        )

        avg_last3 = average(volumes_1h[-3:])
        avg_prev8 = average(volumes_1h[-11:-3])
        volume_acceleration = (
            avg_last3 / avg_prev8
            if avg_prev8 > 0 else 0.0
        )

        # FAST 15M volume
        current_volume_15m = volumes_15m[-1]
        previous_volumes_15m = volumes_15m[-13:-1]
        med_volume_15m = median(previous_volumes_15m)
        volume_ratio_15m = (
            current_volume_15m / med_volume_15m
            if med_volume_15m > 0 else 0.0
        )

        avg_last3_15m = average(volumes_15m[-3:])
        avg_prev8_15m = average(volumes_15m[-11:-3])
        volume_acceleration_15m = (
            avg_last3_15m / avg_prev8_15m
            if avg_prev8_15m > 0 else 0.0
        )

        # Indicators
        rsi_value = rsi(closes_1h)
        macd_line, macd_signal = macd(closes_1h)
        macd_positive = macd_line >= macd_signal

        structure = structure_score(closes_1h)
        resistance = resistance_distance(closes_1h)
        calm = calm_score(closes_1h)

        flow = safe_float(book.get("flow"), 0.0)

        score = calculate_score(
            volume_ratio,
            volume_acceleration,
            rsi_value,
            structure,
            resistance,
            calm,
            m1h,
            m4h,
            m8h,
            m15,
            macd_positive,
            flow,
        )

        # Original PRE-MOVE gate.
        pre_move_gate = (
            1.15 <= volume_ratio <= 6
            and structure >= 5
            and 45 <= rsi_value <= 68
            and 0 <= resistance <= 3.5
            and m1h <= 2.8
            and m4h <= 7
            and m15 <= 2
        )

        # Original FAST logic.
        fast_pre_move = (
            volume_ratio_15m >= 1.80
            and volume_acceleration_15m >= 1.25
            and structure >= 5
            and 45 <= rsi_value <= 68
            and 0 <= resistance <= 3.5
            and m1h <= 2.8
            and m4h <= 7
            and m15 <= 2
            and flow >= 0.95
            and score >= 55
        )

        high_risk_jump = (
            m15 > 3
            or m1h > 4
            or rsi_value > 72
            or volume_ratio_15m > 6
            or volume_acceleration_15m > 3
        )

        quality = True
        if rsi_value > 72:
            quality = False
        if m1h > 4:
            quality = False
        if m4h > 10:
            quality = False
        if volume_ratio > 10:
            quality = False

        if pre_move_gate and score >= 75:
            label = "PRE-MOVE"
        elif pre_move_gate and score >= 65:
            label = "EARLY RADAR"
        elif quality and score >= 55:
            label = "WATCH"
        elif m1h > 4 or m4h > 10:
            label = "ALREADY MOVING"
        else:
            label = "LOW"

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "label": label,
            "pre_move_gate": pre_move_gate,
            "quality": quality,
            "volume_ratio": volume_ratio,
            "volume_acceleration": volume_acceleration,
            "volume_ratio_15m": volume_ratio_15m,
            "volume_acceleration_15m": volume_acceleration_15m,
            "fast_pre_move": fast_pre_move,
            "high_risk_jump": high_risk_jump,
            "rsi": rsi_value,
            "structure": structure,
            "resistance": resistance,
            "calm": calm,
            "momentum_15m": m15,
            "momentum_1h": m1h,
            "momentum_4h": m4h,
            "momentum_8h": m8h,
            "order_flow": flow,
            "macd_positive": macd_positive,
        }

    except Exception as exc:
        return {
            "symbol": symbol,
            "error": str(exc),
        }


# ============================================================
# PERSISTENCE
# ============================================================

def apply_persistence(result, previous_state, now):
    symbol = result["symbol"]
    previous = previous_state.get(symbol, {})

    old_score = safe_float(previous.get("score"), 0.0)
    current_score = safe_float(result.get("score"), 0.0)
    old_streak = int(safe_float(previous.get("streak"), 0.0))
    old_timestamp = int(safe_float(previous.get("timestamp"), 0.0))

    state_is_fresh = (
        old_timestamp > 0
        and now - old_timestamp <= MAX_STREAK_GAP_SECONDS
    )

    if current_score >= 65 and old_score >= 65 and state_is_fresh:
        streak = old_streak + 1
    elif current_score >= 65:
        streak = 1
    else:
        streak = 0

    strengthening = current_score >= old_score + 5

    confirmed = (
        bool(result.get("pre_move_gate"))
        and current_score >= 75
        and streak >= 2
    )

    result["previous_score"] = old_score
    result["streak"] = streak
    result["strengthening"] = strengthening
    result["confirmed"] = confirmed
    result["state_fresh"] = state_is_fresh

    return result


# ============================================================
# ALERT MESSAGES
# ============================================================

def build_confirmed_alert(result):
    return (
        "🚨 CONFIRMED PRE-MOVE\n\n"
        f"🪙 {result['symbol']}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"⭐ Score: {result['score']}/100\n"
        f"🔥 Streak: {result['streak']}\n\n"
        f"📊 Volume: {result['volume_ratio']:.2f}x\n"
        f"⚡ Volume acceleration: {result['volume_acceleration']:.2f}x\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"🏗 Structure: {result['structure']}/8\n"
        f"🎯 Resistance: {result['resistance']:.2f}%\n"
        f"🌊 Order flow: {result['order_flow']:.2f}\n\n"
        f"15m: {result['momentum_15m']:+.2f}%\n"
        f"1H: {result['momentum_1h']:+.2f}%\n"
        f"4H: {result['momentum_4h']:+.2f}%\n"
        f"8H: {result['momentum_8h']:+.2f}%\n\n"
        "🟢 شرایط قبل از حرکت صعودی تأیید شده."
    )


def build_fast_alert(result):
    return (
        "⚡ FAST PRE-MOVE\n\n"
        f"🪙 {result['symbol']}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"⭐ Score: {result['score']}/100\n"
        f"⚡ 15m Volume: {result['volume_ratio_15m']:.2f}x\n"
        f"🚀 15m Volume acceleration: {result['volume_acceleration_15m']:.2f}x\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"🏗 Structure: {result['structure']}/8\n"
        f"🎯 Resistance: {result['resistance']:.2f}%\n"
        f"🌊 Order flow: {result['order_flow']:.2f}\n\n"
        f"15m: {result['momentum_15m']:+.2f}%\n"
        f"1H: {result['momentum_1h']:+.2f}%\n"
        f"4H: {result['momentum_4h']:+.2f}%\n\n"
        "🟡 هشدار زودهنگام است؛ هنوز تأیید کامل PRE-MOVE نیست."
    )


# ============================================================
# ALERT STATE
# ============================================================

def smart_alert(result, alert_state, alerts_enabled):
    if not alerts_enabled:
        return False

    if not result.get("confirmed"):
        return False
    if result.get("score", 0) < 80:
        return False
    if result.get("streak", 0) < 2:
        return False
    if result.get("order_flow", 0) < 1.05:
        return False

    symbol = result["symbol"]
    old = alert_state.get(symbol, {})

    old_score = safe_float(old.get("score"), 0.0)
    old_streak = int(safe_float(old.get("streak"), 0.0))

    # Same confirmed signal is not repeated unless score strengthens >= 5.
    if (
        old_score >= 80
        and old_streak >= 2
        and result["score"] <= old_score + 2
    ):
        return False

    now = int(time.time())
    message = build_confirmed_alert(result)

    if not telegram_send(message):
        return False

    entry = {
        "score": result["score"],
        "streak": result["streak"],
        "timestamp": now,
        "price": safe_float(result.get("price"), 0.0),
        "alert_type": "CONFIRMED",
    }

    # Preserve FAST information for performance tracking.
    for key in (
        "fast_score",
        "fast_timestamp",
        "fast_price",
        "fast_alert_type",
    ):
        if key in old:
            entry[key] = old[key]

    alert_state[symbol] = entry
    return True


def fast_alert(result, alert_state, alerts_enabled):
    if not alerts_enabled:
        return False

    if not result.get("fast_pre_move"):
        return False
    if result.get("high_risk_jump"):
        return False

    symbol = result["symbol"]
    old = alert_state.get(symbol, {})

    old_fast_score = safe_float(old.get("fast_score"), 0.0)
    old_fast_timestamp = int(
        safe_float(old.get("fast_timestamp"), 0.0)
    )
    now = int(time.time())

    if (
        old_fast_timestamp > 0
        and now - old_fast_timestamp < 7200
        and result["score"] < old_fast_score + 5
    ):
        return False

    message = build_fast_alert(result)

    if not telegram_send(message):
        return False

    current = alert_state.get(symbol, {})
    current["fast_score"] = result["score"]
    current["fast_timestamp"] = now
    current["fast_price"] = safe_float(result.get("price"), 0.0)
    current["fast_alert_type"] = "FAST"
    alert_state[symbol] = current

    return True


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan():
    print("=" * 70)
    print("NOBITEX EARLY MOVE RADAR - FINAL")
    print(time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

    now = int(time.time())

    # Create a clean performance-tracking start marker once.
    ensure_tracking_start()

    previous_state = load_json(STATE_PATH, {})
    alert_state = load_json(ALERT_STATE_PATH, {})

    try:
        books = get_market_snapshot()
    except Exception as exc:
        print("MARKET SNAPSHOT FAILED:", exc)
        return

    symbols = sorted(books.keys())

    print("USDT markets:", len(symbols))

    results = []
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
                books[symbol],
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            symbol = futures[future]

            try:
                result = future.result()

                if "error" in result:
                    failed += 1
                    print(symbol, "FAILED:", result["error"])
                    continue

                result = apply_persistence(
                    result,
                    previous_state,
                    now,
                )
                results.append(result)

            except Exception as exc:
                failed += 1
                print(symbol, "FAILED:", exc)

    coverage = (
        len(results) / len(symbols)
        if symbols else 0.0
    )
    coverage_ok = coverage >= MIN_COVERAGE_FOR_ALERTS

    # Save successful state. Failed markets keep previous state so a temporary
    # API failure does not erase history; stale timestamps prevent old streaks.
    new_state = {}
    for result in results:
        new_state[result["symbol"]] = {
            "score": result["score"],
            "streak": result["streak"],
            "timestamp": now,
        }

    for symbol, old in previous_state.items():
        if symbol not in new_state and isinstance(old, dict):
            new_state[symbol] = old

    save_json(STATE_PATH, new_state)

    # Candidates
    candidates = [
        result
        for result in results
        if (
            result.get("pre_move_gate")
            and result.get("quality")
            and result.get("score", 0) >= 65
        )
    ]

    candidates.sort(
        key=lambda result: (
            result.get("score", 0),
            1 if result.get("strengthening") else 0,
            result.get("streak", 0),
            result.get("order_flow", 0),
            result.get("structure", 0),
            result.get("volume_ratio", 0),
        ),
        reverse=True,
    )

    print()
    print(
        f"Valid: {len(results)} | Failed: {failed} | "
        f"Coverage: {coverage * 100:.1f}% | "
        f"Alerts enabled: {coverage_ok}"
    )
    if not coverage_ok:
        print(
            f"ALERTS SUPPRESSED: coverage below "
            f"{MIN_COVERAGE_FOR_ALERTS * 100:.0f}%"
        )

    print()
    print("TOP PRE-MOVE CANDIDATES")
    print("-" * 70)

    if not candidates:
        print("No valid pre-move candidates.")

    for index, result in enumerate(candidates[:TOP_N], 1):
        print(
            f"{index}. {result['symbol']} | "
            f"{result['label']} | "
            f"Score {result['score']} | "
            f"Streak {result['streak']} | "
            f"Vol {result['volume_ratio']:.2f}x | "
            f"Flow {result['order_flow']:.2f} | "
            f"RSI {result['rsi']:.1f} | "
            f"Res {result['resistance']:.2f}%"
        )

    confirmed_alert_count = 0
    fast_alert_count = 0

    # FAST first, then confirmed.
    for result in results:
        if fast_alert(result, alert_state, coverage_ok):
            fast_alert_count += 1

        if smart_alert(result, alert_state, coverage_ok):
            confirmed_alert_count += 1

    save_json(ALERT_STATE_PATH, alert_state)

    watchlist = [
        result
        for result in results
        if result.get("score", 0) >= 55
    ]
    watchlist.sort(
        key=lambda result: result.get("score", 0),
        reverse=True,
    )

    print()
    print("WATCHLIST")
    print("-" * 70)

    for result in watchlist[:15]:
        fast_mark = ""
        if (
            result.get("fast_pre_move")
            and not result.get("high_risk_jump")
        ):
            fast_mark = " ⚡FAST"

        print(
            f"{result['symbol']:12} "
            f"{result['label']:15} "
            f"{result['score']:3}/100 "
            f"streak={result['streak']} "
            f"vol={result['volume_ratio']:.2f}x "
            f"15mVol={result['volume_ratio_15m']:.2f}x "
            f"flow={result['order_flow']:.2f}"
            f"{fast_mark}"
        )

    print()
    print("Confirmed Telegram alerts sent:", confirmed_alert_count)
    print("FAST Telegram alerts sent:", fast_alert_count)
    print("SCAN FINISHED.")


def main():
    if TELEGRAM_TEST_ON_START:
        telegram_test()
    run_scan()


if __name__ == "__main__":
    main()

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"

MAX_WORKERS = 4

COUNTBACK_1H = 90
COUNTBACK_15M = 90

TOP_N = 5

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    ""
).strip()

WORKSPACE = os.environ.get(
    "GITHUB_WORKSPACE",
    "."
)

STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_early_radar_state.json"
)

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json"
)

TELEGRAM_TEST_ON_START = False

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3


# ============================================================
# HTTP
# ============================================================

def http_get_json(url, params=None):

    if params:

        url = (
            url
            + "?"
            + urllib.parse.urlencode(params)
        )

    headers = {
        "User-Agent": "Nobitex-Early-Radar/1.0",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):

        try:

            req = urllib.request.Request(
                url,
                headers=headers,
                method="GET"
            )

            with urllib.request.urlopen(
                req,
                timeout=HTTP_TIMEOUT
            ) as response:

                raw = response.read().decode(
                    "utf-8"
                )

            return json.loads(raw)

        except Exception as e:

            last_error = e

            if attempt < HTTP_RETRIES - 1:

                time.sleep(
                    1.5 * (attempt + 1)
                )

    raise last_error


def http_post_json(url, data):

    headers = {
        "User-Agent": "Nobitex-Early-Radar/1.0",
        "Content-Type": "application/json",
    }

    payload = json.dumps(
        data
    ).encode("utf-8")

    last_error = None

    for attempt in range(HTTP_RETRIES):

        try:

            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST"
            )

            with urllib.request.urlopen(
                req,
                timeout=HTTP_TIMEOUT
            ) as response:

                raw = response.read().decode(
                    "utf-8"
                )

            return json.loads(raw)

        except Exception as e:

            last_error = e

            if attempt < HTTP_RETRIES - 1:

                time.sleep(
                    1.5 * (attempt + 1)
                )

    raise last_error


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        print(
            "Telegram disabled: "
            "secrets are not configured."
        )

        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:

        result = http_post_json(
            url,
            data
        )

        if result.get("ok"):

            print(
                "Telegram message sent."
            )

            return True

        print(
            "Telegram error:",
            result
        )

        return False

    except Exception as e:

        print(
            "Telegram exception:",
            e
        )

        return False


def telegram_test():

    return telegram_send(
        "✅ اتصال رادار Early Move به تلگرام برقرار شد.\n"
        "Nobitex Early Move Radar فعال است."
    )


# ============================================================
# HELPERS
# ============================================================

def safe_float(x, default=0.0):

    try:

        value = float(x)

        if math.isfinite(value):

            return value

    except Exception:

        pass

    return default


def clamp(x, low, high):

    return max(
        low,
        min(high, x)
    )


def median(values):

    values = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not values:

        return 0.0

    return statistics.median(
        values
    )


def average(values):

    values = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not values:

        return 0.0

    return (
        sum(values)
        / len(values)
    )


# ============================================================
# STATE
# ============================================================

def load_json(path, default):

    try:

        if not os.path.exists(path):

            return default

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        return data

    except Exception as e:

        print(
            "State load error:",
            path,
            e
        )

        return default


def save_json(path, data):

    try:

        directory = os.path.dirname(
            path
        )

        if directory:

            os.makedirs(
                directory,
                exist_ok=True
            )

        temp_path = (
            path
            + ".tmp"
        )

        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_path,
            path
        )

        return True

    except Exception as e:

        print(
            "State save error:",
            path,
            e
        )

        return False


# ============================================================
# MARKET SNAPSHOT
# ============================================================

def get_market_snapshot():

    data = http_get_json(
        BASE_URL
        + "/v3/orderbook/all"
    )

    result = {}

    if not isinstance(
        data,
        dict
    ):

        return result

    for symbol, book in data.items():

        symbol = str(
            symbol
        ).upper()

        if not symbol.endswith(
            "USDT"
        ):

            continue

        if not isinstance(
            book,
            dict
        ):

            continue

        bids = book.get(
            "bids",
            []
        )

        asks = book.get(
            "asks",
            []
        )

        bid_value = 0.0
        ask_value = 0.0

        try:

            for row in bids[:20]:

                if len(row) >= 2:

                    price = safe_float(
                        row[0]
                    )

                    amount = safe_float(
                        row[1]
                    )

                    bid_value += (
                        price * amount
                    )

            for row in asks[:20]:

                if len(row) >= 2:

                    price = safe_float(
                        row[0]
                    )

                    amount = safe_float(
                        row[1]
                    )

                    ask_value += (
                        price * amount
                    )

        except Exception:

            pass

        if ask_value > 0:

            flow = (
                bid_value
                / ask_value
            )

        else:

            flow = 0.0

        result[symbol] = {
            "bid_value": bid_value,
            "ask_value": ask_value,
            "flow": flow,
        }

    return result


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    symbol,
    resolution,
    countback
):

    params = {
        "symbol": symbol,
        "resolution": resolution,
        "countback": countback,
        "to": int(time.time()),
    }

    data = http_get_json(
        BASE_URL
        + "/market/udf/history",
        params
    )

    if not isinstance(
        data,
        dict
    ):

        raise ValueError(
            "Invalid candle response"
        )

    if data.get("s") not in (
        "ok",
        "no_data"
    ):

        raise ValueError(
            "CANDLE_STATUS_"
            + str(
                data.get("s")
            )
        )

    closes = [
        safe_float(x)
        for x in data.get(
            "c",
            []
        )
    ]

    volumes = [
        safe_float(x)
        for x in data.get(
            "v",
            []
        )
    ]

    timestamps = data.get(
        "t",
        []
    )

    n = min(
        len(closes),
        len(volumes)
    )

    closes = closes[:n]
    volumes = volumes[:n]

    if len(closes) < 25:

        raise ValueError(
            "CANDLES_TOO_SHORT"
        )

    # Remove current open candle.
    if timestamps:

        try:

            last_timestamp = int(
                timestamps[-1]
            )

            if resolution == "60":

                candle_seconds = 3600

            elif resolution == "15":

                candle_seconds = 900

            else:

                candle_seconds = 3600

            if (
                int(time.time())
                - last_timestamp
                < candle_seconds
            ):

                closes = closes[:-1]
                volumes = volumes[:-1]

        except Exception:

            pass

    if len(closes) < 25:

        raise ValueError(
            "CANDLES_TOO_SHORT_AFTER_REMOVE"
        )

    return (
        closes,
        volumes
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):

    values = [
        safe_float(x)
        for x in values
    ]

    if not values:

        return []

    if len(values) < period:

        return [
            values[-1]
        ] * len(values)

    alpha = (
        2.0
        / (period + 1)
    )

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            alpha * value
            + (
                1 - alpha
            ) * result[-1]
        )

    return result


def rsi(
    values,
    period=14
):

    values = [
        safe_float(x)
        for x in values
    ]

    if len(values) <= period:

        return 50.0

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        diff = (
            values[i]
            - values[i - 1]
        )

        if diff >= 0:

            gains.append(diff)
            losses.append(0.0)

        else:

            gains.append(0.0)
            losses.append(
                abs(diff)
            )

    # IMPORTANT:
    # RSI must include zero gains/losses.
    # Do NOT use average() here because
    # average() intentionally filters zeros.

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

    if avg_loss == 0:

        if avg_gain == 0:

            return 50.0

        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return 100.0 - (
        100.0
        / (1.0 + rs)
    )


def macd(values):

    fast = ema(
        values,
        12
    )

    slow = ema(
        values,
        26
    )

    n = min(
        len(fast),
        len(slow)
    )

    if n == 0:

        return (
            0.0,
            0.0
        )

    line = [
        fast[i]
        - slow[i]
        for i in range(n)
    ]

    signal_values = ema(
        line,
        9
    )

    return (
        line[-1],
        signal_values[-1]
    )


# ============================================================
# STRUCTURE
# ============================================================

def structure_score(closes):

    if len(closes) < 30:

        return 0

    e9 = ema(
        closes,
        9
    )[-1]

    e21 = ema(
        closes,
        21
    )[-1]

    e50 = ema(
        closes,
        50
    )[-1]

    price = closes[-1]

    score = 0

    if price > e9:

        score += 2

    if e9 > e21:

        score += 2

    if e21 > e50:

        score += 2

    recent = closes[-12:]

    if len(recent) >= 8:

        first_half = average(
            recent[:6]
        )

        second_half = average(
            recent[-6:]
        )

        if second_half > first_half:

            score += 2

    return clamp(
        score,
        0,
        8
    )


# ============================================================
# RESISTANCE
# ============================================================

def resistance_distance(closes):

    if len(closes) < 20:

        return 99.0

    price = closes[-1]

    lookback = closes[-25:-1]

    if not lookback:

        return 99.0

    resistance = max(
        lookback
    )

    if resistance <= 0:

        return 99.0

    distance = (
        (
            resistance
            - price
        )
        / price
    ) * 100

    return max(
        0.0,
        distance
    )


# ============================================================
# CALM SCORE
# ============================================================

def calm_score(closes):

    if len(closes) < 20:

        return 0

    recent = closes[-20:]

    returns = []

    for i in range(
        1,
        len(recent)
    ):

        if recent[i - 1] != 0:

            returns.append(
                abs(
                    (
                        recent[i]
                        - recent[i - 1]
                    )
                    / recent[i - 1]
                ) * 100
            )

    if not returns:

        return 0

    avg_move = average(
        returns
    )

    if avg_move <= 0.35:

        return 10

    if avg_move <= 0.60:

        return 8

    if avg_move <= 0.90:

        return 5

    if avg_move <= 1.30:

        return 2

    return 0


# ============================================================
# MOMENTUM
# ============================================================

def momentum_percent(
    closes,
    candles_back
):

    if len(closes) <= candles_back:

        return 0.0

    old = closes[
        -1 - candles_back
    ]

    new = closes[-1]

    if old == 0:

        return 0.0

    return (
        (
            new - old
        )
        / old
    ) * 100


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(
    symbol,
    book
):

    try:

        closes_1h, volumes_1h = get_candles(
            symbol,
            "60",
            COUNTBACK_1H
        )

        closes_15m, volumes_15m = get_candles(
            symbol,
            "15",
            COUNTBACK_15M
        )

        # ----------------------------------------------------
        # PRICE / MOMENTUM
        # ----------------------------------------------------

        price = closes_1h[-1]

        m15 = momentum_percent(
            closes_15m,
            1
        )

        m1h = momentum_percent(
            closes_1h,
            1
        )

        m4h = momentum_percent(
            closes_1h,
            4
        )

        m8h = momentum_percent(
            closes_1h,
            8
        )

        # ----------------------------------------------------
        # 1H VOLUME
        # ----------------------------------------------------

        current_volume = volumes_1h[-1]

        previous_volumes = (
            volumes_1h[-11:-1]
        )

        med_volume = median(
            previous_volumes
        )

        if med_volume > 0:

            volume_ratio = (
                current_volume
                / med_volume
            )

        else:

            volume_ratio = 0.0

        last3 = volumes_1h[-3:]

        prev8 = volumes_1h[-11:-3]

        avg_last3 = average(
            last3
        )

        avg_prev8 = average(
            prev8
        )

        if avg_prev8 > 0:

            volume_acceleration = (
                avg_last3
                / avg_prev8
            )

        else:

            volume_acceleration = 0.0

        # ----------------------------------------------------
        # FAST 15M VOLUME
        # ----------------------------------------------------

        current_volume_15m = (
            volumes_15m[-1]
        )

        previous_volumes_15m = (
            volumes_15m[-13:-1]
        )

        med_volume_15m = median(
            previous_volumes_15m
        )

        if med_volume_15m > 0:

            volume_ratio_15m = (
                current_volume_15m
                / med_volume_15m
            )

        else:

            volume_ratio_15m = 0.0

        last3_15m = (
            volumes_15m[-3:]
        )

        prev8_15m = (
            volumes_15m[-11:-3]
        )

        avg_last3_15m = average(
            last3_15m
        )

        avg_prev8_15m = average(
            prev8_15m
        )

        if avg_prev8_15m > 0:

            volume_acceleration_15m = (
                avg_last3_15m
                / avg_prev8_15m
            )

        else:

            volume_acceleration_15m = 0.0

        # ----------------------------------------------------
        # RSI / MACD
        # ----------------------------------------------------

        rsi_value = rsi(
            closes_1h,
            14
        )

        macd_line, macd_signal = macd(
            closes_1h
        )

        macd_positive = (
            macd_line
            >= macd_signal
        )

        # ----------------------------------------------------
        # STRUCTURE
        # ----------------------------------------------------

        structure = structure_score(
            closes_1h
        )

        resistance = resistance_distance(
            closes_1h
        )

        calm = calm_score(
            closes_1h
        )

        # ----------------------------------------------------
        # ORDER FLOW
        # ----------------------------------------------------

        flow = safe_float(
            book.get("flow"),
            0.0
        )

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        score = 0.0

        # Volume ratio
        if 1.25 <= volume_ratio <= 4:

            score += 18

        elif 1.05 <= volume_ratio < 1.25:

            score += 10

        elif 4 < volume_ratio <= 6:

            score += 8

        elif 6 < volume_ratio <= 10:

            score += 2

        elif volume_ratio > 10:

            score -= 8

        # Volume acceleration
        if volume_acceleration >= 1.20:

            score += 10

        elif volume_acceleration >= 1.05:

            score += 7

        elif volume_acceleration >= 1.00:

            score += 3

        # RSI
        if 45 <= rsi_value <= 60:

            score += 15

        elif 60 < rsi_value <= 68:

            score += 10

        elif 40 <= rsi_value < 45:

            score += 7

        elif 35 <= rsi_value < 40:

            score += 3

        elif rsi_value > 72:

            score -= 10

        # Structure
        score += (
            structure
            * 2.5
        )

        # Resistance
        if 0 <= resistance <= 1:

            score += 15

        elif 1 < resistance <= 2:

            score += 12

        elif 2 < resistance <= 3:

            score += 8

        elif 3 < resistance <= 5:

            score += 4

        # Calm
        score += calm

        # Timeframes
        if 0 <= m1h <= 2.5:

            score += 4

        if 0 <= m4h <= 6:

            score += 4

        # Avoid already moving coins
        if m1h > 4:

            score -= 8

        if m4h > 10:

            score -= 10

        if m8h > 15:

            score -= 8

        if m15 > 2:

            score -= 5

        # MACD
        if macd_positive:

            score += 2

        # Order flow
        if flow >= 1.20:

            score += 5

        elif flow >= 1.05:

            score += 3

        elif flow >= 0.95:

            score += 1

        elif 0 < flow < 0.80:

            score -= 5

        score = int(
            clamp(
                round(score),
                0,
                100
            )
        )

        # ----------------------------------------------------
        # PRE-MOVE GATE
        # ----------------------------------------------------

        pre_move_gate = (
            1.15 <= volume_ratio <= 6
            and structure >= 5
            and 45 <= rsi_value <= 68
            and 0 <= resistance <= 3.5
            and m1h <= 2.8
            and m4h <= 7
            and m15 <= 2
        )

        # ----------------------------------------------------
        # FAST PRE-MOVE
        #
        # This is deliberately separate from the old
        # CONFIRMED PRE-MOVE system.
        # ----------------------------------------------------

        fast_pre_move = (
            volume_ratio_15m >= 1.80
            and volume_acceleration_15m >= 1.25
            and structure >= 5
            and 45 <= rsi_value <= 68
            and 0 <= resistance <= 3.5
            and m1h <= 2.8
            and m4h <= 7
            and m15 <= 2
            and flow >= 0.95
            and score >= 55
        )

        # ----------------------------------------------------
        # HIGH-RISK JUMP
        #
        # Prevent FAST alert when the move is already too
        # aggressive or overheated.
        # ----------------------------------------------------

        high_risk_jump = (
            m15 > 3
            or m1h > 4
            or rsi_value > 72
            or volume_ratio_15m > 6
            or volume_acceleration_15m > 3
        )

        # ----------------------------------------------------
        # QUALITY
        # ----------------------------------------------------

        quality = True

        if rsi_value > 72:

            quality = False

        if m1h > 4:

            quality = False

        if m4h > 10:

            quality = False

        if volume_ratio > 10:

            quality = False

        # ----------------------------------------------------
        # LABEL
        # ----------------------------------------------------

        if pre_move_gate and score >= 75:

            label = "PRE-MOVE"

        elif pre_move_gate and score >= 65:

            label = "EARLY RADAR"

        elif (
            quality
            and score >= 55
        ):

            label = "WATCH"

        elif (
            m1h > 4
            or m4h > 10
        ):

            label = "ALREADY MOVING"

        else:

            label = "LOW"

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "label": label,
            "pre_move_gate": pre_move_gate,
            "quality": quality,

            "volume_ratio": volume_ratio,
            "volume_acceleration": volume_acceleration,

            "volume_ratio_15m": volume_ratio_15m,
            "volume_acceleration_15m": (
                volume_acceleration_15m
            ),

            "fast_pre_move": fast_pre_move,
            "high_risk_jump": high_risk_jump,

            "rsi": rsi_value,
            "structure": structure,
            "resistance": resistance,
            "calm": calm,

            "momentum_15m": m15,
            "momentum_1h": m1h,
            "momentum_4h": m4h,
            "momentum_8h": m8h,

            "order_flow": flow,
            "macd_positive": macd_positive,
        }

    except Exception as e:

        return {
            "symbol": symbol,
            "error": str(e),
        }


# ============================================================
# PERSISTENCE
# ============================================================

def apply_persistence(
    result,
    previous_state
):

    symbol = result["symbol"]

    previous = previous_state.get(
        symbol,
        {}
    )

    old_score = safe_float(
        previous.get("score"),
        0
    )

    current_score = safe_float(
        result.get("score"),
        0
    )

    old_streak = int(
        safe_float(
            previous.get("streak"),
            0
        )
    )

    if (
        current_score >= 65
        and old_score >= 65
    ):

        streak = (
            old_streak
            + 1
        )

    elif current_score >= 65:

        streak = 1

    else:

        streak = 0

    strengthening = (
        current_score
        >= old_score + 5
    )

    confirmed = (
        result.get(
            "pre_move_gate"
        )
        and current_score >= 75
        and streak >= 2
    )

    result["previous_score"] = (
        old_score
    )

    result["streak"] = streak

    result["strengthening"] = (
        strengthening
    )

    result["confirmed"] = (
        confirmed
    )

    return result


# ============================================================
# CONFIRMED TELEGRAM ALERT
# ============================================================

def build_alert(result):

    symbol = result["symbol"]

    return (
        "🚨 CONFIRMED PRE-MOVE\n\n"

        f"🪙 {symbol}\n"

        f"💰 Price: "
        f"{result['price']:.8g}\n"

        f"⭐ Score: "
        f"{result['score']}/100\n"

        f"🔥 Streak: "
        f"{result['streak']}\n\n"

        f"📊 Volume: "
        f"{result['volume_ratio']:.2f}x\n"

        f"⚡ Volume acceleration: "
        f"{result['volume_acceleration']:.2f}x\n"

        f"📈 RSI: "
        f"{result['rsi']:.1f}\n"

        f"🏗 Structure: "
        f"{result['structure']}/8\n"

        f"🎯 Resistance: "
        f"{result['resistance']:.2f}%\n"

        f"🌊 Order flow: "
        f"{result['order_flow']:.2f}\n\n"

        f"15m: "
        f"{result['momentum_15m']:+.2f}%\n"

        f"1H: "
        f"{result['momentum_1h']:+.2f}%\n"

        f"4H: "
        f"{result['momentum_4h']:+.2f}%\n"

        f"8H: "
        f"{result['momentum_8h']:+.2f}%\n\n"

        "🟢 شرایط قبل از حرکت صعودی تأیید شده."
    )


# ============================================================
# FAST TELEGRAM ALERT
# ============================================================

def build_fast_alert(result):

    return (
        "⚡ FAST PRE-MOVE\n\n"

        f"🪙 {result['symbol']}\n"

        f"💰 Price: "
        f"{result['price']:.8g}\n"

        f"⭐ Score: "
        f"{result['score']}/100\n"

        f"⚡ 15m Volume: "
        f"{result['volume_ratio_15m']:.2f}x\n"

        f"🚀 15m Volume acceleration: "
        f"{result['volume_acceleration_15m']:.2f}x\n"

        f"📈 RSI: "
        f"{result['rsi']:.1f}\n"

        f"🏗 Structure: "
        f"{result['structure']}/8\n"

        f"🎯 Resistance: "
        f"{result['resistance']:.2f}%\n"

        f"🌊 Order flow: "
        f"{result['order_flow']:.2f}\n\n"

        f"15m: "
        f"{result['momentum_15m']:+.2f}%\n"

        f"1H: "
        f"{result['momentum_1h']:+.2f}%\n"

        f"4H: "
        f"{result['momentum_4h']:+.2f}%\n\n"

        "🟡 هشدار زودهنگام است؛ "
        "هنوز تأیید کامل PRE-MOVE نیست."
    )


# ============================================================
# SMART CONFIRMED ALERT
# ============================================================

def smart_alert(
    result,
    alert_state
):

    if not result.get(
        "confirmed"
    ):

        return False

    if result.get(
        "score",
        0
    ) < 80:

        return False

    if result.get(
        "streak",
        0
    ) < 2:

        return False

    if result.get(
        "order_flow",
        0
    ) < 1.05:

        return False

    symbol = result["symbol"]

    old = alert_state.get(
        symbol,
        {}
    )

    old_score = safe_float(
        old.get("score"),
        0
    )

    old_streak = int(
        safe_float(
            old.get("streak"),
            0
        )
    )

    # Prevent repeated identical confirmed alerts.
    if (
        old_score >= 80
        and old_streak >= 2
        and result["score"]
        <= old_score + 2
    ):

        return False

    message = build_alert(
        result
    )

    sent = telegram_send(
        message
    )

    if sent:

        # Preserve FAST state.
        entry = {
            "score": result["score"],
            "streak": result["streak"],
            "timestamp": int(
                time.time()
            ),
        }

        if (
            "fast_score"
            in old
        ):

            entry["fast_score"] = (
                old["fast_score"]
            )

        if (
            "fast_timestamp"
            in old
        ):

            entry["fast_timestamp"] = (
                old["fast_timestamp"]
            )

        alert_state[symbol] = (
            entry
        )

    return sent


# ============================================================
# FAST ALERT
# ============================================================

def fast_alert(
    result,
    alert_state
):

    # Must pass FAST conditions.
    if not result.get(
        "fast_pre_move"
    ):

        return False

    # Reject already aggressive jumps.
    if result.get(
        "high_risk_jump"
    ):

        return False

    symbol = result["symbol"]

    old = alert_state.get(
        symbol,
        {}
    )

    old_fast_score = safe_float(
        old.get(
            "fast_score"
        ),
        0
    )

    old_fast_timestamp = int(
        safe_float(
            old.get(
                "fast_timestamp"
            ),
            0
        )
    )

    now = int(
        time.time()
    )

    # --------------------------------------------------------
    # Anti-spam:
    # Same FAST signal is not repeated for 2 hours.
    #
    # If score becomes at least 5 points stronger,
    # another FAST alert is allowed.
    # --------------------------------------------------------

    if (
        old_fast_timestamp > 0
        and (
            now
            - old_fast_timestamp
            < 7200
        )
        and result["score"]
        < old_fast_score + 5
    ):

        return False

    message = build_fast_alert(
        result
    )

    sent = telegram_send(
        message
    )

    if sent:

        current = alert_state.get(
            symbol,
            {}
        )

        current["fast_score"] = (
            result["score"]
        )

        current["fast_timestamp"] = (
            now
        )

        alert_state[symbol] = (
            current
        )

    return sent


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan():

    print(
        "=" * 70
    )

    print(
        "NOBITEX EARLY MOVE RADAR"
    )

    print(
        time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # LOAD STATES
    # --------------------------------------------------------

    previous_state = load_json(
        STATE_PATH,
        {}
    )

    alert_state = load_json(
        ALERT_STATE_PATH,
        {}
    )

    # --------------------------------------------------------
    # MARKET SNAPSHOT
    # --------------------------------------------------------

    try:

        books = get_market_snapshot()

    except Exception as e:

        print(
            "MARKET SNAPSHOT FAILED:",
            e
        )

        return

    # --------------------------------------------------------
    # EXCLUDED SYMBOLS
    # --------------------------------------------------------

    EXCLUDED_SYMBOLS = {
        "USDCUSDT",
        "USDEUSDT",
        "DAIUSDT",
        "XAUTUSDT",
        "PAXGUSDT",
        "WBTCUSDT",
    }

    symbols = sorted(
        symbol
        for symbol in books.keys()
        if symbol
        not in EXCLUDED_SYMBOLS
    )

    print(
        "USDT markets:",
        len(symbols)
    )

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    results = []

    failed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
                books[symbol]
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            futures
        ):

            symbol = futures[
                future
            ]

            try:

                result = future.result()

                if "error" in result:

                    failed += 1

                    print(
                        symbol,
                        "FAILED:",
                        result["error"]
                    )

                    continue

                result = apply_persistence(
                    result,
                    previous_state
                )

                results.append(
                    result
                )

            except Exception as e:

                failed += 1

                print(
                    symbol,
                    "FAILED:",
                    e
                )

    # --------------------------------------------------------
    # SAVE PERSISTENCE
    # --------------------------------------------------------

    new_state = {}

    for result in results:

        symbol = result[
            "symbol"
        ]

        new_state[symbol] = {
            "score": result[
                "score"
            ],

            "streak": result[
                "streak"
            ],

            "timestamp": int(
                time.time()
            ),
        }

    save_json(
        STATE_PATH,
        new_state
    )

    # --------------------------------------------------------
    # CANDIDATES
    # --------------------------------------------------------

    candidates = [

        r
        for r in results

        if (
            r.get(
                "pre_move_gate"
            )
            and r.get(
                "quality"
            )
            and r.get(
                "score",
                0
            ) >= 65
        )

    ]

    candidates.sort(
        key=lambda r: (
            r.get(
                "score",
                0
            ),

            1
            if r.get(
                "strengthening"
            )
            else 0,

            r.get(
                "streak",
                0
            ),

            r.get(
                "order_flow",
                0
            ),

            r.get(
                "structure",
                0
            ),

            r.get(
                "volume_ratio",
                0
            ),
        ),

        reverse=True
    )

    top = candidates[
        :TOP_N
    ]

    # --------------------------------------------------------
    # PRINT
    # --------------------------------------------------------

    print()

    print(
        f"Valid: {len(results)} | "
        f"Failed: {failed}"
    )

    print()

    print(
        "TOP PRE-MOVE CANDIDATES"
    )

    print(
        "-" * 70
    )

    if not top:

        print(
            "No valid pre-move candidates."
        )

    for i, r in enumerate(
        top,
        1
    ):

        print(
            f"{i}. "
            f"{r['symbol']} | "
            f"{r['label']} | "
            f"Score {r['score']} | "
            f"Streak {r['streak']} | "
            f"Vol {r['volume_ratio']:.2f}x | "
            f"Flow {r['order_flow']:.2f} | "
            f"RSI {r['rsi']:.1f} | "
            f"Res {r['resistance']:.2f}%"
        )

    # --------------------------------------------------------
    # SMART ALERTS
    # --------------------------------------------------------

    confirmed_alert_count = 0

    fast_alert_count = 0

    # FAST is intentionally checked first.
    for result in results:

        if fast_alert(
            result,
            alert_state
        ):

            fast_alert_count += 1

        if smart_alert(
            result,
            alert_state
        ):

            confirmed_alert_count += 1

    save_json(
        ALERT_STATE_PATH,
        alert_state
    )

    # --------------------------------------------------------
    # WATCHLIST
    # --------------------------------------------------------

    watchlist = [

        r
        for r in results

        if r.get(
            "score",
            0
        ) >= 55

    ]

    watchlist.sort(
        key=lambda r: r.get(
            "score",
            0
        ),

        reverse=True
    )

    print()

    print(
        "WATCHLIST"
    )

    print(
        "-" * 70
    )

    for r in watchlist[:15]:

        fast_mark = ""

        if (
            r.get(
                "fast_pre_move"
            )
            and not r.get(
                "high_risk_jump"
            )
        ):

            fast_mark = " ⚡FAST"

        print(
            f"{r['symbol']:12} "
            f"{r['label']:15} "
            f"{r['score']:3}/100 "
            f"streak={r['streak']} "
            f"vol={r['volume_ratio']:.2f}x "
            f"15mVol={r['volume_ratio_15m']:.2f}x "
            f"flow={r['order_flow']:.2f}"
            f"{fast_mark}"
        )

    print()

    print(
        "Confirmed Telegram alerts sent:",
        confirmed_alert_count
    )

    print(
        "FAST Telegram alerts sent:",
        fast_alert_count
    )

    print()

    print(
        "SCAN FINISHED."
    )


# ============================================================
# ENTRY
# ============================================================

def main():

    if TELEGRAM_TEST_ON_START:

        telegram_test()

    run_scan()


if __name__ == "__main__":

    main()
