
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://apiv2.nobitex.ir/market/udf/history"

CASES = [
    ("BTCUSDT", 1791645830, 1791660230),
    ("UNIUSDT", 1791691656, 1791706056),
    ("ATOMUSDT", 1791444645, 1792156537),
    ("NOTUSDT", 1791571618, 1791586018),
    ("LTCUSDT", 1792387070, 1792421968),
]

def fmt(ts):
    return datetime.fromtimestamp(
        ts, timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

def fetch(symbol, start, end, page, mode):
    params = {
        "symbol": symbol,
        "resolution": "1",
        "to": end + 120,
        "page": page,
    }

    if mode == "from_to":
        params["from"] = max(0, start - 120)
    else:
        params["countback"] = 500

    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "HistoryDiagnostic/1.0"}
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

        times = data.get("t") or []
        return {
            "status": data.get("s"),
            "candles": len(times),
            "oldest": fmt(int(min(times))) if times else None,
            "newest": fmt(int(max(times))) if times else None,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

def main():
    for symbol, start, end in CASES:
        print(f"\n=== {symbol}: {fmt(start)} to {fmt(end)} ===")

        for mode in ("from_to", "countback"):
            for page in (1, 2):
                result = fetch(symbol, start, end, page, mode)
                print(
                    f"{mode} page={page}: "
                    + json.dumps(result, ensure_ascii=False)
                )
                time.sleep(0.25)

if __name__ == "__main__":
    main()

