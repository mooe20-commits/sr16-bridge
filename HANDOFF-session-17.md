# SR16 Handoff — session 17 (2026-07-14 afternoon)

**This supersedes HANDOFF-session-16.md for picking up sr16-bridge.**

## TL;DR

Session 16's recommendation to "capture during sync, not after" turned out to be **moot** — we discovered the actual issue is parser-level. The Jul-14 snoop HAS the data, just not in the format we expected.

**Real finding:** the ring streams per-record via **cmd 0x09 envelopes** (one record per packet, 6-byte body), NOT via bulk cmd 0xA3/0x43/0x6B/0x73 blocks. P65 (claiming data crosses Bluetooth Classic SPP) was wrong; the data IS crossing BLE, just via a non-standard transport.

**Walk-window data:** 15 cmd 0x09 envelopes cover val16 41040-46680 (the walk window). BUT — these envelopes carry only a 2-byte sequence counter (`tail`), not actual metric values. The walk's step/calorie/distance/HR data lives on the ring but isn't surfaced via BLE during this sync.

## What landed (commit pending)

### Code
- `decode_0ab.py`: `is_record_envelope` property + cmd 0x09 branch in `decode_notify()`. Builds a 12B-padded Record from the 6-byte body (marker + val16 + tail). `tail` is the only meaningful field; remaining 10B is zero-padded for shape consistency.
- `ingest_snoop_to_db.py`: `0x09` added to `is_bulk_cmd()` so 0x09 envelopes route to bulk-records path; their `tail` flows into `a3_hourly.reserved_u16_0`.
- `tests/test_protocol_0ab.py`: +3 tests (single-envelope parse, short-body graceful-degrade, full-Jul-14-round-trip). **42/42 green** (was 39/39).

### Skill
- `sr16-ring-mac-pitfalls`: **P66b** added — documents cmd 0x09 envelope structure, 4-min bucket cadence, and the "tail is a sequence counter, not a metric" finding.

### DB
- 16 fresh 0xE831 records from cmd 0xA3 blocks (val16 57484-61594, 16:00-17:00 UTC) — full data
- 39 fresh 0xE831 records from cmd 0x09 envelopes (val16 15287-51422, 04:14-14:17 UTC) — `tail` only
- 15 of the cmd 0x09 records cover the walk window (val16 41040-46680)
- Total a3_hourly now: 68 rows (was 16)

## What did NOT land (the field-mapping question stays open)

- **0xE831 metric semantics still not locked.** The 0xA3 bulk records at val16 57484-61594 have non-zero steps/cal/dist/hr/intensity, but those correspond to events ~6+ hours AFTER the walk — the ring's BLE-visible buffer at sync time didn't contain the walk data.
- **`tail` field** (the only data in 0x09 envelopes) decrements 87→62 monotonically across the walk window. That's a sequence counter (likely "records-remaining-to-send"), NOT a step count or HR average.

## Key insight from this session

The protocol we reverse-engineered in sessions 9-10 (cmd 0xA3/0x43/0x6B/0x73 = bulk blocks with 5-14 records each) describes the **OLDER ring firmware**. The newer firmware (Jul-14 capture is post firmware-update) shifted to **per-record streaming** via cmd 0x09 envelopes. The bulk-block opcodes still appear in writes (the phone asks for them) but the ring answers with cmd 0x09 envelopes instead.

**Why this matters:** future snoops will likely show the same pattern — bulk fetches return 0x09 envelopes, not 0xA3 blocks. The `is_record_envelope` parser path is now general-purpose for this firmware family.

## Pickup-point snippet (for next session)

```bash
cd ~/projects/sr16-bridge
git status --short    # decode_0ab.py, ingest_snoop_to_db.py, tests, CHANGELOG modified
.venv/bin/python -m pytest -q    # 42 passed

# DB state:
sqlite3 ~/health/sr16.db "SELECT marker, COUNT(*), MIN(val16), MAX(val16) FROM a3_hourly GROUP BY marker"
# 0xE031=13 (older) | 0xE831=55 (54 cmd 0x09 envelopes + 11 cmd 0xA3 blocks) = 68 total

# Snoop files:
ls ~/health/sr16_captures/sr16_knownact_1784019535.zip     # Jul-14 snoop (4.9 MB)
ls ~/health/sr16_captures/sr16_rwfit_20260713_204922.zip   # Jul-13 baseline (older firmware)
```

## Recommendation (unchanged from session 16)

To lock field semantics, **one more capture** with the fresh re-attach pattern:
1. Force-stop RWfit on the phone (`adb shell am force-stop com.rw.revivalfit`)
2. Wait 10-15 seconds (ring radio sleeps per P3)
3. `adb bugreport ~/health/sr16_captures/reattach_<ts>.zip` (begins capture)
4. Re-launch RWfit via `adb shell monkey -p com.rw.revivalfit -c android.intent.category.LAUNCHER 1` (P62)
5. **NOW** the operator walks ~500 steps in a known window
6. The fresh BLE attach + 0x09 begin-sync + per-record streaming all happen over BLE
7. After the walk, tap Sync in RWfit, then `adb bugreport` again to close the capture

This will produce a snoop where:
- The 0xA3 bulk blocks (if any) include walk-window data
- The cmd 0x09 envelopes cover the post-walk period with `tail` sequences
- We can correlate `tail` deltas with RWfit's UI numbers for the same time window

If a third capture still doesn't lock the mapping, **pivot to Gadgetbridge** (Path D) per P47 — at that point the protocol RE thread has exhausted its free options.

## Decision rule

The "can we decode the SR16 field semantics?" question now has a clearer path: extend parser (DONE), re-capture during known activity (operator-dependent, ~15 min). Don't burn more sessions on old-snoop archaeology — the Jul-14 snoop's coverage of the walk window is structural, not a capture bug.
