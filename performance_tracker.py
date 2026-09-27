# -*- coding: utf-8 -*-
"""
NOBITEX SIGNAL PERFORMANCE TRACKER V7.1

Purpose:
- Measure BUILDUP / MICRO / FAST / CONFIRMED / NEWS separately.
- Measure WS_OBI independently from the main scanner alert-state file.
- Never back-fill a checkpoint with a later observation.
- Keep event observations actually seen by the tracker.
- Track MFE/MAE and first observed +1%, +2%, +5%, -2% hits.
- Preserve the existing performance schema and legacy-event handling.
"""

import json
import os
import time
import urllib.parse
import urllib.request

BASE_URL = "https://apiv2.nobitex.ir"
WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")
ALERT_STATE_PATH = os.path.join(WORKSPACE, "nobitex_telegram_alert_state.json")
WS_ALERT_STATE_PATH = os.path.join(WORKSPACE, "nobitex_ws_obi_alert_state.json")
PERFORMANCE_STATE_PATH = os.path.join(WORKSPACE, "nobitex_signal_performance_state.json")
TRACKING_START_PATH = os.path.join(WORKSPACE, "nobitex_performance_tracking_start.json")

# Keep schema compatible with the current v6 performance state.
PERFORMANCE_SCHEMA_VERSION = 6
TRACKING_SCHEMA_VERSION = 2
MIN_RELIABLE_4H_SAMPLES = 100
MAX_EVENTS = 1500
MAX_OBSERVATIONS_PER_EVENT = 60
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

CHECKPOINTS = {
    "5m": 5 * 60,
    "10m": 10 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
}

# WS_OBI is intentionally a separate benchmark class.
EVENT_TYPES = ("BUILDUP", "MICRO", "FAST", "CONFIRMED", "NEWS", "WS_OBI")
THRESHOLDS = {"1pct": 1.0, "2pct": 2.0, "5pct": 5.0, "minus_2pct": -2.0}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def safe_float(value, default=0.0):
    try:
        x = float(value)
        if x == x and abs(x) != float("inf"):
            return x
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
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def http_get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": "Nobitex-Performance-Tracker/7.1",
        "Accept": "application/json",
    }
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
    raise last


def http_post_json(url, data):
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "Nobitex-Performance-Tracker/7.1",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
    raise last


def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        result = http_post_json(
            "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
            {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
        )
        return bool(result.get("ok"))
    except Exception as exc:
        print("TELEGRAM ERROR:", exc)
        return False


def get_tracking_start(now):
    marker = load_json(TRACKING_START_PATH, {})
    if isinstance(marker, dict) and safe_int(marker.get("started_at"), 0) > 0:
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
    data = http_get_json(BASE_URL + "/v3/orderbook/all")
    prices = {}
    if not isinstance(data, dict):
        return prices

    for raw_symbol, book in data.items():
        symbol = str(raw_symbol).upper()
        if not symbol.endswith("USDT") or not isinstance(book, dict):
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
    med = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    positive = sum(v > 0 for v in ordered)
    return {
        "count": n,
        "positive": positive,
        "positive_rate": positive / n * 100.0,
        "average_return": sum(ordered) / n,
        "median_return": med,
        "best_return": max(ordered),
        "worst_return": min(ordered),
        "hit_1pct": sum(v >= 1.0 for v in ordered),
        "hit_2pct": sum(v >= 2.0 for v in ordered),
        "hit_5pct": sum(v >= 5.0 for v in ordered),
        "hit_minus_2pct": sum(v <= -2.0 for v in ordered),
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
            "clean_completed_4h": 0,
            "reliable": False,
        },
        "updated_at": 0,
    }


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
    return [
        (event_type, safe_int(alert.get(key), 0))
        for event_type, key in mappings
        if safe_int(alert.get(key), 0) >= tracking_start
    ]


def ws_obi_candidates(ws_alert_state, tracking_start):
    """Read WS OBI alerts from their own state file, independently."""
    output = []
    if not isinstance(ws_alert_state, dict):
        return output

    for symbol, alert in ws_alert_state.items():
        if not isinstance(alert, dict):
            continue
        timestamp = safe_int(alert.get("timestamp"), 0)
        if timestamp < tracking_start:
            continue
        if not str(symbol).upper().endswith("USDT"):
            continue
        output.append((str(symbol).upper(), timestamp, alert))
    return output


