import os
import json
import urllib.request
import urllib.parse
import time
import math

# ============================================================
# CRYPTO EARLY ALERT - VERSION 2
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ارزهای قابل بررسی
COINS = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
    "XRP-USD": "XRP",
    "ADA-USD": "ADA",
    "DOGE-USD": "DOGE",
    "AVAX-USD": "AVAX",
    "LINK-USD": "LINK",
    "UNI-USD": "UNI",
    "SUSHI-USD": "SUSHI",
    "AAVE-USD": "AAVE",
    "DOT-USD": "DOT",
    "ATOM-USD": "ATOM",
    "LTC-USD": "LTC",
    "BCH-USD": "BCH",
    "ETC-USD": "ETC",
    "NEAR-USD": "NEAR",
    "ALGO-USD": "ALGO",
    "FIL-USD": "FIL",
    "APT-USD": "APT",
    "ARB-USD": "ARB",
    "OP-USD": "OP",
    "INJ-USD": "INJ",
    "PEPE-USD": "PEPE",
    "SHIB-USD": "SHIB",
}

# حداقل امتیاز برای هشدار
ALERT_SCORE = 65

# برای جلوگیری از هشدار پشت سر هم
ALERT_COOLDOWN = 4 * 60 * 60

# ذخیره زمان آخرین هشدار
last_alerts = {}


# ============================================================
# HTTP
# ============================================================

def get_json(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crypto-Early-Alert/2.0",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=25
    ) as response:

        text = response.read().decode()

    return json.loads(text)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN تنظیم نشده.")
        return False

    if not CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID تنظیم نشده.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Crypto-Early-Alert/2.0"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = json.loads(
                response.read().decode()
            )

        print("Telegram:", result)

        return bool(result.get("ok"))

    except Exception as e:

        print("خطا در Telegram:", e)

        return False


# ============================================================
# COINBASE CANDLES
# ============================================================

def get_candles(product):

    now = int(time.time())

    # حدود 7 روز داده
    start = now - (7 * 24 * 3600)

    params = urllib.parse.urlencode({
        "start": str(start),
        "end": str(now),
        "granularity": "ONE_HOUR"
    })

    url = (
        "https://api.coinbase.com/"
        "api/v3/brokerage/market/products/"
        f"{product}/candles?"
        f"{params}"
    )

    data = get_json(url)

    candles = data.get(
        "candles",
        []
    )

    candles.sort(
        key=lambda x: int(x["start"])
    )

    return candles


# ============================================================
# SAFE HELPERS
# ============================================================

def pct_change(new, old):

    if old == 0:
        return 0

    return (
        (new - old) / old
    ) * 100


def average(values):

    if not values:
        return 0

    return sum(values) / len(values)


def stdev(values):

    if len(values) < 2:
        return 0

    avg = average(values)

    variance = average([
        (x - avg) ** 2
        for x in values
    ])

    return math.sqrt(variance)


# ============================================================
# ANALYSIS
# ============================================================

