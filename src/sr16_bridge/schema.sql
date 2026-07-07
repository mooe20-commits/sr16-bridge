-- SR16 bridge SQLite schema. Reference doc, kept in sync via `init_db.py`.
-- Goals: queryable (HR + sessions + history), append-only at the row level.
-- One opt-in destructive hatch: drop_stats() in stats.py.

PRAGMA journal_mode = WAL;       -- concurrent reads while a writer is active
PRAGMA synchronous  = NORMAL;     -- safe with WAL on macOS
PRAGMA foreign_keys = ON;

-- Heart Rate Service measurements (standard org.bluetooth.characteristic.heart_rate_measurement, 0x2A37).
-- RR intervals are sub-second beat-to-beat intervals in unit 1/1024 s, comma-joined text.
-- sensor_contact + energy_expended come straight from the GATT byte — see HR spec §3.108.
CREATE TABLE IF NOT EXISTS hr_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,         -- ISO8601, monotonic per device
    device_uuid     TEXT    NOT NULL,         -- '36BE6673-1486-2E90-38E9-3E097DB4CC43'
    bpm             INTEGER NOT NULL,         -- 0x2A37 byte[1]
    rr_intervals    TEXT,                     -- comma-joined RR samples (1/1024 s units)
    sensor_contact  INTEGER,                  -- 0x2A37 byte[2] bits 0-1 (0=unknown,1=not detected,2=ok)
    energy_expended INTEGER,                  -- optional flag bit + 2-byte value
    raw_hex         TEXT,                     -- full notify payload (for offline RE)
    ingested_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS hr_readings_ts ON hr_readings(ts_utc);
CREATE INDEX IF NOT EXISTS hr_readings_device_ts ON hr_readings(device_uuid, ts_utc);

-- GATT characteristic inventory: a snapshot of everything we saw on the ring at enumerate-time.
-- Used as the source of truth for "what services does the SR16 expose?" so subsequent
-- sessions don't have to re-enumerate.
CREATE TABLE IF NOT EXISTS char_inventory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,
    device_uuid     TEXT    NOT NULL,
    service_uuid    TEXT    NOT NULL,
    char_uuid       TEXT    NOT NULL,
    properties      TEXT    NOT NULL,         -- 'read,notify,write' comma-joined
    has_descriptor  INTEGER NOT NULL DEFAULT 0,
    probe_notes     TEXT
);
CREATE INDEX IF NOT EXISTS char_inventory_unique ON char_inventory(device_uuid, service_uuid, char_uuid);

-- One row per BLE session — connects/disconnects. Diagnostic for "ring dropped at HH:MM" later.
CREATE TABLE IF NOT EXISTS ble_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_uuid     TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    reason          TEXT,
    bytes_received  INTEGER NOT NULL DEFAULT 0
);