def alert_price(alert, event_type, current_prices, symbol):
    keys = {
        "BUILDUP": ("buildup_price", "price"),
        "MICRO": ("micro_price", "price"),
        "FAST": ("fast_price", "price"),
        "NEWS": ("news_price", "price"),
        "CONFIRMED": ("price", "confirmed_price"),
        "WS_OBI": ("price", "mid", "last_trade_price"),
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
        # WS OBI has no main technical score. Preserve its technical context
        # separately and use prior_score only as a reference value.
        "WS_OBI": "prior_score",
    }[event_type]
    return alert.get(key)


def event_metadata(alert, event_type):
    keys = (
        "rsi", "structure", "resistance", "volume_ratio", "volume_acceleration",
        "volume_ratio_15m", "volume_acceleration_15m", "momentum_15m",
        "momentum_1h", "momentum_4h", "momentum_8h", "order_flow", "spread_bps",
        "macd_positive", "price_change_since_scan", "flow_delta",
        "price_change_acceleration", "previous_score", "streak",
    )
    meta = {k: alert.get(k) for k in keys if k in alert}

    if event_type == "BUILDUP":
        meta.update({
            k: alert.get(k)
            for k in (
                "buildup_flow", "buildup_flow_delta", "buildup_spread_bps",
                "buildup_price_change", "buildup_pressure_change",
            )
            if k in alert
        })

    if event_type == "MICRO":
        meta.update({
            k: alert.get(k)
            for k in (
                "micro_flow", "micro_flow_delta", "micro_spread_bps", "micro_price_change",
                "micro_momentum_15m", "micro_momentum_1h", "micro_momentum_4h",
            )
            if k in alert
        })

    if event_type == "FAST":
        meta.update({
            k: alert.get(k)
            for k in ("fast_volume_ratio", "fast_volume_acceleration")
            if k in alert
        })

    if event_type == "WS_OBI":
        # These fields are the independent WS microstructure measurements.
        meta.update({
            "weighted_obi": alert.get("weighted_obi"),
            "obi_5": alert.get("obi_5"),
            "obi_20": alert.get("obi_20"),
            "obi_delta_60s": alert.get("obi_delta_60s"),
            "microprice_gap_bps": alert.get("microprice_gap_bps"),
            "spread_bps": alert.get("spread_bps"),
            "price_change_60s": alert.get("price_change_60s"),
            "prior_score": alert.get("prior_score"),
            "updates": alert.get("updates"),
            "alert_type": alert.get("alert_type"),
            "trigger_version": alert.get("trigger_version"),
        })

    return meta


def register_event(performance, symbol, alert, event_type, timestamp, current_prices, now):
    event_id = f"{str(symbol).upper()}_{event_type}_{timestamp}"
    if event_id in performance["events"]:
        return False

    symbol = str(symbol).upper()
    price = alert_price(alert, event_type, current_prices, symbol)
    if price <= 0:
        return False

    performance["events"][event_id] = {
        "symbol": symbol,
        "type": event_type,
        "alert_time": timestamp,
        "alert_price": price,
        "score": event_score(alert, event_type),
        "checkpoints": {},
        "thresholds": {k: None for k in THRESHOLDS},
        "max_return": 0.0,
        "max_price": price,
        "min_return": 0.0,
        "min_price": price,
        "observations": [{"timestamp": timestamp, "price": price}],
        "clean_tracking": True,
        "metadata": event_metadata(alert, event_type),
        "registered_at": now,
    }

    if event_type == "WS_OBI":
        performance["events"][event_id]["benchmark_source"] = "nobitex_ws_obi_alert_state.json"
        performance["events"][event_id]["signal_family"] = "orderbook_microstructure"
        performance["events"][event_id]["weighted_obi_at_alert"] = safe_float(
            alert.get("weighted_obi"), 0.0
        )

    print(
        "NEW EVENT:",
        event_id,
        "| price:",
        price,
        "| source:",
        "WS_OBI" if event_type == "WS_OBI" else "MAIN",
    )
    return True


def append_observation(event, now, current_price):
    if current_price <= 0:
        return
    observations = event.get("observations")
    if not isinstance(observations, list):
        observations = []
    if observations and safe_int(observations[-1].get("timestamp"), 0) == now:
        return
    observations.append({"timestamp": now, "price": current_price})
    if len(observations) > MAX_OBSERVATIONS_PER_EVENT:
        observations = observations[-MAX_OBSERVATIONS_PER_EVENT:]
    event["observations"] = observations


def select_checkpoint_observation(event, checkpoint_seconds):
    alert_time = safe_int(event.get("alert_time"), 0)
    target = alert_time + checkpoint_seconds
    observations = event.get("observations", [])
    valid = [
        obs
        for obs in observations
        if safe_int(obs.get("timestamp"), 0) >= target
        and safe_float(obs.get("price"), 0.0) > 0
    ]
    if not valid:
        return None
    valid.sort(key=lambda obs: safe_int(obs.get("timestamp"), 0))
    return valid[0]


