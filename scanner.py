# -*- coding: utf-8 -*-
"""
NOBITEX EARLY MOVE RADAR - ONE-TIME CLEAN FINAL

هدف:
- اسکن بازار USDT نوبیتکس
- حفظ منطق اصلی PRE-MOVE
- FAST PRE-MOVE برای هشدار زودتر
- لایه NEWS CATALYST بدون دستکاری امتیاز تکنیکال
- جلوگیری از false positive در صورت خبر منفی تازه
- قیمت هشدار از orderbook
- فقط یک درخواست 15m برای هر بازار و ساخت 1H به‌صورت محلی
- جلوگیری از ادامه streak قدیمی
- کنترل پوشش بازار
- Telegram
- بدون کتابخانه خارجی
"""

import os
import urllib.request
import urllib.parse
import json
import time
import math
import statistics
import threading
import hashlib
import re
import email.utils
import calendar
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"

MAX_WORKERS = 4
COUNTBACK_15M = 380
TOP_N = 5

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

# UDF: about one request per second globally.
UDF_MIN_INTERVAL = 1.05
_udf_rate_lock = threading.Lock()
_udf_next_request_at = 0.0

# A score streak cannot continue after a long gap.
MAX_STREAK_GAP_SECONDS = 25 * 60

# Do not send alerts when too much of the market failed.
MIN_COVERAGE_FOR_ALERTS = 0.80

# Confirmed alert anti-spam.
CONFIRMED_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
ALERT_STATE_TTL_SECONDS = 24 * 60 * 60

# Performance tracking.
TRACKING_SCHEMA_VERSION = 1
TRACKING_START_PATH = "nobitex_performance_tracking_start.json"

# News layer.
NEWS_ENABLED = True
NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/?format=rss"),
]
NEWS_LOOKBACK_SECONDS = 6 * 60 * 60
NEWS_FRESH_SECONDS = 90 * 60
NEWS_ALERT_COOLDOWN_SECONDS = 2 * 60 * 60
NEWS_MIN_SCORE = 8

NEWS_POSITIVE_STRONG = (
    "etf approval",
    "etf approved",
    "approved",
    "listing",
    "listed",
    "partnership",
    "integrates",
    "integration",
    "mainnet",
    "launches",
    "launched",
    "buyback",
    "burn",
    "acquires",
    "acquisition",
    "adoption",
    "wins approval",
    "inflows",
    "treasury",
)
NEWS_POSITIVE_WEAK = (
    "upgrade",
    "funding",
    "investment",
    "staking",
    "institutional",
    "expands",
    "expansion",
    "support",
)
NEWS_NEGATIVE_STRONG = (
    "hack",
    "exploit",
    "breach",
    "stolen",
    "delist",
    "delisting",
    "lawsuit",
    "fraud",
    "bankruptcy",
    "shutdown",
    "halt",
    "attack",
)
NEWS_NEGATIVE_WEAK = (
    "unlock",
    "outflows",
    "liquidation",
    "downgrade",
)

# Common project names. Unknown markets still fall back to exact ticker matching.
NEWS_ALIAS_MAP = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "ether", "eth"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"),
    "ADA": ("cardano", "ada"),
    "DOGE": ("dogecoin", "doge"),
    "BNB": ("bnb", "binance coin"),
    "AVAX": ("avalanche", "avax"),
    "LINK": ("chainlink", "link"),
    "DOT": ("polkadot", "dot"),
    "TRX": ("tron", "trx"),
    "SUI": ("sui",),
    "TON": ("toncoin", "ton"),
    "NEAR": ("near protocol", "near"),
    "APT": ("aptos", "apt"),
    "ATOM": ("cosmos", "atom"),
    "UNI": ("uniswap", "uni"),
    "AAVE": ("aave",),
    "LTC": ("litecoin", "ltc"),
    "ETC": ("ethereum classic", "etc"),
    "ARB": ("arbitrum", "arb"),
    "OP": ("optimism",),
    "FIL": ("filecoin", "fil"),
    "ENA": ("ethena", "ena"),
    "MKR": ("maker", "mkr"),
    "INJ": ("injective", "inj"),
    "TAO": ("bittensor", "tao"),
    "SEI": ("sei",),
    "RENDER": ("render", "render token"),
    "ZEC": ("zcash", "zec"),
    "ZRO": ("layerzero", "zro"),
    "SUSHI": ("sushiswap", "sushi"),
    "PENGU": ("pudgy penguins", "pengu"),
    "HYPE": ("hyperliquid", "hype"),
    "TURBO": ("turbo",),
    "WIF": ("dogwifhat", "wif"),
    "RAY": ("raydium", "ray"),
    "JUP": ("jupiter", "jup"),
    "MANA": ("decentraland", "mana"),
    "SAND": ("the sandbox", "sandbox", "sand"),
    "CRV": ("curve", "curve dao", "crv"),
    "COMP": ("compound", "comp"),
    "MNT": ("mantle", "mnt"),
    "TIA": ("celestia", "tia"),
    "KAS": ("kaspa", "kas"),
    "VIRTUAL": ("virtual protocol", "virtuals", "virtual"),
    "ONDO": ("ondo",),
    "PENDLE": ("pendle",),
}

NEWS_SHORT_SAFE = {
    "ADA", "ARB", "APT", "AVAX", "BNB", "BTC", "DOGE", "DOT",
    "ETH", "ETC", "FIL", "HYPE", "INJ", "LINK", "LTC", "MKR",
    "NEAR", "OP", "RAY", "SEI", "SOL", "SUI", "TAO", "TON",
    "TRX", "UNI", "XRP", "ZEC", "ZRO",
}

EXCLUDED_SYMBOLS = {
    "USDCUSDT",
    "USDEUSDT",
    "DAIUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
    "WBTCUSDT",
}

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

WORKSPACE = os.environ.get(
    "GITHUB_WORKSPACE",
    ".",
)

STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_early_radar_state.json",
)

ALERT_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_telegram_alert_state.json",
)

TRACKING_START_FULL_PATH = os.path.join(
    WORKSPACE,
    TRACKING_START_PATH,
)

