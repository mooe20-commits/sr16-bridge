# SR16 Handoff — 2026-07-13 night session (session 15)

## TL;DR

End-to-end capture pipeline works. We have fresh data from RWfit + a 6.7 MB
snoop ingested into `~/health/sr16.db` (81 new rows, 94 total today). Field
semantics still NOT locked — `u[1]..u[5]` from `0xA3` bulk records do NOT
map cleanly to RWfit's 4647 steps / 3.79 km / 166 kcal / 30-min HR values
at any consistent scale. New bug: ingest stores val16/3600 as `hour_local`
but val16 is in some ring-internal time, not UTC nor local. Cross-comparison
is off by 3 hours. Status responses (cmd 0x04/0x05/0x06) are silently
dropped by the decode pipeline — likely where live HR / live steps live.

## What works

```text
adb kill-server && adb start-server    # RF8M31H582A visible
adb shell monkey -p com.rw.revivalfit -c android.intent.category.LAUNCHER 1
adb shell screencap -p /sdcard/x.png && adb pull /sdcard/x.png /tmp/x.png
adb shell input tap X Y
adb shell input keyevent 4               # back button
adb shell input swipe X1 Y1 X2 Y2 600   # scroll
adb bugreport /Users/mih/health/sr16_captures/sr16_rwfit_<ts>.zip
```

Direct UI reads from RWfit:
- Home tab → Activity widget (4647 / 3.79 / 166.0)
- Home tab → HR widget (Min 63, Avg 76, Max 93)
- Home tab → Sleep widget (84 score, 23:05 → 05:51, 6h 46m)
- Home tab → HRV (30 ms Normal), Blood Oxygen (98%), Stress (41)
- Activity tab → Total steps (4647), distance (3.79 km), calories (166.0 kcal)
- Activity tab → Hourly steps bar chart (peaks 745/705/700 at hours 7-8, 12-13)
- HR detail page → "Detailed data" sub-page with 30-min granularity
  (24 readings spanning 07:00 → 21:00 local time)

## Ground truth captured (2026-07-13 21:09 EEST)

### HR Detailed (RWfit, 30-min granularity, local EEST time)
```text
07:00=78  07:30=70  08:00=83  08:30=78
09:30=79  10:00=78  10:30=78  11:00=65
12:00=80  13:00=67  13:30=93
15:00=79  15:30=81  16:00=79  16:30=81
17:00=89  17:30=76  18:00=77  18:30=75
19:00=63  19:30=74  20:00=73  20:30=72  21:00=69
min=63 max=93 avg=76
```

### Activity (RWfit, daily totals + hourly bar heights)
```text
Total: 4647 steps / 3.79 km / 166.0 kcal
Hourly bars:  0h~25  6h~150  7h~745  8h~705  9h~140  10h~50
              12h~290  13h~700  15h~30  16h~460  17h~300
              18h~225  19h~450  20h~180  21h~280
```

### Capture artifacts
```text
~/health/sr16_captures/sr16_rwfit_20260713_204922.zip       # 60 MB bugreport
~/health/sr16_captures/sr16_rwfit_20260713_204922_extracted/
   FS/data/log/bt/btsnoop_hci.log                          # 6.7 MB snoop
~/health/sr16.db                                          # 94 rows today
```

## What was discovered (new ground truth)

### 1. Third marker variant — `0xE831`

```text
16B markers in this snoop:
  0xE631 = 3  (in-progress hour header on firmware-with-display)
  0xE731 = 95 (regular hourly record)
  0xE831 = 3  (NEW — third firmware variant)
```

The skill file mentioned only `0xE631` and `0xE731`. There's a third
high-byte variant. Update `sr16-0ab-protocol-decoded/SKILL.md` to reflect.

### 2. 30-min granularity (matches RWfit's "every 30 minutes" setting)

```text
RWfit HR detail page → "Activate the scheduled heart rate monitoring
function to automatically measure every 30 minutes"

Each "hour" in cmd 0xA3 contains 6-8 sub-samples covering ~30-40 min
of wall-clock time. These ARE the 30-min measurements, NOT retransmits
of identical data.
```

This is the key insight — the decode loop's "retransmit dedupe" was
erasing the real per-30-min data.

