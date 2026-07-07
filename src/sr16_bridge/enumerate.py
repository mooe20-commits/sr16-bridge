"""GATT service+characteristic inventory — writes to sys/PROBE-LOG.md + SQLite char_inventory.

Usage: .venv/bin/python -m sr16_bridge.enumerate [--duration SECONDS]
"""
import argparse, asyncio, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# Resolve DB path (canonical location: ~/health/sr16.db — owned by user, not app-bundle)
DB_PATH = Path.home() / "health" / "sr16.db"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# Service UUIDs we already know to be interesting (from probe.py output)
KNOWN_SERVICES = {
    "0000180d-0000-1000-8000-00805f9b34fb": "Heart Rate (BT SIG standard)",
    "0000a00a-0000-1000-8000-00805f9b34fb": "VENDOR 0xA00A (proprietary — history + commands)",
}


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


async def enumerate_ring(device: BLEDevice) -> int:
    """Connect, walk services, dump to DB + PROBE-LOG.md. Returns number of characteristics."""
    init_db()
    migrate()
    count = 0
    session_ts = datetime.now(timezone.utc).isoformat()

    async with BleakClient(device.address, timeout=20.0) as client:
        if not client.is_connected:
            raise RuntimeError(f"failed to connect to {device.address}")

        # walk the GATT tree
        for service in client.services:
            svc_uuid = str(service.uuid).lower()
            svc_name = KNOWN_SERVICES.get(svc_uuid, "unknown")
            for char in service.characteristics:
                count += 1
                # bleak >=0.22: char.properties is Iterable[str] (e.g. ['read', 'notify'])
                props_list = list(char.properties)
                properties = ",".join(sorted(props_list)) or "none"
                # write one row per characteristic
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    """INSERT OR REPLACE INTO char_inventory
                       (ts_utc, device_uuid, service_uuid, char_uuid, properties, probe_notes)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_ts, device.address, svc_uuid, str(char.uuid).lower(),
                     properties, svc_name),
                )
                conn.commit()
                conn.close()
                # stream to PROBE-LOG (append-only, human-readable)
                with PROBE_LOG.open("a") as f:
                    f.write(
                        f"  - svc={svc_uuid} ({svc_name})  "
                        f"char={char.uuid}  props={properties}\n"
                    )

    # footer for this run
    with PROBE_LOG.open("a") as f:
        f.write(f"  → {count} characteristics across {len([1])} session(s); DB at {DB_PATH}\n")
    return count


async def find_sr16(seconds: int) -> BLEDevice | None:
    """Scan for the SR16 by advertised name (case-insensitive)."""
    found: dict[str, BLEDevice] = {}

    def cb(device: BLEDevice, adv: AdvertisementData) -> None:
        name = (adv.local_name or device.name or "").strip("\x00")
        if name.upper() == "SR16":
            found[device.address] = device

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        for _ in range(seconds):
            await asyncio.sleep(1)
    finally:
        await scanner.stop()

    # prefer the strongest RSSI
    if not found:
        return None
    # Re-scan once to get fresh RSSIs (BleakScanner gave us the device but not the latest adv)
    return list(found.values())[0]  # there's only one SR16; we confirmed it in the prior probe


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=15,
                   help="seconds to wait for SR16 advertisement")
    args = p.parse_args()

    # log header
    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(f"\n### GATT enumerate run at {datetime.now(timezone.utc).isoformat()}\n")

    device = asyncio.run(find_sr16(args.duration))
    if device is None:
        print("ERR: SR16 not found within scan window", file=sys.stderr)
        print("    → is the ring on? close to the Mac? is it paired to your phone?", file=sys.stderr)
        return 2

    print(f"found SR16 at {device.address}")
    n = asyncio.run(enumerate_ring(device))
    print(f"enumerated {n} characteristics — see sys/PROBE-LOG.md and ~/health/sr16.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