TELEGRAM_TEST_ON_START = False


# ============================================================
# HTTP
# ============================================================

def _rate_limit_udf():
    global _udf_next_request_at

    with _udf_rate_lock:
        now = time.monotonic()
        wait = _udf_next_request_at - now

        if wait > 0:
            time.sleep(wait)

        _udf_next_request_at = time.monotonic() + UDF_MIN_INTERVAL


def http_get_json(url, params=None, udf=False):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    headers = {
        "User-Agent": "Nobitex-Early-Radar/4.0",
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            if udf:
                _rate_limit_udf()

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
                time.sleep(1.2 * (attempt + 1))

    raise last_error


def http_get_text(url):
    headers = {
        "User-Agent": "Nobitex-Early-Radar-News/1.0",
        "Accept": "application/rss+xml, application/xml, text/xml, text/plain",
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
                return response.read().decode(
                    "utf-8",
                    errors="replace",
                )

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
            "User-Agent": "Nobitex-Early-Radar/4.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
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
                time.sleep(1.2 * (attempt + 1))

    raise last_error


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram disabled: secrets are not configured.")
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

        if result.get("ok"):
            print("Telegram message sent.")
            return True

        print("Telegram error:", result)
        return False

    except Exception as exc:
        print("Telegram exception:", exc)
        return False


def telegram_test():
    return telegram_send(
        "✅ اتصال رادار Early Move برقرار است.\n"
        "Nobitex Early Move Radar فعال است."
    )


# ============================================================
# HELPERS / STATE
# ============================================================

def safe_float(value, default=0.0):
    try:
        result = float(value)

        if math.isfinite(result):
            return result

    except Exception:
        pass

    return default


def clamp(value, low, high):
    return max(low, min(high, value))


def median(values):
    clean = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not clean:
        return 0.0

    return statistics.median(clean)


def average(values):
    clean = [
        safe_float(x)
        for x in values
        if safe_float(x) > 0
    ]

    if not clean:
        return 0.0

    return sum(clean) / len(clean)


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as handle:
            return json.load(handle)

    except Exception as exc:
        print("State load error:", path, exc)
        return default


def save_json(path, data):
    try:
        directory = os.path.dirname(path)

        if directory:
            os.makedirs(
                directory,
                exist_ok=True,
            )

        temp_path = path + ".tmp"

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(temp_path, path)
        return True

    except Exception as exc:
        print("State save error:", path, exc)
        return False


def ensure_tracking_start():
    existing = load_json(
        TRACKING_START_FULL_PATH,
        {},
    )

    if (
        isinstance(existing, dict)
        and existing.get("schema_version")
        == TRACKING_SCHEMA_VERSION
        and safe_float(
            existing.get("started_at"),
            0,
        ) > 0
    ):
        return int(existing["started_at"])

    started_at = int(time.time())

    payload = {
        "schema_version": TRACKING_SCHEMA_VERSION,
        "started_at": started_at,
        "created_at": started_at,
    }

    save_json(
        TRACKING_START_FULL_PATH,
        payload,
    )

    print(
        "Performance tracking start:",
        started_at,
    )

    return started_at


# ============================================================
# MARKET / CANDLES
# ============================================================

def get_market_snapshot():
    data = http_get_json(
        BASE_URL + "/v3/orderbook/all"
    )

    result = {}

    if not isinstance(data, dict):
        return result

    # API can include a top-level status key.
    for raw_symbol, book in data.items():
        symbol = str(raw_symbol).upper()

        if not symbol.endswith("USDT"):
            continue

        if symbol in EXCLUDED_SYMBOLS:
            continue

        if not isinstance(book, dict):
            continue

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid_value = 0.0
        ask_value = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            if bids:
                best_bid = safe_float(
                    bids[0][0]
                )

            if asks:
                best_ask = safe_float(
                    asks[0][0]
                )

            for row in bids[:20]:
                if len(row) >= 2:
                    bid_value += (
                        safe_float(row[0])
                        * safe_float(row[1])
                    )

            for row in asks[:20]:
                if len(row) >= 2:
                    ask_value += (
                        safe_float(row[0])
                        * safe_float(row[1])
                    )

        except Exception:
            pass

        flow = (
            bid_value / ask_value
            if ask_value > 0
            else 0.0
        )

        if best_bid > 0 and best_ask > 0:
            live_price = (
                best_bid + best_ask
            ) / 2.0

        elif best_bid > 0:
            live_price = best_bid

        else:
            live_price = best_ask

        result[symbol] = {
            "bid_value": bid_value,
            "ask_value": ask_value,
            "flow": flow,
            "price": live_price,
        }

    return result


def get_15m_candles(symbol):
    params = {
        "symbol": symbol,
        "resolution": "15",
        "countback": COUNTBACK_15M,
        "to": int(time.time()),
    }

    data = http_get_json(
        BASE_URL + "/market/udf/history",
        params=params,
        udf=True,
    )

    if not isinstance(data, dict):
        raise ValueError(
            "INVALID_CANDLE_RESPONSE"
        )

    if data.get("s") not in ("ok", "no_data"):
        raise ValueError(
            "CANDLE_STATUS_"
            + str(data.get("s"))
        )

    timestamps = [
        int(safe_float(x))
        for x in data.get("t", [])
    ]

    closes = [
        safe_float(x)
        for x in data.get("c", [])
    ]

    volumes = [
        safe_float(x)
        for x in data.get("v", [])
    ]

    n = min(
        len(timestamps),
        len(closes),
        len(volumes),
    )

    timestamps = timestamps[:n]
    closes = closes[:n]
    volumes = volumes[:n]

    if len(closes) < 25:
        raise ValueError(
            "CANDLES_TOO_SHORT"
        )

    # Remove currently open 15m candle.
    now = int(time.time())

    if timestamps:
        if now - timestamps[-1] < 900:
            timestamps = timestamps[:-1]
            closes = closes[:-1]
            volumes = volumes[:-1]

    if len(closes) < 25:
        raise ValueError(
            "CANDLES_TOO_SHORT_AFTER_REMOVE"
        )

    return (
        timestamps,
        closes,
        volumes,
    )


def aggregate_15m_to_1h(
    timestamps,
    closes,
    volumes,
):
    buckets = {}

    for ts, close, volume in zip(
        timestamps,
        closes,
        volumes,
    ):
        hour_start = (
            int(ts) // 3600
        ) * 3600

        buckets.setdefault(
            hour_start,
            [],
        ).append(
            (
                int(ts),
                close,
                volume,
            )
        )

    hour_timestamps = []
    hour_closes = []
    hour_volumes = []

    now = int(time.time())

    for hour_start in sorted(buckets):
        rows = sorted(
            buckets[hour_start],
            key=lambda row: row[0],
        )

        if len(rows) < 4:
            continue

        if now < hour_start + 3600:
            continue

        hour_timestamps.append(
            hour_start
        )

        hour_closes.append(
            rows[-1][1]
        )

        hour_volumes.append(
            sum(
                row[2]
                for row in rows
            )
        )

    if len(hour_closes) < 25:
        raise ValueError(
            "HOURLY_AGGREGATION_TOO_SHORT"
        )

    return (
        hour_timestamps,
        hour_closes,
        hour_volumes,
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    clean = [
        safe_float(x)
        for x in values
    ]

    if not clean:
        return []

    if len(clean) < period:
        return [
            clean[-1]
        ] * len(clean)

    alpha = 2.0 / (
        period + 1.0
    )

    result = [clean[0]]

    for value in clean[1:]:
        result.append(
            alpha * value
            + (1.0 - alpha)
            * result[-1]
        )

    return result


def rsi(values, period=14):
    values = [
        safe_float(x)
        for x in values
    ]

    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = (
            values[i]
            - values[i - 1]
        )

        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(
                abs(diff)
            )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains),
    ):
        avg_gain = (
            avg_gain * (
                period - 1
            )
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (
                period - 1
            )
            + losses[i]
        ) / period

    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0

        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return (
        100.0
        - 100.0
        / (1.0 + rs)
    )


def macd(values):
    fast = ema(
        values,
        12,
    )

    slow = ema(
        values,
        26,
    )

    n = min(
        len(fast),
        len(slow),
    )

    if n == 0:
        return 0.0, 0.0

    line = [
        fast[i] - slow[i]
        for i in range(n)
    ]

    signal = ema(
        line,
        9,
    )

    return (
        line[-1],
        signal[-1],
    )


def structure_score(closes):
    if len(closes) < 30:
        return 0

    e9 = ema(
        closes,
        9,
    )[-1]

    e21 = ema(
        closes,
        21,
    )[-1]

    e50 = ema(
        closes,
        50,
    )[-1]

    price = closes[-1]

    score = 0

    if price > e9:
        score += 2

    if e9 > e21:
        score += 2

    if e21 > e50:
        score += 2

    recent = closes[-12:]

    if len(recent) >= 8:
        if (
            average(recent[:6])
            < average(recent[-6:])
        ):
            score += 2

    return clamp(
        score,
        0,
        8,
    )


def resistance_distance(closes):
    if len(closes) < 20:
        return 99.0

    price = closes[-1]
    lookback = closes[-25:-1]

    if not lookback or price <= 0:
        return 99.0

    resistance = max(
        lookback
    )

    if resistance <= 0:
        return 99.0

    distance = (
        (
            resistance - price
        )
        / price
    ) * 100.0

    return max(
        0.0,
        distance,
    )


def calm_score(closes):
    if len(closes) < 20:
        return 0

    recent = closes[-20:]
    returns = []

    for i in range(
        1,
        len(recent),
    ):
        old = recent[i - 1]
        new = recent[i]

        if old == 0:
            continue

        returns.append(
            abs(
                (
                    new - old
                ) / old
            ) * 100.0
        )

    if not returns:
        return 0

    avg_move = average(
        returns
    )

    if avg_move <= 0.35:
        return 10

    if avg_move <= 0.60:
        return 8

    if avg_move <= 0.90:
        return 5

    if avg_move <= 1.30:
        return 2

    return 0


def momentum_percent(
    closes,
    candles_back,
):
    if len(closes) <= candles_back:
        return 0.0

    old = closes[
        -1 - candles_back
    ]

    new = closes[-1]

    if old == 0:
        return 0.0

    return (
        (new - old)
        / old
    ) * 100.0


# ============================================================
# ORIGINAL TECHNICAL SCORE
# ============================================================

def calculate_score(
    volume_ratio,
    volume_acceleration,
    rsi_value,
    structure,
    resistance,
    calm,
    m1h,
    m4h,
    m8h,
    m15,
    macd_positive,
    flow,
):
    score = 0

    if 1.25 <= volume_ratio <= 4.0:
        score += 18

    elif 1.05 <= volume_ratio < 1.25:
        score += 10

    elif (
        4.0 < volume_ratio <= 6.0
    ):
        score += 8

    elif (
        6.0 < volume_ratio <= 10.0
    ):
        score += 2

    elif volume_ratio > 10.0:
        score -= 8

    if volume_acceleration >= 1.20:
        score += 10

    elif volume_acceleration >= 1.05:
        score += 7

    elif volume_acceleration >= 1.00:
        score += 3

    if 45 <= rsi_value <= 60:
        score += 15

    elif 60 < rsi_value <= 68:
        score += 10

    elif 40 <= rsi_value < 45:
        score += 7

    elif 35 <= rsi_value < 40:
        score += 3

    elif rsi_value > 72:
        score -= 10

    score += int(
        round(
            structure * 2.5
        )
    )

    score = min(
        score,
        100,
    )

    if 0 <= resistance <= 1:
        score += 15

    elif 1 < resistance <= 2:
        score += 12

    elif 2 < resistance <= 3:
        score += 8

    elif 3 < resistance <= 5:
        score += 4

    score += calm

    if 0 <= m1h <= 2.5:
        score += 4

    if 0 <= m4h <= 6:
        score += 4

    if m1h > 4:
        score -= 8

    if m4h > 10:
        score -= 10

    if m8h > 15:
        score -= 8

    if m15 > 2:
        score -= 5

    if macd_positive:
        score += 2

    if flow >= 1.20:
        score += 5

    elif flow >= 1.05:
        score += 3

    elif flow >= 0.95:
        score += 1

    elif 0 < flow < 0.80:
        score -= 5

    return int(
        clamp(
            round(score),
            0,
            100,
        )
    )


# ============================================================
# NEWS
# ============================================================

def _xml_local_name(tag):
    return str(tag).rsplit(
        "}",
        1,
    )[-1].lower()


def _xml_child_text(
    node,
    names,
):
    wanted = {
        str(name).lower()
        for name in names
    }

    for child in list(node):
        if (
            _xml_local_name(
                child.tag
            )
            not in wanted
        ):
            continue

        text_value = "".join(
            child.itertext()
        ).strip()

        if text_value:
            return text_value

        href = child.attrib.get(
            "href",
            "",
        ).strip()

        if href:
            return href

    return ""


def _parse_news_timestamp(
    value,
):
    value = str(
        value or ""
    ).strip()

    if not value:
        return 0

    try:
        dt = (
            email.utils
            .parsedate_to_datetime(
                value
            )
        )

        if dt.tzinfo is None:
            return int(
                calendar.timegm(
                    dt.timetuple()
                )
            )

        return int(
            dt.timestamp()
        )

    except Exception:
        return 0


def news_terms(symbol):
    base = str(
        symbol
    ).upper().replace(
        "USDT",
        "",
    )

    terms = list(
        NEWS_ALIAS_MAP.get(
            base,
            (),
        )
    )

    if (
        len(base) >= 3
        or base in NEWS_SHORT_SAFE
    ):
        terms.append(base)

    output = []
    seen = set()

    for term in terms:
        term = str(term).strip().lower()

        if not term or term in seen:
            continue

        seen.add(term)
        output.append(term)

    return output


def headline_mentions_symbol(
    title,
    symbol,
):
    title_lower = str(
        title or ""
    ).lower()

    for term in news_terms(symbol):
        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(term)
            + r"(?![a-z0-9])"
        )

        if re.search(
            pattern,
            title_lower,
        ):
            return True

    return False