def update_event(event, current_price, now):
    alert_price_value = safe_float(event.get("alert_price"), 0.0)
    alert_time = safe_int(event.get("alert_time"), 0)
    if alert_price_value <= 0 or alert_time <= 0 or current_price <= 0:
        return

    append_observation(event, now, current_price)

    current_return = return_percent(alert_price_value, current_price)
    if current_return > safe_float(event.get("max_return"), 0.0):
        event["max_return"] = current_return
        event["max_price"] = current_price
    if current_return < safe_float(event.get("min_return"), 0.0):
        event["min_return"] = current_return
        event["min_price"] = current_price

    thresholds = event.get("thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {k: None for k in THRESHOLDS}

    observations = event.get("observations", [])
    for key, target in THRESHOLDS.items():
        if thresholds.get(key) is not None:
            continue
        for obs in observations:
            price = safe_float(obs.get("price"), 0.0)
            ts = safe_int(obs.get("timestamp"), 0)
            if ts < alert_time or price <= 0:
                continue
            ret = return_percent(alert_price_value, price)
            hit = ret >= target if target > 0 else ret <= target
            if hit:
                thresholds[key] = {
                    "timestamp": ts,
                    "minutes_to_hit": round((ts - alert_time) / 60.0, 1),
                    "price": price,
                    "return_percent": ret,
                    "first_observed": True,
                }
                break
    event["thresholds"] = thresholds

    checkpoints = event.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}

    for name, seconds in CHECKPOINTS.items():
        if name in checkpoints:
            continue
        obs = select_checkpoint_observation(event, seconds)
        if obs:
            ts = safe_int(obs.get("timestamp"), 0)
            price = safe_float(obs.get("price"), 0.0)
            checkpoints[name] = {
                "timestamp": ts,
                "price": price,
                "return_percent": return_percent(alert_price_value, price),
                "observed_at_or_after_target": True,
                "delay_seconds": max(0, ts - (alert_time + seconds)),
            }
    event["checkpoints"] = checkpoints
    event["last_update"] = now


def statistics_by_type(events):
    output = {}
    for event_type in EVENT_TYPES + ("ALL",):
        selected = [e for e in events if event_type == "ALL" or e.get("type") == event_type]
        type_stats = {
            "events": len(selected),
            "checkpoints": {},
            "MFE": blank_stats(),
            "MAE": blank_stats(),
        }

        for checkpoint_name in CHECKPOINTS:
            values = []
            for event in selected:
                checkpoint = event.get("checkpoints", {}).get(checkpoint_name)
                if isinstance(checkpoint, dict):
                    values.append(safe_float(checkpoint.get("return_percent"), 0.0))
            type_stats["checkpoints"][checkpoint_name] = stats(values)

        complete = [
            e
            for e in selected
            if isinstance(e.get("checkpoints", {}).get("4h"), dict)
        ]
        clean_complete = [e for e in complete if e.get("clean_tracking", False)]

        type_stats["MFE"] = stats(
            [safe_float(e.get("max_return"), 0.0) for e in clean_complete]
        )
        type_stats["MAE"] = stats(
            [safe_float(e.get("min_return"), 0.0) for e in clean_complete]
        )

        for key in ("1pct", "2pct", "5pct"):
            hits = []
            for e in selected:
                hit = e.get("thresholds", {}).get(key)
                if isinstance(hit, dict):
                    hits.append(safe_float(hit.get("minutes_to_hit"), 0.0))
            type_stats["time_to_" + key] = {
                "count": len(hits),
                "average_minutes": sum(hits) / len(hits) if hits else 0.0,
                "best_minutes": min(hits) if hits else 0.0,
                "worst_minutes": max(hits) if hits else 0.0,
            }

        output[event_type] = type_stats

    return output


