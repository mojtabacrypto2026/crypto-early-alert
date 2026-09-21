# -*- coding: utf-8 -*-

"""
NOBITEX EARLY MOVE RADAR
GitHub Actions / Pydroid compatible
FINAL PRACTICAL VERSION

- Whole Nobitex USDT market scan
- 1H + 15M candles
- Volume expansion
- Volume acceleration
- RSI
- MACD
- EMA structure
- Resistance distance
- Calm/consolidation score
- Multi-timeframe momentum
- Order-book flow
- Persistence / streak
- Confirmed PRE-MOVE alerts
- Telegram
"""

import os
import urllib.request
import urllib.parse
import json
import time
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"

MAX_WORKERS = 4

COUNTBACK_1H = 90
COUNTBACK_15M = 90

TOP_N = 5

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# GitHub runner uses repository directory.
WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")

STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_early_radar_state.json"
)

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json"
)

# Do NOT send startup test on every scheduled run.
TELEGRAM_TEST_ON_START = False

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3


# ============================================================
# HTTP
# ============================================================

def http_get_json(url, params=None):

    if params:
        url = url + "?" + urllib.parse.urlencode(params)

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

                raw = response.read().decode("utf-8")

            return json.loads(raw)

        except Exception as e:

            last_error = e

            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))

    raise last_error


def http_post_json(url, data):

    headers = {
        "User-Agent": "Nobitex-Early-Radar/1.0",
        "Content-Type": "application/json",
    }

    payload = json.dumps(data).encode("utf-8")

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

                raw = response.read().decode("utf-8")

            return json.loads(raw)

        except Exception as e:

            last_error = e

            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))

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

    except Exception as e:

        print("Telegram exception:", e)
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

    return max(low, min(high, x))


def median(values):

    values = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not values:
        return 0.0

    return statistics.median(values)


def average(values):

    values = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not values:
        return 0.0

    return sum(values) / len(values)


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

        print("State load error:", path, e)
        return default


def save_json(path, data):

    try:

        directory = os.path.dirname(path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        temp_path = path + ".tmp"

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

        os.replace(temp_path, path)

        return True

    except Exception as e:

        print("State save error:", path, e)
        return False


# ============================================================
# MARKET SNAPSHOT
# ============================================================

def get_market_snapshot():

    data = http_get_json(
        BASE_URL + "/v3/orderbook/all"
    )

    result = {}

    if not isinstance(data, dict):
        return result

    for symbol, book in data.items():

        symbol = str(symbol).upper()

        if not symbol.endswith("USDT"):
            continue

        if not isinstance(book, dict):
            continue

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid_value = 0.0
        ask_value = 0.0

        try:

            for row in bids[:20]:

                if len(row) >= 2:

                    price = safe_float(row[0])
                    amount = safe_float(row[1])

                    bid_value += price * amount

            for row in asks[:20]:

                if len(row) >= 2:

                    price = safe_float(row[0])
                    amount = safe_float(row[1])

                    ask_value += price * amount

        except Exception:
            pass

        if ask_value > 0:

            flow = bid_value / ask_value

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

def get_candles(symbol, resolution, countback):

    params = {
        "symbol": symbol,
        "resolution": resolution,
        "countback": countback,
        "to": int(time.time()),
    }

    data = http_get_json(
        BASE_URL + "/market/udf/history",
        params
    )

    if not isinstance(data, dict):
        raise ValueError("Invalid candle response")

    if data.get("s") not in ("ok", "no_data"):

        raise ValueError(
            "CANDLE_STATUS_" + str(data.get("s"))
        )

    closes = [
        safe_float(x)
        for x in data.get("c", [])
    ]

    volumes = [
        safe_float(x)
        for x in data.get("v", [])
    ]

    timestamps = data.get("t", [])

    n = min(
        len(closes),
        len(volumes)
    )

    closes = closes[:n]
    volumes = volumes[:n]

    if len(closes) < 25:
        raise ValueError("CANDLES_TOO_SHORT")

    # Remove current open candle if it is still forming.
    if timestamps:

        try:

            last_timestamp = int(timestamps[-1])

            if resolution == "60":
                candle_seconds = 3600
            elif resolution == "15":
                candle_seconds = 900
            else:
                candle_seconds = 3600

            if (
                int(time.time()) - last_timestamp
                < candle_seconds
            ):
                closes = closes[:-1]
                volumes = volumes[:-1]

        except Exception:
            pass

    if len(closes) < 25:
        raise ValueError("CANDLES_TOO_SHORT_AFTER_REMOVE")

    return closes, volumes


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):

    values = [safe_float(x) for x in values]

    if not values:
        return []

    if len(values) < period:
        return [values[-1]] * len(values)

    alpha = 2.0 / (period + 1)

    result = [values[0]]

    for value in values[1:]:

        result.append(
            alpha * value
            + (1 - alpha) * result[-1]
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

    avg_gain = average(gains[:period])
    avg_loss = average(losses[:period])

    for i in range(period, len(gains)):

        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:

        if avg_gain == 0:
            return 50.0

        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (
        100.0 / (1.0 + rs)
    )


def macd(values):

    fast = ema(values, 12)
    slow = ema(values, 26)

    n = min(len(fast), len(slow))

    if n == 0:
        return 0.0, 0.0

    line = [
        fast[i] - slow[i]
        for i in range(n)
    ]

    signal_values = ema(line, 9)

    return line[-1], signal_values[-1]


# ============================================================
# STRUCTURE
# ============================================================

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

    # Recent higher lows / higher closes
    recent = closes[-12:]

    if len(recent) >= 8:

        first_half = average(recent[:6])
        second_half = average(recent[-6:])

        if second_half > first_half:
            score += 2

    return clamp(score, 0, 8)


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

    resistance = max(lookback)

    if resistance <= 0:
        return 99.0

    distance = (
        (resistance - price)
        / price
    ) * 100

    return max(0.0, distance)


# ============================================================
# CALM SCORE
# ============================================================

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
                ) * 100
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


