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

BASE_URL = "https://apiv2.nobitex.ir"
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
PERFORMANCE_SCHEMA_VERSION = 6
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
    """Treat UDF timestamps as candle starts; close is available at start + 60s."""
    if not isinstance(data, dict):
        return [], "invalid_response"

    status = str(data.get("s", "")).lower()
    if status not in ("ok", "no_data"):
        return [], f"api_status:{status or 'missing'}"

    result = []
    for t, c in zip(data.get("t") or [], data.get("c") or []):
        candle_start = parse_ts(t)
        price = safe_float(c)
        if candle_start is None or price is None or price <= 0:
            continue

        close_ts = int(candle_start) + 60
        if int(start_ts) <= close_ts <= int(end_ts):
            result.append((close_ts, price))

    return sorted(set(result)), status

def analyze_price_gaps(prices, start_ts, end_ts):
    """Measure missing intervals in 1-minute close-price history."""
    start_ts = int(start_ts)
    end_ts = int(end_ts)

    times = sorted({
        int(ts)
        for ts, price in prices
        if start_ts <= int(ts) <= end_ts
        and safe_float(price) is not None
        and safe_float(price) > 0
    })

    expected_candles = max(
        1, int(math.ceil((end_ts - start_ts) / 60.0))
    )

    gaps = []
    estimated_missing_minutes = 0
    max_gap_seconds = 0

    for previous_ts, current_ts in zip(times, times[1:]):
        delta = current_ts - previous_ts
        max_gap_seconds = max(max_gap_seconds, delta)

        # A gap greater than 90 seconds indicates a possible
        # missing 1-minute candle. This is a diagnostic threshold.
        if delta > 90:
            missing = max(1, int(round(delta / 60.0)) - 1)
            gaps.append({
                "from_timestamp": previous_ts,
                "to_timestamp": current_ts,
                "gap_seconds": delta,
                "estimated_missing_minutes": missing,
            })
            estimated_missing_minutes += missing

    coverage_pct = min(
        100.0,
        len(times) / expected_candles * 100.0
    )

    return {
        "observed_candles": len(times),
        "estimated_expected_candles": expected_candles,
        "candle_count_coverage_pct": round(coverage_pct, 2),
        "internal_gap_count": len(gaps),
        "estimated_missing_minutes": estimated_missing_minutes,
        "max_gap_seconds": max_gap_seconds,
        "largest_gaps": sorted(
            gaps,
            key=lambda item: item["gap_seconds"],
            reverse=True,
        )[:10],
    }


