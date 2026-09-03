import os
import json
import urllib.request
import urllib.parse
import time

BINANCE = "https://api.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ارزهای اصلی
MAIN_COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def get_klines(symbol):
    url = (
        BINANCE
        + "/api/v3/klines?"
        + urllib.parse.urlencode({
            "symbol": symbol,
            "interval": "1h",
            "limit": 25
        })
    )
    return get_json(url)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram هنوز تنظیم نشده است.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    req = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(req, timeout=15) as response:
        print(response.read().decode())


def analyze(symbol):
    try:
        candles = get_klines(symbol)

        # آخرین کندل بسته شده
        last = candles[-2]

        close = float(last[4])
        volume = float(last[5])

        # قیمت 1 ساعت قبل
        old_close = float(candles[-3][4])

        change_1h = ((close - old_close) / old_close) * 100

        # میانگین حجم 20 ساعت گذشته
        volumes = [float(x[5]) for x in candles[-22:-2]]
        avg_volume = sum(volumes) / len(volumes)

        volume_ratio = volume / avg_volume if avg_volume else 0

        score = 0

        # حرکت قیمت
        if change_1h >= 1:
            score += 20
        elif change_1h >= 0.5:
            score += 10

        # حجم غیرعادی
        if volume_ratio >= 3:
            score += 30
        elif volume_ratio >= 2:
            score += 25
        elif volume_ratio >= 1.5:
            score += 15

        # حرکت همراه حجم
        if change_1h > 0 and volume_ratio >= 1.5:
            score += 20

        # قدرت حرکت
        if change_1h >= 2:
            score += 15
        elif change_1h >= 1:
            score += 10

        return {
            "symbol": symbol,
            "price": close,
            "change": change_1h,
            "volume_ratio": volume_ratio,
            "score": score
        }

    except Exception as e:
        print("خطا:", symbol, e)
        return None


def main():

    print("Crypto Early Alert started")

    results = []

    for coin in MAIN_COINS:
        result = analyze(coin)

        if result:
            results.append(result)

    # نمایش نتایج
    print("\n--- MARKET SCAN ---")

    for r in results:
        print(
            r["symbol"],
            "Score:", r["score"],
            "1H:", round(r["change"], 2), "%",
            "Volume:", round(r["volume_ratio"], 2), "x"
        )

    # هشدارها
    alerts = []

    for r in results:

        if r["score"] >= 65:

            if r["score"] >= 80:
                level = "🔴 حرکت بسیار قوی"
            else:
                level = "🟠 هشدار جدی"

            message = (
                "🚨 CRYPTO EARLY ALERT\n\n"
                f"🪙 {r['symbol']}\n"
                f"⭐ امتیاز: {r['score']}/100\n"
                f"📈 تغییر 1H: {r['change']:.2f}%\n"
                f"📊 حجم: {r['volume_ratio']:.2f} برابر میانگین\n"
                f"⚠️ وضعیت: {level}\n\n"
                "این هشدار به معنی تضمین رشد نیست."
            )

            alerts.append(message)

    # ارسال هشدار
    for alert in alerts:
        send_telegram(alert)
        time.sleep(1)


if __name__ == "__main__":
    main()
