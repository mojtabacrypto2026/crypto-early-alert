import csv
import json
import math
import statistics
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests


BASE_URL = "https://api.nobitex.ir"

# -----------------------------
# تنظیمات بک‌تست
# -----------------------------

RESOLUTION = "15"
COUNTBACK = 700          # حدود 7 روز کندل 15 دقیقه‌ای
REQUEST_DELAY = 0.35     # فاصله درخواست‌ها برای کاهش فشار روی API

MIN_SCORE = 65

# بعد از هر سیگنال، تا این تعداد کندل دوباره سیگنال نده
COOLDOWN_BARS = 8

# اهدافی که بررسی می‌کنیم
TARGETS = [5, 10, 20]

# حداکثر آینده‌ای که برای رسیدن به هدف بررسی می‌شود
FUTURE_BARS = {
    5: 4,     # حدود 1 ساعت
    10: 16,   # حدود 4 ساعت
    20: 16,
}


session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 crypto-early-alert-backtest"
})


# -----------------------------
# ابزارهای عمومی
# -----------------------------

def safe_float(value):
    try:
        x = float(value)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return None


def percent_change(old, new):
    old = safe_float(old)
    new = safe_float(new)

    if old is None or new is None or old == 0:
        return None

    return ((new - old) / old) * 100


def average(values):
    values = [x for x in values if x is not None]

    if not values:
        return None

    return statistics.mean(values)


# -----------------------------
# دریافت بازارهای Nobitex
# -----------------------------

def get_markets():
    url = f"{BASE_URL}/market/stats"

    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()

        data = response.json()

    except Exception as e:
        print(f"ERROR market stats: {e}")
        return []

    markets = []

    # پاسخ Nobitex معمولاً به شکل:
    # {"stats": {"btc-rls": {...}, ...}}

    stats = data.get("stats", {})

    if not isinstance(stats, dict):
        return []

    for symbol, info in stats.items():

        if not isinstance(info, dict):
            continue

        symbol = str(symbol).lower()

        # فقط بازارهای اصلی قابل معامله
        if symbol.endswith("-rls"):
            quote_currency = "RLS"
        elif symbol.endswith("-usdt"):
            quote_currency = "USDT"
        elif symbol.endswith("-irt"):
            quote_currency = "IRT"
        else:
            continue

        # استخراج ارز پایه
        base_currency = symbol.rsplit("-", 1)[0].upper()

        if not base_currency:
            continue

        markets.append({
            "symbol": symbol,
            "base": base_currency,
            "quote": quote_currency,
        })

    # حذف موارد تکراری
    unique = {}
    for market in markets:
        unique[market["symbol"]] = market

    markets = list(unique.values())

    markets.sort(key=lambda x: x["symbol"])

    return markets


# -----------------------------
# دریافت کندل
# -----------------------------

def get_candles(market):
    url = f"{BASE_URL}/market/udf/history"

    params = {
        "symbol": market,
        "resolution": RESOLUTION,
        "countback": COUNTBACK,
    }

    try:
        response = session.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

    except Exception as e:
        print(f"ERROR candles {market}: {e}")
        return []

    if data.get("s") != "ok":
        return []

    timestamps = data.get("t", [])
    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])
    volumes = data.get("v", [])

    length = min(
        len(timestamps),
        len(opens),
        len(highs),
        len(lows),
        len(closes),
        len(volumes),
    )

    candles = []

    for i in range(length):

        try:
            candle = {
                "timestamp": int(timestamps[i]),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": float(volumes[i]),
            }

            candles.append(candle)

        except Exception:
            continue

    candles.sort(key=lambda x: x["timestamp"])

    return candles


# -----------------------------
# محاسبه ویژگی‌های سیگنال
# -----------------------------

