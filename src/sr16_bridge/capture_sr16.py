"""sr16-bridge: aggressive 1-shot SR16 capture.

The ring advertises for ~10 seconds at a time. The standard enumerate_cocoa.py
script spends too long in discovery (15+ seconds) and the ring goes back to
sleep mid-walk. This script:

1. Opens a PyObjC CoreBluetooth central manager.
2. Discovers ANY peripheral whose name contains 'SR16' or whose identifier
   matches 36BE6673-1486-2E90-38E9-3E097DB4CC43.
3. The INSTANT we see it, calls connect_, then races to discoverServices
   with a 6-second budget per service.
4. Walks each service's characteristics and writes the FULL inventory to
   char_inventory before the ring goes back to sleep.
5. Also dumps the raw advertisement data (manufacturer, service UUIDs) so we
   can confirm vendor service UUIDs without waiting for full discovery.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.capture_sr16
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.capture_sr16 --timeout 30
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.capture_sr16 --unpair-on-find

Acceptance:
    When the ring is awake, this should produce a full char_inventory within
    10 seconds of ring wake — vs session-1's enumerate.py which never finished.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import objc
from CoreBluetooth import (
    CBPeripheral,
    CBCentralManager,
    CBUUID,
    NSData,
    NSDate,
    NSRunLoop,
)
from Foundation import NSObject
# CBCentralManagerDelegate + CBPeripheralDelegate are Objective-C INFORMAL protocols
# — PyObjC recognizes them by selector-name presence on the class, not by import.
# Do NOT try to import them; that path raises ImportError on macOS.

DB_PATH = Path.home() / "health" / "sr16.db"
SR16_NAME = "SR16"
SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"
SR16_UUID_NS = objc.lookUpClass("NSUUID").UUID()


def _pump(seconds: float) -> None:
    """Pump NSRunLoop for `seconds` so delegate callbacks fire."""
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    NSRunLoop.currentRunLoop().runUntilDate_(end)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_char_row(device_uuid: str, service_uuid: str, char_uuid: str,
                   properties: str, has_descriptor: bool, probe_notes: str) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO char_inventory
              (ts_utc, device_uuid, service_uuid, char_uuid, properties,
               has_descriptor, probe_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_now_iso(), device_uuid, service_uuid, char_uuid, properties,
         int(has_descriptor), probe_notes),
    )
    conn.commit()
    conn.close()


class _Central(NSObject):
    """Combined central + peripheral delegate. PyObjC recognizes the selectors
    (centralManager:didDiscoverPeripheral:..., peripheral:didDiscoverServices:..., etc.)
    by method-name presence. The base class is just NSObject; we do NOT inherit
    from the Obj-C informal protocols (CBCentralManagerDelegate / CBPeripheralDelegate)
    because they're not importable Python names — see pitfall #7 in the
    macos-ble-producer-unblocker skill."""

    def initWithArgs_(self, args):
        self = objc.super(_Central, self).init()
        if self is None:
            return None
        self.target_uuid = args["target_uuid"]
        self.captured_peripheral = None
        self.captured_adv_data = {}
        self.services_found = []
        self.chars_found = []
        self.state = "init"
        self.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(self, None, None)
        return self

    # ---- CBCentralManagerDelegate ----

    def centralManagerDidUpdateState_(self, manager):
        st = manager.state()
        print(f"[CB] central state = {st}  (5 = poweredOn)")
        if st == 5:
            self.state = "scanning"
            manager.scanForPeripheralsWithServices_options_(None, None)
            print("[CB] scanForPeripherals → started (no service filter)")

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
        self, manager, peripheral, adv_data, rssi
    ):
        # Filter on name OR on UUID match.
        try:
            name = peripheral.name() or ""
        except Exception:
            name = ""
        ident = str(peripheral.identifier())
        # NSUUID prints like '36BE6673-1486-2E90-38E9-3E097DB4CC43'
        if "SR16" not in name.upper() and self.target_uuid.upper() not in ident.upper():
            return  # not our ring
        if self.captured_peripheral is not None:
            return  # already locked on
        self.captured_peripheral = peripheral
        # Keep raw advertisement for later inspection.
        try:
            self.captured_adv_data = {
                "name": name,
                "identifier": ident,
                "rssi": rssi,
                "advertisement_data": _nsdict_to_py(adv_data),
            }
        except Exception as exc:
            self.captured_adv_data = {"name": name, "identifier": ident, "rssi": rssi,
                                      "adv_error": str(exc)}
        print(f"[CB] *** FOUND SR16 ***  ident={ident}  name='{name}'  rssi={rssi}")
        adv = self.captured_adv_data.get("advertisement_data", {})
        print(f"[CB] adv keys: {list(adv.keys())}")
        if "kCBAdvDataManufacturerData" in adv:
            md = adv["kCBAdvDataManufacturerData"]
            try:
                hex_str = md.hexString()
            except Exception:
                hex_str = "<unprintable>"
            print(f"[CB] manufacturer data: {hex_str}")
        if "kCBAdvDataServiceUUIDs" in adv:
            print(f"[CB] service UUIDs (advertised): {adv['kCBAdvDataServiceUUIDs']}")
        # Stop scanning, connect NOW.
        manager.stopScan()
        print("[CB] connect_ ...")
        manager.connectPeripheral_options_(peripheral, None)
        self.state = "connecting"

    def centralManager_didConnectPeripheral_(self, manager, peripheral):
        print(f"[CB] connected → discovering services...")
        self.state = "discovering"
        # Set the peripheral delegate via the Obj-C setter (setDelegate_), NOT
        # Python's `.delegate =`. The KVO-managed delegate attribute is read-only
        # in modern CoreBluetooth; .setDelegate_ is the supported path.
        # Verify: enumerate_cocoa.py uses peripheral.setDelegate_(delegate) on line 281.
        peripheral.setDelegate_(self)
        # No service filter — discover everything we can.
        peripheral.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, manager, peripheral, error):
        print(f"[CB] connect FAILED: {error}")
        self.state = "failed"

    def centralManager_didDisconnectPeripheral_error_(self, manager, peripheral, error):
        print(f"[CB] disconnected: {error}")
        self.state = "disconnected"

    # ---- CBPeripheralDelegate ----

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            print(f"[CB] discoverServices error: {error}")
            return
        services = peripheral.services() or []
        print(f"[CB] services found: {len(services)}")
        for svc in services:
            uuid_str = str(svc.UUID())
            print(f"[CB]   service {uuid_str}")
            self.services_found.append(uuid_str)
            # Discover characteristics via the PERIPHERAL, not the service.
            # In Swift/Obj-C: peripheral.discoverCharacteristics(_:for:)
            # In PyObjC: peripheral.discoverCharacteristics_forService_(None, svc)
            # Note: CBService itself does NOT have a discoverCharacteristics method.
            peripheral.discoverCharacteristics_forService_(None, svc)
        self.state = "walking_chars"

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
            has_desc = bool(ch.descriptors() or []) if ch.descriptors() else False
            print(f"[CB]     char {char_uuid}  props={props_str}")
            self.chars_found.append({
                "service": svc_uuid, "char": char_uuid,
                "properties": props_str, "has_descriptor": has_desc,
            })

    def peripheral_didDiscoverDescriptorsForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        # We don't need descriptor-level detail for protocol RE.
        pass


def _nsdict_to_py(d) -> dict:
    """Convert NSDict → plain dict. Tolerates failure."""
    try:
        out = {}
        for k in d.allKeys():
            try:
                v = d.valueForKey_(k)
                out[str(k)] = _nsobj_to_py(v)
            except Exception:
                out[str(k)] = "<unreadable>"
        return out
    except Exception:
        return {}


def _nsobj_to_py(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, list):
        return [_nsobj_to_py(x) for x in v]
    try:
        return v.hexString()
    except Exception:
        pass
    try:
        return str(v)
    except Exception:
        return "<unprintable>"


def main() -> int:
    p = argparse.ArgumentParser(description="aggressive 1-shot SR16 capture")
    p.add_argument("--timeout", type=int, default=30,
                   help="seconds to wait for ring + complete discovery (default 30)")
    p.add_argument("--unpair-on-find", action="store_true",
                   help="call blueutil --unpair <addr> after capture (prevents HID auto-bond)")
    args = p.parse_args()

    target_uuid = SR16_UUID
    print(f"[capture_sr16] looking for {target_uuid} / name '{SR16_NAME}'")
    print(f"[capture_sr16] timeout: {args.timeout}s  unpair-on-find: {args.unpair_on_find}")

    c = _Central.alloc().initWithArgs_({"target_uuid": target_uuid})

    # Pump NSRunLoop until we hit one of: connected+discovery-done, failure, or timeout.
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        _pump(0.5)
        if c.state == "failed":
            print("[capture_sr16] connect failed; exiting")
            return 2
        # Done condition: we've discovered chars for every service.
        if c.state == "walking_chars" and c.captured_peripheral is not None:
            # Give one more pump to let pending didDiscover... fire.
            _pump(2.0)
            break

    if c.captured_peripheral is None:
        print(f"[capture_sr16] TIMEOUT — ring not seen in {args.timeout}s")
        return 1

    # Write inventory.
    if c.chars_found:
        ident = c.captured_adv_data.get("identifier", "unknown")
        for ch in c.chars_found:
            write_char_row(ident, ch["service"], ch["char"], ch["properties"],
                           ch["has_descriptor"], "capture_sr16 aggressive walk")
        print(f"[capture_sr16] wrote {len(c.chars_found)} char rows to char_inventory")
    else:
        print(f"[capture_sr16] WARNING: 0 chars discovered (ring may have gone back to sleep)")

    # Dump full results to stdout for diagnostics.
    result = {
        "found_at": _now_iso(),
        "ring": c.captured_adv_data,
        "services": c.services_found,
        "chars": c.chars_found,
    }
    print("=== RESULT ===")
    print(json.dumps(result, indent=2, default=str)[:5000])

    # Optionally unpair to keep it advertising.
    if args.unpair_on_find and c.captured_peripheral is not None:
        addr = c.captured_adv_data.get("identifier", "")
        # Convert UUID → BT address if possible (PyObjC gives UUID, not addr).
        # The blueutil unpair wants MAC format like 38-00-00-00-de-90.
        # If we don't have MAC, skip unpair.
        print(f"[capture_sr16] (UUID={addr}) — unpair requires MAC, which we don't have here.")
        print(f"[capture_sr16] manual: System Settings → Bluetooth → right-click SR16 → Forget")

    return 0


if __name__ == "__main__":
    sys.exit(main())