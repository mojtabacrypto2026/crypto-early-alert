import os
import json
import time
import urllib.request
import urllib.parse

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ارزهایی که در نوبیتکس بررسی می‌شوند
COINS = [
    "btc", "eth", "sol", "xrp", "ada", "doge",
    "avax", "link", "uni", "sushi", "aave",
    "dot", "atom", "ltc", "bch", "etc",
    "near", "algo", "fil", "apt", "arb",
    "op", "inj", "pepe", "shib",
    "cvc", "api3", "gmt", "t"
]

WATCH = 68
TRADE = 80


# -----------------------------
# دریافت اطلاعات از نوبیتکس
# -----------------------------

def get_json(url, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Nobitex-Early-Alert/4.0"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=20
            ) as response:

                return json.loads(
                    response.read().decode()
                )

        except Exception as e:
            last_error = e

            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))

    raise last_error


# -----------------------------
# دریافت کندل
# -----------------------------

def candles(symbol, resolution, count=120):

    now = int(time.time())

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "countback": count,
        "to": now
    })

    url = (
        "https://api.nobitex.ir/"
        "market/udf/history?"
        + params
    )

    data = get_json(url)

    if data.get("s") != "ok":
        return []

    keys = ["t", "o", "h", "l", "c", "v"]

    try:
        n = min(
            len(data.get(k, []))
            for k in keys
        )
    except Exception:
        return []

    rows = []

    for i in range(n):

        try:

            rows.append({
                "t": int(data["t"][i]),
                "o": float(data["o"][i]),
                "h": float(data["h"][i]),
                "l": float(data["l"][i]),
                "c": float(data["c"][i]),
                "v": float(data["v"][i])
            })

        except Exception:
            continue

    rows.sort(
        key=lambda x: x["t"]
    )

    # حذف کندل در حال تشکیل
    if len(rows) > 2:
        rows = rows[:-1]

    return rows


# -----------------------------
# محاسبات
# -----------------------------

def average(values):

    if not values:
        return 0

    return sum(values) / len(values)


def percent(a, b):

    if not b:
        return 0

    return ((a - b) / b) * 100


# -----------------------------
# تحلیل یک ارز
# -----------------------------

def scan_market(symbol):

    # 1 دقیقه
    c1 = candles(
        symbol,
        1,
        120
    )

    # 5 دقیقه
    c5 = candles(
        symbol,
        5,
        120
    )

    # 15 دقیقه
    c15 = candles(
        symbol,
        15,
        120
    )

    if (
        len(c1) < 30
        or len(c5) < 30
        or len(c15) < 20
    ):
        return None

    last = c5[-1]

    price = last["c"]

    # -------------------------
    # حرکت قیمت
    # -------------------------

    m1 = percent(
        price,
        c1[-2]["c"]
    )

    m5 = percent(
        price,
        c5[-2]["c"]
    )

    m15 = percent(
        price,
        c15[-2]["c"]
    )

    m30 = percent(
        price,
        c5[-7]["c"]
    )

    m60 = percent(
        price,
        c5[-13]["c"]
    )

    # -------------------------
    # حجم
    # -------------------------

    old_volume = average([
        x["v"]
        for x in c5[-13:-1]
    ])

    if old_volume > 0:
        volume_ratio = (
            last["v"] /
            old_volume
        )
    else:
        volume_ratio = 0

    recent_volume = average([
        x["v"]
        for x in c5[-4:]
    ])

    previous_volume = average([
        x["v"]
        for x in c5[-12:-4]
    ])

    if previous_volume > 0:
        volume_acceleration = (
            recent_volume /
            previous_volume
        )
    else:
        volume_acceleration = 0

    # -------------------------
    # فشار خرید
    # -------------------------

    candle_range = (
        last["h"] -
        last["l"]
    )

    if candle_range > 0:

        pressure = (
            last["c"] -
            last["l"]
        ) / candle_range

    else:

        pressure = 0.5

    # -------------------------
    # مقاومت
    # -------------------------

    resistance = max(
        x["h"]
        for x in c5[-37:-1]
    )

    resistance_distance = percent(
        price,
        resistance
    )

    # -------------------------
    # امتیاز
    # -------------------------

    score = 0

    reasons = []

    # حرکت 1 دقیقه
    if 0.08 <= m1 < 0.50:

        score += 12

        reasons.append(
            "شتاب تازه 1m"
        )

    # حرکت 5 دقیقه
    elif 0.10 <= m5 < 0.80:

        score += 14

        reasons.append(
            "شتاب تازه 5m"
        )

    elif 0.80 <= m5 < 1.50:

        score += 7

    # روند 15 دقیقه
    if 0.15 <= m15 < 2:

        score += 13

        reasons.append(
            "روند 15m مثبت"
        )

    elif m15 >= 2:

        score += 4

    # روند 1 ساعت
    if 0.20 <= m60 < 3.5:

        score += 8

        reasons.append(
            "روند 1h مثبت"
        )

    elif m60 >= 3.5:

        score -= 6

        reasons.append(
            "رشد 1h زیاد"
        )

    # هم‌جهتی
    if (
        m5 > 0
        and m15 > 0
        and m60 > 0
    ):

        score += 10

        reasons.append(
            "هم‌جهتی تایم‌فریم‌ها"
        )

    # حجم
    if volume_ratio >= 2:

        score += 13

        reasons.append(
            "حجم بسیار غیرعادی"
        )

    elif volume_ratio >= 1.5:

        score += 9

        reasons.append(
            "حجم غیرعادی"
        )

    elif volume_ratio >= 1.25:

        score += 5

        reasons.append(
            "افزایش حجم"
        )

    # شتاب حجم
    if volume_acceleration >= 1.5:

        score += 10

        reasons.append(
            "شتاب حجم"
        )

    elif volume_acceleration >= 1.2:

        score += 5

    # فشار خرید
    if pressure >= 0.72:

        score += 7

        reasons.append(
            "فشار خرید"
        )

    # مقاومت
    if (
        -0.35
        <= resistance_distance
        <= 0.25
    ):

        score += 12

        reasons.append(
            "فشار روی مقاومت"
        )

    elif (
        0.25
        < resistance_distance
        <= 1
    ):

        score += 6

        reasons.append(
            "شکست تازه مقاومت"
        )

    # -------------------------
    # جلوگیری از خرید دیرهنگام
    # -------------------------

    if m30 > 4.5:

        score -= 14

        reasons.append(
            "حرکت 30m زیاد؛ احتمالاً دیر شده"
        )

    if m60 > 7:

        score -= 12

    score = max(
        0,
        min(100, score)
    )

    return {
        "score": score,
        "price": price,

        "m1": m1,
        "m5": m5,
        "m15": m15,
        "m30": m30,
        "m60": m60,

        "volume_ratio": volume_ratio,
        "volume_acceleration": volume_acceleration,

        "resistance_distance":
            resistance_distance,

        "pressure": pressure,

        "reasons": reasons
    }