def analyze(product, name):

    try:

        candles = get_candles(product)

        if len(candles) < 50:

            print(
                name,
                "- داده کافی نیست:",
                len(candles)
            )

            return None

        # ----------------------------------------------------
        # آخرین کندل کامل
        # ----------------------------------------------------

        last = candles[-2]

        close = float(
            last["close"]
        )

        high = float(
            last["high"]
        )

        low = float(
            last["low"]
        )

        volume = float(
            last["volume"]
        )

        # ----------------------------------------------------
        # قیمت‌های قبلی
        # ----------------------------------------------------

        close_2h = float(
            candles[-4]["close"]
        )

        close_4h = float(
            candles[-6]["close"]
        )

        close_6h = float(
            candles[-8]["close"]
        )

        close_12h = float(
            candles[-14]["close"]
        )

        close_24h = float(
            candles[-26]["close"]
        )

        # ----------------------------------------------------
        # تغییرات قیمت
        # ----------------------------------------------------

        change_1h = pct_change(
            close,
            float(candles[-3]["close"])
        )

        change_2h = pct_change(
            close,
            close_2h
        )

        change_4h = pct_change(
            close,
            close_4h
        )

        change_6h = pct_change(
            close,
            close_6h
        )

        change_12h = pct_change(
            close,
            close_12h
        )

        change_24h = pct_change(
            close,
            close_24h
        )

        # ----------------------------------------------------
        # حجم
        # ----------------------------------------------------

        volume_history = []

        for candle in candles[-26:-2]:

            volume_history.append(
                float(candle["volume"])
            )

        avg_volume = average(
            volume_history
        )

        if avg_volume > 0:

            volume_ratio = (
                volume / avg_volume
            )

        else:

            volume_ratio = 0

        # ----------------------------------------------------
        # شتاب حجم
        # ----------------------------------------------------

        recent_volumes = [
            float(x["volume"])
            for x in candles[-6:-2]
        ]

        recent_avg_volume = average(
            recent_volumes
        )

        older_volumes = [
            float(x["volume"])
            for x in candles[-14:-6]
        ]

        older_avg_volume = average(
            older_volumes
        )

        if older_avg_volume > 0:

            volume_acceleration = (
                recent_avg_volume
                / older_avg_volume
            )

        else:

            volume_acceleration = 1

        # ----------------------------------------------------
        # مقاومت 20 ساعته
        # ----------------------------------------------------

        previous_highs = []

        for candle in candles[-22:-2]:

            previous_highs.append(
                float(candle["high"])
            )

        resistance = max(
            previous_highs
        )

        if resistance > 0:

            breakout_percent = (
                (close - resistance)
                / resistance
            ) * 100

        else:

            breakout_percent = 0

        # ----------------------------------------------------
        # فاصله از میانگین 20 ساعته
        # ----------------------------------------------------

        moving_prices = [

            float(x["close"])
            for x in candles[-22:-2]

        ]

        moving_average = average(
            moving_prices
        )

        if moving_average > 0:

            distance_ma = (
                (close - moving_average)
                / moving_average
            ) * 100

        else:

            distance_ma = 0

        # ----------------------------------------------------
        # قدرت کندل آخر
        # ----------------------------------------------------

        candle_range = high - low

        if candle_range > 0:

            candle_position = (
                (close - low)
                / candle_range
            )

        else:

            candle_position = 0.5

        # ----------------------------------------------------
        # امتیاز
        # ----------------------------------------------------

        score = 0
        reasons = []

        # ----------------------------------------------------
        # 1. حرکت اولیه
        # ----------------------------------------------------

        if 0.20 <= change_1h < 0.60:

            score += 8
            reasons.append(
                "شتاب اولیه 1H"
            )

        elif 0.60 <= change_1h < 1.20:

            score += 12
            reasons.append(
                "حرکت مثبت 1H"
            )

        elif change_1h >= 1.20:

            score += 8
            reasons.append(
                "حرکت قوی 1H"
            )

        # ----------------------------------------------------
        # 2. روند 4 ساعت
        # ----------------------------------------------------

        if change_4h >= 1:

            score += 8
            reasons.append(
                "روند 4H مثبت"
            )

        if change_4h >= 2:

            score += 5

        # ----------------------------------------------------
        # 3. روند 12 ساعت
        # ----------------------------------------------------

        if change_12h >= 1:

            score += 7
            reasons.append(
                "روند 12H مثبت"
            )

        # ----------------------------------------------------
        # 4. شتاب
        # ----------------------------------------------------

        if (
            change_1h > 0
            and change_4h > 0
            and change_12h > 0
        ):

            score += 8

            reasons.append(
                "هم‌جهتی روندها"
            )

        # ----------------------------------------------------
        # 5. حجم
        # ----------------------------------------------------

        if volume_ratio >= 1.3:

            score += 7

            reasons.append(
                "افزایش حجم"
            )

        if volume_ratio >= 1.7:

            score += 5

        if volume_ratio >= 2.5:

            score += 5

        # ----------------------------------------------------
        # 6. شتاب حجم
        # ----------------------------------------------------

        if volume_acceleration >= 1.20:

            score += 7

            reasons.append(
                "شتاب حجم"
            )

        if volume_acceleration >= 1.50:

            score += 4

        # ----------------------------------------------------
        # 7. نزدیک شدن به شکست مقاومت
        # ----------------------------------------------------

        if -1.0 <= breakout_percent < 0:

            score += 8

            reasons.append(
                "نزدیک مقاومت"
            )

        elif 0 <= breakout_percent <= 1.5:

            score += 10

            reasons.append(
                "شروع شکست مقاومت"
            )

        elif breakout_percent > 1.5:

            score += 5

        # ----------------------------------------------------
        # 8. قیمت بالای میانگین
        # ----------------------------------------------------

        if 0 < distance_ma <= 2:

            score += 7

            reasons.append(
                "بالای میانگین"
            )

        elif distance_ma > 2:

            score += 3

        # ----------------------------------------------------
        # 9. قدرت کندل
        # ----------------------------------------------------

        if candle_position >= 0.70:

            score += 6

            reasons.append(
                "قدرت خرید"
            )

        # ----------------------------------------------------
        # 10. جلوگیری از ورود بعد از پامپ شدید
        # ----------------------------------------------------

        if change_24h > 12:

            score -= 15

            reasons.append(
                "رشد 24H زیاد"
            )

        elif change_24h > 8:

            score -= 8

        # ----------------------------------------------------
        # محدود کردن امتیاز
        # ----------------------------------------------------

        score = max(
            0,
            min(
                100,
                score
            )
        )

        # ----------------------------------------------------
        # سطح هشدار
        # ----------------------------------------------------

        if score >= 85:

            level = (
                "🔴 بسیار قوی"
            )

        elif score >= 75:

            level = (
                "🟠 قوی"
            )

        elif score >= 65:

            level = (
                "🟡 اولیه"
            )

        else:

            level = (
                "⚪ عادی"
            )

        return {

            "name": name,

            "price": close,

            "score": score,

            "level": level,

            "change_1h": change_1h,

            "change_4h": change_4h,

            "change_12h": change_12h,

            "change_24h": change_24h,

            "volume_ratio": volume_ratio,

            "volume_acceleration":
                volume_acceleration,

            "breakout_percent":
                breakout_percent,

            "distance_ma":
                distance_ma,

            "candle_position":
                candle_position,

            "reasons": reasons

        }

    except Exception as e:

        print(
            "خطا در تحلیل",
            name,
            ":",
            e
        )

        return None