def get_historical_closes(symbol, start_ts, end_ts):
    """Fetch 1-minute candles; try countback if the normal request has no data."""
    start_ts = int(start_ts)
    end_ts = int(end_ts)

    diagnostics = {
        "symbol": symbol,
        "requested_start": start_ts,
        "requested_end": end_ts,
        "requested_minutes": 0,
        "expected_candles": 0,
        "method": "paged_from_to",
        "attempts": 0,
        "fallback_used": False,
        "returned_candles": 0,
        "coverage_complete": False,
        "status": "not_started",
        "errors": [],
        "pages_used": 0,
        "pagination_limit_reached": False,
    }

    if end_ts <= start_ts:
        diagnostics["status"] = "invalid_range"
        return [], diagnostics

    requested_minutes = int(math.ceil((end_ts - start_ts) / 60.0))
    diagnostics["requested_minutes"] = requested_minutes
    diagnostics["expected_candles"] = requested_minutes

    max_pages = min(
        120,
        max(1, int(math.ceil(requested_minutes / 450.0)) + 3)
    )

    all_prices = {}
    seen_signatures = set()
    reached_start = False
    fallback_mode = False

    page = 1
    while page <= max_pages:
        params = {
            "symbol": symbol,
            "resolution": "1",
            "to": end_ts + 120,
            "page": page,
        }

        if fallback_mode:
            # Alternative request: ask UDF for recent candles by countback.
            params["countback"] = min(500, requested_minutes + 3)
            diagnostics["method"] = "paged_countback"
            diagnostics["fallback_used"] = True
        else:
            params["from"] = max(0, start_ts - 120)

        url = (
            BASE_URL + "/market/udf/history?"
            + urllib.parse.urlencode(params)
        )

        data, meta = _fetch_json_with_retry(url)
        diagnostics["attempts"] += meta.get("attempts", 0)
        diagnostics["pages_used"] = page
        diagnostics["errors"].extend(meta.get("errors", []))

        if data is None:
            diagnostics["status"] = "request_failed"
            diagnostics["errors"].append(
                f"page {page}: request failed"
            )
            break

        if not isinstance(data, dict):
            diagnostics["status"] = "invalid_response"
            diagnostics["errors"].append(
                f"page {page}: response is not an object"
            )
            break

        status = str(data.get("s", "")).lower()

        if status == "no_data":
            if page == 1 and not fallback_mode:
                # Retry once using countback instead of from.
                fallback_mode = True
                diagnostics["fallback_used"] = True
                diagnostics["method"] = "paged_countback"
                diagnostics["pages_used"] = 0
                diagnostics["errors"].append(
                    "Normal request returned no_data; trying countback"
                )
                continue

            diagnostics["status"] = (
                "no_data" if not all_prices
                else "pagination_end_no_data"
            )
            break

        if status != "ok":
            diagnostics["status"] = "api_error"
            diagnostics["errors"].append(
                f"page {page}: API status={status or 'missing'}"
            )
            break

        times = data.get("t") or []
        closes = data.get("c") or []

        if not times or not closes:
            diagnostics["status"] = "empty_response"
            diagnostics["errors"].append(
                f"page {page}: empty candle arrays"
            )
            break

        raw_close_times = []
        page_prices = []

        for raw_ts, raw_price in zip(times, closes):
            candle_start = parse_ts(raw_ts)
            price = safe_float(raw_price)

            if candle_start is None or price is None or price <= 0:
                continue

            close_ts = int(candle_start) + 60
            raw_close_times.append(close_ts)

            if start_ts <= close_ts <= end_ts:
                page_prices.append((close_ts, price))

        if not raw_close_times:
            diagnostics["status"] = "invalid_candles"
            diagnostics["errors"].append(
                f"page {page}: no valid timestamps or prices"
            )
            break

        signature = tuple(sorted(set(raw_close_times)))

        if signature in seen_signatures:
            diagnostics["errors"].append(
                f"page {page}: repeated page; pagination stopped"
            )
            break

        seen_signatures.add(signature)

        for ts, price in page_prices:
            all_prices[ts] = price

        oldest_close_ts = min(raw_close_times)

        if oldest_close_ts <= start_ts + 60:
            reached_start = True
            diagnostics["status"] = "ok"
            break

        # A short page may indicate the end of available history.
        if len(times) < 450:
            diagnostics["status"] = "history_exhausted"
            break

        page += 1
        time.sleep(1.05)

    prices = sorted(all_prices.items())
    diagnostics["returned_candles"] = len(prices)

    gap_stats = analyze_price_gaps(prices, start_ts, end_ts)
    diagnostics.update(gap_stats)

    if prices:
        first_ts = prices[0][0]
        last_ts = prices[-1][0]
        endpoint_coverage = (
            first_ts <= start_ts + 60
            and last_ts >= end_ts - 60
        )
    else:
        endpoint_coverage = False

    diagnostics["coverage_complete"] = bool(
        endpoint_coverage
        and reached_start
        and diagnostics.get("internal_gap_count", 0) == 0
        and diagnostics["returned_candles"] >= max(
            1, requested_minutes - 2
        )
    )

    if diagnostics["coverage_complete"]:
        diagnostics["status"] = "ok"
    elif diagnostics["returned_candles"] > 0:
        diagnostics["status"] = "incomplete_coverage"
    elif diagnostics["status"] not in (
        "request_failed", "api_error", "invalid_response",
        "invalid_candles", "empty_response"
    ):
        diagnostics["status"] = "no_data"

    if (
        not reached_start
        and diagnostics["pages_used"] >= max_pages
    ):
        diagnostics["pagination_limit_reached"] = True
        diagnostics["coverage_complete"] = False
        diagnostics["status"] = "pagination_limit_reached"
        diagnostics["errors"].append(
            "Pagination limit reached before requested start"
        )

    return prices, diagnostics





def price_at_or_before(prices, target_ts, max_staleness_seconds=180):
    """Return the latest valid price only if it is not too old."""
    if not prices:
        return None

    best = None

    for item in prices:
        try:
            ts, price = item
            ts = int(ts)
            price = float(price)
        except (TypeError, ValueError, IndexError):
            continue

        if ts > target_ts or price <= 0:
            continue

        if best is None or ts > best[0]:
            best = (ts, price)

    if best is None:
        return None

    if target_ts - best[0] > max_staleness_seconds:
        return None

    return best

def pct(entry_price, current_price):
    """Calculate percentage change from entry price."""
    entry = safe_float(entry_price)
    current = safe_float(current_price)

    if entry is None or current is None or entry <= 0:
        return None

    return ((current - entry) / entry) * 100.0