def score_news_title(
    title,
):
    text_value = str(
        title or ""
    ).lower()

    strong_positive = sum(
        1
        for keyword
        in NEWS_POSITIVE_STRONG
        if keyword in text_value
    )

    weak_positive = sum(
        1
        for keyword
        in NEWS_POSITIVE_WEAK
        if keyword in text_value
    )

    strong_negative = sum(
        1
        for keyword
        in NEWS_NEGATIVE_STRONG
        if keyword in text_value
    )

    weak_negative = sum(
        1
        for keyword
        in NEWS_NEGATIVE_WEAK
        if keyword in text_value
    )

    positive_score = min(
        12,
        strong_positive * 4
        + weak_positive * 2,
    )

    negative_score = min(
        12,
        strong_negative * 6
        + weak_negative * 3,
    )

    return int(
        clamp(
            positive_score
            - negative_score,
            -12,
            12,
        )
    )


def fetch_news_items():
    if not NEWS_ENABLED:
        return []

    now = int(time.time())

    items = []
    seen_titles = set()

    for source_name, feed_url in NEWS_FEEDS:
        try:
            raw = http_get_text(
                feed_url
            )

            root = ET.fromstring(
                raw
            )

        except Exception as exc:
            print(
                "NEWS FEED FAILED:",
                source_name,
                exc,
            )
            continue

        for node in root.iter():
            if _xml_local_name(
                node.tag
            ) not in (
                "item",
                "entry",
            ):
                continue

            title = _xml_child_text(
                node,
                ("title",),
            )

            link = _xml_child_text(
                node,
                ("link", "guid"),
            )

            pub_value = _xml_child_text(
                node,
                (
                    "pubDate",
                    "published",
                    "updated",
                    "date",
                ),
            )

            if not title:
                continue

            if not link:
                for child in list(node):
                    if (
                        _xml_local_name(
                            child.tag
                        )
                        == "link"
                    ):
                        link = child.attrib.get(
                            "href",
                            "",
                        ).strip()

                        if link:
                            break

            published_at = (
                _parse_news_timestamp(
                    pub_value
                )
            )

            if published_at <= 0:
                published_at = now

            age = now - published_at

            if age < -300:
                continue

            if age > NEWS_LOOKBACK_SECONDS:
                continue

            title_key = re.sub(
                r"\s+",
                " ",
                title,
            ).strip().lower()

            dedupe_key = (
                source_name
                + "|"
                + title_key
            )

            if dedupe_key in seen_titles:
                continue

            seen_titles.add(
                dedupe_key
            )

            items.append(
                {
                    "id": hashlib.sha1(
                        (
                            source_name
                            + "|"
                            + title_key
                            + "|"
                            + link
                        ).encode(
                            "utf-8"
                        )
                    ).hexdigest()[:20],
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "published_at": published_at,
                    "age_seconds": max(
                        0,
                        age,
                    ),
                    "score": score_news_title(
                        title
                    ),
                }
            )

    return items


