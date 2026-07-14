# SR16 Bridge — Changelog

All notable changes, newest first.

---

## [session-17] — 2026-07-14 (afternoon)

### Major protocol discovery: cmd 0x09 envelopes (per-record streaming)

**Old assumption:** ring streams bulk data via cmd 0xA3/0x43/0x6B/0x73 blocks (each packet carrying 5-14 records).

**Reality (this session):** the Jul-14 snoop contains **zero** cmd 0xA3/0x43/0x6B/0x73 notifies. The ring streams ONE record per packet inside a **cmd 0x09 envelope** (~15B total: `ab 11 00 09 <frame_seq> <cat=0x05> <sub_type> <status=0x10> <body=6B>`). The 6-byte body = `marker(2) + val16(2) + tail(2)`.

**Bucket cadence:** val16 advances 256 sec per record = **4-minute buckets** (NOT hourly). Explains the "irregular 300-2500 sec deltas" from P66.

**Walk-window data:** 15 cmd 0x09 envelopes with val16 41040-46680 (walk was 11:24-12:58 UTC = val16 41040-46680). BUT — only the 2-byte `tail` field carries data (values 87→62 decreasing monotonically — a sequence counter, not a metric). The 0x09 envelopes' "data" is just a remaining-record counter, NOT steps/cal/dist/HR.

**Code changes:**
- `decode_0ab.py`: new `is_record_envelope` property + cmd 0x09 branch in `decode_notify()` builds a 12B-padded Record from the 6-byte body. `tail` is the only meaningful field; remaining 10B is zero-padded to keep 16B-record shape.
- `ingest_snoop_to_db.py`: added `0x09` to `is_bulk_cmd()` so 0x09 envelopes route to the bulk-records path; their single Record then gets ingested to `a3_hourly` with `tail` → `reserved_u16_0`.
- `tests/test_protocol_0ab.py`: +3 tests covering single-envelope parse, short-body graceful-degrade, and round-trip of all 45 real Jul-14 envelopes. Test count: 39 → 42.

**Result of the field-mapping question:** walk data IS in the snoop, but only as 0x09 envelopes with sequence-counter `tail` values. The actual metric values for the walk window (val16 41040-46680) are NOT in the ring's BLE-visible buffer — they live on the ring but don't reach BLE during this sync. The 0xA3 bulk blocks at val16 57484-61594 (16:00-17:00 UTC) DO have full data, but that's the post-sync state.

**Next-session pickup:** fresh capture during a known activity (P65 Option B pattern) is still the path to lock field semantics. Re-attach capture so the ring's BLE buffer gets re-populated mid-walk.

**Pitfall added (P66b in sr16-ring-mac-pitfalls):** documents the cmd 0x09 envelope structure + the fact that 0x09 envelopes carry only sequence-counter data, not metrics.

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