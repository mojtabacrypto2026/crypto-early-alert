# -*- coding: utf-8 -*-
"""
NOBITEX STREAMING EARLY RADAR V1

Purpose
-------
A persistent, dependency-free WebSocket radar that complements the existing
scanner.py instead of replacing it.

Design
------
1) Reads the latest technical score/streak produced by scanner.py.
2) Gets one initial full order-book snapshot via /v3/orderbook/all.
3) Subscribes to public order-book streams and public 5-minute candle streams.
4) Computes live order-book pressure, pressure persistence, spread, short price
   movement and 5m volume acceleration.
5) Emits two early-warning levels:
   - STREAM BUILDUP: pressure is building while price is still quiet.
   - STREAM MOMENTUM: early price expansion with confirming order-flow pressure.
6) Sends Telegram alerts with strong cooldowns and a higher-quality persistence
   requirement to reduce spoofing/one-snapshot false positives.
7) Reconnects automatically and answers Nobitex/Centrifugo heartbeats.
8) Supports all current USDT markets by sharding across multiple connections
   when the 300-channel-per-connection limit is reached.

Important
---------
This process is intended to run persistently (for example on Android/Pydroid3,
a VPS, or another always-on host). GitHub Actions remains responsible for the
5-minute core scanner. Do not replace scanner.py with this file.

No third-party Python package is required.
"""

import base64
import collections
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://apiv2.nobitex.ir"
WS_HOST = "wss.nobitex.ir"
WS_PATH = "/connection/websocket"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", ".")

TECH_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_early_radar_state.json",
)
STREAM_STATE_PATH = os.path.join(
    WORKSPACE,
    "nobitex_stream_radar_state.json",
)

