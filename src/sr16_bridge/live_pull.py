"""Live ring pull using PyObjC CoreBluetooth.

Bypasses bleak's "device-not-found-when-already-connected" quirk by using
CBCentralManager.retrievePeripheralsWithIdentifiers_ for the known SR16
UUID, then walks the A00A service, opens CCCD notify on B003, writes the
0x03 fetch packet to B002, and collects notifies into an in-memory queue.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.live_pull --sub-type 0x1A

Pitfalls handled (P1, P6, P2):
- P1 / P3 (HID auto-bond, ring asleep): we scan as a fallback; we don't
  fight the bond, we ride it.
- P6 (CCCD): we explicitly write \\x01\\x00 to the 0x2902 descriptor on B003.
- P2 (UUID vs MAC address): we use the BLE UUID form 36BE6673-... exclusively.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

from Foundation import NSUUID, NSString  # type: ignore
from CoreBluetooth import (  # type: ignore
    CBCentralManager,
    CBCharacteristicWriteWithoutResponse,
)

from .protocol import (
    UART_SERVICE_UUID, UART_TX_CHAR_UUID, UART_RX_CHAR_UUID,
    make_begin_sync, make_fetch_request, parse_notify, parse_fetch,
    merge_fetches, SUB_DATA_16B, SUB_DATA_8B, SUB_BYTE_GRID,
)


SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"
RING_NAME = "SR16"
COLLECT_SECONDS = 4.0


# ---- ObjC event pump ---------------------------------------------------------

_pump_run_loop = None  # set on first call


def _pump(seconds: float) -> None:
    """Spin the main run loop for `seconds` so delegate callbacks fire."""
    global _pump_run_loop
    if _pump_run_loop is None:
        from Foundation import NSRunLoop  # type: ignore
        _pump_run_loop = NSRunLoop.mainRunLoop()
    deadline = time.time() + seconds
    while time.time() < deadline:
        _pump_run_loop.runUntilDate_(Foundation_NSDate_dateWithTimeIntervalSinceNow_(0.05))


from Foundation import NSDate  # type: ignore   # noqa: E402

def Foundation_NSDate_dateWithTimeIntervalSinceNow_(seconds: float):
    return NSDate.dateWithTimeIntervalSinceNow_(seconds)


# ---- Central delegate --------------------------------------------------------

class _Central:
    """CoreBluetooth central delegate. Holds onto the discovered peripheral and
    collects notifies into a list."""

    def __init__(self):
        self.manager: Optional[CBCentralManager] = None
        self.peripheral = None
        self.a00a_service = None
        self.b002_write = None       # type = write
        self.b003_notify = None      # type = notify
        self.b003_cccd = None        # CCCD descriptor on B003 (P6)
        self.state = "init"
        self.notifies: list = []     # raw bytes captured
        self._notify_started = False

    def centralManagerDidUpdateState_(self, manager):
        st = manager.state()
        print(f"[CB] state = {st}  (5 = poweredOn)")
        if st != 5:
            return
        self.state = "powered_on"
        # Step 1: try retrievePeripherals_ (works only if CB already tracks it).
        ns_str = NSString.stringWithString_(SR16_UUID)
        uuid_obj = NSUUID.alloc().initWithUUIDString_(ns_str)
        if uuid_obj is not None:
            try:
                peripherals = manager.retrievePeripherals_([uuid_obj])
                if peripherals:
                    self._on_peripheral(manager, peripherals[0], source="retrieve")
                    return
                print("[CB] retrievePeripherals_ empty (not tracked yet)")
            except Exception as exc:
                print(f"[CB] retrievePeripherals_ raised: {exc}")
        # Step 2: brief retry. CB sometimes takes a moment to populate the
        # known-peripherals list after `poweredOn`, especially when the
        # device is connected via a previous session.
        from PyObjCTools import AppHelper  # noqa
        _pump(2.0)
        if uuid_obj is not None:
            try:
                peripherals = manager.retrievePeripherals_([uuid_obj])
                if peripherals:
                    self._on_peripheral(manager, peripherals[0], source="retrieve2")
                    return
            except Exception:
                pass
        # Step 3: scan. If the ring is asleep (no recent advert), nothing
        # will come back and we'll time out cleanly.
        print("[CB] retrievePeripherals_ still empty → scanning...")
        self._start_scan(manager)

    def _start_scan(self, manager):
        self.state = "scanning"
        manager.scanForPeripheralsWithServices_options_(None, None)

    def _on_peripheral(self, manager, peripheral, source: str):
        self.peripheral = peripheral
        try:
            name = peripheral.name() or ""
        except Exception:
            name = ""
        ident = str(peripheral.identifier())
        print(f"[CB] {source}: {ident}  '{name}'")
        peripheral.setDelegate_(self)
        manager.connectPeripheral_options_(peripheral, None)
        self.state = "connecting"

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
        self, manager, peripheral, adv_data, rssi
    ):
        try:
            name = peripheral.name() or ""
        except Exception:
            name = ""
        ident = str(peripheral.identifier())
        if RING_NAME.upper() not in name.upper() and SR16_UUID.upper() not in ident.upper():
            return
        if self.peripheral is not None:
            return
        manager.stopScan()
        self._on_peripheral(manager, peripheral, source=f"scan rssi={rssi}")

    def centralManager_didConnectPeripheral_(self, manager, peripheral):
        print(f"[CB] CONNECTED → discovering A00A service")
        self.state = "discovering"
        from CoreBluetooth import CBUUID
        svc_uuid = CBUUID.UUIDWithString_(UART_SERVICE_UUID)
        peripheral.discoverServices_([svc_uuid])

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            print(f"[CB] discoverServices error: {error}")
            return
        services = peripheral.services() or []
        for svc in services:
            uuid_str = str(svc.UUID())
            if "a00a" in uuid_str.lower():
                self.a00a_service = svc
                print(f"[CB] found A00A service: {uuid_str}")
                from CoreBluetooth import CBUUID
                char_specs = [
                    CBUUID.UUIDWithString_(UART_TX_CHAR_UUID),
                    CBUUID.UUIDWithString_(UART_RX_CHAR_UUID),
                ]
                peripheral.discoverCharacteristics_forService_(char_specs, svc)
            else:
                print(f"[CB] (skipping extra service: {uuid_str})")

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        for ch in service.characteristics() or []:
            cu = str(ch.UUID()).lower()
            props = ch.properties()
            handle = ch.handle()
            print(f"[CB]     char {cu}  props={props}  handle={handle}")
            if cu.endswith("b002"):
                self.b002_write = ch
            elif cu.endswith("b003"):
                self.b003_notify = ch
            # Always probe descriptors (cheap; CCCD is at handle+1 normally).
            peripheral.discoverDescriptorsForCharacteristic_(ch)
        # Set ready once both chars exist (descriptor walk runs in parallel).
        if self.b002_write and self.b003_notify and self.state != "ready":
            self.state = "chars-ready"

    def peripheral_didDiscoverDescriptorsForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            return
        descs = characteristic.descriptors() or []
        cu = str(characteristic.UUID()).lower()
        if cu.endswith("b003"):
            for d in descs:
                if "2902" in str(d.UUID()).lower():
                    self.b003_cccd = d
                    print(f"[CB]       B003 CCCD @ handle={d.handle()}")
        if self.b002_write and self.b003_notify and self.state != "ready":
            self.state = "ready"

    def peripheral_didUpdateValueForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            print(f"[CB] notify error: {error}")
            return
        value = characteristic.value()
        if value is None:
            return
        self.notifies.append(bytes(value))

    def peripheral_didWriteValueForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            print(f"[CB] write-ack error: {error}")


# ---- Driver ------------------------------------------------------------------

def _find_cccd(characteristic) -> Optional[object]:
    """Find the 0x2902 CCCD descriptor on a characteristic."""
    descs = characteristic.descriptors() or []
    for d in descs:
        u = str(d.UUID()).lower()
        # CBUUID form of 0x2902 is "00002902-0000-1000-8000-00805f9b34fb" on macOS
        # but CoreBluetooth will also return the 16-bit numeric form sometimes.
        if "2902" in u:
            return d
    return None


def _start_notify(c: _Central):
    """setNotifyValue_ + manual CCCD write for B003 (P6)."""
    ch = c.b003_notify
    print(f"[CB] setNotifyValue_forCharacteristic_(True, B003)")
    c.peripheral.setNotifyValue_forCharacteristic_(True, ch)
    # Manual CCCD write: on this ring, setNotifyValue alone does NOT enable
    # notifications (observed session 12). P6 of sr16-ring-mac-pitfalls.
    if c.b003_cccd is not None:
        try:
            print(f"[CB] writing \\x01\\x00 to CCCD handle={c.b003_cccd.handle()}")
            c.peripheral.writeValue_forDescriptor_(b"\x01\x00", c.b003_cccd)
        except Exception as exc:
            print(f"[CB] CCCD write raised: {exc}")
    else:
        print(f"[CB] WARN: no CCCD stored — descriptors may not have been discovered yet")


def _write(c: _Central, pkt: bytes):
    c.peripheral.writeValue_forCharacteristic_type_(
        pkt, c.b002_write, CBCharacteristicWriteWithoutResponse
    )


def main() -> int:
    p = argparse.ArgumentParser(description="SR16 live pull (PyObjC)")
    p.add_argument("--sub-type", default="0x1A",
                   help="sub_type byte for make_fetch_request (default 0x1A = today 16B block)")
    p.add_argument("--begin-sync", action="store_true",
                   help="send 0x09 begin-sync marker before the fetch")
    p.add_argument("--seconds", type=float, default=COLLECT_SECONDS,
                   help="seconds to collect notifies after the write")
    args = p.parse_args()

    sub_type = int(args.sub_type, 16) if args.sub_type.startswith("0x") else int(args.sub_type)

    print(f"[live-pull] target SR16 = {SR16_UUID}")
    print(f"[live-pull] service UUID = {UART_SERVICE_UUID}")
    print(f"[live-pull] TX char UUID = {UART_TX_CHAR_UUID}")
    print(f"[live-pull] RX char UUID = {UART_RX_CHAR_UUID}")
    print(f"[live-pull] sub_type     = 0x{sub_type:02x}")
    print(f"[live-pull] begin-sync   = {args.begin_sync}")
    print()

    c = _Central()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    # Wait for connection & GATT discovery (chars + descriptors)
    deadline = time.time() + 60
    while time.time() < deadline:
        _pump(0.2)
        if c.state == "ready":
            break
        if c.state == "chars-ready" and time.time() > deadline - 55:
            # Got chars early but descriptor walk still pending — proceed.
            print("[live-pull] chars-ready but CCCD walk slow; proceeding without")
            c.state = "ready"
            break
        if c.state == "failed":
            return 2
    if c.state != "ready":
        print(f"[live-pull] TIMEOUT (final state={c.state})")
        return 1

    # Start notify
    _start_notify(c)
    _pump(0.3)

    # Optionally begin-sync
    if args.begin_sync:
        pkt = make_begin_sync()
        print(f"[live-pull] begin-sync → {pkt.hex()}")
        _write(c, pkt)
        _pump(1.0)

    # Send fetch
    pkt = make_fetch_request(sub_type, frame_seq=1)
    print(f"[live-pull] fetch → {pkt.hex()}")
    _write(c, pkt)

    # Collect
    print(f"[live-pull] collecting notifies for {args.seconds}s...")
    _pump(args.seconds)

    # Disconnect (clean shutdown — keeps the ring in a good state for the phone)
    if c.peripheral:
        c.manager.cancelPeripheralConnection_(c.peripheral)
        _pump(0.3)

    # Parse
    notifies = c.notifies
    print(f"\n[live-pull] captured {len(notifies)} notifies")
    if not notifies:
        print(f"[live-pull] no notifies → ring did not respond to fetch")
        return 3

    decoded = [parse_notify(n) for n in notifies]
    fetches = [parse_fetch(d) for d in decoded]
    merged = merge_fetches(fetches)

    print(f"\n[live-pull] parsed {len(merged.records)} regular records "
          f"+ {1 if merged.day_summary else 0} day-summary")
    for r in merged.records[:5]:
        print(f"  marker=0x{r.marker:04x}  val16=0x{r.val16:04x}  data={r.data.hex()}")
    if merged.day_summary:
        ds = merged.day_summary
        print(f"  DAY-SUMMARY: marker=0x{ds.marker:04x}  val16=0x{ds.val16:04x}  "
              f"data={ds.data.hex()}")

    # Show first 8 hex dumps so we can eyeball the on-wire pattern
    print(f"\n[live-pull] first 8 raw notifies (hex):")
    for i, n in enumerate(notifies[:8]):
        print(f"  [{i}] {n.hex()}  ({len(n)}B)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