### 3. Timezone bug in `ingest_snoop_to_db.py`

```text
hour_local = val16 // 3600

But val16 is NOT necessarily UTC. Probably ring-internal time.
Empirical: DB hour_local=7 should map to RWfit local 10:00
(because operator's HR detail at 10:00 local is val16=25495,
DB hour_local=7 = val16/3600 = 7).
```

Operator is in EEST (UTC+3). Cross-comparison is consistently off by
3 hours. Need to either:
  - (a) Switch DB to UTC, convert to local at query time, or
  - (b) Pass operator's TZ offset into ingest, store local directly.

### 4. Cmd 0x04/0x05/0x06 status responses — silently dropped

```text
cmd distribution from this snoop:
  03: 918   (write-ack + fetch trigger)
  04: 143   (status response — currently dropped)
  05:  94   (status response — currently dropped)
  06:  81   (status response — currently dropped)
  09:  50   (begin-sync)
  13: 136   (device info + bulk smuggling)
  23, 33, 43, 4b, 53, 5b, 63, 67, 6b, 73, 7b, 83, 8b, 93, 9b, a3
```

Cmd 0x06 last 2 bytes contain a counter: `0x0ffc..0x106c`,
decreasing by 7 (0x07) per emission. 81 records total. **This may be
live HR or live steps.** NOT captured by `ingest_snoop_to_db.py`.

Cmd 0x04 last byte: small payload (1-2 bytes). 143 records.

Cmd 0x05 last 2 bytes: small payload. 94 records.

### 5. Cmd 0x13 contains bulk records in disguise

```text
ab11001396dc051a1031e765900000005d000081d6000b9460
        └─frame─┘cat|sub|status ┌──────── payload ────────┐
                                 ↑ contains "31 e7" = 0xE731 marker

This is a 16B bulk record (0xE731 marker) smuggled inside a 0x13
"device info" packet. The current decoder only handles 0xE731 inside
0xA3 cmd, so these embedded records are silently dropped.
```

### 6. `decode_0ab` `DecodedNotify` API is incomplete

```text
Current attrs: cmd, duration_seconds, is_bulk, is_byte_grid, raw_data,
               record_size, records, segment, transport

Missing:
  - body (the bytes between segment header and any records)
  - For status responses (0x04/0x05/0x06), the payload bytes are lost
```

### 7. Sub-hour dedupe theory confirmed wrong

The session-11 P34 / session-14 P54 retransmit-dedupe theory was
**partially wrong** for the firmware-with-display variant:

```text
0xA3 retransmits:
  - Original P34 claim: all retransmits byte-identical
  - Session 14: day-summary record val16 ADVANCES across retransmits
  - THIS session: per-30-min sub-samples all have DIFFERENT val16s
    (covering 6-8 distinct 30-min windows in each "hour")

Conclusion: do NOT per-record-dedupe by (marker, val16, data) for 0xA3
on this firmware. Bucket to 30-min boundaries (val16 // 1800) instead.
```

## What was attempted and didn't work

1. **Trying to derive field semantics from existing data** — failed
   because the val16 → local-time mapping is broken (TZ bug). The
   ratios between consecutive HR readings and DB u[3] values were
   wildly inconsistent (306x to 605x for what should be the same
   physical HR value).

2. **"Pick the row with val16 closest to hour boundary"** — wrong
   because val16 is not aligned to hour boundaries (it's aligned to
   30-min boundaries but offset by ~3 hours for TZ).

## What's needed next (in priority order)

### Step A — Fix the decode pipeline (30 min code + 5 min operator)

1. Add a debug printer for cmd 0x04/0x05/0x06/0x13 payloads that
   includes timestamp + segment header + raw bytes + ASCII dump.
2. Modify `ingest_snoop_to_db.py` to store `hour_utc` (computed
   properly from val16+offset) and a separate `hour_local` via TZ.
3. Add a parser branch for cmd 0x13 that detects embedded "31 e7"
   inside the body and decodes it as a regular 16B record.
4. Add a parser branch for cmd 0x06 that stores the last 2 bytes
   as a `live_value` field with timestamp.