# ============================================================
# MOMENTUM
# ============================================================

def momentum_percent(closes, candles_back):

    if len(closes) <= candles_back:
        return 0.0

    old = closes[-1 - candles_back]
    new = closes[-1]

    if old == 0:
        return 0.0

    return (
        (new - old) / old
    ) * 100


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(symbol, book):

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
        # VOLUME
        # ----------------------------------------------------

        current_volume = volumes_1h[-1]

        previous_volumes = volumes_1h[-11:-1]

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

        avg_last3 = average(last3)
        avg_prev8 = average(prev8)

        if avg_prev8 > 0:

            volume_acceleration = (
                avg_last3
                / avg_prev8
            )

        else:

            volume_acceleration = 0.0

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

        macd_positive = macd_line >= macd_signal

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
        score += structure * 2.5

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

def apply_persistence(result, previous_state):

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

        streak = old_streak + 1

    elif current_score >= 65:

        streak = 1

    else:

        streak = 0

    strengthening = (
        current_score >= old_score + 5
    )

    confirmed = (
        result.get("pre_move_gate")
        and current_score >= 75
        and streak >= 2
    )

    result["previous_score"] = old_score
    result["streak"] = streak
    result["strengthening"] = strengthening
    result["confirmed"] = confirmed

    return result


# ============================================================
# TELEGRAM ALERT
# ============================================================

def build_alert(result):

    symbol = result["symbol"]

    return (
        "🚨 CONFIRMED PRE-MOVE\n\n"
        f"🪙 {symbol}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"⭐ Score: {result['score']}/100\n"
        f"🔥 Streak: {result['streak']}\n\n"
        f"📊 Volume: {result['volume_ratio']:.2f}x\n"
        f"⚡ Volume acceleration: "
        f"{result['volume_acceleration']:.2f}x\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"🏗 Structure: "
        f"{result['structure']}/8\n"
        f"🎯 Resistance: "
        f"{result['resistance']:.2f}%\n"
        f"🌊 Order flow: "
        f"{result['order_flow']:.2f}\n\n"
        f"15m: {result['momentum_15m']:+.2f}%\n"
        f"1H: {result['momentum_1h']:+.2f}%\n"
        f"4H: {result['momentum_4h']:+.2f}%\n"
        f"8H: {result['momentum_8h']:+.2f}%\n\n"
        "🟢 شرایط قبل از حرکت صعودی تأیید شده."
    )


