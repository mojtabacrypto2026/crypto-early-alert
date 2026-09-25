# -*- coding: utf-8 -*-
"""
NOBITEX SIGNAL PERFORMANCE TRACKER V6

Compatibility:
- Keeps performance schema_version=5 so existing history is not wiped.
- Tracks BUILDUP / MICRO / FAST / CONFIRMED / NEWS independently.
- Adds 5m and 10m checkpoints for early-warning tuning.
- Adds first +1% threshold while preserving +2%, +5%, and -2%.
- Tracks MFE/MAE from sampled order-book midpoint prices.
- Keeps the 100 completed-4h reliability gate.
"""

import json
import os
import time
import urllib.parse
import urllib.request


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

# Keep schema_version 5 for backward compatibility with the existing file.
PERFORMANCE_SCHEMA_VERSION = 5
TRACKING_SCHEMA_VERSION = 1
MIN_RELIABLE_4H_SAMPLES = 100
MAX_EVENTS = 1500

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
    "5m": 5 * 60,
    "10m": 10 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
}

EVENT_TYPES = (
    "BUILDUP",
    "MICRO",
    "FAST",
    "CONFIRMED",
    "NEWS",
)

THRESHOLDS = {
    "1pct": 1.0,
    "2pct": 2.0,
    "5pct": 5.0,
    "minus_2pct": -2.0,
}


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        result = float(value)
        if result == result:
            return result
    except Exception:
        pass
    return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path, data):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(
            data,
            handle,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(temp_path, path)


def http_get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "User-Agent": "Nobitex-Performance-Tracker/6.0",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            request = urllib.request.Request(
                url,
                headers=headers,
                method="GET",
            )
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))

    raise last_error


