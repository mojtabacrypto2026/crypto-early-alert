#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NOBITEX SIGNAL PERFORMANCE TRACKER - V5 EXACT OUTCOMES."""

import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE_URL = "https://api.nobitex.ir"
WORKSPACE = os.path.dirname(os.path.abspath(__file__))

EVENT_LOG_PATH = os.path.join(WORKSPACE, "nobitex_alert_events.jsonl")
LEGACY_STATE_PATH = os.path.join(WORKSPACE, "nobitex_telegram_alert_state.json")
OUTPUT_PATH = os.path.join(WORKSPACE, "nobitex_signal_performance_state.json")
START_PATH = os.path.join(WORKSPACE, "nobitex_performance_tracking_start.json")

SIGNAL_TYPES = ("MICRO", "FAST", "CONFIRMED", "NEWS", "TREND_EARLY")
CHECKPOINTS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
}

TRACKING_SCHEMA_VERSION = 2
PERFORMANCE_SCHEMA_VERSION = 5
MIN_RELIABLE_4H_SAMPLES = 100
MAX_EVENTS = 1500
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.03
HISTORICAL_FETCH_RETRIES = 3
HISTORICAL_FETCH_BACKOFF_SECONDS = 1.0


def now_ts():
    return int(time.time())


def iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


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


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return parse_ts(float(s))
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def safe_float(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def normalize_symbol(value):
    if not value:
        return None
    return str(value).upper().strip().replace("/", "").replace("_", "")


def normalize_signal_type(value):
    if not value:
        return None
    s = str(value).upper().strip()
    return {
        "TREND": "TREND_EARLY",
        "TREND-EARLY": "TREND_EARLY",
        "MICRO_EARLY": "MICRO",
        "FAST_EARLY": "FAST",
    }.get(s, s)


def read_event_log():
    out = []
    if not os.path.exists(EVENT_LOG_PATH):
        return out
    try:
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    x = json.loads(line)
                    if isinstance(x, dict):
                        out.append(x)
                except Exception:
                    pass
    except Exception:
        pass
    return out


def extract_event(raw):
    if not isinstance(raw, dict):
        return None

    symbol = normalize_symbol(
        raw.get("symbol") or raw.get("market") or raw.get("pair")
        or raw.get("ticker")
    )
    kind = normalize_signal_type(
        raw.get("alert_type") or raw.get("signal_type")
        or raw.get("type") or raw.get("event_type")
    )
    ts = parse_ts(
        raw.get("timestamp") or raw.get("ts") or raw.get("created_at")
        or raw.get("time") or raw.get("alert_timestamp")
    )
    price = safe_float(
        raw.get("price") or raw.get("entry_price")
        or raw.get("alert_price")
    )

    if not symbol or kind not in SIGNAL_TYPES or not ts:
        return None
    if price is None or price <= 0:
        return None

    event = {
        "id": str(raw.get("id") or f"{symbol}:{kind}:{ts}:{price}"),
        "symbol": symbol,
        "signal_type": kind,
        "timestamp": int(ts),
        "timestamp_iso": iso(ts),
        "price": price,
        "source": "EVENT_LOG",
    }

    for key in (
        "score", "trend_score", "score_delta_1", "score_acceleration",
        "positive_score_steps", "order_flow", "rsi", "structure",
        "volume_ratio", "momentum_15m", "momentum_1h", "momentum_4h",
        "resistance", "high_risk_jump", "history_depth",
    ):
        if key in raw:
            event[key] = raw[key]
    return event


def load_events():
    events = [e for r in read_event_log() if (e := extract_event(r))]

    if not events:
        legacy = load_json(LEGACY_STATE_PATH, {})
        if isinstance(legacy, dict):
            candidates = legacy.get("alerts") or legacy.get("events") or []
            if isinstance(candidates, dict):
                candidates = list(candidates.values())
            if isinstance(candidates, list):
                for raw in candidates:
                    e = extract_event(raw)
                    if e:
                        e["source"] = "LEGACY_STATE"
                        events.append(e)

    unique = {e["id"]: e for e in events}
    return sorted(unique.values(), key=lambda x: x["timestamp"])[-MAX_EVENTS:]


def http_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "NobitexSignalPerformanceTracker/5.0"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_json_with_retry(url):
    errors = []
    attempts = 0

    for attempt in range(1, HISTORICAL_FETCH_RETRIES + 1):
        attempts = attempt
        try:
            return http_json(url), {
                "success": True,
                "attempts": attempt,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < HISTORICAL_FETCH_RETRIES:
                time.sleep(HISTORICAL_FETCH_BACKOFF_SECONDS * attempt)

    return None, {
        "success": False,
        "attempts": attempts,
        "errors": errors,
    }


def _parse_udf_closes(data, start_ts, end_ts):
    if not isinstance(data, dict):
        return [], "invalid_response"

    status = str(data.get("s", "")).lower()
    if status not in ("ok", "no_data"):
        return [], f"api_status:{status or 'missing'}"

    result = []
    for t, c in zip(data.get("t") or [], data.get("c") or []):
        ts = parse_ts(t)
        price = safe_float(c)
        if ts is not None and price is not None and price > 0:
            if int(start_ts) - 120 <= int(ts) <= int(end_ts):
                result.append((int(ts), price))

    return sorted(set(result)), status


def get_historical_closes(symbol, start_ts, end_ts):
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    expected_candles = max(1, int(math.ceil((end_ts - start_ts) / 60.0)) + 10)

    diagnostics = {
        "symbol": symbol,
        "requested_start": start_ts,
        "requested_end": end_ts,
        "requested_minutes": max(1, int(math.ceil((end_ts - start_ts) / 60.0))),
        "expected_candles": expected_candles,
        "method": None,
        "attempts": 0,
        "fallback_used": False,
        "returned_candles": 0,
        "coverage_complete": False,
        "status": None,
        "errors": [],
    }

    # Nobitex UDF countback is the primary method. It has proved more
    # reliable than relying only on from/to for this project.
    countback_params = {
        "symbol": symbol,
        "resolution": "1",
        "to": end_ts + 120,
        "countback": expected_candles,
    }
    countback_url = (
        BASE_URL + "/market/udf/history?"
        + urllib.parse.urlencode(countback_params)
    )

    data, meta = _fetch_json_with_retry(countback_url)
    diagnostics["attempts"] += meta.get("attempts", 0)
    diagnostics["errors"].extend(meta.get("errors", []))

    if data is not None:
        prices, status = _parse_udf_closes(data, start_ts, end_ts)
        diagnostics["method"] = "countback"
        diagnostics["status"] = status
        diagnostics["returned_candles"] = len(prices)

        if prices:
            first_ts = prices[0][0]
            last_ts = prices[-1][0]
            diagnostics["coverage_complete"] = (
                first_ts <= start_ts + 60 and last_ts >= end_ts - 60
            )
            if diagnostics["coverage_complete"]:
                return prices, diagnostics

    # Fallback to the explicit from/to range if countback failed, returned
    # no usable data, or did not cover the requested interval.
    diagnostics["fallback_used"] = True
    fallback_params = {
        "symbol": symbol,
        "resolution": "1",
        "from": max(0, start_ts - 120),
        "to": end_ts + 120,
    }
    fallback_url = (
        BASE_URL + "/market/udf/history?"
        + urllib.parse.urlencode(fallback_params)
    )

    data, meta = _fetch_json_with_retry(fallback_url)
    diagnostics["attempts"] += meta.get("attempts", 0)
    diagnostics["errors"].extend(meta.get("errors", []))

    if data is None:
        diagnostics["method"] = "from_to"
        diagnostics["status"] = "request_failed"
        diagnostics["returned_candles"] = 0
        diagnostics["coverage_complete"] = False
        return [], diagnostics

    prices, status = _parse_udf_closes(data, start_ts, end_ts)
    diagnostics["method"] = "from_to"
    diagnostics["status"] = status
    diagnostics["returned_candles"] = len(prices)

    if prices:
        first_ts = prices[0][0]
        last_ts = prices[-1][0]
        diagnostics["coverage_complete"] = (
            first_ts <= start_ts + 60 and last_ts >= end_ts - 60
        )

    return prices, diagnostics


def price_at_or_before(prices, target_ts):
    best = None
    for ts, price in prices:
        if ts <= target_ts:
            best = (ts, price)
        else:
            break
    return best


def pct(entry, price):
    if entry <= 0 or price is None:
        return None
    return (price / entry - 1.0) * 100.0


def outcome(event, prices):
    start = event["timestamp"]
    entry = event["price"]
    checkpoints = {}

    for label, seconds in CHECKPOINTS.items():
        target = start + seconds
        hit = price_at_or_before(prices, target)
        if hit is None:
            checkpoints[label] = {
                "available": False,
                "target_timestamp": target,
                "target_timestamp_iso": iso(target),
                "source": "NOBITEX_UDF_1M",
            }
        else:
            ts, price = hit
            checkpoints[label] = {
                "available": True,
                "target_timestamp": target,
                "target_timestamp_iso": iso(target),
                "actual_timestamp": ts,
                "actual_timestamp_iso": iso(ts),
                "price": price,
                "change_pct": pct(entry, price),
                "source": "NOBITEX_UDF_1M",
            }

    window = [(ts, p) for ts, p in prices
              if start <= ts <= start + CHECKPOINTS["4h"]]
    changes = [pct(entry, p) for _, p in window if pct(entry, p) is not None]
    mfe = max(changes) if changes else None
    mae = min(changes) if changes else None

    mfe_ts = next(
        (ts for ts, p in window if pct(entry, p) == mfe), None
    ) if mfe is not None else None
    mae_ts = next(
        (ts for ts, p in window if pct(entry, p) == mae), None
    ) if mae is not None else None

    return {
        "checkpoints": checkpoints,
        "mfe_pct": mfe,
        "mfe_timestamp": mfe_ts,
        "mfe_timestamp_iso": iso(mfe_ts),
        "mae_pct": mae,
        "mae_timestamp": mae_ts,
        "mae_timestamp_iso": iso(mae_ts),
        "source": "NOBITEX_UDF_1M",
    }


def threshold_hit(event, prices, threshold):
    for ts, price in prices:
        if ts < event["timestamp"]:
            continue
        change = pct(event["price"], price)
        if change is not None and change >= threshold:
            return {
                "hit": True,
                "threshold_pct": threshold,
                "timestamp": ts,
                "timestamp_iso": iso(ts),
                "price": price,
                "lead_time_seconds": ts - event["timestamp"],
                "source": "NOBITEX_UDF_1M",
            }
    return {
        "hit": False,
        "threshold_pct": threshold,
        "source": "NOBITEX_UDF_1M",
    }


def tracking_start(events):
    old = load_json(START_PATH, {})
    if isinstance(old, dict) and old.get("started_at"):
        return old

    ts = min((e["timestamp"] for e in events), default=now_ts())
    data = {
        "tracking_schema_version": TRACKING_SCHEMA_VERSION,
        "started_at": ts,
        "started_at_iso": iso(ts),
        "created_at": now_ts(),
        "created_at_iso": iso(now_ts()),
        "measurement_reset_reason":
            "V5 exact outcomes; no later workflow current-price checkpoints.",
    }
    try:
        save_json(START_PATH, data)
    except Exception:
        pass
    return data


def summarize(events):
    result = {}
    for kind in SIGNAL_TYPES:
        rows = [e for e in events if e["signal_type"] == kind]
        stats = {
            "signals": len(rows),
            "available": {},
            "avg_change_pct": {},
            "median_change_pct": {},
            "positive_rate_pct": {},
            "mfe_avg_pct": None,
            "mae_avg_pct": None,
            "threshold_hits": {"+1%": 0, "+2%": 0, "+5%": 0},
        }

        for label in CHECKPOINTS:
            values = [
                e["outcome"]["checkpoints"][label]["change_pct"]
                for e in rows
                if e.get("outcome", {}).get("checkpoints", {}).get(label, {})
                .get("available")
                and e["outcome"]["checkpoints"][label].get("change_pct")
                is not None
            ]
            stats["available"][label] = len(values)
            if values:
                ordered = sorted(values)
                mid = len(ordered) // 2
                stats["avg_change_pct"][label] = sum(values) / len(values)
                stats["median_change_pct"][label] = (
                    ordered[mid] if len(ordered) % 2
                    else (ordered[mid - 1] + ordered[mid]) / 2
                )
                stats["positive_rate_pct"][label] = (
                    sum(x > 0 for x in values) / len(values) * 100
                )
            else:
                stats["avg_change_pct"][label] = None
                stats["median_change_pct"][label] = None
                stats["positive_rate_pct"][label] = None

        mfes = [e["outcome"]["mfe_pct"] for e in rows
                if e.get("outcome", {}).get("mfe_pct") is not None]
        maes = [e["outcome"]["mae_pct"] for e in rows
                if e.get("outcome", {}).get("mae_pct") is not None]
        stats["mfe_avg_pct"] = sum(mfes) / len(mfes) if mfes else None
        stats["mae_avg_pct"] = sum(maes) / len(maes) if maes else None

        for threshold in (1, 2, 5):
            key = f"+{threshold}%"
            stats["threshold_hits"][key] = sum(
                e.get("threshold_hits", {}).get(key, {}).get("hit", False)
                for e in rows
            )

        result[kind] = stats
    return result


def main():
    events = load_events()
    start = tracking_start(events)
    current = now_ts()

    grouped = defaultdict(list)
    for e in events:
        grouped[e["symbol"]].append(e)

    prices_by_symbol = {}
    historical_fetch_diagnostics = {}
    for symbol, rows in grouped.items():
        a = min(e["timestamp"] for e in rows)
        b = max(e["timestamp"] for e in rows) + CHECKPOINTS["4h"]
        prices, diagnostics = get_historical_closes(symbol, a, b)
        prices_by_symbol[symbol] = prices
        historical_fetch_diagnostics[symbol] = diagnostics
        time.sleep(REQUEST_DELAY)

    fetch_summary = {
        "symbols_requested": len(historical_fetch_diagnostics),
        "symbols_with_data": sum(
            bool(d.get("returned_candles"))
            for d in historical_fetch_diagnostics.values()
        ),
        "symbols_failed": sum(
            d.get("status") == "request_failed"
            for d in historical_fetch_diagnostics.values()
        ),
        "fallback_used": sum(
            bool(d.get("fallback_used"))
            for d in historical_fetch_diagnostics.values()
        ),
        "total_attempts": sum(
            int(d.get("attempts", 0))
            for d in historical_fetch_diagnostics.values()
        ),
        "total_candles": sum(
            int(d.get("returned_candles", 0))
            for d in historical_fetch_diagnostics.values()
        ),
    }

    measured = []
    for event in events:
        prices = prices_by_symbol.get(event["symbol"], [])
        row = dict(event)
        row["outcome"] = outcome(event, prices)
        row["threshold_hits"] = {
            "+1%": threshold_hit(event, prices, 1.0),
            "+2%": threshold_hit(event, prices, 2.0),
            "+5%": threshold_hit(event, prices, 5.0),
        }

        for label, seconds in CHECKPOINTS.items():
            cp = row["outcome"]["checkpoints"].get(label)
            if cp and event["timestamp"] + seconds > current:
                cp["available"] = False
                cp["censored"] = True
        measured.append(row)

    summary = summarize(measured)
    reliable_4h = summary["TREND_EARLY"]["available"].get("4h", 0)

    state = {
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
        "tracking_schema_version": TRACKING_SCHEMA_VERSION,
        "generated_at": current,
        "generated_at_iso": iso(current),
        "measurement_source": "NOBITEX_UDF_1M",
        "future_leakage_policy":
            "Use latest 1m close at or before each target; never a future candle.",
        "signal_types": list(SIGNAL_TYPES),
        "checkpoints": CHECKPOINTS,
        "min_reliable_4h_samples": MIN_RELIABLE_4H_SAMPLES,
        "reliable_4h_samples_trend_early": reliable_4h,
        "measurement_ready_for_4h_optimization":
            reliable_4h >= MIN_RELIABLE_4H_SAMPLES,
        "tracking_start": start,
        "event_count": len(measured),
        "historical_fetch_summary": fetch_summary,
        "historical_fetch_diagnostics": historical_fetch_diagnostics,
        "events": measured[-MAX_EVENTS:],
        "summary": summary,
    }
    save_json(OUTPUT_PATH, state)

    print("Performance Tracker V5 EXACT")
    print("Events:", len(measured))
    print("Historical symbols:", fetch_summary["symbols_requested"])
    print("Symbols with data:", fetch_summary["symbols_with_data"])
    print("Historical fetch failures:", fetch_summary["symbols_failed"])
    print("Fallback fetches:", fetch_summary["fallback_used"])
    print("4h reliable TREND_EARLY samples:", reliable_4h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
