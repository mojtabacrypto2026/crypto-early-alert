
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://apiv2.nobitex.ir/market/udf/history"
SYMBOL = "BTCUSDT"
RESOLUTION = "1"
WINDOW_MINUTES = 24 * 60


def fetch_page(page, start_ts, end_ts, use_countback=False):
    params = {
        "symbol": SYMBOL,
        "resolution": RESOLUTION,
        "to": end_ts,
        "page": page,
    }

    if use_countback:
        params["countback"] = 500
    else:
        params["from"] = start_ts

    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)
    except Exception as exc:
        return {
            "page": page,
            "error": str(exc),
            "url": url,
        }

    status = str(data.get("s", "missing")).lower()
    times = data.get("t") or []
    closes = data.get("c") or []

    def show_time(value):
        try:
            value = int(value)
            if value > 10**12:
                value //= 1000
            return datetime.fromtimestamp(
                value, timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(value)

    valid_times = []
    for value in times:
        try:
            stamp = int(value)
            if stamp > 10**12:
                stamp //= 1000
            valid_times.append(stamp)
        except (TypeError, ValueError):
            pass

    return {
        "page": page,
        "mode": "countback" if use_countback else "from_to",
        "status": status,
        "candles": len(times),
        "close_values": len(closes),
        "oldest": show_time(min(valid_times)) if valid_times else None,
        "newest": show_time(max(valid_times)) if valid_times else None,
        "first_raw_timestamp": times[0] if times else None,
        "last_raw_timestamp": times[-1] if times else None,
        "timestamps": valid_times,
        "url": url,
    }


def print_result(result):
    print("-" * 60)
    for key, value in result.items():
        if key != "timestamps":
            print(f"{key}: {value}")


def main():
    # End at the most recent fully closed minute.
    end_ts = (int(time.time()) // 60) * 60 - 60
    start_ts = end_ts - WINDOW_MINUTES * 60

    print("NOBITEX HISTORY PAGINATION TEST")
    print("Symbol:", SYMBOL)
    print("Window start:", datetime.fromtimestamp(
        start_ts, timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC"))
    print("Window end:", datetime.fromtimestamp(
        end_ts, timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC"))

    results = []

    for mode in (False, True):
        mode_name = "countback" if mode else "from_to"
        print(f"\n### MODE: {mode_name} ###")

        first = fetch_page(1, start_ts, end_ts, mode)
        print_result(first)
        results.append(first)

        time.sleep(1.2)

        second = fetch_page(2, start_ts, end_ts, mode)
        print_result(second)
        results.append(second)

        first_times = set(first.get("timestamps", []))
        second_times = set(second.get("timestamps", []))

        print("page_1_page_2_overlap:", len(first_times & second_times))
        print("page_2_has_new_timestamps:", bool(second_times - first_times))
        print("page_1_and_page_2_identical:",
              first_times == second_times and bool(first_times))

        time.sleep(1.2)

    print("\n### SUMMARY ###")
    for result in results:
        print(
            result.get("mode"),
            "page", result.get("page"),
            "status", result.get("status", result.get("error")),
            "candles", result.get("candles", 0),
            "oldest", result.get("oldest"),
            "newest", result.get("newest"),
        )


if __name__ == "__main__":
    main()

