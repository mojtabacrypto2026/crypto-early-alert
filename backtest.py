import urllib.request
import urllib.parse
import time
import csv
import statistics

BASE_URL = "https://api.nobitex.ir"

# تنظیمات بک‌تست
RESOLUTION = "15"       # کندل 15 دقیقه‌ای
COUNTBACK = 700         # حدود 7 روز داده
COOLDOWN_BARS = 8       # جلوگیری از چند هشدار پشت سر هم

TARGETS = {
    "5%": 0.05,
    "10%": 0.10,
    "20%": 0.20
}

FUTURE_WINDOWS = {
    "1h": 4,
    "2h": 8,
    "4h": 16
}


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CryptoEarlyAlert/1.0"}
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def get_market_stats():
    url = BASE_URL + "/market/stats"
    data = get_json(url)

    import json
    result = json.loads(data)

    if result.get("status") != "ok":
        raise Exception("خطا در دریافت market stats")

    return result["stats"]


def get_markets(stats):
    markets = []

    for key, value in stats.items():

        if "-" not in key:
            continue

        if value.get("isClosed", False):
            continue

        parts = key.upper().split("-")

        if len(parts) != 2:
            continue

        base = parts[0]
        quote = parts[1]

        # بازارهای ریالی و تتری
        if quote not in ["IRT", "RLS", "USDT"]:
            continue

        symbol = base + quote

        markets.append({
            "market": key.upper(),
            "symbol": symbol,
            "base": base,
            "quote": quote
        })

    return markets


def get_ohlc(symbol):

    params = {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "countback": COUNTBACK,
        "to": int(time.time())
    }

    url = BASE_URL + "/market/udf/history?" + urllib.parse.urlencode(params)

    try:
        data = get_json(url)

        import json
        result = json.loads(data)

        if result.get("s") != "ok":
            return None

        candles = []

        for i in range(len(result["t"])):

            candles.append({
                "t": int(result["t"][i]),
                "o": float(result["o"][i]),
                "h": float(result["h"][i]),
                "l": float(result["l"][i]),
                "c": float(result["c"][i]),
                "v": float(result["v"][i])
            })

        candles.sort(key=lambda x: x["t"])

        # حذف زمان‌های تکراری
        unique = {}
        for candle in candles:
            unique[candle["t"]] = candle

        return list(unique.values())

    except Exception as e:
        print("خطا در", symbol, ":", e)
        return None


def pct(a, b):

    if a == 0:
        return 0

    return ((b - a) / a) * 100


def calculate_score(candles, i):

    if i < 20:
        return 0

    close = candles[i]["c"]

    # تغییر 15 دقیقه
    change_15m = pct(
        candles[i - 1]["c"],
        close
    )

    # تغییر تقریبی 1 ساعت
    change_1h = pct(
        candles[i - 4]["c"],
        close
    )

    # تغییر 4 ساعت
    change_4h = pct(
        candles[i - 16]["c"],
        close
    )

    # میانگین حجم 20 کندل قبل
    previous_volumes = [
        candles[x]["v"]
        for x in range(i - 20, i)
        if candles[x]["v"] > 0
    ]

    if not previous_volumes:
        return 0

    avg_volume = statistics.mean(previous_volumes)

    if avg_volume == 0:
        return 0

    volume_ratio = candles[i]["v"] / avg_volume

    score = 0

    # افزایش حجم
    if volume_ratio >= 3:
        score += 30
    elif volume_ratio >= 2:
        score += 25
    elif volume_ratio >= 1.5:
        score += 18
    elif volume_ratio >= 1.2:
        score += 10

    # حرکت کوتاه مدت
    if 0.2 <= change_15m <= 2.5:
        score += 15
    elif change_15m > 2.5:
        score += 5

    # حرکت یک ساعته
    if 0.5 <= change_1h <= 5:
        score += 15
    elif change_1h > 5:
        score += 5

    # روند 4 ساعته
    if change_4h > 0:
        score += 5

    # کندل مثبت
    if candles[i]["c"] > candles[i]["o"]:
        score += 5

    # شکست سقف 20 کندل گذشته
    previous_highs = [
        candles[x]["h"]
        for x in range(i - 20, i)
    ]

    if close > max(previous_highs):
        score += 15

    # اگر در همین یک ساعت بیش از حد رشد کرده باشد
    # احتمالاً دیر شده است
    if change_1h > 10:
        score -= 15

    return max(0, min(score, 100))


