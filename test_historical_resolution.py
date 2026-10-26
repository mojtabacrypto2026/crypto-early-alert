
#!/usr/bin/env python3
"""Read-only Nobitex UDF history diagnostic."""
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = "https://apiv2.nobitex.ir/market/udf/history"
SYMBOLS = {
    "BTCUSDT": [("event", "2026-10-10T15:23:50Z", "2026-10-10T19:23:50Z")],
    "UNIUSDT": [("event", "2026-10-11T04:07:36Z", "2026-10-11T08:07:36Z")],
    "ATOMUSDT": [
        ("event_start", "2026-10-08T07:30:45Z", "2026-10-08T11:30:45Z"),
        ("event_end", "2026-10-16T09:15:37Z", "2026-10-16T13:15:37Z"),
    ],
    "NOTUSDT": [("event", "2026-10-09T18:46:58Z", "2026-10-09T22:46:58Z")],
    "LTCUSDT": [("event", "2026-10-19T05:17:50Z", "2026-10-19T09:17:50Z")],
}
RESOLUTIONS = ("1", "5", "15", "60")
TIMEOUT = 20


def to_ts(value):
    return int(datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).timestamp())


def utc(ts):
    return datetime.fromtimestamp(
        int(ts), timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch(symbol, resolution, start, end):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": resolution,
        "from": start,
        "to": end,
    })
    req = urllib.request.Request(
        BASE + "?" + params,
        headers={
            "User-Agent": "Mozilla/5.0 history-diagnostic/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {"http": "ERROR", "error": str(exc)}

    try:
        data = json.loads(raw)
    except Exception:
        return {
            "http": status,
            "error": "non_json_response",
            "sample": raw[:180],
        }

    if data.get("s") != "ok":
        return {
            "http": status,
            "status": data.get("s"),
            "errmsg": data.get("errmsg"),
            "error": data.get("error"),
            "keys": sorted(data.keys()),
        }

    times = data.get("t") or []
    first = min(times) if times else None
    last = max(times) if times else None
    interval = int(resolution) * 60

    return {
        "http": status,
        "status": data.get("s"),
        "count": len(times),
        "first_utc": utc(first) if first is not None else "-",
        "last_utc": utc(last) if last is not None else "-",
        "expected_start_utc": utc(start),
        "expected_end_utc": utc(end),
        "coverage_start": (
            first is not None and first <= start + interval
        ),
        "coverage_end": (
            last is not None and last >= end - interval
        ),
    }


def main():
    now = datetime.now(timezone.utc)
    recent_end = int(now.timestamp())
    recent_start = int((now - timedelta(hours=4)).timestamp())

    print("HISTORICAL RESOLUTION DIAGNOSTIC v1")
    print("UTC now:", now.strftime("%Y-%m-%d %H:%M:%S UTC"))
    print("Read-only test; production files are not modified.")

    for symbol, windows in SYMBOLS.items():
        for label, start_text, end_text in windows:
            start, end = to_ts(start_text), to_ts(end_text)

            for resolution in RESOLUTIONS:
                result = fetch(symbol, resolution, start, end)
                print(
                    f"[{label}] {symbol} res={resolution}m "
                    + json.dumps(result, ensure_ascii=False, sort_keys=True)
                )
                time.sleep(0.15)

        for resolution in RESOLUTIONS:
            result = fetch(
                symbol, resolution, recent_start, recent_end
            )
            print(
                f"[recent_4h_control] {symbol} res={resolution}m "
                + json.dumps(result, ensure_ascii=False, sort_keys=True)
            )
            time.sleep(0.15)

    print("DONE. No production files or state files were changed.")


if __name__ == "__main__":
    main()

