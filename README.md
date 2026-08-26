# sr16-bridge

Local BLE bridge and protocol reverse-engineering toolkit for the SR16 smart ring (Telink chipset). Everything runs locally on a Mac — no cloud, no vendor app, no phone relay.

[![tests](https://img.shields.io/badge/tests-41%20passing-brightgreen)]() [![platform](https://img.shields.io/badge/platform-macOS-black)]() [![python](https://img.shields.io/badge/python-3.11-blue)]()

## What it does

- **Live heart rate** — subscribes to the standard BT SIG Heart Rate characteristic and streams readings into SQLite
- **History sync** — pulls the ring's on-device HR log via the vendor `0xAB` protocol (cmd `0x33`/`0x13`), decoding the vendor's proprietary packet format
- **Protocol decode** — full parser for the vendor's segmented notify stream (`decode_0ab.py`): reassembly, CRC, record extraction, status responses, and the per-record cmd `0x09` envelope
- **Snoop ingestion** — replays captured HCI snoop files into the same DB schema, so vendor-app traffic can be analyzed without the app running
- **Analysis** — rollup queries (daily totals, correlations), Ollama-powered summaries, Telegram digests

## The protocol work

The SR16 speaks a custom `0xAB` framed protocol over two BLE characteristics. This repo documents and implements:

| Piece | Detail |
|---|---|
| Frame | `AB 11 <len> <cmd> ...` segments, reassembled across notifies |
| History pull | cmd `0x33` request → `0x13` responses carrying smuggled records |
| Records | 16-byte HR records: timestamp + HR + reserved fields |
| Streaming | cmd `0x09` envelopes: **one record per packet**, 4-minute buckets |
| Quirks | TZ-offset encoding in timestamps; remaining-record counter in envelope tails |

Session-by-session findings live in [`CHANGELOG.md`](CHANGELOG.md); raw probe evidence in `sys/PROBE-LOG.md`.

## Layout

```
src/sr16_bridge/
  protocol.py         framing, records, status parsing (~630 lines)
  decode_0ab.py       notify decoder — segments, envelopes, records
  history_sync.py     end-to-end history pull over BLE
  ingest_snoop_to_db.py  HCI snoop → SQLite replay
  hr_live*.py         live HR subscription variants
  analyze.py          rollups + correlations
  hermes_export.py    export for local LLM analysis
scripts/
  analyze_rollup.py   day totals / correlation reports
sys/
  PROBE-LOG.md        append-only log of every scan/GATT run
tests/                39+ unit tests on framing and record decode
~/health/sr16.db      SQLite WAL store (outside project tree)
```

## Quick start

```bash
cd ~/projects/sr16-bridge
PYTHONPATH=src .venv/bin/python -m sr16_bridge.hr_live --duration 60

# inspect captured data
sqlite3 -header -column ~/health/sr16.db "SELECT * FROM hr_readings LIMIT 5;"

# run the test suite
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

Requires: ring awake + unpaired + within ~5 m of the Mac. Telink rings sleep hard between advertisements — if discovery fails, tap the ring to wake its radio and retry.

## Status

Reverse-engineering is functionally complete through session 17: history sync decodes end-to-end and cross-validates against the vendor app's numbers. Remaining ideas live in the newest HANDOFF file.

## Skills demonstrated

BLE/GATT enumeration · proprietary binary protocol reverse-engineering from traffic captures · CRC-checked segment reassembly · SQLite WAL schema design · Python packaging with pytest · local-first data pipelines (no cloud)
