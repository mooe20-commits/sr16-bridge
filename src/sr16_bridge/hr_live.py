"""Live Heart Rate notifications → SQLite. v1.0 value-deliverable.

Subscribes to the Bluetooth SIG standard Heart Rate Measurement characteristic
(UUID 0x2A37 inside the Heart Rate service 0x180D), parses the flags byte + value,
and inserts every notification into hr_readings.

Usage:
    .venv/bin/python -m sr16_bridge.hr_live [--duration SECONDS]

Output:
    ~/health/sr16.db  (WAL-mode SQLite, queryable)
    ~/projects/sr16-bridge/sys/PROBE-LOG.md  (run summary appended each run)
"""
import argparse, asyncio, signal, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"

# Bluetooth SIG assigned numbers for Heart Rate service
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate() -> None:
    """Idempotent forward-only migrations for hr_readings schema drift."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    if not _has_column(conn, "hr_readings", "analyzed_at"):
        conn.execute("ALTER TABLE hr_readings ADD COLUMN analyzed_at TEXT")
        conn.commit()
    conn.close()


def parse_hr_measurement(payload: bytearray) -> dict:
    """Parse Bluetooth SIG Heart Rate Measurement characteristic (0x2A37).

    Layout:  byte 0 = flags, byte 1+ = value (8- or 16-bit, depending on flag bit 0).
    Flag bits:  0=HR value 16 bit (else 8), 1=Sensor Contact supported, 2=Sensor Contact detected,
                3=Energy Expended present, 4=RR-interval present (multiple, 2 bytes each)
    """
    flags = payload[0]
    hr_format_16 = bool(flags & 0x01)
    idx = 1
    if hr_format_16:
        bpm = payload[idx] | (payload[idx + 1] << 8)
        idx += 2
    else:
        bpm = payload[idx]
        idx += 1
    sensor_contact_supported = bool(flags & 0x04)  # bit 2
    sensor_contact_detected = bool(flags & 0x02)  # bit 1 (only meaningful if supported)
    energy_expended_present = bool(flags & 0x08)   # bit 3
    energy_expended = None
    if energy_expended_present:
        energy_expended = payload[idx] | (payload[idx + 1] << 8)
        idx += 2
    rr_intervals_raw: list[int] = []
    while idx + 1 < len(payload):
        rr_intervals_raw.append(payload[idx] | (payload[idx + 1] << 8))
        idx += 2
    rr_intervals = ",".join(str(r) for r in rr_intervals_raw) if rr_intervals_raw else None
    return {
        "bpm": bpm,
        "rr_intervals": rr_intervals,
        "sensor_contact": 2 if (sensor_contact_supported and sensor_contact_detected)
                          else (1 if sensor_contact_supported else 0),
        "energy_expended": energy_expended,
    }


async def stream_hr(device_addr: str, until_ts: float, started_wall: float) -> tuple[int, float, int]:
    """Connect, subscribe, stream until cancellation or until_ts (epoch seconds).

    Returns (rows, duration_seconds, total_notify_bytes).
    """
    init_db()
    migrate()
    started_wall = time.time()
    rows: list[tuple] = []
    total_bytes = 0
    started_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    session_id = conn.execute(
        "INSERT INTO ble_sessions(device_uuid, started_at) VALUES (?, ?)",
        (device_addr, started_at),
    ).lastrow
    conn.commit()
    conn.close()

    def on_notify(_handle: int, data: bytearray) -> None:
        nonlocal total_bytes
        total_bytes += len(data)
        parsed = parse_hr_measurement(bytearray(data))
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO hr_readings
               (ts_utc, device_uuid, bpm, rr_intervals, sensor_contact,
                energy_expended, raw_hex)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, device_addr, parsed["bpm"], parsed["rr_intervals"],
             parsed["sensor_contact"], parsed["energy_expended"], data.hex()),
        )
        conn.commit()
        conn.close()
        rows.append((ts, parsed["bpm"]))

    async with BleakClient(device_addr, timeout=20.0) as client:
        if not client.is_connected:
            raise RuntimeError(f"failed to connect to {device_addr}")
        await client.start_notify(HR_MEASUREMENT_CHAR, on_notify)

        # pump until time limit or ctrl-c
        loop = asyncio.get_running_loop()
        stop_evt = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_evt.set)
        while not stop_evt.is_set and time.time() < until_ts:
            await asyncio.sleep(0.5)
        try:
            await client.stop_notify(HR_MEASUREMENT_CHAR)
        except Exception:
            pass

    ended_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE ble_sessions SET ended_at=?, bytes_received=? WHERE id=?",
        (ended_at, total_bytes, session_id),
    )
    conn.commit()
    conn.close()
    return len(rows), time.time() - started_wall, total_bytes


async def find_sr16(seconds: int) -> str | None:
    found: dict[str, str] = {}

    def cb(device, adv) -> None:
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=60,
                   help="seconds to stream (default 60)")
    p.add_argument("--scan", type=int, default=15,
                   help="seconds to wait for SR16 advertisement before connect")
    args = p.parse_args()

    addr = asyncio.run(find_sr16(args.scan))
    if addr is None:
        print("ERR: SR16 not found", file=sys.stderr)
        return 2

    print(f"connecting to {addr}...", file=sys.stderr)
    started = time.time()
    until_ts = started + args.duration
    try:
        rows, duration, total_bytes = asyncio.run(stream_hr(addr, until_ts, started))
    except Exception as exc:
        print(f"ERR: stream failed: {exc}", file=sys.stderr)
        return 3

    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### hr_live run @ {datetime.now(timezone.utc).isoformat()}\n"
            f"  device={addr}\n  duration={duration:.1f}s\n"
            f"  rows_inserted={rows}\n  bytes_received={total_bytes}\n"
        )

    print(f"streamed {rows} HR readings ({total_bytes} bytes) over {duration:.1f}s → {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