def calculate_features(candles, i):

    if i < 25:
        return None

    current = candles[i]

    close = current["close"]
    open_price = current["open"]
    high = current["high"]
    volume = current["volume"]

    if close <= 0:
        return None

    # -------------------------
    # تغییرات قیمت
    # -------------------------

    change_15m = percent_change(
        candles[i - 1]["close"],
        close
    )

    change_1h = percent_change(
        candles[i - 4]["close"],
        close
    )

    change_4h = percent_change(
        candles[i - 16]["close"],
        close
    )

    if change_15m is None:
        return None

    # -------------------------
    # میانگین حجم 20 کندل قبلی
    # -------------------------

    previous_volumes = [
        candles[j]["volume"]
        for j in range(i - 20, i)
        if candles[j]["volume"] > 0
    ]

    avg_volume = average(previous_volumes)

    if avg_volume and avg_volume > 0:
        volume_ratio = volume / avg_volume
    else:
        volume_ratio = 0

    # -------------------------
    # مقاومت 20 کندل قبلی
    # -------------------------

    previous_highs = [
        candles[j]["high"]
        for j in range(i - 20, i)
    ]

    resistance = max(previous_highs)

    breakout_percent = percent_change(
        resistance,
        close
    )

    if breakout_percent is None:
        breakout_percent = 0

    # -------------------------
    # قدرت کندل
    # -------------------------

    candle_change = percent_change(
        open_price,
        close
    )

    if candle_change is None:
        candle_change = 0

    # -------------------------
    # فاصله از میانگین قیمت
    # -------------------------

    previous_closes = [
        candles[j]["close"]
        for j in range(i - 20, i)
    ]

    avg_close = average(previous_closes)

    if avg_close and avg_close > 0:
        distance_from_average = (
            (close - avg_close) / avg_close
        ) * 100
    else:
        distance_from_average = 0

    return {
        "change_15m": change_15m,
        "change_1h": change_1h,
        "change_4h": change_4h,
        "volume_ratio": volume_ratio,
        "breakout_percent": breakout_percent,
        "candle_change": candle_change,
        "distance_from_average": distance_from_average,
    }


# -----------------------------
# امتیازدهی
# -----------------------------

def calculate_score(features):

    score = 0

    # حرکت کوتاه‌مدت مثبت
    if features["change_15m"] >= 0.5:
        score += 10

    # حرکت یک‌ساعته
    if features["change_1h"] >= 1:
        score += 10

    if features["change_1h"] >= 2:
        score += 10

    # حرکت 4 ساعته
    if features["change_4h"] >= 2:
        score += 10

    if features["change_4h"] >= 4:
        score += 10

    # افزایش حجم
    if features["volume_ratio"] >= 1.5:
        score += 10

    if features["volume_ratio"] >= 2:
        score += 10

    # شکست مقاومت
    if features["breakout_percent"] >= 0:
        score += 10

    # کندل مثبت
    if features["candle_change"] > 0:
        score += 5

    # قیمت بالاتر از میانگین
    if features["distance_from_average"] > 0:
        score += 5

    return score


# -----------------------------
# بررسی حرکت بعد از سیگنال
# -----------------------------

def evaluate_future(candles, index):

    entry = candles[index]["close"]

    result = {}

    for target in TARGETS:

        max_bars = FUTURE_BARS[target]

        future_end = min(
            index + max_bars,
            len(candles) - 1
        )

        if future_end <= index:
            result[f"hit_{target}"] = False
            result[f"max_gain_{target}"] = None
            continue

        max_high = max(
            candles[j]["high"]
            for j in range(index + 1, future_end + 1)
        )

        gain = ((max_high - entry) / entry) * 100

        result[f"max_gain_{target}"] = gain
        result[f"hit_{target}"] = gain >= target

    return result


# -----------------------------
# بک‌تست یک بازار
# -----------------------------

def backtest_market(market):

    candles = get_candles(market)

    if len(candles) < 50:
        print(f"{market}: insufficient candles")
        return []

    print(f"{market}: {len(candles)} candles")

    signals = []

    last_signal_index = -COOLDOWN_BARS

    for i in range(25, len(candles)):

        if i - last_signal_index < COOLDOWN_BARS:
            continue

        features = calculate_features(candles, i)

        if not features:
            continue

        score = calculate_score(features)

        if score < MIN_SCORE:
            continue

        future = evaluate_future(candles, i)

        candle = candles[i]

        dt = datetime.fromtimestamp(
            candle["timestamp"],
            tz=timezone.utc
        )

        row = {
            "market": market,
            "time_utc": dt.isoformat(),
            "price": candle["close"],
            "score": score,

            "change_15m": round(
                features["change_15m"], 4
            ),

            "change_1h": round(
                features["change_1h"], 4
            ),

            "change_4h": round(
                features["change_4h"], 4
            ),

            "volume_ratio": round(
                features["volume_ratio"], 4
            ),

            "breakout_percent": round(
                features["breakout_percent"], 4
            ),

            "candle_change": round(
                features["candle_change"], 4
            ),

            "distance_from_average": round(
                features["distance_from_average"], 4
            ),
        }

        row.update(future)

        signals.append(row)

        last_signal_index = i

    return signals


