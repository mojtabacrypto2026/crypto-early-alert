import os
import json
import time
import urllib.request
import urllib.parse

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WATCH = 68
TRADE = 80

# ارزهای مورد بررسی
COINS = [
    "btc", "eth", "sol", "xrp", "ada", "doge",
    "avax", "link", "uni", "sushi", "aave",
    "dot", "atom", "ltc", "bch", "etc",
    "near", "algo", "fil", "apt", "arb",
    "op", "inj", "pepe", "shib",
    "cvc", "api3", "gmt", "t"
]

BASE_URL = "https://api.nobitex.ir"


def get_json(url, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except Exception as e:
            last_error = e

            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))

    raise last_error


# -------------------------------------------------
# پیدا کردن نمادهای واقعی بازار نوبیتکس
# -------------------------------------------------

def discover_markets():
    markets = {}

    for dst in ["usdt", "rls"]:
        try:
            params = urllib.parse.urlencode({
                "dstCurrency": dst
            })

            url = (
                f"{BASE_URL}/market/stats?"
                f"{params}"
            )

            data = get_json(url)

            if data.get("status") != "ok":
                continue

            stats = data.get("stats", {})

            for key in stats.keys():

                # نمونه:
                # btc-usdt
                # btc-rls

                parts = key.lower().split("-")

                if len(parts) != 2:
                    continue

                coin = parts[0]
                quote = parts[1]

                symbol = (
                    coin.upper() +
                    quote.upper()
                )

                markets.setdefault(
                    coin,
                    []
                ).append(symbol)

        except Exception as e:
            print(
                "Market discovery error:",
                dst,
                e
            )

    return markets


def choose_symbol(markets, coin):

    options = markets.get(
        coin.lower(),
        []
    )

    if not options:
        return None

    # اولویت با USDT
    for symbol in options:
        if symbol.endswith("USDT"):
            return symbol

    # بعد IRT
    for symbol in options:
        if symbol.endswith("IRT"):
            return symbol

    # هر بازار موجود
    return options[0]


# -------------------------------------------------
# دریافت کندل
# -------------------------------------------------

