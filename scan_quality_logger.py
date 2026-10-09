
# -*- coding: utf-8 -*-
"""
Nobitex Scan Quality Logger

Records every successfully analyzed market and one summary
per scan in JSON Lines format.

This module does not change alert scoring or alert decisions.
"""

import json
import math
import os
import threading
from datetime import datetime, timezone


OUTPUT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "nobitex_scan_quality.jsonl",
)

_WRITE_LOCK = threading.Lock()


def _json_safe(value):
    """Convert values to JSON-safe types."""
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _get_value(obj, *names):
    """Read a field from a dictionary or an object."""
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return None

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)

    return None


def _append_rows(rows, output_file=None):
    """Append JSON objects as individual lines."""
    path = output_file or OUTPUT_FILE

    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)

        with _WRITE_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            _json_safe(row),
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                    )
                    handle.write("\n")

        return True

    except (OSError, TypeError, ValueError) as exc:
        print(
            "[SCAN QUALITY LOGGER] Write failed: "
            + str(exc)
        )
        return False


def append_scan_quality_snapshot(
    results,
    scan_timestamp,
    market_count,
    failed_count,
    coverage,
    coverage_ok,
    output_file=None,
):
    """
    Record one scan summary and one row for each analyzed market.

    This function is intended to be called once per completed scan.
    It does not send alerts and does not modify scanner results.
    """
    try:
        timestamp = (
            scan_timestamp.isoformat()
            if hasattr(scan_timestamp, "isoformat")
            else str(scan_timestamp)
        )

        if not timestamp or timestamp == "None":
            timestamp = datetime.now(timezone.utc).isoformat()

        rows = []
        seen_symbols = set()

        for result in results or []:
            symbol = _get_value(
                result, "symbol", "market", "trading_pair"
            )

            if not symbol:
                continue

            symbol = str(symbol).upper()

            # Avoid duplicate records for the same market in one scan.
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)

            row = {
                "record_type": "scan_result",
                "scan_timestamp": timestamp,
                "symbol": symbol,
            }

            # Preserve common scanner fields when available.
            field_aliases = {
                "price": ("price", "last_price", "last"),
                "score": ("score", "total_score", "final_score"),
                "label": ("label", "signal", "signal_type"),
                "change_15m": (
                    "change_15m", "price_change_15m",
                    "change15m",
                ),
                "change_1h": (
                    "change_1h", "price_change_1h",
                    "change1h",
                ),
                "change_4h": (
                    "change_4h", "price_change_4h",
                    "change4h",
                ),
                "volume_ratio": (
                    "volume_ratio", "vol_ratio",
                    "volume_multiple",
                ),
                "rsi": ("rsi", "rsi_14"),
                "macd": ("macd",),
                "spread_bps": ("spread_bps", "spread"),
                "orderbook_imbalance": (
                    "orderbook_imbalance",
                    "order_book_imbalance",
                    "imbalance",
                ),
            }

            for output_name, candidates in field_aliases.items():
                value = _get_value(result, *candidates)
                if value is not None:
                    row[output_name] = _json_safe(value)

            # Keep the complete result for later analysis.
            if isinstance(result, dict):
                row["raw_result"] = _json_safe(result)

            rows.append(row)

        rows.append({
            "record_type": "scan_summary",
            "scan_timestamp": timestamp,
            "market_count": _json_safe(market_count),
            "analyzed_count": len(seen_symbols),
            "failed_count": _json_safe(failed_count),
            "coverage": _json_safe(coverage),
            "coverage_ok": bool(coverage_ok),
        })

        return _append_rows(rows, output_file=output_file)

    except Exception as exc:
        # Logging must not crash the scanner.
        print(
            "[SCAN QUALITY LOGGER] Snapshot failed: "
            + str(exc)
        )
        return False