def evaluate_signal(candles, i):

    entry = candles[i]["c"]

    result = {
        "entry": entry,
        "score": calculate_score(candles, i)
    }

    for window_name, bars in FUTURE_WINDOWS.items():

        future = candles[
            i + 1:
            min(len(candles), i + 1 + bars)
        ]

        if not future:
            result[window_name] = {
                "max_gain": None,
                "hit_5": False,
                "hit_10": False,
                "hit_20": False
            }
            continue

        max_high = max(x["h"] for x in future)

        max_gain = (max_high - entry) / entry

        result[window_name] = {
            "max_gain": max_gain,
            "hit_5": max_gain >= 0.05,
            "hit_10": max_gain >= 0.10,
            "hit_20": max_gain >= 0.20
        }

    return result


def backtest_market(market, candles):

    if len(candles) < 100:
        return []

    signals = []

    last_signal_index = -999

    # آخرین کندل‌هایی که آینده کافی برای ارزیابی دارند
    end_index = len(candles) - max(FUTURE_WINDOWS.values()) - 1

    for i in range(20, end_index):

        score = calculate_score(candles, i)

        if score < 65:
            continue

        # جلوگیری از چند سیگنال متوالی
        if i - last_signal_index < COOLDOWN_BARS:
            continue

        result = evaluate_signal(candles, i)

        result["market"] = market
        result["index"] = i
        result["timestamp"] = candles[i]["t"]

        signals.append(result)

        last_signal_index = i

    return signals


def main():

    print("=" * 60)
    print("NOBITEX CRYPTO EARLY ALERT - BACKTEST")
    print("=" * 60)

    print("\nدریافت فهرست بازارهای نوبیتکس...")

    stats = get_market_stats()
    markets = get_markets(stats)

    print("تعداد بازارهای فعال:", len(markets))

    all_signals = []

    for number, market in enumerate(markets, 1):

        print(
            f"[{number}/{len(markets)}] "
            f"{market['symbol']} ..."
        )

        candles = get_ohlc(market["symbol"])

        if candles is None:
            continue

        signals = backtest_market(
            market["market"],
            candles
        )

        for signal in signals:
            all_signals.append(signal)

        # کمی فاصله برای فشار نیاوردن به API
        time.sleep(1)

    print("\n")
    print("=" * 60)
    print("نتیجه بک‌تست")
    print("=" * 60)

    print("تعداد کل سیگنال‌ها:", len(all_signals))

    if not all_signals:
        print("هیچ سیگنالی پیدا نشد.")
        return

    # آمار کلی
    for window in FUTURE_WINDOWS:

        print("\n---", window, "---")

        signals = [
            x for x in all_signals
            if x[window]["max_gain"] is not None
        ]

        if not signals:
            continue

        for target_name, target in TARGETS.items():

            hits = sum(
                1
                for x in signals
                if x[window]["max_gain"] >= target
            )

            rate = (hits / len(signals)) * 100

            print(
                target_name,
                ":",
                hits,
                "/",
                len(signals),
                "=",
                round(rate, 2),
                "%"
            )

        gains = [
            x[window]["max_gain"] * 100
            for x in signals
        ]

        print(
            "میانگین بیشترین رشد:",
            round(statistics.mean(gains), 2),
            "%"
        )

        print(
            "میانه بیشترین رشد:",
            round(statistics.median(gains), 2),
            "%"
        )

    # ذخیره نتایج
    with open(
        "backtest_results.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "market",
            "timestamp",
            "score",
            "entry",
            "1h_max_gain",
            "2h_max_gain",
            "4h_max_gain",
            "1h_hit_5",
            "1h_hit_10",
            "1h_hit_20",
            "2h_hit_5",
            "2h_hit_10",
            "2h_hit_20",
            "4h_hit_5",
            "4h_hit_10",
            "4h_hit_20"
        ])

        for x in all_signals:

            writer.writerow([
                x["market"],
                x["timestamp"],
                x["score"],
                x["entry"],

                x["1h"]["max_gain"],
                x["2h"]["max_gain"],
                x["4h"]["max_gain"],

                x["1h"]["hit_5"],
                x["1h"]["hit_10"],
                x["1h"]["hit_20"],

                x["2h"]["hit_5"],
                x["2h"]["hit_10"],
                x["2h"]["hit_20"],

                x["4h"]["hit_5"],
                x["4h"]["hit_10"],
                x["4h"]["hit_20"]
            ])

    print("\nفایل backtest_results.csv ساخته شد.")
    print("این فایل نتایج تمام سیگنال‌ها را نگه می‌دارد.")


if __name__ == "__main__":
    main()
