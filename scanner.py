import os
import json
import urllib.request
import urllib.parse
import time


# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

COINS = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
    "XRP-USD": "XRP",
    "ADA-USD": "ADA",
    "DOGE-USD": "DOGE",
}


# =========================
# HTTP
# =========================

def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crypto-Early-Alert"
        }
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode()

    return json.loads(text)


# =========================
# TELEGRAM
# =========================

def telegram_request(method):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    return get_json(url)


def check_telegram():

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN تنظیم نشده است.")
        return None

    print("")
    print("==============================")
    print("TELEGRAM CHECK")
    print("==============================")

    # -------------------------
    # تست خود Token و شناسایی Bot
    # -------------------------

    try:

        bot_info = telegram_request("getMe")

        print("BOT INFO:")

        if bot_info.get("ok"):

            bot = bot_info.get("result", {})

            print(
                "Bot name:",
                bot.get("first_name")
            )

            print(
                "Bot username:",
                bot.get("username")
            )

            print(
                "Bot ID:",
                bot.get("id")
            )

        else:

            print("Telegram token error:")
            print(bot_info)

            return None

    except Exception as e:

        print("خطا در getMe:")
        print(e)

        return None

    # -------------------------
    # Webhook
    # -------------------------

    try:

        webhook = telegram_request(
            "getWebhookInfo"
        )

        result = webhook.get(
            "result",
            {}
        )

        webhook_url = result.get(
            "url",
            ""
        )

        pending = result.get(
            "pending_update_count",
            0
        )

        print("")
        print("Webhook URL:", webhook_url)
        print(
            "Pending updates:",
            pending
        )

        # اگر Webhook فعال باشد
        if webhook_url:

            print(
                "Webhook فعال است."
            )

            print(
                "در حال حذف Webhook..."
            )

            deleted = telegram_request(
                "deleteWebhook"
            )

            print(
                "deleteWebhook:",
                deleted
            )

            time.sleep(2)

    except Exception as e:

        print(
            "خطا در بررسی Webhook:",
            e
        )

    # -------------------------
    # دریافت پیام‌ها
    # -------------------------

    try:

        updates = telegram_request(
            "getUpdates"
        )

        print("")
        print(
            "Telegram response:",
            updates
        )

        if not updates.get("ok"):

            print(
                "Telegram error:",
                updates
            )

            return None

        results = updates.get(
            "result",
            []
        )

        if not results:

            print(
                "هیچ پیام جدیدی از تلگرام پیدا نشد."
            )

            return None

        # بررسی پیام‌ها
        for update in results:

            message = update.get(
                "message"
            )

            if not message:
                continue

            chat = message.get(
                "chat"
            )

            if not chat:
                continue

            chat_id = chat.get(
                "id"
            )

            username = chat.get(
                "username"
            )

            first_name = chat.get(
                "first_name"
            )

            print("")
            print(
                "=============================="
            )

            print(
                "TELEGRAM_CHAT_ID:",
                chat_id
            )

            print(
                "Telegram username:",
                username
            )

            print(
                "First name:",
                first_name
            )

            print(
                "=============================="
            )

            return str(chat_id)

    except Exception as e:

        print(
            "خطا در دریافت Chat ID:",
            e
        )

    return None


# =========================
# SEND TELEGRAM
# =========================

def send_telegram(message):

    if not BOT_TOKEN:

        print(
            "BOT TOKEN وجود ندارد."
        )

        return

    if not CHAT_ID:

        print(
            "TELEGRAM_CHAT_ID هنوز تنظیم نشده است."
        )

        return

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
        data=data
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = response.read().decode()

            print(
                "Telegram send result:"
            )

            print(result)

    except Exception as e:

        print(
            "خطا در ارسال تلگرام:",
            e
        )


# =========================
# COINBASE
# =========================

def get_candles(product):

    now = int(
        time.time()
    )

    start = now - (
        30 * 3600
    )

    params = urllib.parse.urlencode({

        "start": start,

        "end": now,

        "granularity": "ONE_HOUR",

        "limit": 30

    })

    url = (

        "https://api.coinbase.com/"
        "api/v3/brokerage/market/products/"
        + product
        + "/candles?"
        + params

    )

    data = get_json(url)

    candles = data.get(
        "candles",
        []
    )

    candles.sort(
        key=lambda x:
        int(x["start"])
    )

    return candles


