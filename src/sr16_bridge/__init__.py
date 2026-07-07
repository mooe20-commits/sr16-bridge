"""sr16-bridge: SR16 smart ring → local SQLite → Hermes gateway.

Layout
------
sys/PROBE-LOG.md   every BLE scan and GATT dump, timestamped
schema.sql         authoritative SQLite DDL (hr_readings + char_inventory + ble_sessions)
probe.py           30s BLE advertisement scan
enumerate.py       GATT service+characteristic inventory (writes to PROBE-LOG.md)
hr_live.py         Heart-Rate-Service notifications → SQLite (v1.0)
history_sync.py    vendor 0xA00A protocol → SQLite (v1.1, RE needed)
"""
__version__ = "0.1.0"
