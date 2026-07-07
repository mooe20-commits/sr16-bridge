"""Enumerate the SR16's vendor GATT services.

Goal: discover the actual UUIDs on the SR16, and confirm whether they match the
Colmi/R02 Nordic-UART UUIDs (`6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E`). If SR16
ships a different vendor service (e.g. 0xA00A in its own namespace), this script
tells us where to point the protocol layer.

Also enumerates the standard Heart Rate Service (0x180D / 0x2A37) used by
`hr_live.py` so we can confirm both paths exist side-by-side.

Output:
    sys/PROBE-LOG.md            (appended, timestamped)
    ~/health/sr16.db            (rows into char_inventory)
    stdout                       (one-line-per-characteristic dump)

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.enumerate_vendor [--scan 15]

Same BLE-producer blocker as hr_live.py applies — if the ring is HID-paired
on macOS, bleak won't see it. Run this AFTER you've "Forget"-ed the device
in System Settings and re-paired fresh.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

from .protocol import (
    UART_RX_CHAR_UUID, UART_SERVICE_UUID, UART_TX_CHAR_UUID,
)

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"

# Standard Bluetooth SIG services we expect to find
EXPECTED_SERVICES = {
    "180D": "Heart Rate",
    "180A": "Device Information",
    "180F": "Battery Service",
    "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E": "Nordic UART (vendor — Colmi/R02 compatibility)",
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def _record_char(ts: str, device: str, svc: str, char: str, props: list[str], has_desc: bool, notes: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    # PRIMARY KEY (device_uuid, service_uuid, char_uuid) per schema; idempotent INSERT OR IGNORE
    conn.execute(
        """INSERT OR IGNORE INTO char_inventory
              (ts_utc, device_uuid, service_uuid, char_uuid, properties, has_descriptor, probe_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, device, svc, char, ",".join(sorted(props)), int(has_desc), notes),
    )
    conn.commit()
    conn.close()


async def find_sr16(seconds: int) -> str | None:
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


async def enumerate(address: str) -> int:
    """Connect, walk every service + characteristic, write to char_inventory.

    Returns count of (service, characteristic) pairs discovered.
    """
    started = datetime.now(timezone.utc).isoformat()
    n_pairs = 0
    async with BleakClient(address, timeout=20.0) as client:
        if not client.is_connected:
            print("ERR: bleak reports not-connected", file=sys.stderr)
            return 0

        print(f"# Services & characteristics @ {address}", file=sys.stderr)
        for service in client.services:
            svc_uuid = service.uuid
            svc_short = svc_uuid[:8] if len(svc_uuid) >= 8 else svc_uuid
            expected = EXPECTED_SERVICES.get(svc_short) or EXPECTED_SERVICES.get(svc_uuid)
            tag = f"  [expected: {expected}]" if expected else ""
            print(f"service {svc_uuid}{tag}", file=sys.stderr)
            for char in service.characteristics:
                props = list(char.properties)   # bleak gives a list of names
                has_desc = bool(char.descriptors)
                notes = ""
                if svc_uuid.upper() == UART_SERVICE_UUID.upper():
                    if char.uuid.upper() == UART_RX_CHAR_UUID.upper():
                        notes = "VENDOR RX (we WRITE here)"
                    elif char.uuid.upper() == UART_TX_CHAR_UUID.upper():
                        notes = "VENDOR TX (we SUBSCRIBE here)"
                print(f"  char {char.uuid}  props={props}  descriptors={has_desc}  {notes}", file=sys.stderr)
                _record_char(started, address, svc_uuid, char.uuid, props, has_desc, notes)
                n_pairs += 1
    return n_pairs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scan", type=int, default=15, help="seconds to wait for SR16 advert")
    args = p.parse_args()

    init_db()
    addr = asyncio.run(find_sr16(args.scan))
    if addr is None:
        print("ERR: SR16 not found (HID-paired on macOS? try Forget device in System Settings)", file=sys.stderr)
        return 2

    print(f"connecting to {addr}...", file=sys.stderr)
    n = asyncio.run(enumerate(addr))

    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### enumerate_vendor @ {datetime.now(timezone.utc).isoformat()}\n"
            f"  device={addr}\n  pairs={n}\n"
        )

    print(f"enumerate OK: {n} (service, characteristic) pairs → {DB_PATH} + {PROBE_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())