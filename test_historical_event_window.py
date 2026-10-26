
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic only: compare historical and recent Nobitex UDF data."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://apiv2.nobitex.ir/market/udf/history"
TIMEOUT = 20
PAUSE = 0.25

CASES = [
    ("BTCUSDT", 1791645830, 1791660230),
    ("UNIUSDT", 1791691656, 1791706056),
    ("ATOMUSDT", 1791444645, 1792156537),
    ("NOTUSDT", 1791571618, 1791586018),
    ("LTCUSDT", 1792387070, 1792421968),
]


def fmt(ts):
    try:
        return datetime.fromtimestamp(
            int(ts), timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def fetch(symbol, start, end, mode):
    params = {
        "symbol": symbol,
        "resolution": "1",
        "to": int(end) + 120,
    }

    if mode == "from_to":
        params["from"] = max(0, int(start) - 120)
    elif mode == "countback":
        params["countback"] = 500
    else:
        raise ValueError(f"Unknown mode: {mode}")

    url = BASE + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NobitexHistoricalDiagnostic/2.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            http_status = response.status

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {
                "http_status": http_status,
                "params": params,
                "json_error": True,
                "body_preview": raw[:700],
            }

        if not isinstance(data, dict):
            return {
                "http_status": http_status,
                "params": params,
                "response_type": type(data).__name__,
                "body_preview": repr(data)[:500],
            }

        times = data.get("t") or []
        closes = data.get("c") or []

        try:
            timestamps = [int(t) for t in times]
        except (TypeError, ValueError):
            timestamps = []

        result = {
            "http_status": http_status,
            "params": params,
            "status": data.get("s"),
            "errmsg": data.get("errmsg"),
            "error": data.get("error"),
            "candles": len(times),
            "close_values": len(closes),
            "oldest": fmt(min(timestamps)) if timestamps else None,
            "newest": fmt(max(timestamps)) if timestamps else None,
            "response_keys": sorted(data.keys()),
        }

        # Keep unexpected API metadata, but not the large candle arrays.
        for key, value in data.items():
            if key not in {"t", "o", "h", "l", "c", "v"}:
                if key not in result:
                    result[key] = value

        return result

    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""

        return {
            "http_status": exc.code,
            "params": params,
            "http_error": str(exc),
            "body_preview": body[:700],
        }

    except Exception as exc:
        return {
            "params": params,
            "exception": f"{type(exc).__name__}: {exc}",
        }


def run_test(symbol, label, start, end):
    print(f"\n=== {symbol} | {label} ===", flush=True)
    print(
        f"requested: {fmt(start)} -> {fmt(end)}",
        flush=True,
    )

    for mode in ("from_to", "countback"):
        result = fetch(symbol, start, end, mode)
        print(
            f"{mode}: "
            + json.dumps(result, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        time.sleep(PAUSE)


def main():
    now = int(datetime.now(timezone.utc).timestamp())
    recent_start = now - 4 * 60 * 60

    print("Nobitex UDF historical diagnostic v2", flush=True)
    print(f"diagnostic_time_utc: {fmt(now)}", flush=True)
    print("Diagnostic only; production files are not modified.", flush=True)

    for symbol, start, end in CASES:
        run_test(
            symbol,
            "historical event window",
            start,
            end,
        )
        run_test(
            symbol,
            "recent control window (last 4 hours)",
            recent_start,
            now,
        )


if __name__ == "__main__":
    main()