# -----------------------------
# ذخیره CSV
# -----------------------------

def save_results(rows):

    filename = "backtest_results.csv"

    fields = [
        "market",
        "time_utc",
        "price",
        "score",
        "change_15m",
        "change_1h",
        "change_4h",
        "volume_ratio",
        "breakout_percent",
        "candle_change",
        "distance_from_average",
        "max_gain_5",
        "hit_5",
        "max_gain_10",
        "hit_10",
        "max_gain_20",
        "hit_20",
    ]

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)

    return filename


# -----------------------------
# خلاصه نتایج
# -----------------------------

def print_summary(rows, markets_count):

    print()
    print("=" * 60)
    print("NOBITEX BACKTEST SUMMARY")
    print("=" * 60)

    print(f"Markets scanned : {markets_count}")
    print(f"Signals found   : {len(rows)}")

    if not rows:
        print()
        print("No signals found.")
        print("This does NOT mean the strategy works or fails.")
        print("It means the current thresholds produced no signals.")
        return

    for target in TARGETS:

        key = f"hit_{target}"

        values = [
            row[key]
            for row in rows
            if row[key] is not None
        ]

        if not values:
            continue

        wins = sum(
            1 for value in values
            if value is True
        )

        rate = (
            wins / len(values)
        ) * 100

        print(
            f"Target +{target}% : "
            f"{wins}/{len(values)} "
            f"({rate:.2f}%)"
        )

    scores = [
        row["score"]
        for row in rows
        if row.get("score") is not None
    ]

    if scores:
        print(
            f"Average score : "
            f"{statistics.mean(scores):.2f}"
        )

    gains = []

    for row in rows:

        gain = row.get("max_gain_10")

        if gain is not None:
            gains.append(gain)

    if gains:
        print(
            f"Average max gain "
            f"(4h window) : "
            f"{statistics.mean(gains):.2f}%"
        )

    print("=" * 60)


# -----------------------------
# اجرای اصلی
# -----------------------------

def main():

    start_time = time.time()

    print("=" * 60)
    print("NOBITEX EARLY-MOVE BACKTEST")
    print("=" * 60)

    print("Resolution :", RESOLUTION)
    print("Candles    :", COUNTBACK)
    print("Min score  :", MIN_SCORE)
    print()

    markets = get_markets()

    if not markets:

        print("ERROR: No Nobitex markets found.")

        # حتی در صورت خطا فایل خروجی بساز
        save_results([])

        return 0

    print(
        f"Found {len(markets)} markets."
    )

    all_results = []

    successful_markets = 0

    for number, market_info in enumerate(
        markets,
        start=1
    ):

        symbol = market_info["symbol"]

        print()
        print(
            f"[{number}/{len(markets)}] "
            f"{symbol}"
        )

        try:

            results = backtest_market(symbol)

            if results:
                successful_markets += 1
                all_results.extend(results)

        except Exception as e:

            print(
                f"ERROR processing {symbol}: {e}"
            )

        time.sleep(REQUEST_DELAY)

    # همیشه CSV ساخته می‌شود
    filename = save_results(all_results)

    elapsed = time.time() - start_time

    print()
    print_summary(
        all_results,
        successful_markets
    )

    print()
    print(
        f"Result file: {filename}"
    )

    print(
        f"Elapsed time: "
        f"{elapsed:.1f} seconds"
    )

    print()
    print("Backtest finished.")

    # مهم:
    # حتی اگر سیگنال پیدا نشود، Workflow شکست نمی‌خورد.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