def assess_four_hour_window(prices, start_ts, end_ts, current_ts):
    """Return whether the full four-hour close-price window is reliable."""
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    current_ts = int(current_ts)

    if end_ts > current_ts:
        return False, {
            "reason": "window_not_mature",
            "observed_candles": 0,
            "internal_gap_count": 0,
            "coverage_pct": 0.0,
        }

    window = []
    for item in prices:
        try:
            ts = int(item[0])
            price = safe_float(item[1])
        except (TypeError, ValueError, IndexError):
            continue

        if (
            start_ts <= ts <= end_ts
            and price is not None
            and price > 0
        ):
            window.append((ts, price))

    window.sort(key=lambda item: item[0])

    expected = max(1, int(math.ceil((end_ts - start_ts) / 60.0)))

    if not window:
        return False, {
            "reason": "no_candles",
            "observed_candles": 0,
            "internal_gap_count": 0,
            "coverage_pct": 0.0,
        }

    times = [ts for ts, _ in window]
    gaps = [
        current - previous
        for previous, current in zip(times, times[1:])
        if current - previous > 90
    ]

    reasons = []

    if times[0] > start_ts + 90:
        reasons.append("start_missing")

    if times[-1] < end_ts - 90:
        reasons.append("end_missing")

    if gaps:
        reasons.append("internal_gaps")

    coverage_pct = min(100.0, len(window) / expected * 100.0)

    return not reasons, {
        "reason": "complete" if not reasons else ",".join(reasons),
        "observed_candles": len(window),
        "expected_candles": expected,
        "internal_gap_count": len(gaps),
        "max_gap_seconds": max(gaps, default=0),
        "coverage_pct": round(coverage_pct, 2),
    }


def outcome(event, prices, current_ts=None):
    """Measure checkpoint outcomes; only report MFE/MAE for a reliable 4h window."""
    if current_ts is None:
        current_ts = now_ts()

    current_ts = int(current_ts)
    start = int(event["timestamp"])
    end = start + CHECKPOINTS["4h"]
    entry = event["price"]

    checkpoints = {}

    for label, seconds in CHECKPOINTS.items():
        target = start + seconds
        base = {
            "available": False,
            "target_timestamp": target,
            "target_timestamp_iso": iso(target),
            "source": "NOBITEX_UDF_1M_CLOSE_ALIGNED",
        }

        if target > current_ts:
            base["censored"] = True
            base["censor_reason"] = "target_in_future"
            checkpoints[label] = base
            continue

        hit = price_at_or_before(prices, target)

        if hit is None:
            base["censored"] = True
            base["censor_reason"] = "no_fresh_close_near_target"
            checkpoints[label] = base
            continue

        ts, price = hit
        base.update({
            "available": True,
            "actual_timestamp": ts,
            "actual_timestamp_iso": iso(ts),
            "price": price,
            "change_pct": pct(entry, price),
        })
        checkpoints[label] = base

    reliable, window_info = assess_four_hour_window(
        prices, start, end, current_ts
    )

    mfe = mae = None
    mfe_ts = mae_ts = None

    if reliable:
        changes = [
            (ts, pct(entry, price))
            for ts, price in prices
            if start <= ts <= end and pct(entry, price) is not None
        ]

        if changes:
            mfe_ts, mfe = max(changes, key=lambda item: item[1])
            mae_ts, mae = min(changes, key=lambda item: item[1])

    return {
        "checkpoints": checkpoints,
        "mfe_pct": mfe,
        "mfe_timestamp": mfe_ts,
        "mfe_timestamp_iso": iso(mfe_ts),
        "mae_pct": mae,
        "mae_timestamp": mae_ts,
        "mae_timestamp_iso": iso(mae_ts),
        "four_hour_window_reliable": reliable,
        "four_hour_window_diagnostics": window_info,
        "excursion_basis": "1m_closes_only",
        "source": "NOBITEX_UDF_1M_CLOSE_ALIGNED",
    }


