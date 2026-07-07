"""Vendor 0xA00A / 0x15 history-sync: pull buffered HR from the ring → SQLite.

Reads the BLE Heart Rate log from the SR16 over Nordic UART (vendor protocol)
and writes each non-zero 5-min slot into `hr_readings`. Designed to be run
manually (no cron) — see HANDOFF-2026-07-07.md "What was NOT done" note
from operator ("i will notice when data should be synced").

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync \\
        --days 1                  # yesterday + today (default)
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync \\
        --days 7                  # full week
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync \\
        --since 2026-07-01        # explicit start day
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync \\
        --dry-run                 # build packets, don't talk to BLE
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync \\
        --scan 30                 # seconds to wait for SR16 advert

Acceptance test (no ring needed — synthetic insert path):
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync --synthetic 7

The --synthetic path bypasses BLE entirely and inserts 7 days of plausible HR
data (288 slots/day @ 5-min) into hr_readings. Then the existing
`analyze.py` cron picks them up automatically (rows with analyzed_at IS NULL).
This is the same testing pattern session-3 used for the analyzer.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

from . import protocol
from .protocol import (
    CMD_BATTERY, CMD_READ_HEART_RATE_LOG, CMD_SET_TIME,
    HeartRateLog, HeartRateLogParser, NoData,
    UART_RX_CHAR_UUID, UART_SERVICE_UUID, UART_TX_CHAR_UUID,
    HR_POINTS_PER_DAY, HR_RANGE_MINUTES,
    read_hr_log_packet, set_time_packet, parse_battery,
    BatteryInfo,
)

DB_PATH = Path.home() / "health" / "sr16.db"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"


# ---------- DB ----------

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(Path(__file__).resolve().parent.joinpath("schema.sql").read_text())
    conn.commit()
    conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate() -> None:
    """Idempotent forward-only migrations for history_sync needs."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # hr_readings needs a `source` column so we can tell history-sync rows from
    # live-stream rows. Default 'live' keeps existing rows compatible.
    if not _has_column(conn, "hr_readings", "source"):
        conn.execute("ALTER TABLE hr_readings ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
        conn.commit()
    # gateway_state — bookmark last successful sync per day
    conn.close()


def _existing_ts_for_day(device_uuid: str, day_iso: str) -> set[str]:
    """Return the set of ts_utc values already in hr_readings for a given day.

    Used to make re-runs idempotent: if you run --days 7 twice in one afternoon,
    you don't get 2016 duplicate rows.
    """
    start = datetime.fromisoformat(day_iso)
    end = start + timedelta(days=1)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts_utc FROM hr_readings WHERE device_uuid = ? AND ts_utc >= ? AND ts_utc < ?",
        (device_uuid, start.isoformat(), end.isoformat()),
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _insert_readings(device_uuid: str, log: HeartRateLog) -> tuple[int, int]:
    """Insert one day's worth of HR readings into hr_readings. Returns (inserted, skipped)."""
    start = log.timestamp
    five_min = timedelta(minutes=HR_RANGE_MINUTES)
    existing = _existing_ts_for_day(device_uuid, start.date().isoformat())
    conn = sqlite3.connect(DB_PATH)
    inserted = 0
    skipped = 0
    for i, bpm in enumerate(log.bpm):
        if not (1 <= bpm <= 254):
            continue   # 0 = no measurement, 255 = sentinel
        ts = (start + i * five_min).isoformat()
        if ts in existing:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO hr_readings
               (ts_utc, device_uuid, bpm, rr_intervals, sensor_contact,
                energy_expended, raw_hex, source)
               VALUES (?, ?, ?, NULL, 2, NULL, ?, 'history_sync')""",
            (ts, device_uuid, bpm, log.raw_packets[0].hex() if log.raw_packets else None),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted, skipped


def _set_kv(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO gateway_state(key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )
    conn.commit()
    conn.close()


def _bump_kv(key: str) -> int:
    """Increment an integer counter in gateway_state."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM gateway_state WHERE key = ?", (key,)).fetchone()
    cur = int(row[0]) if row and row[0] else 0
    new = cur + 1
    conn.execute(
        """INSERT INTO gateway_state(key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, str(new)),
    )
    conn.commit()
    conn.close()
    return new


# ---------- BLE transport ----------

async def _find_sr16(seconds: int) -> str | None:
    """Same scan pattern as hr_live.py. Returns the address or None."""
    found: dict[str, str] = {}

    def cb(device, adv):
        name = (adv.local_name or device.name or "").strip("\x00")
        if name.upper() == "SR16":
            found[device.address] = device.address

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        for _ in range(seconds):
            await asyncio.sleep(1)
    finally:
        await scanner.stop()
    return next(iter(found), None)


class _RingChannel:
    """Async wrapper around the Nordic UART channel for sending commands and
    collecting CMD_READ_HEART_RATE_LOG responses (which arrive as multiple 16-byte
    packets)."""

    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self._queue: asyncio.Queue[bytearray] = asyncio.Queue()

    def _on_notify(self, _handle, data: bytearray) -> None:
        # Only enqueue packets that are for CMD 0x15 — ignore battery etc.
        if len(data) == 16 and data[0] == CMD_READ_HEART_RATE_LOG:
            self._queue.put_nowait(bytearray(data))

    async def connect_notify(self) -> None:
        await self.client.start_notify(UART_TX_CHAR_UUID, self._on_notify)

    async def send(self, pkt: bytearray) -> None:
        rx = self.client.services.get_service(UART_SERVICE_UUID).get_characteristic(UART_RX_CHAR_UUID)
        await self.client.write_gatt_char(rx, pkt, response=False)

    async def drain_hr_log(self, timeout_per_chunk: float = 3.0, max_idle: float = 8.0) -> HeartRateLog | NoData | None:
        """Collect packets until the parser yields a result, or we hit max_idle seconds
        without a new packet. Returns None on timeout (caller decides what to do)."""
        parser = HeartRateLogParser()
        last_seen = time.time()
        while True:
            try:
                pkt = await asyncio.wait_for(self._queue.get(), timeout=timeout_per_chunk)
            except asyncio.TimeoutError:
                if time.time() - last_seen > max_idle:
                    return None
                continue
            last_seen = time.time()
            result = parser.feed(pkt)
            if isinstance(result, (HeartRateLog, NoData)):
                return result
        # unreachable


async def _sync_one_day(channel: _RingChannel, day: datetime) -> HeartRateLog | NoData | None:
    pkt = read_hr_log_packet(day)
    # drain any stale packets
    while not channel._queue.empty():
        try:
            channel._queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    await channel.send(pkt)
    return await channel.drain_hr_log()


# ---------- synthetic path (for gateway testing without a ring) ----------

def _synthetic_sync(days: int) -> tuple[int, int, int]:
    """Generate `days` days of plausible 5-min HR data. Returns (days_with_data, inserted, skipped)."""
    random.seed(42)
    inserted = 0
    skipped = 0
    days_with = 0
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for d in range(days):
        day_start = (now - timedelta(days=d)).replace(hour=0)
        log = HeartRateLog(timestamp=day_start, bpm=[0] * HR_POINTS_PER_DAY)
        for i in range(HR_POINTS_PER_DAY):
            ts = day_start + timedelta(minutes=HR_RANGE_MINUTES * i)
            # simulate sleep gap: 00:00-06:00 mostly zeros (ring off / asleep)
            hour = ts.hour
            if 0 <= hour < 6 and random.random() < 0.7:
                log.bpm[i] = 0
                continue
            # normal day curve: 55-75 bpm resting, occasional spikes
            base = 62 + 8 * (1 if 9 <= hour < 18 else 0)
            log.bpm[i] = max(45, min(120, int(base + random.gauss(0, 5))))
        # add a synthetic raw_packets so _insert_readings() can fill raw_hex
        log.raw_packets = [bytearray(b"\x15\x00" + b"\x00" * 13)]   # header only, never actually sent
        ins, skp = _insert_readings("synthetic", log)
        if ins > 0:
            days_with += 1
        inserted += ins
        skipped += skp
    return days_with, inserted, skipped


# ---------- main ----------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="sr16-bridge: vendor HR-log history sync")
    p.add_argument("--days", type=int, default=1,
                   help="how many days back to sync (default 1 = yesterday)")
    p.add_argument("--since", type=str, default=None,
                   help="explicit start day (YYYY-MM-DD, UTC). Overrides --days.")
    p.add_argument("--scan", type=int, default=15,
                   help="seconds to scan for SR16 advert before connect")
    p.add_argument("--dry-run", action="store_true",
                   help="build packets only; do not talk to BLE")
    p.add_argument("--synthetic", type=int, default=0, metavar="DAYS",
                   help="skip BLE, insert DAYS of synthetic HR data directly (for testing)")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-day line printing")
    return p.parse_args()


async def _run_real_sync(args: argparse.Namespace) -> int:
    addr = await _find_sr16(args.scan)
    if addr is None:
        print("ERR: SR16 not found (still paired as HID? try Forget device in System Settings)", file=sys.stderr)
        return 2

    print(f"connecting to {addr}...", file=sys.stderr)
    async with BleakClient(addr, timeout=20.0) as client:
        if not client.is_connected:
            print("ERR: bleak connected reported False", file=sys.stderr)
            return 3

        channel = _RingChannel(client)
        await channel.connect_notify()

        # Compute the days to sync
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if args.since:
            start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = today - timedelta(days=args.days - 1)  # --days 1 = yesterday only
        # iterate yesterday → today (skip today? ring is still recording it; include for completeness)
        total_inserted = 0
        total_skipped = 0
        days_with_data = 0
        days_no_data = 0
        days_failed = 0

        d = start
        while d <= today:
            if not args.quiet:
                print(f"  requesting {d.date().isoformat()}...", file=sys.stderr)
            try:
                result = await _sync_one_day(channel, d)
            except Exception as exc:
                print(f"  ERR day={d.date().isoformat()}: {exc}", file=sys.stderr)
                days_failed += 1
                d += timedelta(days=1)
                continue
            if isinstance(result, NoData):
                if not args.quiet:
                    print(f"  {d.date().isoformat()}: no data on ring", file=sys.stderr)
                days_no_data += 1
            elif isinstance(result, HeartRateLog):
                ins, skp = _insert_readings(addr, result)
                total_inserted += ins
                total_skipped += skp
                days_with_data += 1
                if not args.quiet:
                    valid = result.valid
                    summary = (
                        f"  {d.date().isoformat()}: {ins} new, {skp} already-present, "
                        f"{len(valid)} valid slots, "
                        f"mean={result.mean_bpm:.1f}" if result.mean_bpm else
                        f"  {d.date().isoformat()}: {ins} new, {skp} already-present, 0 valid slots"
                    )
                    print(summary, file=sys.stderr)
            else:
                print(f"  ERR day={d.date().isoformat()}: timed out waiting for ring", file=sys.stderr)
                days_failed += 1
            d += timedelta(days=1)

    # Telemetry + state
    _set_kv("last_history_sync_ts_utc", datetime.now(timezone.utc).isoformat())
    _set_kv("last_history_sync_days", str(args.days))
    _set_kv("last_history_sync_inserted", str(total_inserted))
    _bump_kv("history_sync_run_count")

    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### history_sync run @ {datetime.now(timezone.utc).isoformat()}\n"
            f"  device={addr}  days={args.days}\n"
            f"  days_with_data={days_with_data}  days_no_data={days_no_data}  days_failed={days_failed}\n"
            f"  inserted={total_inserted}  skipped={total_skipped}\n"
        )

    print(
        f"history_sync OK: {days_with_data} day(s) with data, "
        f"{total_inserted} new readings, {total_skipped} already-present → {DB_PATH}"
    )
    return 0 if days_failed == 0 else 1


def main() -> int:
    args = _parse_args()
    init_db()
    migrate()

    if args.synthetic > 0:
        days_with, inserted, skipped = _synthetic_sync(args.synthetic)
        _set_kv("last_history_sync_ts_utc", datetime.now(timezone.utc).isoformat())
        _set_kv("last_history_sync_inserted", str(inserted))
        _set_kv("last_history_sync_mode", "synthetic")
        PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PROBE_LOG.open("a") as f:
            f.write(
                f"\n#### history_sync (synthetic) @ {datetime.now(timezone.utc).isoformat()}\n"
                f"  days={args.synthetic}  days_with={days_with}  inserted={inserted}  skipped={skipped}\n"
            )
        print(f"synthetic OK: {days_with} day(s), {inserted} inserted, {skipped} skipped → {DB_PATH}")
        return 0

    if args.dry_run:
        print("DRY RUN — would request these days:")
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if args.since:
            start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        else:
            start = today - timedelta(days=args.days - 1)
        d = start
        while d <= today:
            pkt = read_hr_log_packet(d)
            print(f"  {d.date().isoformat()}: {pkt.hex()}")
            d += timedelta(days=1)
        return 0

    return asyncio.run(_run_real_sync(args))


if __name__ == "__main__":
    sys.exit(main())