EXCLUDED_SYMBOLS = {
    "USDCUSDT",
    "USDEUSDT",
    "DAIUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
    "WBTCUSDT",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

MAX_CHANNELS_PER_CONNECTION = 300
MAX_SUBSCRIBE_ID = 1000000

# The stream must never become noisy enough to hide the useful alerts.
BUILDUP_COOLDOWN_SECONDS = 2 * 60 * 60
MOMENTUM_COOLDOWN_SECONDS = 90 * 60

# Technical gate from the existing scanner.
MIN_TECH_SCORE = 60
MAX_STALE_TECH_SECONDS = 12 * 60

# Order-book pressure.
BUILDUP_FLOW = 1.18
BUILDUP_FLOW_DELTA = 0.08
BUILDUP_SPREAD_BPS = 25.0
BUILDUP_MAX_PRICE_30S = 0.35
BUILDUP_MIN_PERSISTENCE = 3

MOMENTUM_FLOW = 1.10
MOMENTUM_FLOW_DELTA = 0.05
MOMENTUM_SPREAD_BPS = 35.0
MOMENTUM_MIN_PRICE_30S = 0.15
MOMENTUM_MAX_PRICE_30S = 1.20
MOMENTUM_MIN_PERSISTENCE = 3

# Do not alert if the last core scan already says the move is too extended.
MAX_CORE_15M_MOVE = 2.0
MAX_CORE_1H_MOVE = 2.8
MAX_CORE_4H_MOVE = 7.0

# Live sampling windows.
PRESSURE_WINDOW_SECONDS = 60
PRICE_WINDOW_SECONDS = 35
PERSISTENCE_WINDOW_SECONDS = 25
MAX_HISTORY_PER_SYMBOL = 240

# 5m candle volume acceleration.
MIN_CANDLE_VOLUME_ACCEL = 1.20
MAX_CANDLE_VOLUME_ACCEL = 4.50

# Reconnect/backoff.
RECONNECT_MIN_SECONDS = 3
RECONNECT_MAX_SECONDS = 30
SOCKET_TIMEOUT_SECONDS = 35

PRINT_EVERY_SECONDS = 60


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        result = float(value)
        if result == result and result not in (float("inf"), float("-inf")):
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
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def now_ms():
    return int(time.time() * 1000)


def return_percent(old, new):
    if old <= 0 or new <= 0:
        return 0.0
    return ((new - old) / old) * 100.0


def trim_deque(dq, cutoff):
    while dq and dq[0][0] < cutoff:
        dq.popleft()


# ============================================================
# HTTP
# ============================================================

def http_get_json(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    headers = {
        "User-Agent": "Nobitex-Streaming-Radar/1.0",
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
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.2 * (attempt + 1))

    raise last_error


def http_post_json(url, data):
    payload = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "Nobitex-Streaming-Radar/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.2 * (attempt + 1))

    raise last_error


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
            return True
        print("Telegram error:", result)
    except Exception as exc:
        print("Telegram exception:", exc)

    return False


# ============================================================
# TECHNICAL STATE
# ============================================================

TECH_STATE = {}
TECH_STATE_LOCK = threading.Lock()


def reload_technical_state():
    raw = load_json(TECH_STATE_PATH, {})
    parsed = {}

    if not isinstance(raw, dict):
        raw = {}

    for symbol, item in raw.items():
        if not isinstance(item, dict):
            continue
        parsed[str(symbol).upper()] = item

    with TECH_STATE_LOCK:
        TECH_STATE.clear()
        TECH_STATE.update(parsed)

    return len(parsed)


def get_technical(symbol):
    with TECH_STATE_LOCK:
        item = TECH_STATE.get(symbol, {})
        return dict(item) if isinstance(item, dict) else {}


def technical_gate(symbol, now_sec):
    item = get_technical(symbol)
    score = safe_float(item.get("score"), 0.0)
    ts = safe_int(item.get("timestamp"), 0)

    if score < MIN_TECH_SCORE:
        return False, item

    if ts <= 0 or now_sec - ts > MAX_STALE_TECH_SECONDS:
        return False, item

    m15 = safe_float(item.get("momentum_15m"), safe_float(item.get("m15"), 0.0))
    m1h = safe_float(item.get("momentum_1h"), safe_float(item.get("m1h"), 0.0))
    m4h = safe_float(item.get("momentum_4h"), safe_float(item.get("m4h"), 0.0))

    # The current scanner persists price_change_since_scan, not candle m15/m1h/m4h.
    # If those fields are absent we do not invent them; the score alone remains the gate.
    if abs(m15) > 99 or abs(m1h) > 99 or abs(m4h) > 99:
        return False, item

    if m15 > MAX_CORE_15M_MOVE or m1h > MAX_CORE_1H_MOVE or m4h > MAX_CORE_4H_MOVE:
        return False, item

    return True, item


# ============================================================
# ORDERBOOK METRICS
# ============================================================


def parse_orderbook(book):
    bids = book.get("bids", []) if isinstance(book, dict) else []
    asks = book.get("asks", []) if isinstance(book, dict) else []

    bid_value = 0.0
    ask_value = 0.0
    best_bid = 0.0
    best_ask = 0.0

    try:
        if bids:
            best_bid = safe_float(bids[0][0])
        if asks:
            best_ask = safe_float(asks[0][0])

        for row in bids[:20]:
            if len(row) >= 2:
                bid_value += safe_float(row[0]) * safe_float(row[1])

        for row in asks[:20]:
            if len(row) >= 2:
                ask_value += safe_float(row[0]) * safe_float(row[1])
    except Exception:
        pass

    if best_bid > 0 and best_ask > 0:
        price = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / price) * 10000.0
    elif best_bid > 0:
        price = best_bid
        spread_bps = 999.0
    elif best_ask > 0:
        price = best_ask
        spread_bps = 999.0
    else:
        price = 0.0
        spread_bps = 999.0

    flow = bid_value / ask_value if ask_value > 0 else 0.0

    return price, flow, spread_bps


# ============================================================
# STREAM STATE
# ============================================================

class SymbolTelemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.price_history = collections.deque(maxlen=MAX_HISTORY_PER_SYMBOL)
        self.flow_history = collections.deque(maxlen=MAX_HISTORY_PER_SYMBOL)
        self.spread_history = collections.deque(maxlen=MAX_HISTORY_PER_SYMBOL)
        self.candle_volume_history = collections.deque(maxlen=60)
        self.current_candle = None
        self.last_alert = {
            "BUILDUP": 0,
            "MOMENTUM": 0,
        }
        self.counts = {
            "orderbook": 0,
            "candle": 0,
            "alerts": 0,
        }
        self.last_seen = 0

    def add_orderbook(self, ts, price, flow, spread):
        with self.lock:
            self.price_history.append((ts, price))
            self.flow_history.append((ts, flow))
            self.spread_history.append((ts, spread))
            self.last_seen = ts
            self.counts["orderbook"] += 1

    def add_candle(self, candle):
        ts = safe_int(candle.get("t"), 0)
        volume = safe_float(candle.get("v"), 0.0)
        close = safe_float(candle.get("c"), 0.0)

        if ts <= 0:
            return

        with self.lock:
            previous = self.current_candle
            if previous and safe_int(previous.get("t"), 0) == ts:
                previous_volume = safe_float(previous.get("v"), 0.0)
                if volume >= previous_volume:
                    delta = volume - previous_volume
                    if delta > 0:
                        self.candle_volume_history.append((time.time(), delta))
            else:
                self.current_candle = {
                    "t": ts,
                    "v": volume,
                    "c": close,
                }

            self.current_candle = {
                "t": ts,
                "v": volume,
                "c": close,
            }
            self.counts["candle"] += 1

    def snapshot(self, now_ts):
        with self.lock:
            cutoff = now_ts - PRESSURE_WINDOW_SECONDS
            trim_deque(self.price_history, now_ts - PRICE_WINDOW_SECONDS)
            trim_deque(self.flow_history, cutoff)
            trim_deque(self.spread_history, cutoff)
            while self.candle_volume_history and self.candle_volume_history[0][0] < now_ts - PRESSURE_WINDOW_SECONDS:
                self.candle_volume_history.popleft()

            prices = list(self.price_history)
            flows = list(self.flow_history)
            spreads = list(self.spread_history)
            volume_deltas = [x[1] for x in self.candle_volume_history]
            last_alert = dict(self.last_alert)

        return prices, flows, spreads, volume_deltas, last_alert

    def mark_alert(self, alert_type, now_ts):
        with self.lock:
            self.last_alert[alert_type] = now_ts
            self.counts["alerts"] += 1


TELEMETRY = collections.defaultdict(SymbolTelemetry)


def order_flow_delta(flows):
    if len(flows) < 5:
        return 0.0
    recent = [value for _, value in flows[-3:]]
    previous = [value for _, value in flows[-8:-3]]
    if not recent or not previous:
        return 0.0
    recent_avg = sum(recent) / len(recent)
    previous_sorted = sorted(previous)
    mid = previous_sorted[len(previous_sorted) // 2]
    if mid <= 0:
        return 0.0
    return recent_avg - mid


def price_change_since(prices, seconds):
    if not prices:
        return 0.0
    latest_ts, latest_price = prices[-1]
    target_ts = latest_ts - seconds
    baseline = None
    for ts, price in prices:
        if ts <= target_ts:
            baseline = price
        else:
            break
    if baseline is None and len(prices) >= 2:
        baseline = prices[0][1]
    return return_percent(baseline or 0.0, latest_price)


def volume_acceleration(volume_deltas):
    if len(volume_deltas) < 6:
        return 0.0
    recent = volume_deltas[-3:]
    previous = volume_deltas[-6:-3]
    a = sum(recent) / len(recent)
    b = sum(previous) / len(previous)
    if b <= 0:
        return 0.0
    return a / b


def persistence_count(flows, price_30s, kind):
    cutoff = time.time() - PERSISTENCE_WINDOW_SECONDS
    qualifying = 0
    for ts, flow in flows:
        if ts < cutoff:
            continue
        if kind == "BUILDUP":
            if flow >= BUILDUP_FLOW:
                qualifying += 1
        else:
            if flow >= MOMENTUM_FLOW:
                qualifying += 1
    return qualifying


# ============================================================
# ALERT DECISION
# ============================================================


def evaluate_symbol(symbol):
    now_ts = time.time()
    now_sec = int(now_ts)

    tech_ok, tech = technical_gate(symbol, now_sec)
    if not tech_ok:
        return None

    prices, flows, spreads, volume_deltas, last_alert = TELEMETRY[symbol].snapshot(now_ts)
    if not prices or not flows or not spreads:
        return None

    current_price = prices[-1][1]
    current_flow = flows[-1][1]
    spread_bps = spreads[-1][1]
    flow_delta = order_flow_delta(flows)
    price_30s = price_change_since(prices, 30)
    vol_accel = volume_acceleration(volume_deltas)

    if current_price <= 0:
        return None

    buildup_persistence = persistence_count(flows, price_30s, "BUILDUP")
    momentum_persistence = persistence_count(flows, price_30s, "MOMENTUM")

    # STREAM BUILDUP is earlier than the existing MICRO layer: price remains quiet.
    buildup = (
        current_flow >= BUILDUP_FLOW
        and flow_delta >= BUILDUP_FLOW_DELTA
        and spread_bps <= BUILDUP_SPREAD_BPS
        and -0.10 <= price_30s <= BUILDUP_MAX_PRICE_30S
        and buildup_persistence >= BUILDUP_MIN_PERSISTENCE
        and (vol_accel == 0.0 or MIN_CANDLE_VOLUME_ACCEL <= vol_accel <= MAX_CANDLE_VOLUME_ACCEL)
    )

    # STREAM MOMENTUM requires actual short price expansion plus order-flow support.
    momentum = (
        current_flow >= MOMENTUM_FLOW
        and flow_delta >= MOMENTUM_FLOW_DELTA
        and spread_bps <= MOMENTUM_SPREAD_BPS
        and MOMENTUM_MIN_PRICE_30S <= price_30s <= MOMENTUM_MAX_PRICE_30S
        and momentum_persistence >= MOMENTUM_MIN_PERSISTENCE
        and (vol_accel == 0.0 or vol_accel >= MIN_CANDLE_VOLUME_ACCEL)
    )

    high_risk = (
        price_30s > MOMENTUM_MAX_PRICE_30S
        or spread_bps > 60
        or current_flow > 6.0
        or flow_delta > 4.0
    )

    return {
        "symbol": symbol,
        "price": current_price,
        "price_30s": price_30s,
        "order_flow": current_flow,
        "flow_delta": flow_delta,
        "spread_bps": spread_bps,
        "volume_accel_5m": vol_accel,
        "buildup_persistence": buildup_persistence,
        "momentum_persistence": momentum_persistence,
        "score": safe_float(tech.get("score"), 0.0),
        "streak": safe_int(tech.get("streak"), 0),
        "core_timestamp": safe_int(tech.get("timestamp"), 0),
        "buildup": buildup,
        "momentum": momentum and not high_risk,
        "high_risk": high_risk,
    }


def alert_allowed(symbol, alert_type, now_ts):
    last = TELEMETRY[symbol].snapshot(now_ts)[4].get(alert_type, 0)
    cooldown = BUILDUP_COOLDOWN_SECONDS if alert_type == "BUILDUP" else MOMENTUM_COOLDOWN_SECONDS
    return last <= 0 or now_ts - last >= cooldown


def build_message(result, alert_type):
    if alert_type == "BUILDUP":
        title = "🟡 STREAM PRESSURE BUILD EARLY WARNING"
        body = "فشار خرید در استریم order book پایدار شده، در حالی که قیمت هنوز آرام است."
    else:
        title = "⚡ STREAM MOMENTUM EARLY WARNING"
        body = "حرکت کوتاه‌مدت قیمت با فشار خرید استریم هم‌زمان شده است."

    return (
        f"{title}\n\n"
        f"🪙 {result['symbol']}\n"
        f"💰 Price: {result['price']:.8g}\n"
        f"📈 30s: {result['price_30s']:+.2f}%\n"
        f"🌊 Order flow: {result['order_flow']:.2f} (Δ {result['flow_delta']:+.2f})\n"
        f"📏 Spread: {result['spread_bps']:.1f} bps\n"
        f"📊 5m volume accel: "
        f"{result['volume_accel_5m']:.2f}x\n"
        f"⭐ Core score: {result['score']:.0f}/100 | streak {result['streak']}\n"
        f"🔁 Pressure persistence: "
        f"{result['buildup_persistence'] if alert_type == 'BUILDUP' else result['momentum_persistence']} updates\n\n"
        f"🟠 {body}\n"
        "این هشدار سیگنال قطعی خرید یا تضمین جهش نیست."
    )


def try_emit(result):
    symbol = result["symbol"]
    now_ts = time.time()

    # Earlier BUILDUP has priority because it is the intended earliest signal.
    if result.get("buildup") and alert_allowed(symbol, "BUILDUP", now_ts):
        message = build_message(result, "BUILDUP")
        if telegram_send(message):
            TELEMETRY[symbol].mark_alert("BUILDUP", now_ts)
            print("STREAM BUILDUP ALERT:", symbol)
            return True

    if result.get("momentum") and alert_allowed(symbol, "MOMENTUM", now_ts):
        message = build_message(result, "MOMENTUM")
        if telegram_send(message):
            TELEMETRY[symbol].mark_alert("MOMENTUM", now_ts)
            print("STREAM MOMENTUM ALERT:", symbol)
            return True

    return False


# ============================================================
# STATE PERSISTENCE
# ============================================================

STATE_LOCK = threading.Lock()


def load_stream_state():
    raw = load_json(STREAM_STATE_PATH, {})
    if not isinstance(raw, dict):
        return

    symbols = raw.get("symbols", {})
    if not isinstance(symbols, dict):
        return

    for symbol, item in symbols.items():
        if not isinstance(item, dict):
            continue

        symbol = str(symbol).upper()
        telemetry = TELEMETRY[symbol]
        with telemetry.lock:
            telemetry.last_alert["BUILDUP"] = safe_float(item.get("last_alert_buildup"), 0.0)
            telemetry.last_alert["MOMENTUM"] = safe_float(item.get("last_alert_momentum"), 0.0)
            if safe_float(item.get("last_price"), 0.0) > 0:
                telemetry.price_history.append((
                    safe_float(item.get("last_price_timestamp"), time.time()),
                    safe_float(item.get("last_price"), 0.0),
                ))
            if safe_float(item.get("last_flow"), 0.0) > 0:
                telemetry.flow_history.append((
                    safe_float(item.get("last_price_timestamp"), time.time()),
                    safe_float(item.get("last_flow"), 0.0),
                ))
            if safe_float(item.get("last_spread_bps"), 999.0) < 999:
                telemetry.spread_history.append((
                    safe_float(item.get("last_price_timestamp"), time.time()),
                    safe_float(item.get("last_spread_bps"), 999.0),
                ))


def persist_stream_state():
    snapshot = {}
    now_ts = time.time()

    for symbol, telemetry in TELEMETRY.items():
        prices, flows, spreads, volume_deltas, last_alert = telemetry.snapshot(now_ts)
        if not prices:
            continue

        snapshot[symbol] = {
            "last_price": prices[-1][1],
            "last_price_timestamp": prices[-1][0],
            "last_flow": flows[-1][1] if flows else 0.0,
            "last_spread_bps": spreads[-1][1] if spreads else 999.0,
            "last_alert_buildup": last_alert.get("BUILDUP", 0),
            "last_alert_momentum": last_alert.get("MOMENTUM", 0),
        }

    with STATE_LOCK:
        save_json(
            STREAM_STATE_PATH,
            {
                "schema_version": 1,
                "updated_at": int(now_ts),
                "symbols": snapshot,
            },
        )


# ============================================================
# RAW WEBSOCKET CLIENT (RFC 6455)
# ============================================================

class RawWebSocket:
    def __init__(self, host, path, timeout=SOCKET_TIMEOUT_SECONDS):
        self.host = host
        self.path = path
        self.timeout = timeout
        self.sock = None
        self._fragment_buffer = bytearray()

    def connect(self):
        raw = socket.create_connection(
            (self.host, 443),
            timeout=self.timeout,
        )
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://nobitex.ir\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock.sendall(request)

        response = self._read_http_headers()
        lines = response.split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101"):
            raise RuntimeError("WebSocket handshake failed: " + (lines[0] if lines else "empty response"))

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        accept = headers.get("sec-websocket-accept", "")
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")

        if accept != expected:
            raise RuntimeError("WebSocket accept key mismatch")

    def _read_http_headers(self):
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed during WebSocket handshake")
            buffer.extend(chunk)
            if len(buffer) > 65536:
                raise RuntimeError("Oversized WebSocket handshake")
        head, _ = bytes(buffer).split(b"\r\n\r\n", 1)
        return head.decode("latin1", errors="replace")

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def send_text(self, text):
        self.send_frame(0x1, text.encode("utf-8"))

    def send_pong_empty(self):
        self.send_text("{}")

    def send_frame(self, opcode, payload=b""):
        if self.sock is None:
            raise ConnectionError("Socket is not connected")

        first = 0x80 | (opcode & 0x0F)
        length = len(payload)

        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length < 65536:
            header = bytes([first, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack(">Q", length)

        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)

    def recv_frame(self):
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = bytearray(self._recv_exact(length)) if length else bytearray()

        if masked:
            for i in range(length):
                payload[i] ^= mask[i % 4]

        return fin, opcode, bytes(payload)

    def recv_message(self):
        fragments = bytearray()

        while True:
            fin, opcode, payload = self.recv_frame()

            if opcode == 0x8:
                return "close", payload

            if opcode == 0x9:
                # WebSocket ping -> WebSocket pong.
                self.send_frame(0xA, payload)
                continue

            if opcode == 0xA:
                continue

            if opcode == 0x0:
                fragments.extend(payload)
            elif opcode in (0x1, 0x2):
                fragments = bytearray(payload)
            else:
                continue

            if fin:
                if opcode == 0x2:
                    return "binary", bytes(fragments)
                return "text", bytes(fragments).decode("utf-8", errors="replace")


# ============================================================
# CENTRIFUGO/NOBITEX STREAM HANDLING
# ============================================================


def extract_push(message):
    if not isinstance(message, dict):
        return None

    push = message.get("push")
    if not isinstance(push, dict):
        return None

    channel = str(push.get("channel", ""))
    pub = push.get("pub")
    if not isinstance(pub, dict):
        return None

    data = pub.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    return channel, data


def channel_info(channel):
    prefix = "public:orderbook-"
    if channel.startswith(prefix):
        return "orderbook", channel[len(prefix):].upper()

    prefix = "public:candle-"
    if channel.startswith(prefix):
        rest = channel[len(prefix):]
        parts = rest.rsplit("-", 1)
        if len(parts) == 2:
            return "candle", parts[0].upper()

    return None, None


def process_orderbook(symbol, data):
    price, flow, spread_bps = parse_orderbook(data)
    if price <= 0:
        return

    ts = time.time()
    TELEMETRY[symbol].add_orderbook(ts, price, flow, spread_bps)

    result = evaluate_symbol(symbol)
    if result:
        try_emit(result)


def process_candle(symbol, data):
    if not symbol.endswith("USDT"):
        return
    TELEMETRY[symbol].add_candle(data)


def websocket_worker(symbols, worker_index):
    channels = []
    for symbol in symbols:
        channels.append("public:orderbook-" + symbol)
        channels.append("public:candle-" + symbol + "-5")

    # A connection has 300 channel subscriptions. Each market uses two channels.
    if len(channels) > MAX_CHANNELS_PER_CONNECTION:
        raise RuntimeError(
            f"Worker {worker_index}: {len(channels)} channels exceeds the Nobitex limit"
        )

    backoff = RECONNECT_MIN_SECONDS

    while True:
        ws = RawWebSocket(WS_HOST, WS_PATH)
        try:
            print(f"WS[{worker_index}] connecting: {len(symbols)} markets / {len(channels)} channels")
            ws.connect()

            ws.send_text(json.dumps({"connect": {}, "id": 1}, separators=(",", ":")))

            # Wait for the Centrifugo connection response before subscribing.
            connected = False
            connect_deadline = time.time() + 15
            while time.time() < connect_deadline:
                kind, payload = ws.recv_message()
                if kind == "close":
                    raise ConnectionError("Closed while waiting for connect response")
                if kind != "text":
                    continue
                if payload == "{}":
                    ws.send_pong_empty()
                    continue
                try:
                    response = json.loads(payload)
                except Exception:
                    continue
                if isinstance(response, dict) and isinstance(response.get("connect"), dict):
                    if response.get("connect", {}).get("error"):
                        raise RuntimeError("Centrifugo connect error: " + str(response["connect"]))
                    connected = True
                    break

            if not connected:
                raise TimeoutError("Timed out waiting for Centrifugo connect response")

            subscribe_id = 2
            for channel in channels:
                ws.send_text(
                    json.dumps(
                        {
                            "id": subscribe_id,
                            "subscribe": {"channel": channel},
                        },
                        separators=(",", ":"),
                    )
                )
                subscribe_id += 1
                if subscribe_id > MAX_SUBSCRIBE_ID:
                    subscribe_id = 2

            print(f"WS[{worker_index}] connected and subscribed")
            backoff = RECONNECT_MIN_SECONDS
            last_state_write = time.time()
            last_tech_reload = time.time()
            message_count = 0

            while True:
                # Keep technical state current without hitting GitHub/API for every message.
                if time.time() - last_tech_reload >= 60:
                    reload_technical_state()
                    last_tech_reload = time.time()

                kind, payload = ws.recv_message()
                message_count += 1

                if kind == "close":
                    raise ConnectionError("Remote WebSocket close frame")

                if kind != "text":
                    continue

                if payload == "{}":
                    # Nobitex documents an application-level ping using {} and expects {}.
                    ws.send_pong_empty()
                    continue

                try:
                    message = json.loads(payload)
                except Exception:
                    continue

                # Ignore subscription/connection responses; act on push messages.
                info = extract_push(message)
                if not info:
                    continue

                channel, data = info
                stream_type, symbol = channel_info(channel)
                if not stream_type or not symbol:
                    continue

                if stream_type == "orderbook":
                    process_orderbook(symbol, data)
                elif stream_type == "candle":
                    process_candle(symbol, data)

                if time.time() - last_state_write >= PRINT_EVERY_SECONDS:
                    persist_stream_state()
                    print(
                        f"WS[{worker_index}] alive | messages={message_count} | symbols={len(symbols)}"
                    )
                    last_state_write = time.time()

        except Exception as exc:
            print(f"WS[{worker_index}] disconnected:", exc)
            ws.close()
            persist_stream_state()
            sleep_for = min(backoff, RECONNECT_MAX_SECONDS)
            sleep_for += random.random() * 1.5
            print(f"WS[{worker_index}] reconnecting in {sleep_for:.1f}s")
            time.sleep(sleep_for)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
        finally:
            ws.close()


# ============================================================
# STARTUP / MAIN
# ============================================================


def get_usdt_markets():
    data = http_get_json(BASE_URL + "/v3/orderbook/all")
    markets = []
    if not isinstance(data, dict):
        return markets

    for raw_symbol, book in data.items():
        symbol = str(raw_symbol).upper()
        if not symbol.endswith("USDT"):
            continue
        if symbol in EXCLUDED_SYMBOLS:
            continue
        if not isinstance(book, dict):
            continue
        markets.append(symbol)

    return sorted(set(markets))


def seed_orderbooks(markets):
    data = http_get_json(BASE_URL + "/v3/orderbook/all")
    count = 0
    if not isinstance(data, dict):
        return 0

    for symbol in markets:
        book = data.get(symbol)
        if not isinstance(book, dict):
            continue
        price, flow, spread_bps = parse_orderbook(book)
        if price <= 0:
            continue
        TELEMETRY[symbol].add_orderbook(time.time(), price, flow, spread_bps)
        count += 1

    return count


def main():
    print("=" * 78)
    print("NOBITEX STREAMING EARLY RADAR V1")
    print("WebSocket + orderbook + 5m candle stream")
    print("=" * 78)

    reload_technical_state()
    load_stream_state()

    markets = get_usdt_markets()
    if not markets:
        raise RuntimeError("No USDT markets found")

    seeded = seed_orderbooks(markets)
    print(f"USDT markets: {len(markets)} | initial orderbooks: {seeded}")

    # Two channels per market. Split into <=150 markets per connection.
    markets_per_worker = max(1, MAX_CHANNELS_PER_CONNECTION // 2)
    market_chunks = list(chunks(markets, markets_per_worker))

    print(
        f"Connections required: {len(market_chunks)} "
        f"({markets_per_worker} markets max per connection)"
    )

    threads = []
    for index, market_chunk in enumerate(market_chunks, 1):
        thread = threading.Thread(
            target=websocket_worker,
            args=(market_chunk, index),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    try:
        while True:
            time.sleep(30)
            reload_technical_state()
            persist_stream_state()
    except KeyboardInterrupt:
        print("Stopping streaming radar...")
        persist_stream_state()


if __name__ == "__main__":
    main()
