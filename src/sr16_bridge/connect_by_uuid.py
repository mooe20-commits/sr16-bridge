"""connect-by-uuid: bypass scan, talk directly to the SR16 by its UUID.

The capture_sr16.py script filters on UUID but doesn't try retrievePeripherals_
because it expects to find the device via scan. When the device is HID-paired
+ connected (the auto-bond state), it won't advertise — but CoreBluetooth
already KNOWS about it and can return it via retrievePeripherals_ with the UUID.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.connect_by_uuid
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import objc
from CoreBluetooth import (
    CBCentralManager,
    CBUUID,
    NSData,
    NSDate,
    NSRunLoop,
)
from Foundation import NSObject

DB_PATH = Path.home() / "health" / "sr16.db"
SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"


def _pump(seconds: float) -> None:
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    NSRunLoop.currentRunLoop().runUntilDate_(end)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_char_row(device_uuid, service_uuid, char_uuid, properties, has_descriptor, notes):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = __import__("sqlite3").connect(DB_PATH)
    conn.execute(
        """INSERT INTO char_inventory
              (ts_utc, device_uuid, service_uuid, char_uuid, properties,
               has_descriptor, probe_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_now_iso(), device_uuid, service_uuid, char_uuid, properties,
         int(has_descriptor), notes),
    )
    conn.commit()
    conn.close()


class _Central(NSObject):
    def init(self):
        self = objc.super(_Central, self).init()
        if self is None:
            return None
        self.manager = None
        self.target_uuid = None
        self.peripheral = None
        self.services = []
        self.chars = []
        self.state = "init"
        return self

    def centralManagerDidUpdateState_(self, manager):
        st = manager.state()
        print(f"[CB] state = {st}  (5 = poweredOn)")
        if st != 5:
            return
        self.state = "powered_on"
        # Ask CoreBluetooth for the SR16 by UUID directly. No scan.
        # Build NSUUID from string. NSUUID.initWithUUIDString_ expects the canonical
        # 36-char form "8-4-4-4-12" which our UUID already is.
        from Foundation import NSUUID, NSString
        ns_str = NSString.stringWithString_(SR16_UUID)
        uuid_obj = NSUUID.alloc().initWithUUIDString_(ns_str)
        if uuid_obj is None:
            print(f"[CB] NSUUID init failed for {SR16_UUID!r} — falling back to scan")
            self.state = "scanning"
            manager.scanForPeripheralsWithServices_options_(None, None)
            return
        try:
            peripherals = manager.retrievePeripherals_([uuid_obj])
        except Exception as exc:
            print(f"[CB] retrievePeripherals_ raised: {exc}")
            peripherals = []
        if peripherals is None:
            peripherals = []
        print(f"[CB] retrievePeripherals_({SR16_UUID}) → {len(peripherals)} device(s)")
        if peripherals:
            for p in peripherals:
                ident = str(p.identifier())
                name = ""
                try:
                    name = str(p.name())
                except Exception:
                    pass
                state = int(p.state())
                print(f"[CB]   found: ident={ident}  name='{name}'  state={state}")
            self.peripheral = peripherals[0]
            self.peripheral.setDelegate_(self)
            manager.connectPeripheral_options_(self.peripheral, None)
            self.state = "connecting"
        else:
            print("[CB] retrievePeripherals_ returned 0 — SR16 not yet known to CoreBluetooth")
            print("[CB] falling back to scan...")
            self.state = "scanning"
            manager.scanForPeripheralsWithServices_options_(None, None)

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
        self, manager, peripheral, adv_data, rssi
    ):
        try:
            name = peripheral.name() or ""
        except Exception:
            name = ""
        ident = str(peripheral.identifier())
        if "SR16" not in name.upper() and SR16_UUID.upper() not in ident.upper():
            return
        if self.peripheral is not None:
            return
        self.peripheral = peripheral
        print(f"[CB] FOUND via scan: {ident}  '{name}'  rssi={rssi}")
        manager.stopScan()
        peripheral.setDelegate_(self)
        manager.connectPeripheral_options_(peripheral, None)
        self.state = "connecting"

    def centralManager_didConnectPeripheral_(self, manager, peripheral):
        print(f"[CB] CONNECTED → discovering services")
        self.state = "discovering"
        peripheral.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, manager, peripheral, error):
        print(f"[CB] connect FAILED: {error}")
        self.state = "failed"

    def centralManager_didDisconnectPeripheral_error_(self, manager, peripheral, error):
        print(f"[CB] disconnected: {error}")

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            print(f"[CB] discoverServices error: {error}")
            return
        services = peripheral.services() or []
        print(f"[CB] services: {len(services)}")
        for svc in services:
            uuid_str = str(svc.UUID())
            print(f"[CB]   service {uuid_str}")
            self.services.append(uuid_str)
            peripheral.discoverCharacteristics_forService_(None, svc)
        self.state = "walking"

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        svc_uuid = str(service.UUID())
        chars = service.characteristics() or []
        for ch in chars:
            char_uuid = str(ch.UUID())
            props = []
            for bit, name in [(0x01, "broadcast"), (0x02, "read"), (0x04, "write-no-response"),
                              (0x08, "write"), (0x10, "notify"), (0x20, "indicate"),
                              (0x40, "signed-write"), (0x80, "extended")]:
                if ch.properties() & bit:
                    props.append(name)
            props_str = ",".join(props)
            descs = ch.descriptors() or []
            has_desc = bool(descs)
            print(f"[CB]     char {char_uuid}  props={props_str}")
            self.chars.append({"svc": svc_uuid, "char": char_uuid,
                              "props": props_str, "has_desc": has_desc})

    def peripheral_didDiscoverDescriptorsForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        pass


def main() -> int:
    print(f"[connect-by-uuid] target = {SR16_UUID}")
    c = _Central.alloc().init()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + 45
    while time.time() < deadline:
        _pump(0.5)
        if c.state == "failed":
            return 2
        # Done when we've discovered chars for every service.
        if c.state == "walking" and c.peripheral is not None:
            _pump(3.0)
            break

    if c.peripheral is None:
        print("[connect-by-uuid] TIMEOUT — SR16 not reached")
        return 1

    if not c.chars:
        print("[connect-by-uuid] connected but 0 chars (ring may have slept)")
        return 3

    # Write to char_inventory.
    ident = str(c.peripheral.identifier())
    for ch in c.chars:
        write_char_row(ident, ch["svc"], ch["char"], ch["props"], ch["has_desc"],
                       "connect_by_uuid — post-HID-bond direct connect")
    print(f"\n=== RESULT ===")
    print(f"peripheral: {ident}")
    print(f"services: {c.services}")
    print(f"chars: {len(c.chars)}")
    for ch in c.chars:
        print(f"  service={ch['svc']}  char={ch['char']}  props={ch['props']}")
    print(f"\nwrote {len(c.chars)} rows to char_inventory")
    return 0


if __name__ == "__main__":
    sys.exit(main())