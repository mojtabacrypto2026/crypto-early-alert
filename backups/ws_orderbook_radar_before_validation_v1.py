# -*- coding: utf-8 -*-
"""
NOBITEX WEBSOCKET OBI RADAR

Separate early-warning layer for the existing Crypto Early Alert system.

Adds:
- Public Nobitex WebSocket orderbook stream.
- Multi-level order-book imbalance at 1/3/5/10/20 levels.
- Distance-weighted imbalance, microprice gap and spread.
- 60-second pressure-change detection.
- Separate Telegram WS OBI early warning.
- Bounded JSONL raw samples for later labeling/model training.
- REST seed before WebSocket subscription.

The existing scanner.py scoring is intentionally untouched.
"""

import argparse
import json
import math
import os
import signal
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

try:
    import websocket
except Exception:
    websocket = None

BASE_URL = "https://apiv2.nobitex.ir"
WS_URL = "wss://ws.nobitex.ir/connection/websocket"

MAX_WS_CHANNELS = 440
DEFAULT_SECONDS = 210
BOOK_LEVELS = (1, 3, 5, 10, 20)
HTTP_TIMEOUT = 15
HTTP_RETRIES = 3

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")

RADAR_STATE_PATH = os.path.join(WORKSPACE, "nobitex_early_radar_state.json")
WS_SIGNAL_STATE_PATH = os.path.join(WORKSPACE, "nobitex_ws_orderbook_signal.json")
WS_ALERT_STATE_PATH = os.path.join(WORKSPACE, "nobitex_ws_obi_alert_state.json")
WS_RAW_PATH = os.path.join(WORKSPACE, "nobitex_ws_raw_samples.jsonl")

# New signal thresholds. These do not alter the main scanner score.
OBI_ALERT_WEIGHTED_MIN = 0.24
OBI_ALERT_LEVEL5_MIN = 0.20
OBI_ALERT_DELTA_MIN = 0.10
OBI_ALERT_MICROPRICE_GAP_BPS_MIN = 1.2
OBI_ALERT_MAX_SPREAD_BPS = 45.0
OBI_ALERT_MAX_PRICE_CHANGE_60S = 0.80
OBI_ALERT_MIN_UPDATES = 3

OBI_PRIOR_SCORE_MIN = 55
OBI_STRONG_WEIGHTED_MIN = 0.42
OBI_STRONG_LEVEL5_MIN = 0.35

ALERT_COOLDOWN_SECONDS = 2 * 60 * 60
ALERT_IMPROVEMENT_DELTA = 0.10
BASELINE_SECONDS = 60
MAX_HISTORY_PER_SYMBOL = 120

RAW_MAX_LINES = 3000
RAW_MAX_BYTES = 5 * 1024 * 1024
RAW_SAMPLE_INTERVAL_SECONDS = 5
RAW_CAPTURE_WEIGHTED_MIN = 0.15
RAW_CAPTURE_DELTA_MIN = 0.07

EXCLUDED_SYMBOLS = {
    "USDCUSDT",
    "USDEUSDT",
    "DAIUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
    "WBTCUSDT",
}


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print("JSON LOAD ERROR:", path, exc)
        return default


def save_json(path, data):
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        return True
    except Exception as exc:
        print("JSON SAVE ERROR:", path, exc)
        return False


def http_get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nobitex-WS-OBI-Radar/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )

    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
    raise last_error