def main():
    now = int(time.time())
    tracking_start = get_tracking_start(now)

    print("=" * 72)
    print("NOBITEX SIGNAL PERFORMANCE TRACKER V7.1")
    print("UTC:", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)))
    print("Tracking started:", tracking_start)
    print("WS OBI benchmark: ENABLED")
    print("=" * 72)

    alert_state = load_json(ALERT_STATE_PATH, {})
    ws_alert_state = load_json(WS_ALERT_STATE_PATH, {})
    performance = load_json(PERFORMANCE_STATE_PATH, {})

    if not isinstance(performance, dict) or performance.get("schema_version") != PERFORMANCE_SCHEMA_VERSION:
        old_events = performance.get("events", {}) if isinstance(performance, dict) else {}
        performance = new_performance_state()
        if isinstance(old_events, dict):
            for event_id, event in old_events.items():
                if isinstance(event, dict):
                    event["clean_tracking"] = False
                    event["legacy_checkpoint_data"] = True
                    performance["events"][event_id] = event
        print("Performance state migrated; legacy events excluded from clean reliability.")

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

    # Main scanner alerts.
    if isinstance(alert_state, dict):
        for symbol, alert in alert_state.items():
            for event_type, timestamp in candidates(alert, tracking_start):
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

    # Independent WS OBI alerts.
    ws_candidates = ws_obi_candidates(ws_alert_state, tracking_start)
    print("WS OBI alerts available for registration:", len(ws_candidates))
    for symbol, timestamp, alert in ws_candidates:
        if register_event(
            performance,
            symbol,
            alert,
            "WS_OBI",
            timestamp,
            current_prices,
            now,
        ):
            registered += 1

    print("New events registered:", registered)

    updated = 0
    for event_id, event in list(performance["events"].items()):
        if not isinstance(event, dict):
            continue

        symbol = str(event.get("symbol", "")).upper()
        current_price = safe_float(current_prices.get(symbol), 0.0)
        if not symbol or current_price <= 0:
            continue

        update_event(event, current_price, now)
        performance["events"][event_id] = event
        updated += 1

    print("Events updated:", updated)

    if len(performance["events"]) > MAX_EVENTS:
        ordered = sorted(
            performance["events"].items(),
            key=lambda item: safe_int(item[1].get("alert_time"), 0),
            reverse=True,
        )
        performance["events"] = dict(ordered[:MAX_EVENTS])

    events = [e for e in performance["events"].values() if isinstance(e, dict)]
    performance["statistics"] = statistics_by_type(events)

    completed_4h = sum(
        isinstance(e.get("checkpoints", {}).get("4h"), dict)
        for e in events
    )
    clean_completed_4h = sum(
        e.get("clean_tracking", False)
        and isinstance(e.get("checkpoints", {}).get("4h"), dict)
        for e in events
    )

    performance["reliability"] = {
        "minimum_required_4h": MIN_RELIABLE_4H_SAMPLES,
        "completed_4h": completed_4h,
        "clean_completed_4h": clean_completed_4h,
        "reliable": clean_completed_4h >= MIN_RELIABLE_4H_SAMPLES,
    }

    if (
        clean_completed_4h >= MIN_RELIABLE_4H_SAMPLES
        and not performance["milestones"].get("100_clean_4h")
    ):
        performance["milestones"]["100_clean_4h"] = True
        telegram_send(
            "📊 ۱۰۰ نمونه تمیز ۴ساعته برای رادار Nobitex ثبت شد.\n\n"
            "اکنون می‌توان عملکرد BUILDUP / MICRO / FAST / CONFIRMED / NEWS / WS_OBI "
            "را با داده واقعی تنظیم کرد."
        )

    performance["updated_at"] = now
    save_json(PERFORMANCE_STATE_PATH, performance)

    print("\nPERFORMANCE SUMMARY")
    for event_type in EVENT_TYPES:
        data = performance["statistics"][event_type]
        print("\n", event_type, "| events:", data["events"])
        for checkpoint_name in ("5m", "10m", "15m", "30m", "1h", "4h"):
            cp = data["checkpoints"][checkpoint_name]
            print(
                " ", checkpoint_name,
                "| n=", cp["count"],
                "| avg=", f"{cp['average_return']:+.2f}%",
                "| +1=", cp["hit_1pct"],
                "| +2=", cp["hit_2pct"],
                "| +5=", cp["hit_5pct"],
                "| <=-2=", cp["hit_minus_2pct"],
            )
        print(
            " MFE avg/best:",
            f"{data['MFE']['average_return']:+.2f}%/{data['MFE']['best_return']:+.2f}%",
            "| MAE worst:",
            f"{data['MAE']['worst_return']:+.2f}%",
        )
        for key in ("1pct", "2pct", "5pct"):
            timing = data["time_to_" + key]
            print(
                " time_to_" + key,
                "hits=",
                timing["count"],
                "avg_min=",
                f"{timing['average_minutes']:.1f}",
            )

    print(
        "\nCompleted 4H:",
        completed_4h,
        "| Clean 4H:",
        clean_completed_4h,
        "/",
        MIN_RELIABLE_4H_SAMPLES,
    )
    print("Reliable:", performance["reliability"]["reliable"])
    print("PERFORMANCE TRACKER FINISHED")


if __name__ == "__main__":
    main()
