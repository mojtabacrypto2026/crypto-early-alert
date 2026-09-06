import os
import json
import urllib.request
import urllib.parse
import time

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

def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Crypto-Early-Alert"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())

def get_candles(product):
    now = int(time.time())
    start = now - (25 * 3600)

    params = urllib.parse.urlencode({
        "start": start,
        "end": now,
        "granularity": "ONE_HOUR",
        "limit": 25
    })

    url = (
        "https://api.coinbase.com/api/v3/brokerage/market/products/"
        + product
        + "/candles?"
        + params
    )

    data = get_json(url)
    candles = data.get("candles", [])

    candles.sort(key=lambda x: int(x["start"]))
    return candles

def find_chat_id():
    if not BOT_TOKEN:
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    try:
        data = get_json(url)

        for update in data.get("result", []):
            message = update.get("message")

            if message and message.get("chat"):
                chat_id = message["chat"]["id"]
                print("TELEGRAM_CHAT_ID:", chat_id)
                return str(chat_id)

    except Exception as e:
        print("خطا در دریافت Chat ID:", e)

    return None

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram Chat ID هنوز تنظیم نشده است.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    req = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(req, timeout=20) as response:
        print(response.read().decode())

def analyze(product, name):
    try:
        candles = get_candles(product)

        if len(candles) < 22:
            print("داده کافی نیست:", name)
            return None

        last = candles[-2]
        previous = candles[-3]

        close = float(last["close"])
        old_close = float(previous["close"])
        volume = float(last["volume"])

        change_1h = ((close - old_close) / old_close) * 100

        volumes = [
            float(x["volume"])
            for x in candles[-22:-2]
        ]

        avg_volume = sum(volumes) / len(volumes)

        volume_ratio = (
            volume / avg_volume
            if avg_volume > 0
            else 0
        )

        score = 0

        if change_1h >= 1:
            score += 20
        elif change_1h >= 0.5:
            score += 10

        if volume_ratio >= 3:
            score += 30
        elif volume_ratio >= 2:
            score += 25
        elif volume_ratio >= 1.5:
            score += 15

        if change_1h > 0 and volume_ratio >= 1.5:
            score += 20

        if change_1h >= 2:
            score += 15
        elif change_1h >= 1:
            score += 10

        return {
            "name": name,
            "price": close,
            "change": change_1h,
            "volume_ratio": volume_ratio,
            "score": score
        }

    except Exception as e:
        print("خطا:", name, e)
        return None

def main():

    print("Crypto Early Alert started")

    if not CHAT_ID:
        print("CHAT_ID تنظیم نشده؛ در حال پیدا کردن آن...")
        find_chat_id()

    results = []

    for product, name in COINS.items():

        result = analyze(product, name)

        if result:
            results.append(result)

    print("\n--- MARKET SCAN ---")

    for r in results:
        print(
            r["name"],
            "Score:", r["score"],
            "1H:", round(r["change"], 2), "%",
            "Volume:", round(r["volume_ratio"], 2), "x"
        )

    for r in results:

        if r["score"] >= 65:

            level = (
                "🔴 حرکت بسیار قوی"
                if r["score"] >= 80
                else "🟠 هشدار جدی"
            )

            message = (
                "🚨 CRYPTO EARLY ALERT\n\n"
                f"🪙 {r['name']}\n"
                f"⭐ امتیاز: {r['score']}/100\n"
                f"📈 تغییر 1H: {r['change']:.2f}%\n"
                f"📊 حجم: {r['volume_ratio']:.2f} برابر میانگین\n"
                f"⚠️ وضعیت: {level}\n\n"
                "این هشدار تضمین رشد نیست."
            )

            send_telegram(message)

if __name__ == "__main__":
    main()
