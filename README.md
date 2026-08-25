# sr16-bridge

Local BLE bridge from the SR16 smart ring to a private SQLite store. All analysis runs locally on this Mac — no cloud, no phone, no vendor app.

**Current status: active development — live-HR ingestion is implemented; BLE interoperability and end-to-end validation are ongoing.**

See `HANDOFF-2026-07-07.md` for the full session log and the single acceptance test that unblocks session 2.

## Layout

```
src/sr16_bridge/
  __init__.py       package marker
  schema.sql        SQLite DDL (hr_readings, char_inventory, ble_sessions)
  enumerate.py      one-shot GATT enumeration → DB + sys/PROBE-LOG.md
  hr_live.py        subscribe to BT SIG Heart Rate notifications → SQLite (v1.0)
sys/
  PROBE-LOG.md      every BLE scan + GATT run, timestamped (append-only)
  HID-CODES.md      (empty — for future vendor-protocol opcodes)
HANDOFF-2026-07-07.md
.venv/              Python 3.11 venv with bleak + pyobjc-CoreBluetooth
~/health/sr16.db    SQLite WAL store (lives OUTSIDE project tree)
```

## Quick start

```bash
cd ~/projects/sr16-bridge
PYTHONPATH=src .venv/bin/python -m sr16_bridge.hr_live --duration 60
sqlite3 -header -column ~/health/sr16.db "SELECT * FROM hr_readings LIMIT 5;"
```

Requires: ring awake + unpaired + within ~5m of Mac. If `hr_live` says "SR16 not found", **tap the ring** to wake its Telink radio and retry — Telink rings sleep hard between advertisements.
