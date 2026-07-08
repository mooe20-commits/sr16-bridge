"""Attempt to grab an *already-connected* SR16 via CoreBluetooth's
retrieveConnectedPeripheralsWithServices_ and immediately subscribe to all
notify chars. Bypasses the scan/advertise race entirely.

If macOS owns the GATT connection as a HID device, CoreBluetooth returns []
and we have to drop the connection in System Settings first.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import objc
from CoreBluetooth import (
    CBCentralManager,
    CBUUID,
    NSDate,
    NSRunLoop,
)
from Foundation import NSObject, NSData as NSDataClass

SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"
FF02_RX_CHAR_UUID = "0000FF02-0000-1000-8000-00805F9B34FB"
NOTIFY_TARGETS = [
    "0000FF01-0000-1000-8000-00805F9B34FB",  # FF01 - FF00 TX
    "0000B002-0000-1000-8000-00805F9B34FB",  # B002 - A00A
    "00000BC1-0000-1000-8000-00805F9B34FB",  # 0BC1 - 0BC0 main
    "00000BC2-0000-1000-8000-00805F9B34FB",  # 0BC2 - 0BC0 sub
]


def _pump(seconds: float) -> None:
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Central(NSObject):
    def init(self):
        self = objc.super(_Central, self).init()
        if self is None:
            return None
        self.manager = None
        self.peripheral = None
        self.ff02_char = None
        self.notify_chars = {}
        self.notify_states = {}
        self.notifications = []
        self.state = "init"
        return self

    def centralManagerDidUpdateState_(self, manager):
        if manager.state() != 5:
            print(f"[CB] state = {manager.state()} (not poweredOn)")
            return
        self.manager = manager
        print("[CB] poweredOn. Asking macOS for already-connected peripherals...")
        # Pass any-service sentinel [].
        retrieved = manager.retrieveConnectedPeripheralsWithServices_([])
        self._retrieved = retrieved
        print(f"[CB] retrieveConnectedPeripheralsWithServices_ → {len(retrieved)} device(s)")
        for p in retrieved:
            try:
                name = p.name() or ""
            except Exception:
                name = ""
            ident = str(p.identifier())
            print(f"   - {ident}  '{name}'")
            if SR16_UUID.upper() in ident.upper() or "SR16" in name.upper():
                self.peripheral = p
                p.setDelegate_(self)
                # Already-connected devices don't fire didConnect; we go straight to discover.
                self.state = "discovering"
                p.discoverServices_(None)
                print(f"[CB] selected SR16, discovering services...")
                return
        print("[CB] SR16 not in connected list → cannot grab, must disconnect first")
        self.state = "not_found"

    def centralManager_didConnectPeripheral_(self, manager, peripheral):
        print("[CB] CONNECTED")
        self.state = "discovering"
        peripheral.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, manager, peripheral, error):
        print(f"[CB] connect FAILED: {error}")
        self.state = "failed"

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            print(f"[CB] discoverServices error: {error}")
            return
        services = peripheral.services() or []
        print(f"[CB] services: {len(services)}")
        for svc in services:
            print(f"[CB]   service {svc.UUID()}")
            peripheral.discoverCharacteristics_forService_(None, svc)

    @staticmethod
    def _props_str(props: int) -> str:
        out = []
        if props & 0x02: out.append("read")
        if props & 0x04: out.append("write")
        if props & 0x08: out.append("write-no-response")
        if props & 0x10: out.append("notify")
        if props & 0x20: out.append("indicate")
        return ",".join(out) or "(none)"

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        svc_uuid = service.UUID().UUIDString() if service.UUID() else "?"
        ff02 = CBUUID.UUIDWithString_(FF02_RX_CHAR_UUID)
        notify_uuids = {
            n: CBUUID.UUIDWithString_(n) for n in NOTIFY_TARGETS
        }
        for ch in (service.characteristics() or []):
            cuid = ch.UUID()
            props = ch.properties()
            print(f"[CB]   {svc_uuid}  {cuid.UUIDString()}  props={self._props_str(props)}")
            if cuid.isEqual_(ff02):
                self.ff02_char = ch
            if props & 0x10:
                for sn, nuid in notify_uuids.items():
                    if cuid.isEqual_(nuid) and sn not in self.notify_chars:
                        self.notify_chars[sn] = ch
                        self.notify_states[sn] = "pending"
                        print(f"[CB]   → subscribing {sn}")
                        peripheral.setNotifyValue_forCharacteristic_(True, ch)
        if self.ff02_char is not None and self.notify_chars:
            self.state = "ready"

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        cuid = characteristic.UUID()
        sn = cuid.UUIDString().upper().replace("0000", "")[:4]
        if error:
            self.notify_states[sn] = f"err: {error}"
            print(f"[CB] notify-state ERR {sn}: {error}")
            return
        on = characteristic.isNotifying()
        self.notify_states[sn] = "on" if on else "off"
        print(f"[CB] NOTIFY {'ON' if on else 'OFF'} for {sn}")

    def peripheral_didUpdateValueForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            print(f"[CB] notify error: {error}")
            return
        data = characteristic.value()
        if data is None:
            return
        try:
            raw = bytes(data)
        except Exception:
            return
        hex_str = raw.hex()
        cuid = characteristic.UUID().UUIDString().upper()
        sn = cuid.replace("0000", "")[:4]
        print(f"[RX] NOTIFY on {sn}: {len(raw)} bytes  {hex_str}")
        self.notifications.append({"ts": _now(), "char": sn, "len": len(raw), "raw_hex": hex_str})


def _send(central, packet: bytes) -> None:
    data = NSDataClass.dataWithBytes_length_(packet, len(packet))
    central.peripheral.writeValue_forCharacteristic_type_(data, central.ff02_char, 0)
    print(f"[TX] {packet.hex()}  (cmd=0x{packet[0]:02x})")


def main() -> int:
    print(f"[grab_connected] target SR16 {SR16_UUID}")
    c = _Central.alloc().init()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + 20
    while time.time() < deadline:
        _pump(0.3)
        if c.state in ("ready", "not_found", "failed"):
            break

    if c.peripheral is None:
        print(f"\n[grab_connected] FAIL: state={c.state}")
        print("  → macOS owns the ring as HID, CB can't grab it.")
        print("  → Drop the connection: System Settings → Bluetooth → click ⓘ → Disconnect")
        return 1

    if c.ff02_char is None or not c.notify_chars:
        print(f"\n[grab_connected] FAIL: state={c.state}, ff02={c.ff02_char}, "
              f"subscribed={list(c.notify_chars.keys())}")
        return 2

    # Wait for NOTIFY ON ack
    _pump(1.5)
    print(f"\n[grab_connected] READY. subscribed={list(c.notify_chars.keys())}")
    print(f"[grab_connected] notify_states={c.notify_states}")

    # Send CMD_BATTERY (0x03) since that's our baseline.
    pkt = bytearray(16)
    pkt[0] = 0x03
    pkt[-1] = sum(pkt[:15]) & 0xFF
    _send(c, bytes(pkt))
    _pump(5.0)

    if c.peripheral is not None:
        try:
            c.manager.cancelPeripheralConnection_(c.peripheral)
        except Exception:
            pass
    _pump(0.5)

    print("\n=== SUMMARY ===")
    print(f"notify_states: {c.notify_states}")
    print(f"notifications: {len(c.notifications)}")
    for n in c.notifications[:20]:
        print(f"  {n['ts']}  char={n['char']}  len={n['len']}  {n['raw_hex']}")
    return 0 if c.notifications else 3


if __name__ == "__main__":
    sys.exit(main())