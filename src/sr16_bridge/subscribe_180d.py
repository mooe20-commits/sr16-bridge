"""Subscribe to standard Heart Rate Measurement (0x2A37 inside 0x180D).

Session 8 quick-win: the SR16 advertises 0x180D. If it's exposed after
connect (which session-6.5 inventory didn't walk), we can stream HR
notifications with zero vendor-protocol RE.

Strategy:
- Use bleak (Python CoreBluetooth wrapper) — simpler than PyObjC and
  already a project dep.
- Scan for SR16 by name; on hit, connect immediately.
- Subscribe to 0x2A37 inside 0x180D.
- Pump until SIGINT/SIGTERM or --duration.
- Every notify → parse 0x2A37 flags/payload → INSERT into hr_readings.

The standard 0x2A37 spec is fixed by Bluetooth SIG; doesn't require
any vendor knowledge. If the ring ships 0x180D on the GATT table, this
just works.

Run:
    .venv/bin/python -m sr16_bridge.subscribe_180d --duration 90

Operator action required to drop macOS HID bond first (System Settings →
Bluetooth → SR16 → ⓘ → Disconnect). Once dropped, ring re-advertises within
~5-30s. Re-launch this script immediately after the Disconnect click.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"
BODY_SENSOR_LOCATION_CHAR = "00002a38-0000-1000-8000-00805f9b34fb"

# Last-seen SR16 BT UUID (session 6 inventory) — used as a connect hint.
SR16_UUID_HINT = "36BE6673-1486-2E90-38E9-3E097DB4CC43"
SR16_MAC = "38:00:00:00:DE:90"
# Both identifiers refer to the same physical device on macOS CoreBluetooth.
# bleak returns the UUID form; blueutil returns the MAC form. We accept either.
SR16_IDS = {SR16_UUID_HINT.lower(), SR16_MAC.lower().replace("-", ":")}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate() -> None:
    conn = sqlite3.connect(DB_PATH)
    if not _has_column(conn, "hr_readings", "analyzed_at"):
        conn.execute("ALTER TABLE hr_readings ADD COLUMN analyzed_at TEXT")
        conn.commit()
    conn.close()


def parse_hr_measurement(payload: bytearray) -> dict:
    """Bluetooth SIG HR Measurement (0x2A37) parser — see hr_live.py for full spec."""
    flags = payload[0]
    hr_format_16 = bool(flags & 0x01)
    idx = 1
    if hr_format_16:
        bpm = payload[idx] | (payload[idx + 1] << 8)
        idx += 2
    else:
        bpm = payload[idx]
        idx += 1
    energy_expended_present = bool(flags & 0x08)
    energy_expended = None
    if energy_expended_present:
        energy_expended = payload[idx] | (payload[idx + 1] << 8)
        idx += 2
    rr = []
    while idx + 1 < len(payload):
        rr.append(payload[idx] | (payload[idx + 1] << 8))
        idx += 2
    rr_str = ",".join(str(r) for r in rr) if rr else None
    sensor_contact_supported = bool(flags & 0x04)
    sensor_contact_detected = bool(flags & 0x02)
    sensor_contact = 2 if (sensor_contact_supported and sensor_contact_detected) else (1 if sensor_contact_supported else 0)
    return {"bpm": bpm, "rr_intervals": rr_str, "sensor_contact": sensor_contact, "energy_expended": energy_expended}


async def find_sr16(seconds: int, address_hint: str | None) -> tuple[str, dict] | None:
    """Scan until we see SR16 advertised. Returns (address, advertised_services_dict).

    Filter by address_hint if given: match against either MAC form or BLE-UUID form
    (bleak returns UUID form on macOS; blueutil returns MAC form — same device).
    """
    found: dict[str, dict] = {}
    accepted_ids = {address_hint.lower().replace("-", ":")} if address_hint else set()
    # Also accept the BLE UUID form of the same address
    if address_hint:
        accepted_ids.add(address_hint.lower().replace(":", "-"))

    def cb(device, adv):
        name = (adv.local_name or device.name or "").strip("\x00")
        if name.upper() != "SR16":
            return
        addr_norm = device.address.lower().replace("-", ":")
        if accepted_ids and addr_norm not in accepted_ids:
            return
        services = [str(s).lower() for s in (adv.service_uuids or [])]
        found[device.address] = {"services": services, "rssi": adv.rssi}

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if found:
                break
    finally:
        await scanner.stop()
    if not found:
        return None
    addr, info = next(iter(found.items()))
    return addr, info


async def run(duration: int, scan_seconds: int, address: str | None) -> int:
    init_db()
    migrate()

    print(f"[subscribe_180d] scanning for SR16 (timeout {scan_seconds}s)...", file=sys.stderr, flush=True)
    found = await find_sr16(scan_seconds, address)
    if found is None:
        print("[subscribe_180d] ERR: SR16 not found in advertise window", file=sys.stderr)
        return 2
    addr, info = found
    print(f"[subscribe_180d] SR16 @ {addr} rssi={info['rssi']} advertised_services={info['services']}", file=sys.stderr, flush=True)
    print(f"[subscribe_180d] hr_service_in_advert={HR_SERVICE_UUID in info['services']}", file=sys.stderr, flush=True)

    started = time.time()
    until_ts = started + duration
    rows_inserted = 0
    total_bytes = 0
    first_notify_ts: float | None = None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO ble_sessions(device_uuid, started_at) VALUES (?, ?)",
        (addr, datetime.now(timezone.utc).isoformat()),
    )
    session_id = cur.lastrowid
    conn.commit()
    conn.close()

    def on_notify(_sender: object, data: bytearray) -> None:
        nonlocal rows_inserted, total_bytes, first_notify_ts
        total_bytes += len(data)
        if first_notify_ts is None:
            first_notify_ts = time.time()
        parsed = parse_hr_measurement(bytearray(data))
        ts = datetime.now(timezone.utc).isoformat()
        c = sqlite3.connect(DB_PATH)
        c.execute(
            """INSERT INTO hr_readings
               (ts_utc, device_uuid, bpm, rr_intervals, sensor_contact,
                energy_expended, raw_hex)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, addr, parsed["bpm"], parsed["rr_intervals"],
             parsed["sensor_contact"], parsed["energy_expended"], data.hex()),
        )
        c.commit()
        c.close()
        rows_inserted += 1
        print(f"  HR notify #{rows_inserted}: bpm={parsed['bpm']} raw={data.hex()}", file=sys.stderr, flush=True)

    print(f"[subscribe_180d] connecting to {addr}...", file=sys.stderr, flush=True)
    async with BleakClient(addr, timeout=20.0) as client:
        if not client.is_connected:
            print("[subscribe_180d] ERR: failed to connect", file=sys.stderr)
            return 3

        # Walk services to confirm 0x180D is exposed (and discover what else we got).
        # This project's bleak version uses the .services property (not get_services()).
        svcs = client.services
        svc_uuids = [str(s.uuid).lower() for s in svcs]
        print(f"[subscribe_180d] post-connect services: {svc_uuids}", file=sys.stderr, flush=True)
        hr_exposed = HR_SERVICE_UUID in svc_uuids
        print(f"[subscribe_180d] 0x180D exposed post-connect: {hr_exposed}", file=sys.stderr, flush=True)

        if not hr_exposed:
            print("[subscribe_180d] ERR: 0x180D NOT in service table after connect → no live HR possible", file=sys.stderr)
            return 4

        await client.start_notify(HR_MEASUREMENT_CHAR, on_notify)
        print(f"[subscribe_180d] subscribed to 0x2A37; streaming for {duration}s...", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()
        stop_evt = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_evt.set)
        while not stop_evt.is_set and time.time() < until_ts:
            await asyncio.sleep(0.5)
            if rows_inserted > 0 and time.time() - (first_notify_ts or time.time()) > 15:
                # Got data + quiet for 15s → ring probably asleep; bail early
                print("[subscribe_180d] 15s quiet after first notify → assuming ring asleep, bailing", file=sys.stderr, flush=True)
                break
        try:
            await client.stop_notify(HR_MEASUREMENT_CHAR)
        except Exception:
            pass

    elapsed = time.time() - started
    ended_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE ble_sessions SET ended_at=?, bytes_received=? WHERE id=?",
        (ended_at, total_bytes, session_id),
    )
    conn.commit()
    conn.close()

    # PROBE-LOG block
    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### subscribe_180d run @ {datetime.now(timezone.utc).isoformat()}\n"
            f"  device={addr} rssi={info['rssi']} hr_in_advert={HR_SERVICE_UUID in info['services']}\n"
            f"  post_connect_services={svc_uuids}\n"
            f"  hr_exposed_post_connect={hr_exposed}\n"
            f"  duration={elapsed:.1f}s rows_inserted={rows_inserted} bytes_received={total_bytes}\n"
            f"  first_notify_latency_s={(first_notify_ts - started) if first_notify_ts else None}\n"
        )

    print(f"\n[subscribe_180d] DONE: rows={rows_inserted} bytes={total_bytes} elapsed={elapsed:.1f}s → {DB_PATH}")
    return 0 if rows_inserted > 0 else 5


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=90, help="seconds to stream")
    p.add_argument("--scan", type=int, default=60, help="seconds to wait for SR16 advertisement")
    p.add_argument("--address", default=SR16_MAC, help="BT MAC to filter on (avoids name collisions)")
    args = p.parse_args()
    return asyncio.run(run(args.duration, args.scan, args.address))


if __name__ == "__main__":
    sys.exit(main())