def apply_news_to_result(
    result,
    news_items,
):
    symbol = result.get(
        "symbol",
        "",
    )

    matches = [
        item
        for item in news_items
        if (
            isinstance(item, dict)
            and headline_mentions_symbol(
                item.get("title", ""),
                symbol,
            )
            and item.get(
                "score",
                0,
            ) != 0
        )
    ]

    if not matches:
        result.update(
            {
                "news_score": 0,
                "news_sentiment": "none",
                "news_age_minutes": 0.0,
                "news_source": "",
                "news_title": "",
                "news_link": "",
                "news_id": "",
                "news_count": 0,
            }
        )
        return result

    best = max(
        matches,
        key=lambda item: (
            abs(
                item.get(
                    "score",
                    0,
                )
            ),
            -item.get(
                "age_seconds",
                0,
            ),
        ),
    )

    score = int(
        best.get(
            "score",
            0,
        )
    )

    # A second independent headline or source increases confidence.
    if score > 0:
        distinct_sources = len(
            {
                str(
                    item.get(
                        "source",
                        "",
                    )
                )
                for item in matches
            }
        )

        distinct_ids = len(
            {
                str(
                    item.get(
                        "id",
                        "",
                    )
                )
                for item in matches
            }
        )

        if distinct_ids >= 2:
            score += 2

        if distinct_sources >= 2:
            score += 2

        score = min(
            12,
            score,
        )

    result["news_score"] = score

    result["news_sentiment"] = (
        "positive"
        if score > 0
        else "negative"
        if score < 0
        else "none"
    )

    result["news_age_minutes"] = (
        best.get(
            "age_seconds",
            0,
        ) / 60.0
    )

    result["news_source"] = (
        best.get(
            "source",
            "",
        )
    )

    result["news_title"] = (
        best.get(
            "title",
            "",
        )
    )

    result["news_link"] = (
        best.get(
            "link",
            "",
        )
    )

    result["news_id"] = (
        best.get(
            "id",
            "",
        )
    )

    result["news_count"] = len(
        matches
    )

    return result


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(
    symbol,
    book,
):
    try:
        (
            timestamps_15m,
            closes_15m,
            volumes_15m,
        ) = get_15m_candles(
            symbol
        )

        (
            _,
            closes_1h,
            volumes_1h,
        ) = aggregate_15m_to_1h(
            timestamps_15m,
            closes_15m,
            volumes_15m,
        )

        book_price = safe_float(
            book.get(
                "price",
                0,
            ),
            0.0,
        )

        price = (
            book_price
            if book_price > 0
            else closes_15m[-1]
        )

        m15 = momentum_percent(
            closes_15m,
            1,
        )

        m1h = momentum_percent(
            closes_1h,
            1,
        )

        m4h = momentum_percent(
            closes_1h,
            4,
        )

        m8h = momentum_percent(
            closes_1h,
            8,
        )

        current_volume = (
            volumes_1h[-1]
        )

        previous_volumes = (
            volumes_1h[-11:-1]
        )

        med_volume = median(
            previous_volumes
        )

        volume_ratio = (
            current_volume
            / med_volume
            if med_volume > 0
            else 0.0
        )

        avg_last3 = average(
            volumes_1h[-3:]
        )

        avg_prev8 = average(
            volumes_1h[-11:-3]
        )

        volume_acceleration = (
            avg_last3
            / avg_prev8
            if avg_prev8 > 0
            else 0.0
        )

        current_volume_15m = (
            volumes_15m[-1]
        )

        previous_volumes_15m = (
            volumes_15m[-13:-1]
        )

        med_volume_15m = median(
            previous_volumes_15m
        )

        volume_ratio_15m = (
            current_volume_15m
            / med_volume_15m
            if med_volume_15m > 0
            else 0.0
        )

        avg_last3_15m = average(
            volumes_15m[-3:]
        )

        avg_prev8_15m = average(
            volumes_15m[-11:-3]
        )

        volume_acceleration_15m = (
            avg_last3_15m
            / avg_prev8_15m
            if avg_prev8_15m > 0
            else 0.0
        )

        rsi_value = rsi(
            closes_1h
        )

        macd_line, macd_signal = macd(
            closes_1h
        )

        macd_positive = (
            macd_line
            >= macd_signal
        )

        structure = structure_score(
            closes_1h
        )

        resistance = (
            resistance_distance(
                closes_1h
            )
        )

        calm = calm_score(
            closes_1h
        )

        flow = safe_float(
            book.get(
                "flow",
                0,
            ),
            0.0,
        )

        score = calculate_score(
            volume_ratio,
            volume_acceleration,
            rsi_value,
            structure,
            resistance,
            calm,
            m1h,
            m4h,
            m8h,
            m15,
            macd_positive,
            flow,
        )

        pre_move_gate = (
            1.15
            <= volume_ratio
            <= 6.0
            and structure >= 5
            and 45
            <= rsi_value
            <= 68
            and 0
            <= resistance
            <= 3.5
            and m1h <= 2.8
            and m4h <= 7.0
            and m15 <= 2.0
        )

        # Preserved FAST logic.
        fast_pre_move = (
            volume_ratio_15m >= 1.80
            and volume_acceleration_15m >= 1.25
            and structure >= 5
            and 45
            <= rsi_value
            <= 68
            and 0
            <= resistance
            <= 3.5
            and m1h <= 2.8
            and m4h <= 7.0
            and m15 <= 2.0
            and flow >= 0.95
            and score >= 55
        )

        high_risk_jump = (
            m15 > 3
            or m1h > 4
            or rsi_value > 72
            or volume_ratio_15m > 6
            or volume_acceleration_15m > 3
        )

        quality = True

        if rsi_value > 72:
            quality = False

        if m1h > 4:
            quality = False

        if m4h > 10:
            quality = False

        if volume_ratio > 10:
            quality = False

        if (
            pre_move_gate
            and score >= 75
        ):
            label = "PRE-MOVE"

        elif (
            pre_move_gate
            and score >= 65
        ):
            label = "EARLY RADAR"

        elif (
            quality
            and score >= 55
        ):
            label = "WATCH"

        elif (
            m1h > 4
            or m4h > 10
        ):
            label = "ALREADY MOVING"

        else:
            label = "LOW"

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "label": label,
            "pre_move_gate": pre_move_gate,
            "quality": quality,
            "volume_ratio": volume_ratio,
            "volume_acceleration": volume_acceleration,
            "volume_ratio_15m": volume_ratio_15m,
            "volume_acceleration_15m": volume_acceleration_15m,
            "fast_pre_move": fast_pre_move,
            "high_risk_jump": high_risk_jump,
            "rsi": rsi_value,
            "structure": structure,
            "resistance": resistance,
            "calm": calm,
            "momentum_15m": m15,
            "momentum_1h": m1h,
            "momentum_4h": m4h,
            "momentum_8h": m8h,
            "order_flow": flow,
            "macd_positive": macd_positive,
            "news_score": 0,
            "news_sentiment": "none",
            "news_age_minutes": 0.0,
            "news_source": "",
            "news_title": "",
            "news_link": "",
            "news_id": "",
            "news_count": 0,
        }

    except Exception as exc:
        return {
            "symbol": symbol,
            "error": str(exc),
        }


