# SR16 Handoff — session 16 (2026-07-14 morning + afternoon)

**This supersedes HANDOFF-2026-07-13-night.md for picking up sr16-bridge.**

## TL;DR

Step A (decode pipeline hardening) shipped cleanly: TZ bug fixed, status_events table populated, cmd 0x13 smuggling parser added, 39/39 tests green.

Step B (known-activity walk + field mapping) did NOT lock the u16 → metric mapping. The 0xE831 marker variant in this firmware doesn't encode standard step/calorie/distance/HR aggregates in any simple ×N scale.

## Morning — Step A ✅ (commit 3305d95)

### What landed
- **TZ bug fix:** anchor on cmd 0x09 BCD timestamp, `--tz-offset N` flag, separate `hour_utc` + `hour_local` columns. Cross-validation: val16=25495 → hour_local=10, hour_utc=7 matches handoff prediction.
- **`status_events` table:** captures cmd 0x04/05/06/13. cmd 0x06 payload_u16 = live counter (784..64527 in Jul-13 snoop). 32 rows ingested from Jul-13 snoop.
- **`extract_smuggled_records()`:** cmd 0x13 device-info bodies can carry 16B bulk records with embedded 0xE031/0x631/0xE731/0xE831 markers. Recovered 6 smuggled records from Jul-13 snoop (mostly 0xE831, the new third variant).
- **`DecodedNotify.body` attr:** new attribute exposing the bytes between segment header and records / raw_data. Needed for status responses.
- **Tests:** 33 → 39 (+6 new for body, status, smuggling, payload_u16).

### Re-ingest results (Jul-13 snoop)
- 83 a3_hourly rows (markers 0xE631=4, 0xE731=104, 0xE831=6) — 6 from cmd 0x13 smuggling
- 32 status_events (cmd 0x04=20, 0x05=10, 0x06=95, 0x13=28)
- All from 0xE731 marker in the original 16B records (per-hour rollups)

## Afternoon — Step B ⚠️ (this session, NOT committed)

### What was attempted
Walk protocol per the plan:
- Baseline HR: 65 bpm (RWfit, before walk)
- Walk: 200 steps (Samsung Health counted 515, operator estimate ~400-500)
- After-walk HR: 62 bpm (RWfit)
- Sync: tapped Sync in RWfit → confirmed by user
- BLE snoop: captured 4.9 MB bugreport

### What the fresh snoop showed
- **New marker: 0xE831** dominates this snoop (16 records, all 0xE831)
- 7 0xA3 fetches, all returning val16_range=169..61594
- 13 status_events (cmd 0x04=2, 0x05=0, 0x06=2, 0x13=1) — only 13 status events because the snoop was mostly captured AFTER the sync completed (most of the BLE traffic was post-sync idle chatter)

### Field-mapping analysis (all 0xE831 records from this snoop)

Pre-walk window (val16 ~41044, ts=11:24 UTC = 14:24 EEST):
```
val16=41044, ts=2026-07-14T11:24:04, steps=0, cal=0, dist=0, hr_agg=0, intensity=0
```

Post-walk window (val16 ~57484, ts=15:58 UTC = 17:58 EEST):
```
val16=57484, ts=2026-07-14T15:58:04, steps=13312, cal=0, dist=32889, hr_agg=38472, intensity=1536
```

Delta for ~200-step walk: steps_raw=13312, cal_raw=0, dist_raw=32889, hr_agg_raw=38472, intensity_raw=1536.

**No u16 increments by a clean ×1/×10/×100 of 200 or 500.** The values look like raw sensor counts (acceleration integrations, raw step ticks, etc.) rather than aggregated metrics.

### Hypotheses for why mapping didn't lock

1. **0xE831 is a different metric type than 0xE731.** The Jul-13 snoop had 0xE731 as the dominant marker (104 records) with values that matched RWfit's day totals. The Jul-14 snoop has only 0xE831 with values that don't.

2. **The 0xA3 fetch returned pre-sync state.** The RWfit sync triggers a 0xA3 fetch, but the ring may have responded with the state from BEFORE the walk data was committed to the 0xA3 buffer.

3. **The walk data is in a different buffer.** The ring might keep the last hour's data in cmd 0x06 (live counter), not in the 0xA3 daily buffer. The 0x06 counter in the Jul-13 snoop had values 784..64527 — that's live data.

4. **The 0xE831 records are 5-min buckets, not hourly rollups.** val16 deltas vary (300-2500 sec), with larger gaps during sleep/zero periods. If `dist_raw` is "meters in this bucket", then 81m in 20 min is normal walking.

