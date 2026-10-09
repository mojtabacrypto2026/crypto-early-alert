#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe, backup-first patch for issue 3. Run from repository root."""
from pathlib import Path
import shutil, subprocess, sys, time

root = Path.cwd()
scanner = root / "scanner.py"
workflow = root / ".github" / "workflows" / "alert.yml"

helper = r"""

# ============================================================
# SCAN QUALITY DATASET (observational only; no alert logic changes)
# ============================================================
SCAN_QUALITY_LOG_PATH = os.path.join(WORKSPACE, "nobitex_scan_quality.jsonl")
SCAN_QUALITY_MAX_LINES = 10000


def append_scan_quality_snapshot(results, timestamp, coverage):
    """Append one row per successfully analyzed market; never changes alerts."""
    rows = []
    for result in results:
        if not isinstance(result, dict) or result.get("error"):
            continue
        symbol = str(result.get("symbol", "")).strip().upper()
        price = safe_float(result.get("price"), 0.0)
        if not symbol or price <= 0:
            continue
        rows.append({
            "schema_version": 1,
            "timestamp": int(timestamp),
            "symbol": symbol,
            "price": price,
            "score": safe_float(result.get("score"), 0.0),
            "label": str(result.get("label", "")),
            "pre_move_gate": bool(result.get("pre_move_gate", False)),
            "quality": bool(result.get("quality", False)),
            "fast_pre_move": bool(result.get("fast_pre_move", False)),
            "trend_early": bool(result.get("trend_early", False)),
            "micro_early": bool(result.get("micro_early", False)),
            "confirmed": bool(result.get("confirmed", False)),
            "high_risk_jump": bool(result.get("high_risk_jump", False)),
            "streak": int(safe_float(result.get("streak"), 0)),
            "order_flow": safe_float(result.get("order_flow"), 0.0),
            "volume_ratio": safe_float(result.get("volume_ratio"), 0.0),
            "volume_ratio_15m": safe_float(result.get("volume_ratio_15m"), 0.0),
            "volume_acceleration_15m": safe_float(result.get("volume_acceleration_15m"), 0.0),
            "rsi": safe_float(result.get("rsi"), 0.0),
            "structure": safe_float(result.get("structure"), 0.0),
            "momentum_15m": safe_float(result.get("momentum_15m"), 0.0),
            "momentum_1h": safe_float(result.get("momentum_1h"), 0.0),
            "momentum_4h": safe_float(result.get("momentum_4h"), 0.0),
            "news_score": safe_float(result.get("news_score"), 0.0),
            "coverage": round(safe_float(coverage, 0.0), 4),
        })
    if not rows:
        print("Scan quality dataset: no valid rows to append.")
        return
    try:
        with open(SCAN_QUALITY_LOG_PATH, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\\n")
        # Keep the working-tree dataset bounded; 10,000 rows is enough for recent evaluation.
        with open(SCAN_QUALITY_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > SCAN_QUALITY_MAX_LINES:
            with open(SCAN_QUALITY_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-SCAN_QUALITY_MAX_LINES:])
        print("Scan quality records saved:", len(rows))
    except Exception as exc:
        # A dataset write error must not stop scanning or affect alerts.
        print("Scan quality dataset error:", exc)

"""

def main():
    if not scanner.is_file() or not workflow.is_file():
        raise RuntimeError("Run from the repository root containing scanner.py and .github/workflows/alert.yml")
    s = scanner.read_text(encoding="utf-8")
    w = workflow.read_text(encoding="utf-8")
    if "append_scan_quality_snapshot(" in s or "nobitex_scan_quality.jsonl" in w:
        raise RuntimeError("Dataset patch appears already present; refusing duplicate insertion.")
    anchor = "    coverage_ok = (\n        coverage\n        >= MIN_COVERAGE_FOR_ALERTS\n    )\n"
    add_anchor = "          git add nobitex_alert_events.jsonl\n"
    if s.count(anchor) != 1 or s.count("def ensure_tracking_start(") != 1 or w.count(add_anchor) != 1:
        raise RuntimeError("Expected insertion points did not match. No files changed.")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    sb = scanner.with_name("scanner.py.backup_issue3_" + stamp)
    wb = workflow.with_name("alert.yml.backup_issue3_" + stamp)
    shutil.copy2(scanner, sb); shutil.copy2(workflow, wb)
    try:
        s = s.replace("def ensure_tracking_start(", helper + "\n\ndef ensure_tracking_start(", 1)
        s = s.replace(anchor, anchor + "\n    # Observational data only; alert thresholds/decisions remain unchanged.\n    append_scan_quality_snapshot(results, now, coverage)\n", 1)
        w = w.replace(add_anchor, add_anchor + "          touch nobitex_scan_quality.jsonl\n          git add nobitex_scan_quality.jsonl\n", 1)
        scanner.write_text(s, encoding="utf-8")
        workflow.write_text(w, encoding="utf-8")
        subprocess.run([sys.executable, "-m", "py_compile", "scanner.py"], cwd=root, check=True)
        print("PASS: scanner.py syntax check")
        print("PATCH APPLIED. Review git diff before committing.")
        print("Backups:", sb.name, "and", wb.name)
        print("Dataset: nobitex_scan_quality.jsonl; maximum 10,000 recent records.")
    except Exception:
        shutil.copy2(sb, scanner); shutil.copy2(wb, workflow)
        print("Validation failed; original files restored.")
        raise

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("PATCH NOT APPLIED:", e, file=sys.stderr)
        sys.exit(1)