def http_post_json(url, data):
    payload = json.dumps(data).encode("utf-8")

    headers = {
        "User-Agent": "Nobitex-Performance-Tracker/6.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))

    raise last_error


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


def get_tracking_start(now):
    marker = load_json(TRACKING_START_PATH, {})

    if (
        isinstance(marker, dict)
        and marker.get("schema_version") == TRACKING_SCHEMA_VERSION
        and safe_int(marker.get("started_at"), 0) > 0
    ):
        return safe_int(marker["started_at"], now)

    save_json(
        TRACKING_START_PATH,
        {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "started_at": now,
            "created_at": now,
        },
    )
    return now


def get_prices():
    data = http_get_json(
        BASE_URL + "/v3/orderbook/all"
    )

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

        try:
            bid = safe_float(bids[0][0]) if bids else 0.0
            ask = safe_float(asks[0][0]) if asks else 0.0
        except Exception:
            continue

        if bid > 0 and ask > 0:
            prices[symbol] = (bid + ask) / 2.0
        elif bid > 0:
            prices[symbol] = bid
        elif ask > 0:
            prices[symbol] = ask

    return prices


def return_percent(start_price, current_price):
    if start_price <= 0 or current_price <= 0:
        return 0.0
    return ((current_price - start_price) / start_price) * 100.0


def blank_stats():
    return {
        "count": 0,
        "positive": 0,
        "positive_rate": 0.0,
        "average_return": 0.0,
        "median_return": 0.0,
        "best_return": 0.0,
        "worst_return": 0.0,
        "hit_1pct": 0,
        "hit_2pct": 0,
        "hit_5pct": 0,
        "hit_minus_2pct": 0,
    }


def stats(values):
    if not values:
        return blank_stats()

    ordered = sorted(values)
    n = len(ordered)

    if n % 2:
        med = ordered[n // 2]
    else:
        med = (
            ordered[n // 2 - 1]
            + ordered[n // 2]
        ) / 2.0

    return {
        "count": n,
        "positive": sum(value > 0 for value in ordered),
        "positive_rate": (
            sum(value > 0 for value in ordered)
            / n
            * 100.0
        ),
        "average_return": sum(ordered) / n,
        "median_return": med,
        "best_return": max(ordered),
        "worst_return": min(ordered),
        "hit_1pct": sum(value >= 1.0 for value in ordered),
        "hit_2pct": sum(value >= 2.0 for value in ordered),
        "hit_5pct": sum(value >= 5.0 for value in ordered),
        "hit_minus_2pct": sum(value <= -2.0 for value in ordered),
    }


def new_performance_state():
    return {
        "schema_version": PERFORMANCE_SCHEMA_VERSION,
        "events": {},
        "milestones": {},
        "statistics": {},
        "reliability": {
            "minimum_required_4h": MIN_RELIABLE_4H_SAMPLES,
            "completed_4h": 0,
            "reliable": False,
        },
        "updated_at": 0,
    }


# ============================================================
# EVENT REGISTRATION
# ============================================================

def candidates(alert, tracking_start):
    if not isinstance(alert, dict):
        return []

    mappings = (
        ("BUILDUP", "buildup_timestamp"),
        ("MICRO", "micro_timestamp"),
        ("FAST", "fast_timestamp"),
        ("CONFIRMED", "timestamp"),
        ("NEWS", "news_timestamp"),
    )

    out = []

    for event_type, key in mappings:
        timestamp = safe_int(alert.get(key), 0)
        if timestamp >= tracking_start:
            out.append((event_type, timestamp))

    return out


def alert_price(alert, event_type, current_prices, symbol):
    keys = {
        "BUILDUP": ("buildup_price", "price"),
        "MICRO": ("micro_price", "price"),
        "FAST": ("fast_price", "price"),
        "NEWS": ("news_price", "price"),
        "CONFIRMED": ("price", "confirmed_price"),
    }[event_type]

    for key in keys:
        price = safe_float(alert.get(key), 0.0)
        if price > 0:
            return price

    return safe_float(current_prices.get(symbol), 0.0)


def event_score(alert, event_type):
    key = {
        "BUILDUP": "buildup_score",
        "MICRO": "micro_score",
        "FAST": "fast_score",
        "NEWS": "news_score",
        "CONFIRMED": "score",
    }[event_type]
    return alert.get(key)


def event_metadata(alert, event_type):
    common = {
        "rsi": alert.get("rsi"),
        "structure": alert.get("structure"),
        "resistance": alert.get("resistance"),
    }

    if event_type == "BUILDUP":
        common.update(
            {
                "buildup_flow": alert.get("buildup_flow"),
                "buildup_flow_delta": alert.get("buildup_flow_delta"),
                "buildup_spread_bps": alert.get("buildup_spread_bps"),
                "buildup_price_change": alert.get("buildup_price_change"),
                "buildup_pressure_change": alert.get("buildup_pressure_change"),
            }
        )

    elif event_type == "MICRO":
        common.update(
            {
                "micro_flow": alert.get("micro_flow"),
                "micro_flow_delta": alert.get("micro_flow_delta"),
                "micro_spread_bps": alert.get("micro_spread_bps"),
                "micro_price_change": alert.get("micro_price_change"),
                "micro_momentum_15m": alert.get("micro_momentum_15m"),
                "micro_momentum_1h": alert.get("micro_momentum_1h"),
                "micro_momentum_4h": alert.get("micro_momentum_4h"),
            }
        )

    elif event_type == "FAST":
        common.update(
            {
                "fast_volume_ratio": alert.get("fast_volume_ratio"),
                "fast_volume_acceleration": alert.get("fast_volume_acceleration"),
            }
        )

    return common


def register_event(performance, symbol, alert, event_type, timestamp, current_prices, now):
    event_id = (
        str(symbol).upper()
        + "_"
        + event_type
        + "_"
        + str(timestamp)
    )

    if event_id in performance["events"]:
        return False

    symbol = str(symbol).upper()
    alert_price_value = alert_price(
        alert,
        event_type,
        current_prices,
        symbol,
    )

    if alert_price_value <= 0:
        print("SKIP EVENT - NO PRICE:", event_id)
        return False

    performance["events"][event_id] = {
        "symbol": symbol,
        "type": event_type,
        "alert_time": timestamp,
        "alert_price": alert_price_value,
        "score": event_score(alert, event_type),
        "checkpoints": {},
        "max_return": 0.0,
        "max_price": alert_price_value,
        "min_return": 0.0,
        "min_price": alert_price_value,
        "thresholds": {
            "1pct": None,
            "2pct": None,
            "5pct": None,
            "minus_2pct": None,
        },
        "metadata": event_metadata(alert, event_type),
        "registered_at": now,
    }

    print(
        "NEW EVENT:",
        event_id,
        "| price:",
        alert_price_value,
    )
    return True


# ============================================================
# EVENT UPDATES
# ============================================================

def update_event(event, current_price, now):
    alert_price_value = safe_float(event.get("alert_price"), 0.0)
    alert_time = safe_int(event.get("alert_time"), 0)

    if alert_price_value <= 0 or alert_time <= 0:
        return

    current_return = return_percent(
        alert_price_value,
        current_price,
    )

    if current_return > safe_float(event.get("max_return"), 0.0):
        event["max_return"] = current_return
        event["max_price"] = current_price

    if current_return < safe_float(event.get("min_return"), 0.0):
        event["min_return"] = current_return
        event["min_price"] = current_price

    thresholds = event.get("thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {}

    for key, target in THRESHOLDS.items():
        if thresholds.get(key) is not None:
            continue

        hit = (
            current_return >= target
            if target > 0
            else current_return <= target
        )

        if hit:
            thresholds[key] = {
                "timestamp": now,
                "minutes_to_hit": round(
                    (now - alert_time) / 60.0,
                    1,
                ),
                "price": current_price,
                "return_percent": current_return,
            }

    event["thresholds"] = thresholds

    checkpoints = event.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}

    elapsed = now - alert_time

    for checkpoint_name, seconds in CHECKPOINTS.items():
        if elapsed >= seconds and checkpoint_name not in checkpoints:
            checkpoints[checkpoint_name] = {
                "timestamp": now,
                "price": current_price,
                "return_percent": current_return,
            }

    event["checkpoints"] = checkpoints
    event["last_update"] = now


# ============================================================
# STATISTICS
# ============================================================

def statistics_by_type(events):
    output = {}

    for event_type in EVENT_TYPES + ("ALL",):
        selected = [
            event
            for event in events
            if event_type == "ALL"
            or event.get("type") == event_type
        ]

        type_stats = {
            "events": len(selected),
            "checkpoints": {},
            "MFE": blank_stats(),
            "MAE": blank_stats(),
        }

        for checkpoint_name in CHECKPOINTS:
            values = []
            for event in selected:
                checkpoint = (
                    event.get("checkpoints", {})
                    .get(checkpoint_name)
                )
                if isinstance(checkpoint, dict):
                    values.append(
                        safe_float(
                            checkpoint.get("return_percent"),
                            0.0,
                        )
                    )

            type_stats["checkpoints"][checkpoint_name] = stats(values)

        complete = [
            event
            for event in selected
            if isinstance(
                event.get("checkpoints", {}).get("4h"),
                dict,
            )
        ]

        type_stats["MFE"] = stats(
            [safe_float(event.get("max_return"), 0.0) for event in complete]
        )
        type_stats["MAE"] = stats(
            [safe_float(event.get("min_return"), 0.0) for event in complete]
        )

        for threshold_key in ("1pct", "2pct", "5pct"):
            hit_times = []

            for event in selected:
                hit = (
                    event.get("thresholds", {})
                    .get(threshold_key)
                )
                if isinstance(hit, dict):
                    hit_times.append(
                        safe_float(
                            hit.get("minutes_to_hit"),
                            0.0,
                        )
                    )

            type_stats[
                "time_to_" + threshold_key
            ] = {
                "count": len(hit_times),
                "average_minutes": (
                    sum(hit_times) / len(hit_times)
                    if hit_times
                    else 0.0
                ),
                "best_minutes": (
                    min(hit_times)
                    if hit_times
                    else 0.0
                ),
                "worst_minutes": (
                    max(hit_times)
                    if hit_times
                    else 0.0
                ),
            }

        output[event_type] = type_stats

    return output


# ============================================================
# MAIN
# ============================================================

def main():
    now = int(time.time())
    tracking_start = get_tracking_start(now)

    print("=" * 72)
    print("NOBITEX SIGNAL PERFORMANCE TRACKER V6")
    print(
        "UTC:",
        time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(now),
        ),
    )
    print("Tracking started:", tracking_start)
    print("=" * 72)

    alert_state = load_json(
        ALERT_STATE_PATH,
        {},
    )

    performance = load_json(
        PERFORMANCE_STATE_PATH,
        {},
    )

    # Do NOT wipe schema-5 history just because the tracker gained new fields.
    if not isinstance(performance, dict):
        performance = new_performance_state()
    elif performance.get("schema_version") != PERFORMANCE_SCHEMA_VERSION:
        # Migration guard: keep the old events if they are readable.
        old_events = performance.get("events", {})
        performance = new_performance_state()
        if isinstance(old_events, dict):
            performance["events"] = old_events
        print("Performance state migrated to compatible schema 5 format.")

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

    registered = 0

    if isinstance(alert_state, dict):
        for symbol, alert in alert_state.items():
            for event_type, timestamp in candidates(
                alert,
                tracking_start,
            ):
                if register_event(
                    performance,
                    symbol,
                    alert,
                    event_type,
                    timestamp,
                    current_prices,
                    now,
                ):
                    registered += 1

    print("New events registered:", registered)

    updated = 0

    for event_id, event in list(
        performance["events"].items()
    ):
        if not isinstance(event, dict):
            continue

        symbol = str(
            event.get("symbol", "")
        ).upper()

        if not symbol:
            continue

        current_price = safe_float(
            current_prices.get(symbol),
            0.0,
        )

        if current_price <= 0:
            continue

        update_event(
            event,
            current_price,
            now,
        )
        performance["events"][event_id] = event
        updated += 1

    print("Events updated:", updated)

    if len(performance["events"]) > MAX_EVENTS:
        ordered = sorted(
            performance["events"].items(),
            key=lambda item: safe_int(
                item[1].get("alert_time"),
                0,
            ),
            reverse=True,
        )
        performance["events"] = dict(
            ordered[:MAX_EVENTS]
        )

    events = [
        event
        for event in performance["events"].values()
        if isinstance(event, dict)
    ]

    performance["statistics"] = statistics_by_type(events)

    completed_4h = sum(
        isinstance(
            event.get("checkpoints", {}).get("4h"),
            dict,
        )
        for event in events
    )

    performance["reliability"] = {
        "minimum_required_4h": MIN_RELIABLE_4H_SAMPLES,
        "completed_4h": completed_4h,
        "reliable": completed_4h >= MIN_RELIABLE_4H_SAMPLES,
    }

    if (
        completed_4h >= MIN_RELIABLE_4H_SAMPLES
        and not performance["milestones"].get("100_4h")
    ):
        performance["milestones"]["100_4h"] = True

        message = (
            "📊 ۱۰۰ نمونه کامل ۴ساعته برای رادار Nobitex ثبت شد.\n\n"
            "عملکرد BUILDUP / MICRO / FAST / CONFIRMED / NEWS اکنون "
            "برای مقایسه و تنظیم مبتنی بر داده آماده است."
        )

        if telegram_send(message):
            print("MILESTONE Telegram sent.")
        else:
            print("MILESTONE reached; Telegram not sent.")

    performance["updated_at"] = now
    save_json(
        PERFORMANCE_STATE_PATH,
        performance,
    )

    print("\nPERFORMANCE SUMMARY")

    for event_type in EVENT_TYPES:
        data = performance["statistics"][event_type]
        print(
            "\n",
            event_type,
            "| events:",
            data["events"],
        )

        for checkpoint_name in (
            "5m",
            "10m",
            "15m",
            "1h",
            "4h",
        ):
            checkpoint = data["checkpoints"][checkpoint_name]
            print(
                " ",
                checkpoint_name,
                "| n=",
                checkpoint["count"],
                "| avg=",
                f"{checkpoint['average_return']:+.2f}%",
                "| +1=",
                checkpoint["hit_1pct"],
                "| +2=",
                checkpoint["hit_2pct"],
                "| +5=",
                checkpoint["hit_5pct"],
                "| <=-2=",
                checkpoint["hit_minus_2pct"],
            )

        print(
            " MFE avg/best:",
            f"{data['MFE']['average_return']:+.2f}%",
            f"/{data['MFE']['best_return']:+.2f}%",
            "| MAE worst:",
            f"{data['MAE']['worst_return']:+.2f}%",
        )

        for threshold_name in (
            "1pct",
            "2pct",
            "5pct",
        ):
            timing = data["time_to_" + threshold_name]
            print(
                " time_to_"
                + threshold_name,
                "hits=",
                timing["count"],
                "avg_min=",
                f"{timing['average_minutes']:.1f}",
            )

    print(
        "\nCompleted 4H:",
        completed_4h,
        "/",
        MIN_RELIABLE_4H_SAMPLES,
        "| Reliable:",
        performance["reliability"]["reliable"],
    )
    print("PERFORMANCE TRACKER FINISHED")


if __name__ == "__main__":
    main()