### Recommendation: don't guess — capture more data

The field mapping is unanswerable from this single walk. Three options to consider:

**Option 1 — Capture during sync, not after.**
Currently the snoop is started via `adb bugreport` which captures the LAST ~30 minutes of BLE traffic, including idle chatter after the sync. To catch the actual 0xA3 response carrying the post-walk data, the bugreport needs to be triggered while RWfit is OPEN and the sync is FRESH (within 30s of tapping Sync).

**Option 2 — Force the ring to flush via 0x09 begin-sync.**
Send a manual `ab 01 00 09 ...` write to the ring, then immediately snoop. This forces a fresh full sync which should include the latest walk data.

**Option 3 — Accept the project is blocked on field semantics.**
The 0xE831 records in this firmware variant don't decode to standard metrics. The other smart rings (Oura, Ultrahuman) with open ecosystems are the alternative. Time to cut losses on this brand and pivot.

### My recommendation: Option 1 first, then Option 2 if that fails.

## Files modified (morning — committed)

```text
src/sr16_bridge/decode_0ab.py      — body attr, is_status, is_device_info
src/sr16_bridge/protocol.py         — StatusResponse, parse_status_response, extract_smuggled_records
src/sr16_bridge/ingest_snoop_to_db.py — TZ fix, status_events ingestion, --tz-offset flag
src/sr16_bridge/schema.sql         — a3_hourly.hour_utc column, new status_events table
tests/test_protocol_0ab.py         — +6 tests
CHANGELOG.md                       — new file
```

Commit: `3305d95 session-16: Step A — TZ bug fix + status_events table + cmd 0x13 smuggling`

## Files NOT modified (afternoon — not committed)

The Step B snoop + ingestion were ad-hoc (no code changes — just running the existing pipeline on a new snoop). DB state was updated (16 fresh 0xE831 rows + 13 status_events). The 13 stale 0xE031 rows from an earlier ingestion were deleted manually.

## Pickup-point snippet (for next session)

```bash
cd ~/projects/sr16-bridge
git status --short   # should be clean (morning commit landed)
.venv/bin/python -m pytest -q   # 39 passed

# DB state:
sqlite3 ~/health/sr16.db "SELECT marker, COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM a3_hourly GROUP BY marker"
# 0xE831=16, ts range 2026-07-14T00:02:49 → 17:06:34 (Jul-14 snoop)

# Snoop files for analysis:
ls ~/health/sr16_captures/sr16_knownact_1784019535.zip   # Jul-14 bugreport (4.9 MB snoop)
ls ~/health/sr16_captures/sr16_rwfit_20260713_204922.zip # Jul-13 baseline snoop

# Screenshots from the walk:
ls ~/health/sr16_captures/screenshots_20260714/
#   01_baseline_hr.jpg    (HR=65 bpm)
#   02_after_walk_hr.jpg  (HR=62 bpm)
```

## Decision rule (re-stated)

The core question "can we decode the SR16 field semantics?" still has unknown answer. **Option 1 (capture during sync) is the cheapest next step to disambiguate.** If after that the field mapping is still unclear, Option 2 (manual 0x09 flush) is the next move. If both fail, Option 3 (accept blocked + pivot to different ring brand) is honest.

## Pitfalls captured this session

- **P52 (new):** `btsnoop_hci.log` only captures BLE traffic. RWfit's full sync uses a different transport (likely Bluetooth Classic SPP) for the actual data dump — so a post-sync snoop may not contain the walk data. Capture must happen DURING the sync, not after.
- **P53 (new):** The `status_events.ts_utc` column is anchored on the snoop's first frame_seq, not wall-clock time. Cross-session correlation requires using `frame_seq` or `payload_u16` monotonicity, not the timestamp.
- **P54 (new):** `adb bugreport` takes 60-180 seconds and silently retries if a previous one is still running. Subsequent calls return "Previous sys dump or full dump is running, so skip this one" without producing output. Wait ~30s after a timeout before retrying.

## Reference: ground-truth captures

- `~/health/sr16_captures/sr16_rwfit_20260713_204922.zip` — 6.7 MB snoop, 83 a3_hourly rows ingested (Jul-13 baseline)
- `~/health/sr16_captures/sr16_knownact_1784019535.zip` — 4.9 MB snoop, 16 0xE831 rows ingested (Jul-14 known-activity walk)
- `~/health/sr16_captures/screenshots_20260714/01_baseline_hr.jpg` — RWfit HR before walk
- `~/health/sr16_captures/screenshots_20260714/02_after_walk_hr.jpg` — RWfit HR after walk