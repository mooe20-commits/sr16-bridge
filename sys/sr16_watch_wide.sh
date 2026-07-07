#!/usr/bin/env bash
# sr16_watch_wide.sh — wide-mode BLE scanner for ring wake diagnosis.
#
# Unlike sr16_watch.sh (which greps for "SR16|Telink|..." names), this logs
# EVERY device blueutil sees on every poll. Useful when the ring is waking
# but advertising under a name we don't expect (e.g. as an HID mouse —
# session-3's blocker re-surfaced).
#
# Usage: ./sys/sr16_watch_wide.sh [DURATION_SEC] [POLL_INTERVAL_SEC]
#   default: 180s scan, 2s polls, 2s inquiry duration
#
# Output:
#   - first poll: dump the FULL baseline device list with timestamps
#   - subsequent polls: only NEW addresses (devices not in baseline)
#   - a periodic "no new devices" heartbeat so you know it's still alive

set -euo pipefail

cd "$(dirname "$0")/.."

DURATION="${1:-180}"
INTERVAL="${2:-2}"
INQ="${3:-2}"

if ! command -v blueutil >/dev/null; then
    echo "blueutil not found. Install: brew install blueutil" >&2
    exit 2
fi

END_TS=$((SECONDS + DURATION))
LOG=/tmp/sr16_watch_wide_$(date +%s).log

# Seed a known-addresses set so we can detect new arrivals.
KNOWN_FILE=/tmp/sr16_watch_wide_known_$$
> "$KNOWN_FILE"

echo "[$(date +%H:%M:%S)] WIDE scanner for ${DURATION}s, polling every ${INTERVAL}s" | tee "$LOG"
echo "[$(date +%H:%M:%S)] Log: $LOG" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] WAKE THE RING NOW (display on for 10s = the window)" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] If macOS shows a 'pair mouse/keyboard' popup → CLICK DENY" | tee -a "$LOG"
echo "" | tee -a "$LOG"

ATTEMPT=0
while [ $SECONDS -lt $END_TS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    OUT="$(blueutil --inquiry "$INQ" 2>&1 || true)"

    if [ -z "$OUT" ]; then
        printf "  [attempt %d @ %s] (empty scan — nothing advertising)\n" \
            "$ATTEMPT" "$(date +%H:%M:%S)" | tee -a "$LOG"
    else
        # Collect all addresses from this scan.
        THIS_FILE=/tmp/sr16_watch_wide_this_$$
        echo "$OUT" > "$THIS_FILE"

        # First poll: dump everything as baseline.
        if [ "$ATTEMPT" -eq 1 ]; then
            echo "  [attempt 1 @ $(date +%H:%M:%S)] BASELINE devices seen:" | tee -a "$LOG"
            echo "$OUT" | sed 's/^/    /' | tee -a "$LOG"
            echo "$OUT" | grep -oE 'address: [0-9a-fA-F:-]+' | awk '{print $2}' > "$KNOWN_FILE"
        else
            # Find addresses in this scan not in baseline.
            NEW_ADDRS=$(comm -23 \
                <(echo "$OUT" | grep -oE 'address: [0-9a-fA-F:-]+' | awk '{print $2}' | sort -u) \
                <(sort -u "$KNOWN_FILE"))
            if [ -n "$NEW_ADDRS" ]; then
                echo "  [attempt $ATTEMPT @ $(date +%H:%M:%S)] *** NEW DEVICE(S) APPEARED ***" | tee -a "$LOG"
                for addr in $NEW_ADDRS; do
                    echo "$OUT" | grep -B0 -A0 "$addr" | sed 's/^/    /' | tee -a "$LOG"
                    echo "$addr" >> "$KNOWN_FILE"
                done
                echo "  >>> IF THIS LOOKS LIKE THE RING: tell Hermes the address; we'll run enumerate_cocoa NOW" | tee -a "$LOG"
            else
                printf "  [attempt %d @ %s] no new devices (baseline still n=%d)\n" \
                    "$ATTEMPT" "$(date +%H:%M:%S)" "$(wc -l < "$KNOWN_FILE" | tr -d ' ')" | tee -a "$LOG"
            fi
        fi
        rm -f "$THIS_FILE"
    fi

    sleep "$INTERVAL"
done

echo "" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] Scanner ended. Full log: $LOG" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] Run: tail -50 $LOG" | tee -a "$LOG"
rm -f "$KNOWN_FILE"
exit 0