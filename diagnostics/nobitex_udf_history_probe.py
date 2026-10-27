
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only diagnostic for Nobitex historical candle API."""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://apiv2.nobitex.ir"
SYMBOLS = ["BTCUSDT", "UNIUSDT", "ATOMUSDT"]
RESOLUTIONS = ["1", "5", "15", "60"]
TIMEOUT = 15
DELAY = 1.05


def timestamp(value):
    return int(
        datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    )


def request_history(params):
    url = BASE_URL + "/market/udf/history?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nobitex-History-Diagnostic/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")

        data = json.loads(raw)

        if not isinstance(data, dict):
            return {"error": "Invalid response format"}

        times = data.get("t") or []

        return {
            "status": data.get("s"),
            "candles": len(times),
            "first_timestamp": times[0] if times else None,
            "last_timestamp": times[-1] if times else None,
            "api_error": data.get("errmsg"),
        }

    except Exception as exc:
        return {
            "error": type(exc).__name__ + ": " + str(exc)
        }


def main():
    now = int(time.time())

    windows = {
        "historical": (
            timestamp("2026-10-12T08:00:00Z"),
            timestamp("2026-10-12T12:00:00Z"),
        ),
        "recent": (now - 4 * 3600, now),
    }

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Read-only test; does not change scanner or tracker.",
        "results": [],
    }

    for symbol in SYMBOLS:
        for resolution in RESOLUTIONS:
            for window_name, (start, end) in windows.items():
                tests = [
                    (
                        "from_to",
                        {
                            "symbol": symbol,
                            "resolution": resolution,
                            "from": start,
                            "to": end,
                        },
                    ),
                    (
                        "padded_from_to",
                        {
                            "symbol": symbol,
                            "resolution": resolution,
                            "from": start - 120,
                            "to": end + 120,
                        },
                    ),
                    (
                        "countback_100",
                        {
                            "symbol": symbol,
                            "resolution": resolution,
                            "to": end,
                            "countback": 100,
                        },
                    ),
                ]

                for test_name, params in tests:
                    result = request_history(params)
                    report["results"].append({
                        "symbol": symbol,
                        "resolution": resolution,
                        "window": window_name,
                        "test": test_name,
                        "result": result,
                    })
                    time.sleep(DELAY)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

