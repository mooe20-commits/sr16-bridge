# sr16-bridge Handoff — session 11 (2026-07-08 evening)

Read this first when picking up sr16-bridge in a new session.

## TL;DR — session 11 status

**Path A (protocol rewrite) is complete.** `protocol.py` now models the
actual 0xAB protocol observed in the snoop — packet builders, per-cmd
constants, parsers, per-record dedupe for 0xA3 retransmits, plus an
offline replay harness and a connect-scaffold waiting on Path B.

**Correction to session 10 handoff:** the "0xA3 retransmits are
byte-identical" claim was wrong. Only the 9 hourly records (records
1-9) are byte-identical across the 4 retransmits; the day-summary
record (record 0, marker `0xE131`) varies in `val16` (advances with
wall-clock time). Data12 of the summary IS identical across retransmits.
So dedupe is per-record, not per-packet. This is now tested.

## What we shipped this session

| File | Status | Purpose |
|---|---|---|
| `src/sr16_bridge/protocol.py` | rewritten | 0xAB model: builders, parsers, dedupe, 11 cmd constants |
| `src/sr16_bridge/history_sync_offline.py` | new | Replay captured notifies as a SyncReport (no ring needed) |
| `src/sr16_bridge/connect_pull.py` | new | Scaffold for live BLE pull, ready for Path B UUID |
| `tests/test_protocol_0ab.py` | new | 22 tests for the new protocol layer |
| `HANDOFF-session-11.md` | new | This file |

Total: +830 lines, 0 new external deps (uses `pytest` for tests only).

## Protocol.py rewrite — what changed

### Wire format (unchanged from session 10)
```
ab <dir=0x01|0x11> <type=0x00> <cmd> [5B seg hdr] [N x 8B or 16B records]
```

### Phone→ring writes — now fully decoded (was unknown)
The 78 phone→ring writes in the snoop break down as:

| Cmd | Count | sub_type → triggers ring notify |
|---|---|---|
| 0x03 (echo/ack) | 75 | 0x1A → 0x43/0xA3 (16B blocks)<br>0x17 → 0x6B/0x73 (8B blocks)<br>0x63 → 0x67 (byte grid)<br>0x02-0x0D → per-hour 16B blocks |
| 0x04 (status query) | 1 | 0x0E → battery/version? |
| 0x05 (config) | 1 | 0x02 |
| 0x09 (begin sync) | 1 | one-shot start marker |

The phone builds a fetch request as:
```
ab 01 00 03 <frame_seq LE u16> <category=0x05> <sub_type> <status_flag>
```
Frame_seq increments per retry (status_flag flips 0x10→0x30). Ring
matches the seq in its response segment header.

The 0x09 begin-sync packet was reverse-engineered too:
`ab 01 00 09 57 38 02 01 00 1a 07 08 0c 32 06` (15B).
The 5B tail is 5B BCD of (year%100, month, day, hour, min) — wait, only
5 fields. Let me re-check: `1a 07 08 0c 32 06` is 6 bytes. Re-counting:
`ab 01 00 09` (4) + `57 38 02 01 00` (5B seg header) + `1a 07 08 0c 32 06`
(6B BCD) = 15B. Yes 6 BCD fields: yy/mm/dd/hh/mm/ss.

### 0xA3 per-record dedupe (CORRECTION)

**Old claim (session 10):** 0xA3 retransmits are byte-identical except frame_seq.

**Reality:** The 4 0xA3 packets in the snoop share **identical records
1-9** (the hourly records), but record 0 (the day-summary with marker
`0xE131`) has a different `val16` in each — 0xFE03, 0x0804, 0xF104,
0x8306. The data12 IS identical across all four summaries.

So:
- Per-packet dedupe (`dedupe_retransmits`) keeps all 4 packets because
  the summary val16 differs → use this only when you need to track
  wall-clock changes in the summary.
- Per-record dedupe (`merge_fetches` + `parse_fetch`) keeps the LATEST
  summary by val16, and unions the regular records. → use this for
  extracting "the day's data."

Both are now implemented and tested.

### 0x6B / 0x73 — older-day blocks with 8B records

13 records per 0x6B packet, 14 per 0x73. Records 0-11 are regular
(0xE031); record 12 is the day-summary (0xE131). The summary's
4B data tail is LE u32 — note this is a single u32, not 2×u16.

### 0xA3 metrics — STILL UNCONFIRMED

The 12B data tail of 0xA3 records is 6 × u16. Session 10 guessed
`[reserved, hr_avg, hr_min, hr_max, steps, calories]` — not yet
validated. The protocol layer exposes `A3_METRIC_*` constants for the
guessed layout so callers can reference by name, but the values will
need to be cross-checked against a second capture during known
activity (e.g. operator does a 5-min run before triggering sync).

## Files

### `src/sr16_bridge/protocol.py`
- `make_write_packet(cmd, sub_data)` — emits `ab 01 00 <cmd> [data]`
- `make_fetch_request(sub_type, frame_seq, status_flag)` — the 0x03
  fetch with sub_type selecting the metric family
- `make_begin_sync(now)` — the 0x09 start-of-sync marker
- `make_status_query(frame_seq)` — the 0x04 query
- `parse_notify(value)` — wraps `decode_0ab.decode_notify`
- `parse_fetch(decoded)` — splits records into regular vs day-summary,
  dedupes regular by `(val16, data)`, picks the latest summary
- `merge_fetches(fetches)` — same, but across multiple retransmits
- `dedupe_retransmits(decoded)` — per-packet body-key dedupe (frame_seq
  ignored)
