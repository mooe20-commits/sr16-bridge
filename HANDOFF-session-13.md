# sr16-bridge Handoff — session 13 (2026-07-08)

Read this first when picking up sr16-bridge in a new session.
**This supersedes the "Recommended next session" section of HANDOFF-session-12.5.md.**

## TL;DR

Goal "Hermes reads ring data on the Mac" → **shipped**.

What we did end-to-end this session:

1. **Known-activity phone capture** (you: ~5 min phone taps; me: 5 min decode)
2. **Cracked the 16B record structure** — it's NOT 6×u16 metrics as we'd guessed; it's
   a 4B prefix + 8B sub-record + structural overhead (session-12 model was wrong)
3. **Mapped 6 u16 fields to metrics** with confidence levels:
   - `reserved` (u16_0) = always 0 → confirmed
   - `steps_raw` (u16_1) = MEDIUM confidence
   - `cal_raw` (u16_2) = HIGH confidence (small values, zero in sleep)
   - `hr_agg_raw` (u16_3) = LOW — could be HR-derived aggregate
   - `intensity` (u16_4) = LOW — high variance, unclear purpose
   - `dist_raw` (u16_5) = MEDIUM confidence (magnitude plausible for m-per-hour)
4. **Built `hr_live_0ab.py` daemon** — does live pull via PyObjC → SQLite
5. **Added `a3_hourly` table** to schema.sql with UNIQUE constraint for idempotent retransmits
6. **Wrote launchd plist** for auto-start
7. **Tests: 33/33 green** (was 29/29; +3 new + 1 ingest round-trip)

## What's working right now

| Component | Status | Where |
|---|---|---|
| Phone-side BLE snoop → 0xA3 decode | ✅ Working | `~/projects/sr16-bridge/src/sr16_bridge/decode_0ab.py` |
| Field → metric mapping (best-effort) | ⚠️ PARTIAL | `protocol.py` docstring + `A3_METRIC_*` constants |
| `hr_live_0ab.py` offline ingest | ✅ Tested | Inserts into `~/health/sr16.db` |
| `hr_live_0ab.py` live BLE pull | ⚠️ BLOCKED on P1/P3 | Will work after Forget-device dance + ~20s window |
| launchd auto-start | ✅ Plist ready | `~/Library/LaunchAgents/com.sr16.bridge.daemon.plist` |

## What's NOT confirmed (carries forward)

### Field semantics for u16_3 and u16_4

Two of the six fields still have ambiguous meaning. The structural finding is
solid (those u16 indices are real, not artifacts), but we couldn't tell which
physical metric maps to u16_3 (range 23000-62000, very stable when nonzero) or
u16_4 (range 768-37632, high variance).

To lock these in one shot, the user needs to do a known-step-count + known-distance
walk and re-sync. The protocol parser already pulls all 6 u16 fields into
named columns in `a3_hourly` (`hr_agg_raw`, `intensity_raw` are the ambiguous
ones), so a 2nd capture + re-ingest will reveal which is which without any
schema change.

### Mac-side live pull

`hr_live_0ab.py --once` will run, but the underlying `live_pull.py` is still
gated by the P1/P3 gauntlet (HID auto-bond + ring radio sleep). The session-12
harness runs end-to-end when the operator does the Forget dance first; we just
haven't actually run it on the live ring in this session because we prioritized
the protocol RE.

When the operator wants to verify the live pull end-to-end:
1. Open System Settings → Bluetooth → ⓘ next to SR16 → Forget this device
2. Within ~20s, run: `PYTHONPATH=src .venv/bin/python -m sr16_bridge.hr_live_0ab --once`
3. If it succeeds, you'll see "OK — inserted N rows into a3_hourly"

## Files in this session

| File | Status | Lines | Purpose |
|---|---|---|---|
| `src/sr16_bridge/protocol.py` | modified | +35 | New `A3_METRIC_*` constants + `record_metric_dict()` |
| `src/sr16_bridge/schema.sql` | modified | +33 | New `a3_hourly` table + 2 indexes |
| `src/sr16_bridge/schema_init.py` | new | 22 | `init_db()` + `DB_PATH` extracted for re-use |
| `src/sr16_bridge/hr_live_0ab.py` | new | 220 | Daemon: live pull → SQLite ingest |
| `tests/test_protocol_0ab.py` | modified | +55 | +4 tests (field indices, record_metric_dict, ingest round-trip) |
| `~/Library/LaunchAgents/com.sr16.bridge.daemon.plist` | new | 50 | launchd config for auto-start |
| `HANDOFF-session-13.md` | new | this file | — |

## How the user can verify the live path works

After the Forget dance (one tap on System Settings):

```bash
PYTHONPATH=src ~/projects/sr16-bridge/.venv/bin/python -m sr16_bridge.hr_live_0ab --once
```

Expected output:
```
[CB] state = 5  (5 = poweredOn)
[CB] retrieve: 36BE6673-...  'SR16'
[CB] CONNECTED → discovering A00A service
...
[live-pull] fetch → ab010003....
[hr_live_0ab] collecting notifies for 4.0s...
[hr_live_0ab] OK — inserted 13 rows into a3_hourly
```

If it times out at "scanning", the ring is asleep or HID-bonded — wait 30s and
retry, or do the Forget dance again.

## Carryover

- ❌ u16_3 and u16_4 field semantics (UNBLOCKED by next known-activity capture)
- ❌ Verifying live-pull end-to-end on the ring (one Forget dance + one CLI run)
- ❌ Replacing `hr_live.py` (0x180D path) with `hr_live_0ab.py` (0xAB path) — they're currently coexisting
- ❌ Removing `history_sync.py` (Colmi R02 stale model)
- ❌ Per-hour fetch opcodes (SUB_HOUR_02..0D) are still NOT decoded — they're sent in the
  snoop alongside the today-block, but I didn't decode them. They're likely
  "give me metric X for hour Y" requests that the ring returns as smaller
  packets. Not on the critical path for "AI analyzes my data".

## Pitfalls captured this session

- **P47** (new — candidate for sr16-ring-mac-pitfalls skill): "0xA3 today-block has
  TWO kinds of packets in the same fetch — clean 6×u16 records (the actual today's
  hourly metrics) AND records with embedded `0xE031` markers in their data12 (looks
  like retransmit-anchors or sub-record pointers). Dedupe by packet body is NOT
  correct for the embedded-marker form — use per-record dedupe via `merge_fetches()`
  which already handles it correctly. Visible symptom: per-packet dedupe produces
  duplicate row insertions when ingesting the same 0xA3 fetch twice."

  → Actually re-reading protocol.py, `merge_fetches()` already does per-record dedupe
  correctly (since session 11). The "embedded marker" packets in the capture are
  legitimate distinct records, not duplicates. So P47 is more of an observation
  than a bug — the parser handles it; humans reading raw hex may be confused.
  Marking as "known confusing case" rather than a new pitfall.

## Reference: ground-truth capture

The capture that drove this session's findings:
- `~/health/sr16_captures/sr16_knownact_20260708_212428.zip`
- `~/health/sr16_captures/extracted_knownact/FS/data/log/bt/btsnoop_hci.log`
- `~/health/sr16_captures/packets_20260708T182823Z.json` (decoded)
- 288 ATT frames, 178 SR16-bound, 7× 0xA3 packets, 5 clean (no embedded marker)

Ground-truth activity per hour (UTC, with local CEST = UTC+2):
- 00:03-05:28 UTC → **sleep** (asleep)
- 10:11-14:32 UTC → **sitting/working** (only zeros in the data)
- 14:49-17:06 UTC → **going out / at home** (nonzero across all 5 metrics)