# =========================
# ANALYSIS
# =========================

def analyze(product, name):

    try:

        candles = get_candles(
            product
        )

        if len(candles) < 22:

            print(
                name,
                "داده کافی نیست."
            )

            return None

        last = candles[-2]

        previous = candles[-3]

        close = float(
            last["close"]
        )

        old_close = float(
            previous["close"]
        )

        volume = float(
            last["volume"]
        )

        change_1h = (

            (
                close
                - old_close
            )
            / old_close

        ) * 100

        volumes = []

        for candle in candles[-22:-2]:

            volumes.append(
                float(
                    candle["volume"]
                )
            )

        avg_volume = (
            sum(volumes)
            / len(volumes)
        )

        if avg_volume > 0:

            volume_ratio = (
                volume
                / avg_volume
            )

        else:

            volume_ratio = 0

        # -------------------------
        # SCORE
        # -------------------------

        score = 0

        if change_1h >= 2:

            score += 25

        elif change_1h >= 1:

            score += 20

        elif change_1h >= 0.5:

            score += 10

        if volume_ratio >= 3:

            score += 30

        elif volume_ratio >= 2:

            score += 25

        elif volume_ratio >= 1.5:

            score += 15

        if (
            change_1h > 0
            and volume_ratio >= 1.5
        ):

            score += 20

        if change_1h >= 2:

            score += 15

        elif change_1h >= 1:

            score += 10

        return {

            "name": name,

            "price": close,

            "change": change_1h,

            "volume_ratio":
                volume_ratio,

            "score": score

        }

    except Exception as e:

        print(
            "خطا در تحلیل",
            name,
            ":",
            e
        )

        return None


# =========================
# MAIN
# =========================

def main():

    print("")
    print("==============================")
    print("CRYPTO EARLY ALERT")
    print("==============================")
    print("")

    # -------------------------
    # Telegram
    # -------------------------

    found_chat_id = check_telegram()

    if found_chat_id:

        print("")
        print(
            "Chat ID پیدا شد:"
        )

        print(
            found_chat_id
        )

        print("")
        print(
            "این عدد را در GitHub Secret"
        )

        print(
            "با نام TELEGRAM_CHAT_ID قرار بده."
        )

    elif CHAT_ID:

        print("")
        print(
            "TELEGRAM_CHAT_ID از قبل تنظیم شده."
        )

    else:

        print("")
        print(
            "TELEGRAM_CHAT_ID هنوز تنظیم نشده."
        )

    # -------------------------
    # Market Scan
    # -------------------------

    results = []

    print("")
    print(
        "شروع بررسی بازار..."
    )
    print("")

    for product, name in COINS.items():

        result = analyze(
            product,
            name
        )

        if result:

            results.append(
                result
            )

    # -------------------------
    # Results
    # -------------------------

    print("")
    print("==============================")
    print("MARKET SCAN")
    print("==============================")

    for result in results:

        print(

            result["name"],

            "| Score:",

            result["score"],

            "| 1H:",

            round(
                result["change"],
                2
            ),

            "%",

            "| Volume:",

            round(
                result["volume_ratio"],
                2
            ),

            "x"

        )

    # -------------------------
    # Alerts
    # -------------------------

    alerts_sent = 0

    for result in results:

        if result["score"] < 65:

            continue

        if result["score"] >= 80:

            level = (
                "🔴 حرکت بسیار قوی"
            )

        else:

            level = (
                "🟠 هشدار جدی"
            )

        message = (

            "🚨 CRYPTO EARLY ALERT\n\n"

            f"🪙 ارز: "
            f"{result['name']}\n"

            f"⭐ امتیاز: "
            f"{result['score']}/100\n"

            f"📈 تغییر 1H: "
            f"{result['change']:.2f}%\n"

            f"📊 حجم: "
            f"{result['volume_ratio']:.2f} "
            f"برابر میانگین\n"

            f"⚠️ وضعیت: "
            f"{level}\n\n"

            "⚠️ این هشدار تضمین رشد نیست."
        )

        print("")
        print(
            "ارسال هشدار برای:",
            result["name"]
        )

        send_telegram(
            message
        )

        alerts_sent += 1

    print("")
    print("==============================")

    print(
        "تعداد هشدارها:",
        alerts_sent
    )

    print(
        "=============================="
    )

    print("")


# =========================
# START
# =========================

if __name__ == "__main__":

    main()