# ============================================================
# PERSISTENCE
# ============================================================

def apply_persistence(
    result,
    previous_state,
    now,
):
    symbol = result["symbol"]
    previous = previous_state.get(
        symbol,
        {},
    )

    old_score = safe_float(
        previous.get(
            "score",
            0,
        ),
        0.0,
    )

    current_score = safe_float(
        result.get(
            "score",
            0,
        ),
        0.0,
    )

    old_streak = int(
        safe_float(
            previous.get(
                "streak",
                0,
            ),
            0.0,
        )
    )

    old_timestamp = int(
        safe_float(
            previous.get(
                "timestamp",
                0,
            ),
            0.0,
        )
    )

    state_is_fresh = (
        old_timestamp > 0
        and now
        - old_timestamp
        <= MAX_STREAK_GAP_SECONDS
    )

    if (
        current_score >= 65
        and old_score >= 65
        and state_is_fresh
    ):
        streak = (
            old_streak + 1
        )

    elif current_score >= 65:
        streak = 1

    else:
        streak = 0

    strengthening = (
        current_score
        >= old_score + 5
    )

    confirmed = (
        bool(
            result.get(
                "pre_move_gate"
            )
        )
        and current_score >= 75
        and streak >= 2
    )

    result["previous_score"] = (
        old_score
    )

    result["streak"] = streak
    result["strengthening"] = (
        strengthening
    )

    result["confirmed"] = (
        confirmed
    )

    result["state_fresh"] = (
        state_is_fresh
    )

    return result


