#!/usr/bin/env bash
# sr16_analyzer_cron.sh — watchdog for sr16-bridge analyze output.
#
# Runs analyze.py, summarizes the result. If there are no new analysis_runs rows
# (no new HR data → no new analysis), it prints nothing — cron stays silent
# per the no_agent + empty-stdout = SILENT contract.
#
# If there ARE new analysis_runs, prints a short Telegram-friendly summary of the
# latest one. Cron delivers that via `deliver='telegram'` in the job config.

set -euo pipefail

PROJECT="$HOME/projects/sr16-bridge"
DB="$HOME/health/sr16.db"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "$PROJECT"

# Capture how many analysis_runs rows existed BEFORE running analyze
before=$(sqlite3 "$DB" "SELECT COUNT(*) FROM analysis_runs;" 2>/dev/null || echo 0)

# Run analyze with quiet flag; the script handles its own DB writes
PYTHONPATH=src .venv/bin/python -m sr16_bridge.analyze --window-min 5 --quiet
ec=$?

# Whatever happened, check whether the count of analysis_runs increased
after=$(sqlite3 "$DB" "SELECT COUNT(*) FROM analysis_runs;" 2>/dev/null || echo 0)
new_count=$((after - before))

# Empty stdout = silent (no new data → no notification)
if [ "$new_count" -le 0 ]; then
  exit 0
fi

# New analysis rows — build a Telegram summary of the latest
sqlite3 -separator '|' "$DB" "
SELECT
  id, rows_analyzed, window_start_ts, window_end_ts, model_id,
  summary_json, error
FROM analysis_runs
ORDER BY id DESC
LIMIT $new_count;
" | head -3 | while IFS='|' read -r id rows ws we model summary err; do
  if [ -n "$err" ]; then
    printf '🫀 sr16-bridge ⚠️ run #%s ERR: %s\nwindow %s → %s (rows=%s)\n' \
      "$id" "$err" "$ws" "$we" "$rows"
  else
    printf '🫀 sr16-bridge run #%s — %s rows · %s\nwindow %s → %s\n%s\n' \
      "$id" "$rows" "$model" "$ws" "$we" "$summary"
  fi
done

exit "$ec"
