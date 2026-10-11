
name: Crypto Early Alert

on:
  workflow_dispatch:
  schedule:
    - cron: "7,17,27,37,47,57 * * * *"

permissions:
  contents: write

concurrency:
  group: crypto-early-alert-main
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: main

      - name: Sync latest persisted state before scan
        run: |
          git fetch origin main
          git checkout main
          git reset --hard origin/main
          git status --short
          git log -1 --oneline

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Syntax check
        run: |
          python -m py_compile scanner.py
          python -m py_compile performance_tracker.py
          python -m py_compile ws_orderbook_radar.py
          python -m py_compile scan_quality_logger.py

      - name: Install WebSocket client
        continue-on-error: true
        run: |
          python -m pip install --disable-pip-version-check --no-input "websocket-client==1.8.0"

      - name: Start WebSocket OBI radar in background
        continue-on-error: true
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          python ws_orderbook_radar.py --seconds 210 > ws_obi_radar.log 2>&1 &
          echo $! > ws_obi_radar.pid

      - name: Run Nobitex Early Move Radar
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python scanner.py

      - name: Wait for WebSocket OBI radar
        run: |
          if [ -f ws_obi_radar.pid ]; then
            PID="$(cat ws_obi_radar.pid)"
            wait "$PID" || true
          fi

      - name: Show WebSocket OBI log
        if: always()
        run: |
          if [ -f ws_obi_radar.log ]; then
            tail -n 160 ws_obi_radar.log
          fi

      - name: Track alert performance
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python performance_tracker.py

      - name: Verify persistent alert event log
        if: always()
        run: |
          if [ -f nobitex_alert_events.jsonl ]; then
            echo "Alert event log exists."
            echo "Event lines: $(grep -cve '^[[:space:]]*$' nobitex_alert_events.jsonl || true)"
            tail -n 5 nobitex_alert_events.jsonl || true
          else
            echo "Alert event log does not exist yet."
            touch nobitex_alert_events.jsonl
          fi

      - name: Verify scan quality log
        if: always()
        run: |
          if [ -f nobitex_scan_quality.jsonl ]; then
            echo "Scan quality log exists."
            echo "Scan quality lines: $(grep -cve '^[[:space:]]*$' nobitex_scan_quality.jsonl || true)"
            tail -n 5 nobitex_scan_quality.jsonl || true
          else
            echo "Scan quality log does not exist yet."
          fi

      - name: Save persistent state and measurement data
        if: always()
        run: |
          set -e
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          for file in \
            nobitex_early_radar_state.json \
            nobitex_telegram_alert_state.json \
            nobitex_signal_performance_state.json \
            nobitex_performance_tracking_start.json \
            nobitex_ws_orderbook_signal.json \
            nobitex_ws_obi_alert_state.json \
            nobitex_ws_raw_samples.jsonl \
            nobitex_alert_events.jsonl \
            nobitex_scan_quality.jsonl; do
            if [ -f "$file" ]; then git add -- "$file"; fi
          done

          if git diff --cached --quiet; then
            echo "No persistent data changed; nothing to commit."
            exit 0
          fi

          git commit -m "Persist radar state and measurement data"

          for attempt in 1 2 3; do
            if git push origin HEAD:main; then
              echo "Persistent data successfully pushed."
              exit 0
            fi

            echo "Push attempt $attempt failed; rebasing on latest main."
            git fetch origin main

            if ! git rebase origin/main; then
              echo "::error::Rebase conflict while saving persistent JSON/JSONL data."
              git rebase --abort || true
              exit 1
            fi

            sleep "$attempt"
          done

          echo "::error::Could not persist data after 3 push attempts."
          exit 1