def candles(symbol, resolution, count=120):

    now = int(time.time())

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "countback": count,
        "to": now
    })

    url = (
        f"{BASE_URL}/market/udf/history?"
        f"{params}"
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


# -------------------------------------------------
# ابزارهای محاسباتی
# -------------------------------------------------

def average(values):

    if not values:
        return 0

    return sum(values) / len(values)


def percent(a, b):

    if b == 0:
        return 0

    return ((a - b) / b) * 100


# -------------------------------------------------
# تحلیل
# -------------------------------------------------

def scan_market(symbol):

    # فقط 5m برای غربال اولیه
    c5 = candles(
        symbol,
        5,
        120
    )

    if len(c5) < 40:
        return None

    last = c5[-1]

    price = last["c"]

    m5 = percent(
        c5[-1]["c"],
        c5[-2]["c"]
    )

    m15 = percent(
        c5[-1]["c"],
        c5[-4]["c"]
    )

    m30 = percent(
        c5[-1]["c"],
        c5[-7]["c"]
    )

    m60 = percent(
        c5[-1]["c"],
        c5[-13]["c"]
    )

    # حجم
    old_volume = average([
        x["v"]
        for x in c5[-25:-1]
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
        for x in c5[-16:-4]
    ])

    if previous_volume > 0:
        volume_acceleration = (
            recent_volume /
            previous_volume
        )
    else:
        volume_acceleration = 0

    # فشار خرید
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

    # مقاومت
    resistance = max(
        x["h"]
        for x in c5[-37:-1]
    )

    resistance_distance = percent(
        price,
        resistance
    )

    score = 0
    reasons = []

    # حرکت تازه
    if 0.05 <= m5 < 0.80:
        score += 15
        reasons.append("شتاب 5m")

    elif 0.80 <= m5 < 1.50:
        score += 8
        reasons.append("حرکت 5m")

    # روند 15 دقیقه
    if 0.10 <= m15 < 2:
        score += 15
        reasons.append("روند 15m مثبت")

    elif m15 >= 2:
        score += 4

    # روند یک ساعت
    if 0.20 <= m60 < 3.5:
        score += 8
        reasons.append("روند 1h مثبت")

    elif m60 >= 3.5:
        score -= 5
        reasons.append("رشد 1h زیاد")

    # هم جهت بودن
    if (
        m5 > 0 and
        m15 > 0 and
        m60 > 0
    ):
        score += 10
        reasons.append("هم‌جهتی")

    # حجم
    if volume_ratio >= 2:
        score += 15
        reasons.append("حجم بسیار بالا")

    elif volume_ratio >= 1.5:
        score += 10
        reasons.append("حجم بالا")

    elif volume_ratio >= 1.25:
        score += 5
        reasons.append("افزایش حجم")

    # شتاب حجم
    if volume_acceleration >= 1.5:
        score += 10
        reasons.append("شتاب حجم")

    elif volume_acceleration >= 1.2:
        score += 5

    # فشار خرید
    if pressure >= 0.72:
        score += 7
        reasons.append("فشار خرید")

    # نزدیک مقاومت
    if -0.35 <= resistance_distance <= 0.25:
        score += 12
        reasons.append("نزدیک مقاومت")

    elif 0.25 < resistance_distance <= 1:
        score += 6
        reasons.append("شکست مقاومت")

    # جلوگیری از ورود دیرهنگام
    if m30 > 4.5:
        score -= 14
        reasons.append("حرکت 30m زیاد")

    if m60 > 7:
        score -= 12

    score = max(
        0,
        min(100, score)
    )

    return {
        "score": score,
        "price": price,
        "m5": m5,
        "m15": m15,
        "m30": m30,
        "m60": m60,
        "volume_ratio": volume_ratio,
        "volume_acceleration": volume_acceleration,
        "resistance_distance": resistance_distance,
        "pressure": pressure,
        "reasons": reasons
    }


# -------------------------------------------------
# تحلیل عمیق فقط برای کاندیداهای برتر
# -------------------------------------------------

def deep_scan(symbol, base):

    c1 = candles(
        symbol,
        1,
        60
    )

    c15 = candles(
        symbol,
        15,
        60
    )

    if (
        len(c1) < 10 or
        len(c15) < 10
    ):
        return base

    m1 = percent(
        c1[-1]["c"],
        c1[-2]["c"]
    )

    m15_real = percent(
        c15[-1]["c"],
        c15[-2]["c"]
    )

    score = base["score"]
    reasons = list(
        base["reasons"]
    )

    if 0.05 <= m1 < 0.50:
        score += 10
        reasons.append("شروع حرکت 1m")

    elif m1 >= 0.50:
        score -= 4

    if 0 < m15_real < 1.5:
        score += 7
        reasons.append("تأیید 15m")

    elif m15_real < -0.2:
        score -= 8

    score = max(
        0,
        min(100, score)
    )

    base["score"] = score
    base["m1"] = m1
    base["m15_real"] = m15_real
    base["reasons"] = reasons

    return base


# -------------------------------------------------
# وضعیت BTC
# -------------------------------------------------

def bitcoin_regime(markets):

    symbol = choose_symbol(
        markets,
        "btc"
    )

    if not symbol:
        print("BTC market not found")
        return False

    try:

        btc = scan_market(
            symbol
        )

        if not btc:
            return False

        print(
            "BTC:",
            symbol,
            f"5m={btc['m5']:+.2f}%",
            f"15m={btc['m15']:+.2f}%",
            f"1h={btc['m60']:+.2f}%"
        )

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

        return False


# -------------------------------------------------
# تلگرام
# -------------------------------------------------

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:

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


# -------------------------------------------------
# اجرای اصلی
# -------------------------------------------------

def main():

    print(
        "===== NOBITEX EARLY ALERT V5 ====="
    )

    # پیدا کردن بازارهای واقعی
    markets = discover_markets()

    print(
        "Markets discovered:",
        len(markets)
    )

    if not markets:

        print(
            "ERROR: Nobitex markets unavailable."
        )

        return

    # وضعیت BTC
    regime = bitcoin_regime(
        markets
    )

    print(
        "BTC regime:",
        "FAVORABLE"
        if regime
        else "WEAK"
    )

    # ------------------------------------------
    # مرحله اول: غربال 5 دقیقه‌ای
    # ------------------------------------------

    candidates = []

    for coin in COINS:

        symbol = choose_symbol(
            markets,
            coin
        )

        if not symbol:
            print(
                "NO MARKET:",
                coin.upper()
            )
            continue

        try:

            result = scan_market(
                symbol
            )

            if result:

                candidates.append(
                    (
                        coin.upper(),
                        symbol,
                        result
                    )
                )

                print(
                    coin.upper(),
                    symbol,
                    f"score={result['score']}",
                    f"5m={result['m5']:+.2f}%",
                    f"15m={result['m15']:+.2f}%",
                    f"1h={result['m60']:+.2f}%",
                    f"vol={result['volume_ratio']:.2f}x"
                )

        except Exception as e:

            print(
                "ERROR",
                coin.upper(),
                symbol,
                e
            )

    # مرتب‌سازی
    candidates.sort(
        key=lambda x: x[2]["score"],
        reverse=True
    )

    # ------------------------------------------
    # فقط 8 گزینه برتر → تحلیل عمیق
    # ------------------------------------------

    deep_results = []

    for coin, symbol, result in candidates[:8]:

        try:

            result = deep_scan(
                symbol,
                result
            )

            deep_results.append(
                (
                    coin,
                    symbol,
                    result
                )
            )

        except Exception as e:

            print(
                "Deep scan error:",
                coin,
                e
            )

    deep_results.sort(
        key=lambda x: x[2]["score"],
        reverse=True
    )

    print(
        "===== TOP CANDIDATES ====="
    )

    for coin, symbol, x in deep_results[:8]:

        print(
            coin,
            symbol,
            f"score={x['score']}",
            f"1m={x.get('m1', 0):+.2f}%",
            f"5m={x['m5']:+.2f}%",
            f"15m={x['m15']:+.2f}%",
            f"1h={x['m60']:+.2f}%"
        )

    # ------------------------------------------
    # ارسال هشدار
    # ------------------------------------------

    for coin, symbol, x in deep_results[:8]:

        score = x["score"]

        if score < WATCH:
            continue

        if x["m5"] <= 0:
            continue

        if x["m15"] <= 0:
            continue

        # جلوگیری از تعقیب پامپ
        if x["m30"] >= 4.5:
            continue

        if score >= TRADE and regime:

            tag = "🚨 هشدار معامله"

        else:

            tag = "👀 هشدار دیده‌بانی"

        reasons = ", ".join(
            x["reasons"]
        )

        message = (
            f"{tag}\n"
            f"{coin} — نوبیتکس\n"
            f"Market: {symbol}\n\n"

            f"امتیاز: {score}/100\n\n"

            f"1m: {x.get('m1', 0):+.2f}%\n"
            f"5m: {x['m5']:+.2f}%\n"
            f"15m: {x['m15']:+.2f}%\n"
            f"1h: {x['m60']:+.2f}%\n"
            f"30m: {x['m30']:+.2f}%\n\n"

            f"حجم: {x['volume_ratio']:.2f}x\n"
            f"شتاب حجم: "
            f"{x['volume_acceleration']:.2f}x\n"

            f"فشار خرید: "
            f"{x['pressure']:.2f}\n"

            f"فاصله مقاومت: "
            f"{x['resistance_distance']:+.2f}%\n\n"

            f"دلایل:\n"
            f"{reasons}\n\n"

            f"⚠️ این هشدار سیگنال قطعی خرید نیست."
        )

        sent = send_telegram(
            message
        )

        print(
            "Telegram:",
            coin,
            sent
        )


if __name__ == "__main__":
    main()
