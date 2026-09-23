# -*- coding: utf-8 -*-
"""
NOBITEX SIGNAL PERFORMANCE TRACKER - FINAL

Tracks newly-created FAST and CONFIRMED alerts from a clean tracking epoch.
Checkpoints: 15m, 30m, 1h, 2h, 4h.
Also tracks max favorable excursion (MFE) using current sampled price.
"""

import os
import urllib.request
import urllib.parse
import json
import time


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"
WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json",
)

PERFORMANCE_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_signal_performance_state.json",
)

TRACKING_START_PATH = os.path.join(
    WORKSPACE,
    "nobitex_performance_tracking_start.json",
)

TRACKING_SCHEMA_VERSION = 1
PERFORMANCE_SCHEMA_VERSION = 4
MIN_RELIABLE_4H_SAMPLES = 100
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

CHECKPOINTS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
}


# ============================================================
# HTTP / JSON
# ============================================================

def http_get_json(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Nobitex-Performance-Tracker/4.0",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
    raise last_error


def http_post_json(url, data):
    payload = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "Nobitex-Performance-Tracker/3.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    try:
        result = http_post_json(
            url,
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
        )
        return bool(result.get("ok"))
    except Exception as exc:
        print("TELEGRAM ERROR:", exc)
        return False


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print("LOAD ERROR:", path, exc)
        return default


def save_json(path, data):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if result == result:
            return result
    except Exception:
        pass
    return default


# ============================================================
# TRACKING START
# ============================================================

def get_tracking_start(now):
    marker = load_json(TRACKING_START_PATH, {})

    if (
        isinstance(marker, dict)
        and marker.get("schema_version") == TRACKING_SCHEMA_VERSION
    ):
        started_at = int(safe_float(marker.get("started_at"), 0))
        if started_at > 0:
            return started_at

    # This should normally already be created by scanner.py.
    started_at = now
    save_json(
        TRACKING_START_PATH,
        {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "started_at": started_at,
            "created_at": started_at,
        },
    )
    return started_at


# ============================================================
# CURRENT PRICES
# ============================================================

def get_prices():
    data = http_get_json(BASE_URL + "/v3/orderbook/all")
    prices = {}

    if not isinstance(data, dict):
        return prices

    for raw_symbol, book in data.items():
        symbol = str(raw_symbol).upper()
        if not symbol.endswith("USDT"):
            continue
        if not isinstance(book, dict):
            continue

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid = 0.0
        ask = 0.0

        try:
            if bids:
                bid = safe_float(bids[0][0])
            if asks:
                ask = safe_float(asks[0][0])
        except Exception:
            continue

        if bid > 0 and ask > 0:
            prices[symbol] = (bid + ask) / 2.0
        elif bid > 0:
            prices[symbol] = bid
        elif ask > 0:
            prices[symbol] = ask

    return prices


# ============================================================
# STATS
# ============================================================

def build_stats(values):
    if not values:
        return {
            "count": 0,
            "positive": 0,
            "positive_rate": 0.0,
            "average_return": 0.0,
            "best_return": 0.0,
            "worst_return": 0.0,
            "hit_2pct": 0,
            "hit_5pct": 0,
        }

    return {
        "count": len(values),
        "positive": sum(1 for value in values if value > 0),
        "positive_rate": (
            sum(1 for value in values if value > 0)
            / len(values)
            * 100.0
        ),
        "average_return": sum(values) / len(values),
        "best_return": max(values),
        "worst_return": min(values),
        "hit_2pct": sum(1 for value in values if value >= 2.0),
        "hit_5pct": sum(1 for value in values if value >= 5.0),
    }


def return_percent(start, current):
    if start <= 0:
        return 0.0
    return ((current - start) / start) * 100.0


# ============================================================
# MAIN
# ============================================================

def main():
    now = int(time.time())
    tracking_start = get_tracking_start(now)

    print("=" * 70)
    print("NOBITEX SIGNAL PERFORMANCE TRACKER - FINAL")
    print(time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Tracking started:", tracking_start)
    print("=" * 70)

    alert_state = load_json(ALERT_STATE_PATH, {})
    performance = load_json(PERFORMANCE_STATE_PATH, {})

    # New schema automatically starts a clean dataset.
    if (
        not isinstance(performance, dict)
        or performance.get("schema_version") != PERFORMANCE_SCHEMA_VERSION
    ):
        performance = {
            "schema_version": PERFORMANCE_SCHEMA_VERSION,
            "events": {},
            "milestones": {},
            "statistics": {},
            "reliability": {
                "minimum_required_4h": MIN_RELIABLE_4H_SAMPLES,
                "completed_4h": 0,
                "reliable": False,
            },
        }
        print("Clean performance dataset initialized.")

    if not isinstance(performance.get("events"), dict):
        performance["events"] = {}
    if not isinstance(performance.get("milestones"), dict):
        performance["milestones"] = {}

    try:
        current_prices = get_prices()
    except Exception as exc:
        print("CURRENT PRICE FETCH FAILED:", exc)
        return

    print("Current prices:", len(current_prices))

    # ========================================================
    # REGISTER NEW ALERTS
    # ========================================================
    for symbol, alert in alert_state.items():
        if not isinstance(alert, dict):
            continue

        candidates = []

        fast_timestamp = int(safe_float(alert.get("fast_timestamp"), 0))
        if fast_timestamp >= tracking_start:
            candidates.append(("FAST", fast_timestamp))

        confirmed_timestamp = int(safe_float(alert.get("timestamp"), 0))
        if confirmed_timestamp >= tracking_start:
            candidates.append(("CONFIRMED", confirmed_timestamp))

        for alert_type, alert_time in candidates:
            event_id = (
                symbol
                + "_"
                + alert_type
                + "_"
                + str(alert_time)
            )

            if event_id in performance["events"]:
                continue

            if alert_type == "FAST":
                alert_price = safe_float(alert.get("fast_price"), 0.0)
                score = alert.get("fast_score")
            else:
                alert_price = safe_float(alert.get("price"), 0.0)
                score = alert.get("score")

            if alert_price <= 0:
                alert_price = safe_float(current_prices.get(symbol), 0.0)

            if alert_price <= 0:
                print("SKIP EVENT - NO PRICE:", event_id)
                continue

            performance["events"][event_id] = {
                "symbol": symbol,
                "type": alert_type,
                "alert_time": alert_time,
                "alert_price": alert_price,
                "score": score,
                "checkpoints": {},
                "max_return": 0.0,
                "max_price": alert_price,
                "thresholds": {
                    "2pct": None,
                    "5pct": None,
                },
                "registered_at": now,
            }

            print(
                "NEW EVENT:",
                event_id,
                "PRICE:",
                alert_price,
            )

    # ========================================================
    # UPDATE EVENTS
    # ========================================================
    for event_id, event in list(performance["events"].items()):
        symbol = event.get("symbol")
        alert_time = int(safe_float(event.get("alert_time"), 0))
        alert_price = safe_float(event.get("alert_price"), 0)

        if not symbol or alert_time <= 0 or alert_price <= 0:
            continue

        current_price = safe_float(current_prices.get(symbol), 0)
        if current_price <= 0:
            continue

        current_return = return_percent(alert_price, current_price)

        thresholds = event.get("thresholds", {})
        if not isinstance(thresholds, dict):
            thresholds = {"2pct": None, "5pct": None}

        for key, target in (("2pct", 2.0), ("5pct", 5.0)):
            if thresholds.get(key) is None and current_return >= target:
                thresholds[key] = {
                    "timestamp": now,
                    "minutes_to_hit": round((now - alert_time) / 60.0, 1),
                    "price": current_price,
                }

        event["thresholds"] = thresholds

        if current_return > safe_float(event.get("max_return"), 0):
            event["max_return"] = current_return
            event["max_price"] = current_price

        checkpoints = event.get("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}

        elapsed = now - alert_time

        for name, seconds in CHECKPOINTS.items():
            if elapsed >= seconds and name not in checkpoints:
                checkpoints[name] = {
                    "price": current_price,
                    "return_percent": current_return,
                    "timestamp": now,
                }

        event["checkpoints"] = checkpoints
        performance["events"][event_id] = event

    # Keep latest 1000 events.
    if len(performance["events"]) > 1000:
        ordered = sorted(
            performance["events"].items(),
            key=lambda item: int(
                safe_float(item[1].get("alert_time"), 0)
            ),
            reverse=True,
        )
        performance["events"] = dict(ordered[:1000])

    # ========================================================
    # STATISTICS
    # ========================================================
    checkpoint_values = {name: [] for name in CHECKPOINTS}
    mfe_values = []

    for event in performance["events"].values():
        checkpoints = event.get("checkpoints", {})
        for name in CHECKPOINTS:
            checkpoint = checkpoints.get(name)
            if isinstance(checkpoint, dict):
                checkpoint_values[name].append(
                    safe_float(checkpoint.get("return_percent"), 0)
                )

        if "max_return" in event:
            mfe_values.append(safe_float(event.get("max_return"), 0))

    statistics_data = {
        name: build_stats(values)
        for name, values in checkpoint_values.items()
    }

    statistics_data["MFE"] = build_stats(mfe_values)

    for key in ("2pct", "5pct"):
        hit_minutes = []
        for event in performance["events"].values():
            hit = event.get("thresholds", {}).get(key)
            if isinstance(hit, dict):
                hit_minutes.append(safe_float(hit.get("minutes_to_hit"), 0))
        statistics_data[f"time_to_{key}"] = {
            "count": len(hit_minutes),
            "average_minutes": (sum(hit_minutes) / len(hit_minutes)) if hit_minutes else 0.0,
            "best_minutes": min(hit_minutes) if hit_minutes else 0.0,
            "worst_minutes": max(hit_minutes) if hit_minutes else 0.0,
        }

    performance["statistics"] = statistics_data

    completed_4h = len(checkpoint_values["4h"])
    performance["reliability"] = {
        "minimum_required_4h": MIN_RELIABLE_4H_SAMPLES,
        "completed_4h": completed_4h,
        "reliable": completed_4h >= MIN_RELIABLE_4H_SAMPLES,
    }

    # ========================================================
    # MILESTONE
    # ========================================================
    milestone_sent = bool(
        performance["milestones"].get("100_4h", False)
    )

    if completed_4h >= MIN_RELIABLE_4H_SAMPLES and not milestone_sent:
        performance["milestones"]["100_4h"] = True
        message = (
            "📊 ۱۰۰ نمونه کامل ۴ساعته برای رادار Nobitex ثبت شد.\n\n"
            "حالا می‌توان عملکرد واقعی هشدارها را بررسی کرد."
        )
        if telegram_send(message):
            print("MILESTONE Telegram sent.")
        else:
            print("MILESTONE reached; Telegram not sent.")

    performance["updated_at"] = now
    save_json(PERFORMANCE_STATE_PATH, performance)

    print()
    print("PERFORMANCE SUMMARY")
    print("-" * 50)

    for name in CHECKPOINTS:
        stats = statistics_data[name]
        print(
            name,
            "| samples:", stats["count"],
            "| positive:", f"{stats['positive_rate']:.1f}%",
            "| avg:", f"{stats['average_return']:+.2f}%",
            "| +2%:", stats["hit_2pct"],
            "| +5%:", stats["hit_5pct"],
        )

    print()
    print(
        "MFE | samples:",
        statistics_data["MFE"]["count"],
        "| avg:",
        f"{statistics_data['MFE']['average_return']:+.2f}%",
        "| best:",
        f"{statistics_data['MFE']['best_return']:+.2f}%",
    )

    print()
    for key in ("2pct", "5pct"):
        timed = statistics_data[f"time_to_{key}"]
        print(
            f"{key}: hits={timed['count']} | "
            f"avg_minutes={timed['average_minutes']:.1f}"
        )

    print()
    print("Completed 4H:", completed_4h, "/", MIN_RELIABLE_4H_SAMPLES)
    print("Reliable:", performance["reliability"]["reliable"])
    print("PERFORMANCE TRACKER FINISHED")


if __name__ == "__main__":
    main()
