# SR16 Bridge — Changelog

All notable changes, newest first.

---

## [session-16b] — 2026-07-14 (afternoon)

### Step B attempt — known-activity walk + field mapping

**Walk protocol:** baseline HR 65 bpm → walked 200 steps (Samsung Health counted 515, operator estimate ~400-500) → after-walk HR 62 bpm → sync done. Captured 4.9 MB snoop via `adb bugreport`.

**Pipeline:** new 0xE831 marker variant — all 16 records from the fresh snoop carry marker 0xE831 (not 0xE731 from the Jul-13 snoop). 7 0xA3 fetches in the snoop, all returning val16_range=169..61594.

**Result: field mapping NOT locked.** None of the 6 u16 fields incremented by a clean ×1/×10/×100 of 500 between pre-walk and post-walk records. The walk happened in the val16=41044→57484 window, but those records are all zeros (sleep). The first non-zero post-walk record is val16=57484 (15:58 UTC = 17:58 EEST) with steps_raw=13312, cal_raw=0, dist_raw=32889, hr_agg=38472, intensity=1536.

**Honest assessment:** the 0xE831 records appear to encode something other than standard step/calorie/distance/HR aggregates. The scale and pattern don't match RWfit's daily totals (4647 steps / 3.79 km / 166 kcal). Possibilities:
- Different firmware's metric encoding (decimeters vs meters, raw sensor ticks vs aggregated counts)
- Different time-bucket semantics (5-min active windows vs hourly rollups)
- The 0xA3 fetch in the fresh snoop returned pre-sync state, not post-sync state

**See HANDOFF-session-16.md for full analysis and next-step options.**

---

## [session-16] — 2026-07-14 (morning)

### Step A: Decode pipeline hardening (handoff-2026-07-13-night)

**Fixed: TZ bug in ingest_snoop_to_db.py.**
Old code used `snoop_mtime` as the anchor date and `val16 // 3600` as
the hour, ignoring that val16 isn't UTC-aligned. Cross-comparison with
RWfit's HR detail page showed a consistent 3-hour offset for the
operator (EEST, UTC+3). New code anchors on the **first cmd 0x09
begin-sync timestamp** from the snoop (BCD-parsed from the body),
falls back to snoop mtime if absent, and stores both `hour_utc` and
`hour_local` separately via a `--tz-offset N` flag.

**Added: `status_events` table + ingestion.**
cmd 0x04 / 0x05 / 0x06 status responses and cmd 0x13 device-info
notifies were silently dropped. New table captures every notify with:
- cmd, frame_seq, category, sub_type, status_flag
- body_hex (full payload)
- payload_hex (last 1-2 bytes — the "live value")
- payload_u16 (LE-decoded last 2 bytes)
- snoop_file (forensics)

cmd 0x06's payload_u16 column ranges 784..64527 in the Jul-13 snoop —
the live counter that may be live HR or live steps.

**Added: cmd 0x13 record smuggling parser.**
cmd 0x13 device-info bodies can carry one or more 16B bulk records
with embedded 0xE031/0xE631/0xE731/0xE831 markers — these were
silently dropped before. `extract_smuggled_records()` rescans the body
and pulls them out; the Jul-13 snoop yielded 6 smuggled records
(mostly 0xE831, the new third marker variant).

**Schema changes:**
- `a3_hourly.hour_utc INTEGER NOT NULL DEFAULT 0` (added via ALTER TABLE
  for the existing DB; CREATE TABLE updated for fresh inits).
- New `status_events` table + 2 indexes.

**decode_0ab.py changes:**
- `DecodedNotify` now has a `body: bytes` attribute (the bytes between
  the segment header and the records / raw_data). Needed for status
  responses where the payload is not record-structured.
- New properties: `is_status`, `is_device_info`.

**protocol.py additions:**
- `StatusResponse` dataclass + `parse_status_response()` helper.
- `extract_smuggled_records()` for cmd 0x13 smuggling.

**Tests:** 33 → 39 (+6 new tests for body attr, status responses,
smuggled records, payload_u16 extraction).

**Files changed:**
- src/sr16_bridge/decode_0ab.py
- src/sr16_bridge/protocol.py
- src/sr16_bridge/ingest_snoop_to_db.py
- src/sr16_bridge/schema.sql
- tests/test_protocol_0ab.py

**Cross-validation (handoff prediction):**
val16=25495 should map to RWfit 10:00 local (UTC+3) = hour_utc=7.
After fix: `hour_local=10, hour_utc=7` ✓.

---

## [session-15] — 2026-07-13

See HANDOFF-2026-07-13-night.md.