# ============================================================
# ALERT MESSAGES
# ============================================================

def build_confirmed_alert(
    result,
):
    news_note = ""

    if (
        result.get(
            "news_sentiment"
        )
        == "positive"
        and result.get(
            "news_score",
            0,
        ) >= 4
    ):
        news_note = (
            "\n📰 News support: +"
            + str(
                result["news_score"]
            )
            + "/12"
        )

    return (
        "🚨 CONFIRMED PRE-MOVE\n\n"
        f"🪙 {result['symbol']}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"⭐ Score: {result['score']}/100\n"
        f"🔥 Streak: {result['streak']}\n\n"
        f"📊 Volume: {result['volume_ratio']:.2f}x\n"
        f"⚡ Volume acceleration: {result['volume_acceleration']:.2f}x\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"🏗 Structure: {result['structure']}/8\n"
        f"🎯 Resistance: {result['resistance']:.2f}%\n"
        f"🌊 Order flow: {result['order_flow']:.2f}\n"
        f"15m: {result['momentum_15m']:+.2f}%\n"
        f"1H: {result['momentum_1h']:+.2f}%\n"
        f"4H: {result['momentum_4h']:+.2f}%\n"
        f"8H: {result['momentum_8h']:+.2f}%"
        f"{news_note}\n\n"
        "🟢 شرایط قبل از حرکت صعودی تأیید شده."
    )


def build_fast_alert(
    result,
):
    return (
        "⚡ FAST PRE-MOVE\n\n"
        f"🪙 {result['symbol']}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"⭐ Score: {result['score']}/100\n"
        f"⚡ 15m Volume: {result['volume_ratio_15m']:.2f}x\n"
        f"🚀 15m Volume acceleration: {result['volume_acceleration_15m']:.2f}x\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"🏗 Structure: {result['structure']}/8\n"
        f"🎯 Resistance: {result['resistance']:.2f}%\n"
        f"🌊 Order flow: {result['order_flow']:.2f}\n"
        f"15m: {result['momentum_15m']:+.2f}%\n"
        f"1H: {result['momentum_1h']:+.2f}%\n"
        f"4H: {result['momentum_4h']:+.2f}%\n\n"
        "🟡 هشدار زودهنگام است؛ هنوز تأیید کامل PRE-MOVE نیست."
    )


def build_news_alert(
    result,
):
    link_line = ""

    if result.get(
        "news_link"
    ):
        link_line = (
            "\n🔗 "
            + str(
                result["news_link"]
            )
        )

    return (
        "📰 NEWS CATALYST\n\n"
        f"🪙 {result['symbol']}\n"
        f"⭐ Technical score: {result['score']}/100\n"
        f"📰 News score: +{result['news_score']}/12\n"
        f"⏱ Age: {result['news_age_minutes']:.0f} min\n"
        f"📡 Source: {result['news_source']}\n"
        f"📚 Matching headlines: {result.get('news_count', 1)}\n\n"
        f"📝 {result['news_title']}"
        f"{link_line}\n\n"
        "🟡 خبر تازه ممکن است محرک حرکت باشد؛ "
        "این پیام به‌تنهایی تأیید تکنیکال نیست."
    )


# ============================================================
# ALERT STATE
# ============================================================

def prune_alert_state(
    alert_state,
    now,
):
    if not isinstance(
        alert_state,
        dict,
    ):
        return {}

    cleaned = {}

    for symbol, entry in alert_state.items():
        if not isinstance(
            entry,
            dict,
        ):
            continue

        timestamps = [
            int(
                safe_float(
                    entry.get(
                        "timestamp",
                        0,
                    ),
                    0,
                )
            ),
            int(
                safe_float(
                    entry.get(
                        "fast_timestamp",
                        0,
                    ),
                    0,
                )
            ),
            int(
                safe_float(
                    entry.get(
                        "news_timestamp",
                        0,
                    ),
                    0,
                )
            ),
        ]

        latest = max(
            timestamps
        )

        if latest <= 0:
            continue

        if (
            now - latest
            <= ALERT_STATE_TTL_SECONDS
        ):
            cleaned[symbol] = entry

    return cleaned