# ============================================================
# SMART ALERT
# ============================================================

def smart_alert(
    result,
    alert_state
):

    if not result.get("confirmed"):
        return False

    if result.get("score", 0) < 80:
        return False

    if result.get("streak", 0) < 2:
        return False

    if result.get("order_flow", 0) < 1.05:
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

    # Prevent repeated identical alerts.
    if (
        old_score >= 80
        and old_streak >= 2
        and result["score"] <= old_score + 2
    ):
        return False

    message = build_alert(result)

    sent = telegram_send(message)

    if sent:

        alert_state[symbol] = {
            "score": result["score"],
            "streak": result["streak"],
            "timestamp": int(time.time()),
        }

    return sent


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan():

    print("=" * 70)
    print("NOBITEX EARLY MOVE RADAR")
    print(time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

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

        print("MARKET SNAPSHOT FAILED:", e)
        return
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
        if symbol not in EXCLUDED_SYMBOLS
    )

symbols = sorted(
    symbol
    for symbol in books.keys()
    if symbol not in EXCLUDED_SYMBOLS
))

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

        for future in as_completed(futures):

            symbol = futures[future]

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

                results.append(result)

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

        symbol = result["symbol"]

        new_state[symbol] = {
            "score": result["score"],
            "streak": result["streak"],
            "timestamp": int(time.time()),
        }

    save_json(
        STATE_PATH,
        new_state
    )

    # --------------------------------------------------------
    # CANDIDATES
    # --------------------------------------------------------

    candidates = [

        r for r in results

        if (
            r.get("pre_move_gate")
            and r.get("quality")
            and r.get("score", 0) >= 65
        )

    ]

    candidates.sort(
        key=lambda r: (
            r.get("score", 0),
            1 if r.get("strengthening") else 0,
            r.get("streak", 0),
            r.get("order_flow", 0),
            r.get("structure", 0),
            r.get("volume_ratio", 0),
        ),
        reverse=True
    )

    top = candidates[:TOP_N]

    # --------------------------------------------------------
    # PRINT
    # --------------------------------------------------------

    print()
    print(
        f"Valid: {len(results)} | Failed: {failed}"
    )

    print()
    print("TOP PRE-MOVE CANDIDATES")
    print("-" * 70)

    if not top:

        print("No valid pre-move candidates.")

    for i, r in enumerate(top, 1):

        print(
            f"{i}. {r['symbol']} | "
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

    alert_count = 0

    for result in results:

        if smart_alert(
            result,
            alert_state
        ):

            alert_count += 1

    save_json(
        ALERT_STATE_PATH,
        alert_state
    )

    # --------------------------------------------------------
    # WATCHLIST
    # --------------------------------------------------------

    watchlist = [

        r for r in results

        if r.get("score", 0) >= 55
    ]

    watchlist.sort(
        key=lambda r: r.get(
            "score",
            0
        ),
        reverse=True
    )

    print()
    print("WATCHLIST")
    print("-" * 70)

    for r in watchlist[:15]:

        print(
            f"{r['symbol']:12} "
            f"{r['label']:15} "
            f"{r['score']:3}/100 "
            f"streak={r['streak']} "
            f"vol={r['volume_ratio']:.2f}x "
            f"flow={r['order_flow']:.2f}"
        )

    print()
    print(
        "Telegram alerts sent:",
        alert_count
    )

    print()
    print("SCAN FINISHED.")


# ============================================================
# ENTRY
# ============================================================

def main():

    if TELEGRAM_TEST_ON_START:

        telegram_test()

    run_scan()


if __name__ == "__main__":
    main()