# ============================================================
# ALERT CONTROL
# ============================================================

def can_send_alert(name, score):

    now = time.time()

    previous = last_alerts.get(
        name,
        0
    )

    # هشدار خیلی قوی اجازه عبور سریع‌تر دارد
    if score >= 85:

        cooldown = 60 * 60

    else:

        cooldown = ALERT_COOLDOWN

    if now - previous < cooldown:

        return False

    last_alerts[name] = now

    return True


# ============================================================
# FORMAT ALERT
# ============================================================

def make_message(result):

    reasons = result["reasons"]

    if reasons:

        reason_text = "\n".join(
            "• " + r
            for r in reasons[:7]
        )

    else:

        reason_text = "• چند نشانه هم‌زمان"

    return (

        "🚨 CRYPTO EARLY ALERT V2\n\n"

        f"🪙 ارز: {result['name']}\n"

        f"⭐ امتیاز: "
        f"{result['score']}/100\n"

        f"⚠️ سطح: "
        f"{result['level']}\n\n"

        f"📈 1H: "
        f"{result['change_1h']:.2f}%\n"

        f"📈 4H: "
        f"{result['change_4h']:.2f}%\n"

        f"📈 12H: "
        f"{result['change_12h']:.2f}%\n"

        f"📊 حجم: "
        f"{result['volume_ratio']:.2f}x\n"

        f"⚡ شتاب حجم: "
        f"{result['volume_acceleration']:.2f}x\n"

        f"🚧 فاصله مقاومت: "
        f"{result['breakout_percent']:.2f}%\n\n"

        "🔎 دلایل:\n"
        f"{reason_text}\n\n"

        "⚠️ هشدار زودهنگام است، "
        "نه تضمین رشد."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print("=" * 55)
    print("CRYPTO EARLY ALERT V2")
    print("=" * 55)
    print("")

    if not BOT_TOKEN:

        print(
            "WARNING: "
            "TELEGRAM_BOT_TOKEN تنظیم نشده."
        )

    if not CHAT_ID:

        print(
            "WARNING: "
            "TELEGRAM_CHAT_ID تنظیم نشده."
        )

    results = []

    print(
        "تعداد ارزها:",
        len(COINS)
    )

    print("")
    print(
        "شروع اسکن..."
    )

    # --------------------------------------------------------
    # Scan
    # --------------------------------------------------------

    for product, name in COINS.items():

        print(
            "بررسی:",
            name
        )

        result = analyze(
            product,
            name
        )

        if result:

            results.append(
                result
            )

        # جلوگیری از فشار زیاد به API
        time.sleep(0.25)

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    results.sort(
        key=lambda x:
        x["score"],
        reverse=True
    )

    # --------------------------------------------------------
    # Print results
    # --------------------------------------------------------

    print("")
    print("=" * 55)
    print("TOP EARLY SIGNALS")
    print("=" * 55)

    for result in results:

        print(

            f"{result['name']:6} | "

            f"Score "
            f"{result['score']:3} | "

            f"1H "
            f"{result['change_1h']:6.2f}% | "

            f"4H "
            f"{result['change_4h']:6.2f}% | "

            f"Vol "
            f"{result['volume_ratio']:4.2f}x"

        )

    # --------------------------------------------------------
    # Alerts
    # --------------------------------------------------------

    alerts = []

    for result in results:

        if result["score"] < ALERT_SCORE:

            continue

        if not can_send_alert(
            result["name"],
            result["score"]
        ):

            print(
                "Cooldown:",
                result["name"]
            )

            continue

        alerts.append(
            result
        )

    print("")
    print(
        "تعداد سیگنال‌های واجد شرایط:",
        len(alerts)
    )

    # --------------------------------------------------------
    # Send Telegram
    # --------------------------------------------------------

    for result in alerts:

        message = make_message(
            result
        )

        print("")
        print(
            "ارسال هشدار:",
            result["name"],
            result["score"]
        )

        send_telegram(
            message
        )

    print("")
    print("=" * 55)
    print("SCAN FINISHED")
    print("=" * 55)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