def smart_alert(
    result,
    alert_state,
    alerts_enabled,
):
    if not alerts_enabled:
        return False

    if not result.get(
        "confirmed"
    ):
        return False

    if result.get(
        "score",
        0,
    ) < 80:
        return False

    if result.get(
        "streak",
        0,
    ) < 2:
        return False

    if result.get(
        "order_flow",
        0,
    ) < 1.05:
        return False

    # Fresh negative news blocks a bullish confirmation.
    if (
        result.get(
            "news_sentiment"
        )
        == "negative"
        and result.get(
            "news_age_minutes",
            999999,
        ) * 60
        <= NEWS_FRESH_SECONDS
    ):
        return False

    symbol = result["symbol"]
    old = alert_state.get(
        symbol,
        {},
    )

    old_score = safe_float(
        old.get(
            "score",
            0,
        ),
        0.0,
    )

    old_streak = int(
        safe_float(
            old.get(
                "streak",
                0,
            ),
            0.0,
        )
    )

    old_timestamp = int(
        safe_float(
            old.get(
                "timestamp",
                0,
            ),
            0.0,
        )
    )

    now = int(time.time())

    within_cooldown = (
        old_timestamp > 0
        and now
        - old_timestamp
        < CONFIRMED_ALERT_COOLDOWN_SECONDS
    )

    if (
        within_cooldown
        and old_score >= 80
        and old_streak >= 2
        and result["score"]
        < old_score + 5
    ):
        return False

    if not telegram_send(
        build_confirmed_alert(
            result
        )
    ):
        return False

    entry = {
        "score": result[
            "score"
        ],
        "streak": result[
            "streak"
        ],
        "timestamp": now,
        "price": safe_float(
            result.get(
                "price",
                0,
            ),
            0.0,
        ),
        "alert_type": "CONFIRMED",
    }

    for key in (
        "fast_score",
        "fast_timestamp",
        "fast_price",
        "fast_alert_type",
        "news_score",
        "news_timestamp",
        "news_price",
        "news_alert_type",
        "news_id",
        "news_source",
        "news_title",
        "news_link",
    ):
        if key in old:
            entry[key] = old[key]

    alert_state[symbol] = entry
    return True


def fast_alert(
    result,
    alert_state,
    alerts_enabled,
):
    if not alerts_enabled:
        return False

    if not result.get(
        "fast_pre_move"
    ):
        return False

    if result.get(
        "high_risk_jump"
    ):
        return False

    # Fresh negative news blocks a bullish FAST alert.
    if (
        result.get(
            "news_sentiment"
        )
        == "negative"
        and result.get(
            "news_age_minutes",
            999999,
        ) * 60
        <= NEWS_FRESH_SECONDS
    ):
        return False

    symbol = result["symbol"]
    old = alert_state.get(
        symbol,
        {},
    )

    old_fast_score = safe_float(
        old.get(
            "fast_score",
            0,
        ),
        0.0,
    )

    old_fast_timestamp = int(
        safe_float(
            old.get(
                "fast_timestamp",
                0,
            ),
            0.0,
        )
    )

    now = int(time.time())

    if (
        old_fast_timestamp > 0
        and now
        - old_fast_timestamp
        < 7200
        and result["score"]
        < old_fast_score + 5
    ):
        return False

    if not telegram_send(
        build_fast_alert(
            result
        )
    ):
        return False

    current = alert_state.get(
        symbol,
        {},
    )

    current["fast_score"] = (
        result["score"]
    )

    current["fast_timestamp"] = now

    current["fast_price"] = safe_float(
        result.get(
            "price",
            0,
        ),
        0.0,
    )

    current["fast_alert_type"] = (
        "FAST"
    )

    alert_state[symbol] = current
    return True


def news_alert(
    result,
    alert_state,
    alerts_enabled,
):
    if not alerts_enabled:
        return False

    if result.get(
        "news_sentiment"
    ) != "positive":
        return False

    if result.get(
        "news_score",
        0,
    ) < NEWS_MIN_SCORE:
        return False

    if (
        result.get(
            "news_age_minutes",
            999999,
        ) * 60
        > NEWS_FRESH_SECONDS
    ):
        return False

    # Do not label an already accelerated move as early news.
    if result.get(
        "high_risk_jump"
    ):
        return False

    # Require at least some technical stability.
    if result.get(
        "score",
        0,
    ) < 50:
        return False

    symbol = result["symbol"]
    old = alert_state.get(
        symbol,
        {},
    )

    news_id = str(
        result.get(
            "news_id",
            "",
        )
        or ""
    )

    old_news_id = str(
        old.get(
            "news_id",
            "",
        )
        or ""
    )

    if (
        news_id
        and news_id == old_news_id
    ):
        return False

    old_news_score = safe_float(
        old.get(
            "news_score",
            0,
        ),
        0.0,
    )

    old_news_timestamp = int(
        safe_float(
            old.get(
                "news_timestamp",
                0,
            ),
            0.0,
        )
    )

    now = int(time.time())

    if (
        old_news_timestamp > 0
        and now
        - old_news_timestamp
        < NEWS_ALERT_COOLDOWN_SECONDS
        and result["news_score"]
        < old_news_score + 3
    ):
        return False

    if not telegram_send(
        build_news_alert(
            result
        )
    ):
        return False

    current = alert_state.get(
        symbol,
        {},
    )

    current["news_score"] = (
        result["news_score"]
    )

    current["news_timestamp"] = now

    current["news_price"] = safe_float(
        result.get(
            "price",
            0,
        ),
        0.0,
    )

    current["news_alert_type"] = (
        "NEWS"
    )

    current["news_id"] = news_id

    current["news_source"] = (
        result.get(
            "news_source",
            "",
        )
    )

    current["news_title"] = (
        result.get(
            "news_title",
            "",
        )
    )

    current["news_link"] = (
        result.get(
            "news_link",
            "",
        )
    )

    alert_state[symbol] = current
    return True


# ============================================================
# SCAN
# ============================================================