- `parse_device_info(value)`, `parse_byte_grid(value)` — typed wrappers
- `record_u16_metrics(r)`, `record_u32_metric(r)` — metric extractors

Constants exported:
- All 11 cmds as `CMD_*`
- Sub-types as `SUB_*` (16B, 8B, byte_grid, plus per-hour 0x02-0x0D)
- Status flags as `STATUS_FLAG_INITIAL=0x10`, `STATUS_FLAG_RETRY=0x30`
- Markers `MARKER_REGULAR=0xE031`, `MARKER_DAY_SUMMARY=0xE131`
- `SLOT_SECONDS = 4110` (the vendor's slot duration)
- GATT handles `UART_WRITE_HANDLE=0x003E`, `UART_NOTIFY_HANDLE=0x0040`
- PLACEHOLDER `UART_SERVICE_UUID` etc. — replace once Path B delivers
  the 128-bit UUID

### `src/sr16_bridge/history_sync_offline.py`
- `replay_capture(packets_path)` → `SyncReport`
- `print_report(report)` / `--json` CLI
- Loads `~/health/sr16_captures/packets_20260708T100733Z.json` by default
- Demos: dedupe, day-summary extraction, byte grid, device serial
- Output is the canonical "what the protocol layer sees" view

### `src/sr16_bridge/connect_pull.py`
- Scaffold for live BLE pull using the new protocol layer
- `find_sr16(scan_seconds)` — same scanner pattern as before
- `sync_today_block(client, sub_type)` — fetch + collect + merge
- Refuses to run unless `UART_SERVICE_UUID` is replaced (the UUID_KNOWN
  flag). Pass `--force` to run anyway.
- Documents what Path B needs to fill in (see below)

### `tests/test_protocol_0ab.py`
22 tests, all passing:
- Packet builder format tests (5)
- Notify parsing on real snoop data (5)
- 0xA3 per-record dedupe (2)
- 0x43 per-packet dedupe (2)
- High-level helpers (4)
- Constants + edge cases (4)

Total test count: 27 (5 from session 10 + 22 new). All green.

## What session 12 should pick up

### Path B (you, on the phone) — to unblock live ring pull
1. Take a fresh `adb bugreport` capturing the START of a connection
   (not the middle of a sync). This catches the service-discovery phase.
2. Run `decode_snoop.py` on the new btsnoop_hci.log
3. Find the service UUID owning char handles 0x003e/0x0040
4. Update `UART_SERVICE_UUID` and `UART_TX_CHAR_UUID` in `protocol.py`
5. Bonus: do a 5-min run before triggering sync → cross-check the
   `A3_METRIC_*` guesses against known activity

### Path C (next session, with Path B's UUID) — wire it up
1. Replace `UART_SERVICE_UUID` placeholder
2. Run `connect_pull.py --scan 30` against the real ring
3. The output should match what `history_sync_offline.py` shows on the
   captured snoop (same day, same records)
4. Once that works, the existing `hr_live.py` can be ported to read
   0xAB-notify data instead of 0x180D HR — and we get access to HR
   avg/min/max per hour, plus steps, calories, distance, instead of
   just raw BPM

### Path D (lower priority) — the 0x67 byte grid semantics
The 100B grid with values {0, 1, 2} is hypothesized to be a wear
bitmap (0=off-finger, 1=on-finger, 2=charging?). Needs a side-channel
to validate (e.g. correlate with timestamps where the ring was
charging per RWfit logs).

## What's NOT done / open blockers

- ❌ `UART_SERVICE_UUID` real value (Path B)
- ❌ `UART_TX_CHAR_UUID` / `UART_RX_CHAR_UUID` real values (Path B)
- ❌ 0xA3 6×u16 metric semantics (needs corroborating capture)
- ❌ 0x67 byte grid value semantics (off-finger vs charging vs other)
- ❌ Live ring pull end-to-end (connect_pull scaffold ready)
- ❌ Port `hr_live.py` from 0x180D to 0xAB (unlocks aggregated metrics)
- ❌ Launchd plist for hr_live daemon
- ❌ DB schema for non-BPM metrics (steps, cal, dist per hour)
- ❌ `history_sync.py` rewrite — the old Colmi R02 version is still in
  the tree. Marked for deletion in session 12 once `connect_pull`
  validates against the live ring.

## Pitfalls captured this session

### 35. 0xA3 retransmits are NOT byte-identical (2026-07-08)
Only records 1-9 (regular hourly) are byte-identical across
retransmits. Record 0 (day-summary, marker 0xE131) varies in `val16`
(advances with wall-clock time) — the data12 is identical though.
Decoders must dedupe per-record, not per-packet. `parse_fetch` /
`merge_fetches` in protocol.py handle this correctly.

### 36. Phone fetch uses cmd 0x03 + sub_type, not a dedicated opcode (2026-07-08)
The vendor has no dedicated "send me data" opcode. The phone sends
`ab 01 00 03 <frame_seq LE> <category=0x05> <sub_type> <status_flag>`
where `sub_type` picks which block the ring returns. 11 distinct
sub_types observed (0x1A, 0x17, 0x63, 0x02-0x0D). Status_flag flips
0x10→0x30 on retries.

### 37. 0x09 begin-sync payload is 6B BCD, not 6B BCD + extra (2026-07-08)
The 0x09 packet is 15B total: 4B transport + cmd + 6B seg header +
6B BCD `(yy, mm, dd, hh, mm, ss)`. Earlier writeup guessed the seg
header was 5B (matching bulk records); it is actually 6B for 0x09.