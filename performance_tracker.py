# -*- coding: utf-8 -*-

import os
import json
import time
import urllib.request
import urllib.parse

BASE_URL = "https://apiv2.nobitex.ir"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json"
)

PERFORMANCE_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_signal_performance_state.json"
)

CHECKPOINTS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
}


def http_get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nobitex-Performance-Tracker/1.0",
            "Accept": "application/json",
        },
        method="GET"
    )

    with urllib.request.urlopen(
        req,
        timeout=20
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print("LOAD ERROR:", e)
        return default


def save_json(path, data):
    temp = path + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp,
        path
    )


def get_prices():
    data = http_get_json(
        BASE_URL + "/v3/orderbook/all"
    )

    prices = {}

    if not isinstance(data, dict):
        return prices

    for symbol, book in data.items():

        symbol = str(symbol).upper()

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
                bid = float(bids[0][0])

            if asks:
                ask = float(asks[0][0])

        except Exception:
            continue

        if bid > 0 and ask > 0:
            prices[symbol] = (bid + ask) / 2

        elif bid > 0:
            prices[symbol] = bid

        elif ask > 0:
            prices[symbol] = ask

    return prices


def return_percent(start, current):

    if start <= 0:
        return 0.0

    return (
        (current - start)
        / start
    ) * 100.0


def main():

    now = int(time.time())

    alert_state = load_json(
        ALERT_STATE_PATH,
        {}
    )

    performance = load_json(
        PERFORMANCE_STATE_PATH,
        {
            "events": {}
        }
    )

    if not isinstance(
        performance,
        dict
    ):
        performance = {
            "events": {}
        }

    if not isinstance(
        performance.get("events"),
        dict
    ):
        performance["events"] = {}

    try:
        prices = get_prices()

    except Exception as e:
        print(
            "PRICE FETCH FAILED:",
            e
        )
        return

    # ---------------------------------------------------------
    # REGISTER NEW ALERTS
    # ---------------------------------------------------------

    for symbol, alert in alert_state.items():

        if not isinstance(alert, dict):
            continue

        candidates = []

        if alert.get("fast_timestamp"):
            candidates.append(
                (
                    "FAST",
                    int(
                        float(
                            alert["fast_timestamp"]
                        )
                    )
                )
            )

        if alert.get("timestamp"):
            candidates.append(
                (
                    "CONFIRMED",
                    int(
                        float(
                            alert["timestamp"]
                        )
                    )
                )
            )

        if symbol not in prices:
            continue

        current_price = prices[symbol]

        for alert_type, timestamp in candidates:

            event_id = (
                symbol
                + "_"
                + alert_type
                + "_"
                + str(timestamp)
            )

            if event_id in performance["events"]:
                continue

            performance["events"][event_id] = {
                "symbol": symbol,
                "type": alert_type,
                "alert_time": timestamp,
                "alert_price": current_price,
                "checkpoints": {},
                "max_return": 0.0,
                "max_price": current_price,
            }

            print(
                "NEW PERFORMANCE EVENT:",
                event_id,
                current_price
            )

    # ---------------------------------------------------------
    # UPDATE EVENTS
    # ---------------------------------------------------------

    for event_id, event in list(
        performance["events"].items()
    ):

        symbol = event.get(
            "symbol"
        )

        alert_time = int(
            event.get(
                "alert_time",
                0
            )
        )

        alert_price = float(
            event.get(
                "alert_price",
                0
            )
        )

        if (
            not symbol
            or alert_time <= 0
            or alert_price <= 0
        ):
            continue

        if symbol not in prices:
            continue

        current_price = prices[symbol]

        current_return = return_percent(
            alert_price,
            current_price
        )

        if current_return > float(
            event.get(
                "max_return",
                0
            )
        ):
            event["max_return"] = (
                current_return
            )
            event["max_price"] = (
                current_price
            )

        elapsed = now - alert_time

        for name, seconds in CHECKPOINTS.items():

            if elapsed >= seconds:

                if name not in event["checkpoints"]:

                    event["checkpoints"][name] = {
                        "price": current_price,
                        "return_percent": current_return,
                        "timestamp": now,
                    }

        performance["events"][event_id] = event

    # ---------------------------------------------------------
    # LIMIT OLD EVENTS
    # ---------------------------------------------------------

    events = performance["events"]

    if len(events) > 1000:

        ordered = sorted(
            events.items(),
            key=lambda x:
            int(
                x[1].get(
                    "alert_time",
                    0
                )
            ),
            reverse=True
        )

        performance["events"] = dict(
            ordered[:1000]
        )

    # ---------------------------------------------------------
    # STATISTICS
    # ---------------------------------------------------------

    completed_1h = []
    completed_4h = []

    for event in performance["events"].values():

        checkpoints = event.get(
            "checkpoints",
            {}
        )

        if "1h" in checkpoints:
            completed_1h.append(
                checkpoints["1h"]["return_percent"]
            )

        if "4h" in checkpoints:
            completed_4h.append(
                checkpoints["4h"]["return_percent"]
            )

    performance["statistics"] = {
        "total_events": len(
            performance["events"]
        ),
        "completed_1h": len(
            completed_1h
        ),
        "completed_4h": len(
            completed_4h
        ),
        "average_1h_return": (
            sum(completed_1h)
            / len(completed_1h)
            if completed_1h
            else 0.0
        ),
        "average_4h_return": (
            sum(completed_4h)
            / len(completed_4h)
            if completed_4h
            else 0.0
        ),
        "positive_1h": sum(
            1
            for x in completed_1h
            if x > 0
        ),
        "positive_4h": sum(
            1
            for x in completed_4h
            if x > 0
        ),
    }

    performance["updated_at"] = now

    save_json(
        PERFORMANCE_STATE_PATH,
        performance
    )

    print(
        "PERFORMANCE TRACKER FINISHED"
    )

    print(
        "Events:",
        len(performance["events"])
    )

    print(
        "Completed 1H:",
        len(completed_1h)
    )

    print(
        "Completed 4H:",
        len(completed_4h)
    )


if __name__ == "__main__":
    main()