### Step B — Known-activity walk (20 min operator + 10 min code)

1. Operator taps real-time HR measurement in RWfit, screenshots
   current HR (already have: 69 bpm).
2. Operator walks exactly 1000 steps (counted manually or with a
   pedometer app, NOT relying on the ring).
3. Operator taps real-time HR again, screenshots new HR.
4. Sync once.
5. Take a fresh snoop, ingest with the new parser.
6. Look for which u16 incremented by exactly 1000 (×1 or ×10 or
   ×100 — whichever fits).

### Step C — Lock the field mapping (10 min code)

After step A+B, the schema can be updated:
- `steps_raw` confirmed: rename to `steps_count` with scale factor
- `cal_raw` → `cal_kcal` (probably ×0.1)
- `dist_raw` → `dist_m` (probably ×1)
- `hr_agg_raw` → `hr_avg_bpm` (probably ×1)
- `intensity_raw` → `intensity_units` (TBD — may not be useful)

Update `ingest_snoop_to_db.py`, `analyze_rollup.py`, and the
skill file's "6×u16 metric mapping" section.

### Step D — Add live-HR path

If step A reveals that cmd 0x06 carries live HR, expose it as a
real-time query endpoint:
```bash
PYTHONPATH=src .venv/bin/python -m sr16_bridge.live_query --cmd 0x06
```

This is optional — only useful if operator wants live monitoring.

## Files in repo

```text
src/sr16_bridge/
  ingest_snoop_to_db.py          # main ingester (has TZ bug + drops cmd 0x04/05/06/13)
  decode_0ab.py                  # 0xAB protocol parser (missing body attr)
  protocol.py                    # higher-level packet builders/parsers
  hr_live_0ab.py                 # BLE live daemon (NOT loaded — see session-14)
  schema.sql                     # a3_hourly schema (current: reserved_u16_0 etc)

scripts/
  analyze_rollup.py              # offline analysis, broken due to TZ bug

~/health/sr16.db                 # 94 rows for 2026-07-13
~/health/sr16_captures/
  sr16_rwfit_20260713_204922.zip       # THIS session's capture
  sr16_rwfit_20260713_204922_extracted/
    FS/data/log/bt/btsnoop_hci.log    # 6.7 MB

~/projects/sr16-bridge/
  HANDOFF-2026-07-13-night.md    # THIS file
  HANDOFF-2026-07-13.md          # session-14 closeout
  HANDOFF-session-{10,11,12,12.5,13}.md  # historical
```

## Pickup-point snippet

When picking up this project in a new session, run these in order:

```bash
# 1. Verify environment
cd ~/projects/sr16-bridge
git status --short && git log -1 --date=iso --format='%h %ad %s'
.venv/bin/python -m pytest -q    # expect 33 passed

# 2. Verify state files are present
ls HANDOFF-2026-07-13-night.md HANDOFF-2026-07-13.md
ls -la ~/health/sr16.db
ls ~/health/sr16_captures/sr16_rwfit_20260713_204922.zip

# 3. Verify phone is connected
adb kill-server && adb start-server
adb devices                       # expect RF8M31H582A
adb shell dumpsys bluetooth_manager | grep "38:00"   # ring connected

# 4. Read this handoff IN FULL before doing anything else

# 5. Implement Step A (TZ fix + status response parser + cmd 0x13 smuggling)
#    - Tests: pytest stays green
#    - Re-ingest the snoop, verify hour_local matches RWfit (off by 0, not 3)
#    - Verify cmd 0x06 live_value column populates with the 0x0ffc..0x106c counter
```

## Decision rule (re-stated)

The core question "can we decode the SR16 field semantics?" still has
unknown answer. The next session's job is to answer it YES or NO
definitively. If yes (Step B locks the mapping), ship a clean local
analyzer. If no (the data is corrupt or non-standard), accept that
custom decode won't work and either:

  - Use RWfit's in-app export (if available without login), or
  - Recommend a different ring brand with Gadgetbridge support, or
  - Drop the project.

The 30-min granularity and the cmd 0x13 record-smuggling discoveries
make me optimistic that one good known-activity walk + Step A fixes
will lock the mapping. But it's an experiment, not a guarantee.