def run_scan():
    print("=" * 70)
    print("NOBITEX EARLY MOVE RADAR - ONE-TIME CLEAN FINAL")
    print(time.strftime(
        "%Y-%m-%d %H:%M:%S"
    ))
    print("=" * 70)

    now = int(time.time())

    ensure_tracking_start()

    previous_state = load_json(
        STATE_PATH,
        {},
    )

    alert_state = load_json(
        ALERT_STATE_PATH,
        {},
    )

    alert_state = prune_alert_state(
        alert_state,
        now,
    )

    try:
        books = get_market_snapshot()

    except Exception as exc:
        print(
            "MARKET SNAPSHOT FAILED:",
            exc,
        )
        return

    symbols = sorted(
        books.keys()
    )

    print(
        "USDT markets:",
        len(symbols),
    )

    results = []
    failed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
                books[symbol],
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            futures
        ):
            symbol = futures[
                future
            ]

            try:
                result = future.result()

                if "error" in result:
                    failed += 1
                    print(
                        symbol,
                        "FAILED:",
                        result[
                            "error"
                        ],
                    )
                    continue

                result = apply_persistence(
                    result,
                    previous_state,
                    now,
                )

                results.append(
                    result
                )

            except Exception as exc:
                failed += 1
                print(
                    symbol,
                    "FAILED:",
                    exc,
                )

    # News is fetched once per full scan.
    news_items = fetch_news_items()

    for result in results:
        apply_news_to_result(
            result,
            news_items,
        )

    coverage = (
        len(results)
        / len(symbols)
        if symbols
        else 0.0
    )

    coverage_ok = (
        coverage
        >= MIN_COVERAGE_FOR_ALERTS
    )

    new_state = {}

    for result in results:
        new_state[
            result["symbol"]
        ] = {
            "score": result[
                "score"
            ],
            "streak": result[
                "streak"
            ],
            "timestamp": now,
        }

    # Failed markets keep prior state.
    for symbol, old in (
        previous_state.items()
    ):
        if (
            symbol not in new_state
            and isinstance(
                old,
                dict,
            )
        ):
            new_state[
                symbol
            ] = old

    save_json(
        STATE_PATH,
        new_state,
    )

    candidates = [
        result
        for result in results
        if (
            result.get(
                "pre_move_gate"
            )
            and result.get(
                "quality"
            )
            and result.get(
                "score",
                0,
            ) >= 65
        )
    ]

    candidates.sort(
        key=lambda result: (
            result.get(
                "score",
                0,
            ),
            1
            if result.get(
                "news_score",
                0,
            ) > 0
            else 0,
            1
            if result.get(
                "strengthening"
            )
            else 0,
            result.get(
                "streak",
                0,
            ),
            result.get(
                "order_flow",
                0,
            ),
            result.get(
                "structure",
                0,
            ),
            result.get(
                "volume_ratio",
                0,
            ),
        ),
        reverse=True,
    )

    print()
    print(
        f"Valid: {len(results)} | "
        f"Failed: {failed} | "
        f"Coverage: {coverage * 100:.1f}% | "
        f"Alerts enabled: {coverage_ok}"
    )

    if not coverage_ok:
        print(
            "ALERTS SUPPRESSED: "
            f"coverage below "
            f"{MIN_COVERAGE_FOR_ALERTS * 100:.0f}%"
        )

    print()
    print(
        "TOP PRE-MOVE CANDIDATES"
    )
    print(
        "-" * 70
    )

    if not candidates:
        print(
            "No valid pre-move candidates."
        )

    for index, result in enumerate(
        candidates[:TOP_N],
        1,
    ):
        news_mark = ""

        if (
            result.get(
                "news_sentiment"
            )
            == "positive"
            and result.get(
                "news_score",
                0,
            ) >= 4
        ):
            news_mark = (
                f" | News +{result['news_score']}"
            )

        print(
            f"{index}. "
            f"{result['symbol']} | "
            f"{result['label']} | "
            f"Score {result['score']} | "
            f"Streak {result['streak']} | "
            f"Vol {result['volume_ratio']:.2f}x | "
            f"Flow {result['order_flow']:.2f} | "
            f"RSI {result['rsi']:.1f} | "
            f"Res {result['resistance']:.2f}%"
            f"{news_mark}"
        )

    news_alert_count = 0
    fast_alert_count = 0
    confirmed_alert_count = 0

    # NEWS first because it can arrive before technical confirmation.
    for result in results:
        if news_alert(
            result,
            alert_state,
            coverage_ok,
        ):
            news_alert_count += 1

        if fast_alert(
            result,
            alert_state,
            coverage_ok,
        ):
            fast_alert_count += 1

        if smart_alert(
            result,
            alert_state,
            coverage_ok,
        ):
            confirmed_alert_count += 1

    save_json(
        ALERT_STATE_PATH,
        alert_state,
    )

    watchlist = [
        result
        for result in results
        if result.get(
            "score",
            0,
        ) >= 55
    ]

    watchlist.sort(
        key=lambda result: (
            result.get(
                "score",
                0,
            ),
            result.get(
                "news_score",
                0,
            ),
        ),
        reverse=True,
    )

    print()
    print("WATCHLIST")
    print("-" * 70)

    for result in watchlist[:15]:
        fast_mark = ""

        if (
            result.get(
                "fast_pre_move"
            )
            and not result.get(
                "high_risk_jump"
            )
        ):
            fast_mark = " ⚡FAST"

        news_mark = ""

        if (
            result.get(
                "news_sentiment"
            )
            == "positive"
            and result.get(
                "news_score",
                0,
            ) >= 4
        ):
            news_mark = (
                f" 📰NEWS+{result['news_score']}"
            )

        print(
            f"{result['symbol']:12} "
            f"{result['label']:15} "
            f"{result['score']:3}/100 "
            f"streak={result['streak']} "
            f"vol={result['volume_ratio']:.2f}x "
            f"15mVol={result['volume_ratio_15m']:.2f}x "
            f"flow={result['order_flow']:.2f}"
            f"{fast_mark}{news_mark}"
        )

    print()
    print(
        "News Telegram alerts sent:",
        news_alert_count,
    )

    print(
        "FAST Telegram alerts sent:",
        fast_alert_count,
    )

    print(
        "Confirmed Telegram alerts sent:",
        confirmed_alert_count,
    )

    print(
        "SCAN FINISHED."
    )


def main():
    if TELEGRAM_TEST_ON_START:
        telegram_test()

    run_scan()


if __name__ == "__main__":
    main()