def threshold_hit(event, prices, threshold, current_ts=None):
    """Do not label a threshold as missed when its four-hour window is incomplete."""
    if current_ts is None:
        current_ts = now_ts()

    current_ts = int(current_ts)
    start_ts = int(event["timestamp"])
    end_ts = start_ts + CHECKPOINTS["4h"]

    reliable, window_info = assess_four_hour_window(
        prices, start_ts, end_ts, current_ts
    )

    base = {
        "threshold_pct": threshold,
        "window_start_timestamp": start_ts,
        "window_start_timestamp_iso": iso(start_ts),
        "window_end_timestamp": end_ts,
        "window_end_timestamp_iso": iso(end_ts),
        "available": reliable,
        "censored": not reliable,
        "window_reliable": reliable,
        "window_diagnostics": window_info,
        "source": "NOBITEX_UDF_1M_CLOSE_ALIGNED",
    }

    if not reliable:
        base["hit"] = None
        base["censor_reason"] = window_info.get("reason")
        return base

    for ts, price in prices:
        if ts < start_ts:
            continue
        if ts > end_ts:
            break

        change = pct(event["price"], price)

        if change is not None and change >= threshold:
            base.update({
                "hit": True,
                "timestamp": ts,
                "timestamp_iso": iso(ts),
                "price": price,
                "lead_time_seconds": ts - start_ts,
            })
            return base

    base["hit"] = False
    return base

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
            "threshold_samples": {"+1%": 0, "+2%": 0, "+5%": 0},
            "threshold_hit_rate_pct": {"+1%": None, "+2%": None, "+5%": None},
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

            candidates = [
                e.get("threshold_hits", {}).get(key, {})
                for e in rows
            ]

            valid = [
                item for item in candidates
                if (
                    isinstance(item, dict)
                    and item.get("available") is True
                    and isinstance(item.get("hit"), bool)
                )
            ]

            hit_count = sum(
                item["hit"] is True
                for item in valid
            )

            stats["threshold_hits"][key] = hit_count
            stats["threshold_samples"][key] = len(valid)
            stats["threshold_hit_rate_pct"][key] = (
                hit_count / len(valid) * 100.0
                if valid else None
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
        "symbols_without_data": sum(
            not bool(d.get("returned_candles"))
            for d in historical_fetch_diagnostics.values()
        ),
        "symbols_with_complete_coverage": sum(
            bool(d.get("coverage_complete"))
            for d in historical_fetch_diagnostics.values()
        ),
        "symbols_with_incomplete_coverage": sum(
            bool(d.get("returned_candles"))
            and not bool(d.get("coverage_complete"))
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
        "symbols_with_internal_gaps": sum(
            d.get("internal_gap_count", 0) > 0
            for d in historical_fetch_diagnostics.values()
        ),
        "total_internal_gaps": sum(
            int(d.get("internal_gap_count", 0))
            for d in historical_fetch_diagnostics.values()
        ),
        "estimated_missing_minutes": sum(
            int(d.get("estimated_missing_minutes", 0))
            for d in historical_fetch_diagnostics.values()
        ),
        "max_gap_seconds": max(
            (
                int(d.get("max_gap_seconds", 0))
                for d in historical_fetch_diagnostics.values()
            ),
            default=0,
        ),
    }

    measured = []
    for event in events:
        prices = prices_by_symbol.get(event["symbol"], [])
        row = dict(event)
        row["outcome"] = outcome(event, prices, current_ts=current)
        row["threshold_hits"] = {
            "+1%": threshold_hit(event, prices, 1.0, current_ts=current),
            "+2%": threshold_hit(event, prices, 2.0, current_ts=current),
            "+5%": threshold_hit(event, prices, 5.0, current_ts=current),
        }

        for label, seconds in CHECKPOINTS.items():
            cp = row["outcome"]["checkpoints"].get(label)
            if cp and event["timestamp"] + seconds > current:
                cp["available"] = False
                cp["censored"] = True
        measured.append(row)

    summary = summarize(measured)
    reliable_4h = sum(
        1
        for event in measured
        if (
            event.get("signal_type") == "TREND_EARLY"
            and event.get("outcome", {}).get("four_hour_window_reliable") is True
        )
    )

    state = {
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
        "tracking_schema_version": TRACKING_SCHEMA_VERSION,
        "generated_at": current,
        "generated_at_iso": iso(current),
        "measurement_source": "NOBITEX_UDF_1M_CLOSE_ALIGNED",
        "future_leakage_policy":
            "Use only 1m candle closes whose close timestamp is at or before each target; never use a future candle close.",
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
    print("Symbols with complete coverage:", fetch_summary["symbols_with_complete_coverage"])
    print("Symbols with incomplete coverage:", fetch_summary["symbols_with_incomplete_coverage"])
    print(
        "Symbols without historical data:",
        fetch_summary["symbols_without_data"],
    )
    print("Historical fetch request failures:", fetch_summary["symbols_failed"])
    print("Fallback fetches:", fetch_summary["fallback_used"])

    print(
        "Symbols with internal gaps:",
        fetch_summary["symbols_with_internal_gaps"],
    )
    print(
        "Total internal gaps:",
        fetch_summary["total_internal_gaps"],
    )
    print(
        "Estimated missing minutes:",
        fetch_summary["estimated_missing_minutes"],
    )
    print(
        "Maximum gap seconds:",
        fetch_summary["max_gap_seconds"],
    )

    print("4h reliable TREND_EARLY samples:", reliable_4h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
