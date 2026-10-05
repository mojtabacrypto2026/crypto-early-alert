# NOBITEX EARLY ALERT — PROJECT STATE

## Current Branch
main

## Current Phase
Baseline / Measurement Preparation

## Goal
Detect significant upward crypto moves on Nobitex as early as possible,
before the main price movement begins.

## Current Production Components

- scanner.py
- performance_tracker.py
- streaming_radar.py
- backtest.py
- ws_orderbook_radar.py
- Telegram alerts
- Nobitex REST API
- Nobitex WebSocket

## Current Data Pipeline

Nobitex REST
→ Market data
→ 15m candles
→ Technical analysis
→ Order-flow / order-book analysis
→ Early-move logic
→ Telegram alert

Nobitex WebSocket
→ Order-book / streaming data
→ Microstructure monitoring

Performance Tracker
→ Alert performance measurement

## Current Priority

1. Preserve the current working version.
2. Create an immutable alert event log.
3. Fix exact 1H candle aggregation.
4. Measure real future price outcomes.
5. Connect performance tracking to the event log.
6. Only then optimize scores and thresholds.

## Important Rule

Do NOT change scoring formulas or alert thresholds
during the measurement preparation phase.

## Event Log

Planned file:

nobitex_alert_events.jsonl

Purpose:

Store every successfully sent alert as an immutable event.

## 1H Candle Rule

A 1H candle must contain exactly four valid 15m candles:

00
15
30
45

Incomplete or missing 15m candles must not create a 1H candle.

## Performance Horizons

Each alert should eventually be evaluated at:

5m
15m
30m
1h
2h
4h

## Performance Metrics

- Forward return
- Maximum favorable excursion (MFE)
- Maximum adverse excursion (MAE)
- Time to +1%
- Time to +2%
- Time to +5%
- Lead time
- False-positive rate

## Versioning Rule

Never delete old working versions without explicit approval.

Never claim a GitHub change was completed unless the GitHub operation
actually succeeds.

## Current Known Limitation

GitHub write operations may return HTTP 403.
If that happens, the modified file must be provided for manual replacement
instead of claiming that the repository was updated.

## Next Action

Implement Event Log and exact 1H aggregation correction in scanner.py
without changing score formulas or thresholds.
