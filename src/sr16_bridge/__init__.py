"""sr16-bridge: SR16 smart ring → local SQLite → Hermes gateway.

Layout
------
sys/PROBE-LOG.md   every BLE scan, GATT dump, and analyze run, timestamped
schema.sql         authoritative SQLite DDL (hr_readings + char_inventory + ble_sessions + analysis_runs + gateway_state)
probe.py           30s BLE advertisement scan
enumerate.py       GATT service+characteristic inventory (writes to PROBE-LOG.md)
hr_live.py         Heart-Rate-Service notifications → SQLite (v1.0; live-verification blocked)
history_sync.py    vendor 0xA00A protocol → SQLite (planned, vendor RE needed)
analyze.py         gateway: SQLite → local Ollama → SQLite (analysis_runs) — verified with synthetic data 2026-07-07
"""
__version__ = "0.3.0"