# -----------------------------
# وضعیت بیت‌کوین
# -----------------------------

def bitcoin_regime():

    try:

        btc = scan_market("btc")

        if not btc:
            return True

        return (
            btc["m15"] > -0.8
            and
            btc["m60"] > -1.5
        )

    except Exception as e:

        print(
            "BTC regime error:",
            e
        )

        return True


# -----------------------------
# ارسال تلگرام
# -----------------------------

def send_telegram(message):

    if (
        not BOT_TOKEN
        or not CHAT_ID
    ):

        print(
            "Telegram secrets are not set."
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    try:

        request = urllib.request.Request(
            url,
            data=data
        )

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = json.loads(
                response.read().decode()
            )

            return result.get(
                "ok",
                False
            )

    except Exception as e:

        print(
            "Telegram error:",
            e
        )

        return False


# -----------------------------
# اجرای اصلی
# -----------------------------

def main():

    print(
        "===== NOBITEX EARLY ALERT V4 ====="
    )

    regime = bitcoin_regime()

    print(
        "BTC regime:",
        "FAVORABLE"
        if regime
        else "WEAK"
    )

    results = []

    for coin in COINS:

        if coin == "btc":
            continue

        try:

            result = scan_market(
                coin
            )

            if result:

                results.append(
                    (
                        coin.upper(),
                        result
                    )
                )

                print(
                    coin.upper(),
                    f"score={result['score']}",
                    f"1m={result['m1']:+.2f}%",
                    f"5m={result['m5']:+.2f}%",
                    f"15m={result['m15']:+.2f}%",
                    f"1h={result['m60']:+.2f}%",
                    f"vol={result['volume_ratio']:.2f}x"
                )

        except Exception as e:

            print(
                "ERROR",
                coin.upper(),
                e
            )

    # -------------------------
    # بهترین فرصت‌ها
    # -------------------------

    results.sort(
        key=lambda x:
        x[1]["score"],
        reverse=True
    )

    for coin, x in results[:8]:

        score = x["score"]

        if score < WATCH:
            continue

        if x["m5"] < 0:
            continue

        if x["m15"] <= 0:
            continue

        # جلوگیری از هشدار بعد از پامپ
        if x["m30"] >= 4.5:
            continue

        # هشدار معامله فقط وقتی BTC مناسب است
        if (
            score < TRADE
            and not regime
        ):
            continue

        if (
            score >= TRADE
            and regime
        ):

            tag = (
                "🚨 هشدار معامله"
            )

        else:

            tag = (
                "👀 هشدار دیده‌بانی"
            )

        reasons = ", ".join(
            x["reasons"]
        )

        message = (
            f"{tag}\n"
            f"{coin} — بازار نوبیتکس\n\n"

            f"امتیاز: {score}/100\n"

            f"1m: {x['m1']:+.2f}%\n"
            f"5m: {x['m5']:+.2f}%\n"
            f"15m: {x['m15']:+.2f}%\n"
            f"1h: {x['m60']:+.2f}%\n\n"

            f"30m: {x['m30']:+.2f}%\n"

            f"حجم: "
            f"{x['volume_ratio']:.2f}x\n"

            f"شتاب حجم: "
            f"{x['volume_acceleration']:.2f}x\n"

            f"فاصله مقاومت: "
            f"{x['resistance_distance']:+.2f}%\n"

            f"فشار خرید: "
            f"{x['pressure']:.0%}\n\n"

            f"دلایل:\n"
            f"{reasons}\n\n"

            "⚠️ هشدار برای شناسایی "
            "فشار/شروع حرکت است؛ "
            "تضمین رشد نیست."
        )

        send_telegram(
            message
        )


if __name__ == "__main__":
    main()