def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram disabled: secrets are not configured.")
        return False

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
        data=payload,
        headers={
            "User-Agent": "Nobitex-WS-OBI-Radar/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            print("WS OBI Telegram message sent.")
            return True
        print("WS OBI Telegram error:", result)
        return False
    except Exception as exc:
        print("WS OBI Telegram exception:", exc)
        return False


def normalize_side(rows):
    clean = []
    if not isinstance(rows, list):
        return clean

    for row in rows[:50]:
        try:
            if len(row) < 2:
                continue
            price = safe_float(row[0])
            quantity = safe_float(row[1])
            if price <= 0 or quantity <= 0:
                continue
            clean.append([price, quantity])
        except Exception:
            continue
    return clean


def get_symbols_and_seed_books():
    data = http_get_json(BASE_URL + "/v3/orderbook/all")
    if not isinstance(data, dict):
        raise ValueError("INVALID_ORDERBOOK_ALL_RESPONSE")

    books = {}
    for raw_symbol, raw_book in data.items():
        symbol = str(raw_symbol).upper()
        if symbol in EXCLUDED_SYMBOLS or not symbol.endswith("USDT"):
            continue
        if not isinstance(raw_book, dict):
            continue

        bids = normalize_side(raw_book.get("bids", []))
        asks = normalize_side(raw_book.get("asks", []))
        if not bids or not asks:
            continue

        books[symbol] = {
            "bids": bids,
            "asks": asks,
            "last_trade_price": safe_float(raw_book.get("lastTradePrice")),
            "last_update": safe_float(raw_book.get("lastUpdate")),
            "seeded_at": time.time(),
        }
    return books


def notional_depth(side, levels):
    return sum(price * quantity for price, quantity in side[:levels])


def level_imbalance(bids, asks, levels):
    bid_depth = notional_depth(bids, levels)
    ask_depth = notional_depth(asks, levels)
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def weighted_imbalance(bids, asks, max_levels=20):
    bid_weighted = 0.0
    ask_weighted = 0.0

    for index, (price, quantity) in enumerate(bids[:max_levels]):
        weight = 1.0 / math.sqrt(index + 1.0)
        bid_weighted += price * quantity * weight

    for index, (price, quantity) in enumerate(asks[:max_levels]):
        weight = 1.0 / math.sqrt(index + 1.0)
        ask_weighted += price * quantity * weight

    total = bid_weighted + ask_weighted
    if total <= 0:
        return 0.0
    return (bid_weighted - ask_weighted) / total


def microprice_and_gap(bids, asks):
    if not bids or not asks:
        return 0.0, 0.0

    best_bid, bid_qty = bids[0]
    best_ask, ask_qty = asks[0]
    if best_bid <= 0 or best_ask <= 0 or bid_qty <= 0 or ask_qty <= 0:
        return 0.0, 0.0

    midpoint = (best_bid + best_ask) / 2.0
    microprice = ((best_ask * bid_qty) + (best_bid * ask_qty)) / (bid_qty + ask_qty)
    if midpoint <= 0:
        return microprice, 0.0

    gap_bps = (microprice - midpoint) / midpoint * 10000.0
    return microprice, gap_bps


def spread_bps(bids, asks):
    if not bids or not asks:
        return 999.0
    bid = bids[0][0]
    ask = asks[0][0]
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return 999.0
    return (ask - bid) / midpoint * 10000.0


def compute_features(book):
    bids = normalize_side(book.get("bids", []))
    asks = normalize_side(book.get("asks", []))

    features = {
        "obi_1": level_imbalance(bids, asks, 1),
        "obi_3": level_imbalance(bids, asks, 3),
        "obi_5": level_imbalance(bids, asks, 5),
        "obi_10": level_imbalance(bids, asks, 10),
        "obi_20": level_imbalance(bids, asks, 20),
        "obi_weighted": weighted_imbalance(bids, asks, 20),
        "spread_bps": spread_bps(bids, asks),
        "microprice": 0.0,
        "microprice_gap_bps": 0.0,
        "mid": 0.0,
        "last_trade_price": safe_float(book.get("last_trade_price")),
    }

    if bids and asks:
        features["mid"] = (bids[0][0] + asks[0][0]) / 2.0

    features["microprice"], features["microprice_gap_bps"] = microprice_and_gap(bids, asks)
    return features


def load_prior_scores():
    state = load_json(RADAR_STATE_PATH, {})
    if not isinstance(state, dict):
        return {}

    scores = {}
    for symbol, row in state.items():
        if isinstance(row, dict):
            score = safe_float(row.get("score"))
            if score > 0:
                scores[str(symbol).upper()] = score
    return scores


def load_alert_state():
    state = load_json(WS_ALERT_STATE_PATH, {})
    return state if isinstance(state, dict) else {}


def append_raw_samples(new_rows):
    if not new_rows:
        return False

    existing = []
    if os.path.exists(WS_RAW_PATH):
        try:
            with open(WS_RAW_PATH, "r", encoding="utf-8") as handle:
                existing = handle.readlines()
        except Exception as exc:
            print("RAW LOG READ ERROR:", exc)

    serialized = [
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in new_rows
    ]
    combined = existing + serialized

    if len(combined) > RAW_MAX_LINES:
        combined = combined[-RAW_MAX_LINES:]

    if len("".join(combined).encode("utf-8")) > RAW_MAX_BYTES:
        trimmed = []
        total = 0
        for line in reversed(combined):
            size = len(line.encode("utf-8"))
            if total + size > RAW_MAX_BYTES:
                break
            trimmed.append(line)
            total += size
        combined = list(reversed(trimmed))

    try:
        directory = os.path.dirname(WS_RAW_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(WS_RAW_PATH, "w", encoding="utf-8") as handle:
            handle.writelines(combined)
        print("Raw WS samples saved:", len(new_rows), "| total lines:", len(combined))
        return True
    except Exception as exc:
        print("RAW LOG SAVE ERROR:", exc)
        return False


def build_alert(symbol, features, info, prior_score):
    return (
        "⚡ WS OBI EARLY WARNING\n\n"
        f"🪙 {symbol}\n"
        f"💰 Price: {features['mid']:.8g}\n"
        f"🌊 OBI 1/3/5/10/20: "
        f"{features['obi_1']:+.2f}/"
        f"{features['obi_3']:+.2f}/"
        f"{features['obi_5']:+.2f}/"
        f"{features['obi_10']:+.2f}/"
        f"{features['obi_20']:+.2f}\n"
        f"🧠 Weighted OBI: {features['obi_weighted']:+.2f} "
        f"(Δ {info['obi_delta']:+.2f})\n"
        f"🎯 Microprice gap: {features['microprice_gap_bps']:+.1f} bps\n"
        f"📏 Spread: {features['spread_bps']:.1f} bps\n"
        f"📈 Price/60s: {info['price_change_60s']:+.2f}%\n"
        f"⭐ Previous technical score: {prior_score:.0f}\n"
        f"🔄 WS updates: {info['updates']}\n\n"
        "🟠 فشار خرید در چند سطح اردربوک همزمان قوی‌تر شده در حالی که قیمت هنوز جهش نکرده است. "
        "این هشدار زودهنگام مستقل از امتیاز اصلی است و تأیید جهش نیست."
    )


def should_alert(symbol, features, info, prior_score, alert_state):
    if info["updates"] < OBI_ALERT_MIN_UPDATES:
        return False
    if features["spread_bps"] > OBI_ALERT_MAX_SPREAD_BPS:
        return False
    if info["price_change_60s"] > OBI_ALERT_MAX_PRICE_CHANGE_60S:
        return False

    normal = (
        features["obi_weighted"] >= OBI_ALERT_WEIGHTED_MIN
        and features["obi_5"] >= OBI_ALERT_LEVEL5_MIN
        and info["obi_delta"] >= OBI_ALERT_DELTA_MIN
        and features["microprice_gap_bps"] >= OBI_ALERT_MICROPRICE_GAP_BPS_MIN
    )

    strong = (
        features["obi_weighted"] >= OBI_STRONG_WEIGHTED_MIN
        and features["obi_5"] >= OBI_STRONG_LEVEL5_MIN
        and info["obi_delta"] >= 0.06
        and features["microprice_gap_bps"] >= 0.8
    )

    if prior_score >= OBI_PRIOR_SCORE_MIN:
        qualified = normal
    else:
        qualified = strong

    if not qualified:
        return False

    old = alert_state.get(symbol, {})
    last_timestamp = int(safe_float(old.get("timestamp")))
    old_weighted = safe_float(old.get("weighted_obi"))
    now = int(time.time())

    if (
        last_timestamp > 0
        and now - last_timestamp < ALERT_COOLDOWN_SECONDS
        and features["obi_weighted"] < old_weighted + ALERT_IMPROVEMENT_DELTA
    ):
        return False

    return True


class NobitexOBIRadar:
    def __init__(self, duration_seconds):
        self.duration_seconds = max(30, int(duration_seconds))
        self.books = {}
        self.history = {}
        self.last_raw_sample_at = {}
        self.alert_state = load_alert_state()
        self.prior_scores = load_prior_scores()
        self.new_raw_rows = []
        self.latest_signals = {}
        self.stop_event = threading.Event()
        self.ws = None
        self.lock = threading.RLock()
        self.alert_count = 0
        self.update_count = 0
        self.error_count = 0

    def seed(self):
        print("Seeding orderbooks from REST...")
        self.books = get_symbols_and_seed_books()
        print("Seeded markets:", len(self.books))

        for symbol, book in self.books.items():
            features = compute_features(book)
            self.history[symbol] = deque(
                [{"ts": time.time(), **features}],
                maxlen=MAX_HISTORY_PER_SYMBOL,
            )
            self.latest_signals[symbol] = {
                "symbol": symbol,
                "seeded": True,
                **features,
                "obi_delta_60s": 0.0,
                "price_change_60s": 0.0,
                "updates": 0,
                "prior_score": self.prior_scores.get(symbol, 0.0),
            }

    def channel_list(self):
        symbols = sorted(self.books.keys())[:MAX_WS_CHANNELS]
        return ["public:orderbook-" + symbol for symbol in symbols]

    def connect(self):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")

        print("Connecting WebSocket:", WS_URL)
        self.ws = websocket.create_connection(
            WS_URL,
            timeout=10,
            enableTrace=False,
        )
        self.ws.settimeout(10)

        self.ws.send(json.dumps({"connect": {}, "id": 1}))

        request_id = 10
        for channel in self.channel_list():
            if self.stop_event.is_set():
                break
            self.ws.send(json.dumps({
                "id": request_id,
                "subscribe": {"channel": channel},
            }))
            request_id += 1

        print("WS subscriptions sent:", request_id - 10)

    def parse_message(self, raw):
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        text = str(raw).strip()
        if text == "{}":
            return {"type": "ping"}

        try:
            payload = json.loads(text)
        except Exception:
            return None

        if not isinstance(payload, dict):
            return None

        push = payload.get("push")
        if not isinstance(push, dict):
            return {"type": "other", "payload": payload}

        pub = push.get("pub")
        if not isinstance(pub, dict):
            return {"type": "other", "payload": payload}

        channel = str(push.get("channel", ""))
        data = pub.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return None
        if not isinstance(data, dict):
            return None

        return {"type": "publication", "channel": channel, "data": data}

    def handle_publication(self, channel, data):
        prefix = "public:orderbook-"
        if not channel.startswith(prefix):
            return

        symbol = channel[len(prefix):].upper()
        if symbol not in self.books:
            return

        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])
        bids = normalize_side(raw_bids)
        asks = normalize_side(raw_asks)
        if not bids or not asks:
            return

        book = {
            "bids": bids,
            "asks": asks,
            "last_trade_price": safe_float(data.get("lastTradePrice")),
            "last_update": safe_float(data.get("lastUpdate")),
        }

        features = compute_features(book)
        now = time.time()

        with self.lock:
            self.books[symbol] = book
            if symbol not in self.history:
                self.history[symbol] = deque(maxlen=MAX_HISTORY_PER_SYMBOL)

            history = self.history[symbol]
            recent = [
                item for item in history
                if now - safe_float(item.get("ts"), now) <= BASELINE_SECONDS
            ]

            previous_obi = (
                statistics.median(
                    [safe_float(item.get("obi_weighted")) for item in recent]
                )
                if recent else features["obi_weighted"]
            )

            price_reference = features["mid"]
            old_price_candidates = [
                safe_float(item.get("mid"))
                for item in recent
                if safe_float(item.get("mid")) > 0
            ]
            if old_price_candidates:
                price_reference = old_price_candidates[0]

            history.append({"ts": now, **features})
            self.update_count += 1
            updates = len(history)

            one_minute_candidates = [
                item for item in history
                if now - safe_float(item.get("ts"), now) >= BASELINE_SECONDS
            ]

            if one_minute_candidates:
                reference = one_minute_candidates[-1]
                reference_obi = safe_float(reference.get("obi_weighted"), previous_obi)
                reference_price = safe_float(reference.get("mid"), price_reference)
            else:
                reference_obi = previous_obi
                reference_price = price_reference

            price_change_60s = 0.0
            if reference_price > 0:
                price_change_60s = (
                    (features["mid"] - reference_price) / reference_price * 100.0
                )

            obi_delta = features["obi_weighted"] - reference_obi
            info = {
                "obi_delta": obi_delta,
                "price_change_60s": price_change_60s,
                "updates": updates,
            }

            prior_score = self.prior_scores.get(symbol, 0.0)
            self.latest_signals[symbol] = {
                "symbol": symbol,
                "timestamp": int(now),
                "seeded": False,
                **features,
                "obi_delta_60s": obi_delta,
                "price_change_60s": price_change_60s,
                "updates": updates,
                "prior_score": prior_score,
            }

            raw_due = (
                now - safe_float(self.last_raw_sample_at.get(symbol, 0.0))
                >= RAW_SAMPLE_INTERVAL_SECONDS
            )
            interesting = (
                features["obi_weighted"] >= RAW_CAPTURE_WEIGHTED_MIN
                or obi_delta >= RAW_CAPTURE_DELTA_MIN
                or prior_score >= 65
            )

            if raw_due and interesting:
                self.last_raw_sample_at[symbol] = now
                self.new_raw_rows.append({
                    "captured_at": int(now),
                    "captured_at_iso": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                    ),
                    "symbol": symbol,
                    "source": "nobitex_public_orderbook_websocket",
                    "prior_score": prior_score,
                    "last_trade_price": features["last_trade_price"],
                    "mid": features["mid"],
                    "spread_bps": features["spread_bps"],
                    "microprice": features["microprice"],
                    "microprice_gap_bps": features["microprice_gap_bps"],
                    "obi_1": features["obi_1"],
                    "obi_3": features["obi_3"],
                    "obi_5": features["obi_5"],
                    "obi_10": features["obi_10"],
                    "obi_20": features["obi_20"],
                    "obi_weighted": features["obi_weighted"],
                    "obi_delta_60s": obi_delta,
                    "price_change_60s": price_change_60s,
                    "updates": updates,
                    "raw_bids": raw_bids[:20],
                    "raw_asks": raw_asks[:20],
                    "raw_last_trade_price": data.get("lastTradePrice"),
                    "raw_last_update": data.get("lastUpdate"),
                })

            if should_alert(symbol, features, info, prior_score, self.alert_state):
                message = build_alert(symbol, features, info, prior_score)
                if telegram_send(message):
                    self.alert_count += 1
                    self.alert_state[symbol] = {
                        "timestamp": int(now),
                        "symbol": symbol,
                        "price": features["mid"],
                        "weighted_obi": features["obi_weighted"],
                        "obi_5": features["obi_5"],
                        "obi_20": features["obi_20"],
                        "obi_delta_60s": obi_delta,
                        "microprice_gap_bps": features["microprice_gap_bps"],
                        "spread_bps": features["spread_bps"],
                        "price_change_60s": price_change_60s,
                        "prior_score": prior_score,
                        "alert_type": "WS_OBI",
                        "trigger_version": "ws-obi-v1",
                    }

    def read_loop(self):
        deadline = time.time() + self.duration_seconds
        while not self.stop_event.is_set() and time.time() < deadline:
            try:
                raw = self.ws.recv()
            except Exception:
                if time.time() >= deadline:
                    break
                continue

            parsed = self.parse_message(raw)
            if not parsed:
                continue

            if parsed["type"] == "ping":
                try:
                    self.ws.send("{}")
                except Exception:
                    pass
                continue

            if parsed["type"] != "publication":
                continue

            try:
                self.handle_publication(parsed["channel"], parsed["data"])
            except Exception as exc:
                self.error_count += 1
                print("WS PUBLICATION ERROR:", exc)

    def close(self):
        self.stop_event.set()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

    def finalize(self):
        now = int(time.time())
        snapshot = {
            "schema_version": 1,
            "updated_at": now,
            "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "source": "nobitex_public_orderbook_websocket",
            "ws_url": WS_URL,
            "markets_seeded": len(self.books),
            "ws_updates": self.update_count,
            "telegram_alerts_sent": self.alert_count,
            "errors": self.error_count,
            "symbols": self.latest_signals,
        }

        save_json(WS_SIGNAL_STATE_PATH, snapshot)
        save_json(WS_ALERT_STATE_PATH, self.alert_state)
        append_raw_samples(self.new_raw_rows)

        print("=" * 72)
        print("WS OBI RADAR FINISHED")
        print("Seeded markets:", len(self.books))
        print("WS updates:", self.update_count)
        print("Telegram alerts:", self.alert_count)
        print("Raw samples this run:", len(self.new_raw_rows))
        print("Errors:", self.error_count)
        print("=" * 72)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Nobitex WebSocket multi-level OBI early radar"
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=DEFAULT_SECONDS,
        help="WebSocket collection duration",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if websocket is None:
        print("ERROR: websocket-client is not installed.")
        print("Install with: python -m pip install websocket-client")
        return 2

    radar = NobitexOBIRadar(args.seconds)

    def handle_signal(signum, frame):
        print("Stop signal received:", signum)
        radar.close()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        radar.seed()
        if not radar.books:
            raise RuntimeError("NO_USDT_MARKETS_AVAILABLE")
        radar.connect()
        radar.read_loop()
    except Exception as exc:
        print("WS OBI RADAR ERROR:", repr(exc))
        radar.error_count += 1
    finally:
        radar.close()
        radar.finalize()

    # A transient WS failure must not disable the primary scanner.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
