#!/usr/bin/env bash
# sr16_watch.sh — poll the BLE radio until the SR16 advertises.
#
# Usage:  ./sys/sr16_watch.sh              # scan 60s, poll every 2s
#         ./sys/sr16_watch.sh 120          # scan 120s
#
# When the ring is found, this prints the address + RSSI and exits 0.
# If the ring never appears, exits 1.

set -euo pipefail

cd "$(dirname "$0")/.."
DURATION="${1:-60}"

if ! command -v blueutil >/dev/null; then
    echo "blueutil not found. Install: brew install blueutil" >&2
    exit 2
fi

END_TS=$((SECONDS + DURATION))
echo "[$(date +%H:%M:%S)] Scanning for SR16 for ${DURATION}s. WAKE THE RING:"
echo "  - put it on your finger, or"
echo "  - place it on the charger for 3s then off, or"
echo "  - tap it firmly"
echo
ATTEMPT=0
while [ $SECONDS -lt $END_TS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    # Each blueutil inquiry is ~10s by default; let's do shorter polls (5s) and run them in series.
    OUT="$(blueutil --inquiry 5 2>&1 || true)"
    # blueutil prints one line per device: "address: ..., name: \"...\""
    if echo "$OUT" | grep -qiE 'SR16|Telink|R02|R03'; then
        echo
        echo "==================================="
        echo "FOUND IT (attempt $ATTEMPT):"
        echo "$OUT" | grep -iE 'SR16|Telink|R02|R03|^[[:space:]]*address'
        echo "==================================="
        exit 0
    fi
    printf "  [attempt %d @ %s] not yet visible... (keep waking)\n" "$ATTEMPT" "$(date +%H:%M:%S)"
    sleep 1
done

echo
echo "TIMEOUT — SR16 never appeared in ${DURATION}s."
echo "Try: charge the ring for 5 min, then re-run."
exit 1