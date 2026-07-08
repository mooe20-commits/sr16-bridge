"""SR16 connect-and-pull scaffold (Path B companion).

The protocol.py rewrite (Path A, session 11) is ready to send/receive
0xAB frames once we have the 128-bit service UUID for the chars at
handles 0x003E (WRITE) and 0x0040 (NOTIFY). This file is the placeholder
where Path B's output goes.

What session 11 still needs from a fresh phone capture:

1. 128-bit service UUID for the owning service of handles 0x003e/0x0040
   - From tshark dissection of a bugreport taken at the START of a
     connection (when service-discovery runs)
   - Replace `UART_SERVICE_UUID` in protocol.py once known

2. Confirm the WRITE/NOTIFY char handle mapping:
   - Handle 0x003E = WRITE (phone -> ring)
   - Handle 0x0040 = NOTIFY (ring -> phone)
   - The 128-bit char UUIDs may differ from the handles — tshark
     dissects ATT attribute handles, but char-by-UUID lookup needs
     the char declaration values. The phone's `dumpsys
     bluetooth_manager` should show the GATT DB after a fresh pair.

3. Verify CCCD write is needed for the notify char (Pitfall P6 in
   sr16-ring-mac-pitfalls skill). On Linux/BlueZ bleak handles this
   automatically for `start_notify`. On macOS PyObjC you need a
   manual write to the 0x2902 descriptor.

Until those three are filled in, this module is a documented scaffold.

Usage once UUIDs are known:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.connect_pull --scan 30

Or to dry-run a fetch against the offline capture (no ring needed):
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync_offline
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner

from .protocol import (
    UART_SERVICE_UUID, UART_TX_CHAR_UUID, UART_RX_CHAR_UUID,
    UART_WRITE_HANDLE, UART_NOTIFY_HANDLE,
    make_begin_sync, make_fetch_request, parse_notify, parse_fetch,
    SUB_DATA_16B, CMD_ACK,
)
from .protocol import UUID_KNOWN as _PROTOCOL_UUID_KNOWN


RING_NAME = "SR16"


# ---------------------------------------------------------------------------
# Local gate: True if both (a) the protocol module flagged the UUID real and
# (b) the literal value doesn't contain the placeholder fragment.
# ---------------------------------------------------------------------------
UUID_KNOWN = _PROTOCOL_UUID_KNOWN and "PLACEHOLDER" not in UART_SERVICE_UUID


async def find_sr16(scan_seconds: int) -> Optional[str]:
    """Scan for SR16 advert. Returns address (CoreBluetooth UUID form on macOS)."""
    found: list = []

    def cb(device, adv):
        name = (adv.local_name or device.name or "").strip("\x00")
        if name.upper() == RING_NAME:
            found.append(device.address)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        for _ in range(scan_seconds):
            await asyncio.sleep(1)
    finally:
        await scanner.stop()
    return found[0] if found else None


async def sync_today_block(client: BleakClient, sub_type: int = SUB_DATA_16B):
    """One-shot: send a fetch request, collect notifies until idle, return
    the merged ParsedFetch. Requires UUIDs to be known."""
    if not UUID_KNOWN:
        raise RuntimeError(
            "UART_SERVICE_UUID is a placeholder. Decode a fresh bugreport "
            "with decode_snoop.py to get the 128-bit UUID for handles "
            "0x003e/0x0040, then update protocol.py."
        )

    queue: asyncio.Queue = asyncio.Queue()

    def on_notify(_sender, data: bytearray) -> None:
        queue.put_nowait(bytes(data))

    # TODO: filter by handle 0x0040 once bleak version is pinned. Newer
    # bleak versions pass (characteristic, data) — handle check goes on
    # the characteristic handle.
    await client.start_notify(UART_NOTIFY_HANDLE, on_notify)

    # Send the fetch request
    pkt = make_fetch_request(sub_type, frame_seq=1)
    # Use the WRITE char UUID if known, else fall back to handle
    await client.write_gatt_char(UART_RX_CHAR_UUID, pkt, response=False)

    # Drain until idle
    notifies = []
    while True:
        try:
            raw = await asyncio.wait_for(queue.get(), timeout=2.0)
        except asyncio.TimeoutError:
            break
        notifies.append(parse_notify(raw))
        if len(notifies) > 20:  # safety: don't loop forever
            break

    await client.stop_notify(UART_NOTIFY_HANDLE)
    if not notifies:
        return None
    from .protocol import merge_fetches
    return merge_fetches([parse_fetch(n) for n in notifies])


async def run(args: argparse.Namespace) -> int:
    if not UUID_KNOWN:
        print(
            "WARN: 128-bit service UUID not yet discovered.\n"
            "This module is a scaffold. To complete Path B:\n"
            "  1. Enable Dev Options snoop on Galaxy (already done)\n"
            "  2. Trigger a sync from RWfit at the START of a connection\n"
            "  3. Pull bugreport, decode with decode_snoop.py\n"
            "  4. Find the service UUID for handle 0x003e/0x0040\n"
            "  5. Update protocol.py UART_SERVICE_UUID\n",
            file=sys.stderr,
        )
        if not args.force:
            return 1

    addr = await find_sr16(args.scan)
    if addr is None:
        print("ERR: SR16 not found in scan window", file=sys.stderr)
        return 2

    print(f"connecting to {addr}...", file=sys.stderr)
    async with BleakClient(addr, timeout=20.0) as client:
        if not client.is_connected:
            print("ERR: bleak connect failed", file=sys.stderr)
            return 3

        if args.begin_sync:
            pkt = make_begin_sync()
            print(f"sending begin-sync: {pkt.hex()}", file=sys.stderr)
            await client.write_gatt_char(UART_RX_CHAR_UUID, pkt, response=False)

        result = await sync_today_block(client)
        if result is None:
            print("ERR: no notifies received", file=sys.stderr)
            return 4
        print(f"got {len(result.records)} records, "
              f"summary val16=0x{result.day_summary.val16:04x}" if result.day_summary
              else f"got {len(result.records)} records (no day-summary)")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SR16 connect-and-pull scaffold")
    p.add_argument("--scan", type=int, default=20, help="seconds to scan")
    p.add_argument("--begin-sync", action="store_true",
                   help="send 0x09 begin-sync marker first")
    p.add_argument("--force", action="store_true",
                   help="run even with placeholder UUIDs")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())