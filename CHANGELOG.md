# SR16 Bridge — Changelog

All notable changes, newest first.

---

## [session-16] — 2026-07-14

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