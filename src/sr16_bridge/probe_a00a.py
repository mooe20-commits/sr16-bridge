"""H1 test: write to A00A:B002, subscribe to A00A:B003 for notify response.

Hypothesis: the SR16's actual vendor protocol lives on the A00A service,
not the FF00 service. Earlier sweep proved FF02 (FF00 RX) ignores all
single-byte writes. The 0xA00A service is the "vendor main" per the
original HANDOFF notes — chars are:
    B002: read,write,write-no-response,notify
    B003: read,notify

B002's CCCD isn't available (Code 10), but B003 has read+notify and
should respond if A00A is the real vendor channel.

We try the full Colmi R02 opcode set (0x01, 0x03, 0x12, 0x13, 0x15,
0x16, 0x19, 0x1A) and also a few vendor-extension candidates
(0xA0, 0xA1, 0xB0, 0xC0).

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.probe_a00a
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

A00A_SERVICE_UUID = "0000A00A-0000-1000-8000-00805F9B34FB"
A00A_B002_TX_UUID = "0000B002-0000-1000-8000-00805F9B34FB"  # write target
A00A_B003_RX_UUID = "0000B003-0000-1000-8000-00805F9B34FB"  # notify target

# Colmi R02 opcodes + a few vendor candidates
OPCODES_TO_TEST = [
    0x01, 0x03, 0x12, 0x13, 0x15, 0x16, 0x19, 0x1A,  # Colmi set
    0xA0, 0xA1, 0xB0, 0xC0,  # vendor candidates
]

PACKET_LEN = 16


def _pump(seconds: float) -> None:
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_packet(command: int, sub_data: bytes = b"") -> bytes:
    pkt = bytearray(PACKET_LEN)
    pkt[0] = command
    pkt[1:1 + len(sub_data)] = sub_data
    pkt[-1] = sum(pkt[:15]) & 0xFF
    return bytes(pkt)


class _Central(NSObject):
    def init(self):
        self = objc.super(_Central, self).init()
        if self is None:
            return None
        self.manager = None
        self.peripheral = None
        self.b002_char = None  # write to this
        self.b003_char = None  # subscribe to this
        self.b003_state = "pending"
        self.notifications = []
        self.state = "init"
        return self

    def centralManagerDidUpdateState_(self, manager):
        if manager.state() != 5:
            return
        self.manager = manager
        manager.scanForPeripheralsWithServices_options_(None, None)
        print("[CB] scanning...")

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
        print(f"[CB] FOUND: {ident}  '{name}'  rssi={rssi}")
        manager.stopScan()
        peripheral.setDelegate_(self)
        manager.connectPeripheral_options_(peripheral, None)
        self.state = "connecting"

    def centralManager_didConnectPeripheral_(self, manager, peripheral):
        print("[CB] CONNECTED")
        self.state = "discovering"
        peripheral.discoverServices_(None)

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            print(f"[CB] discoverServices error: {error}")
            return
        services = peripheral.services() or []
        print(f"[CB] services: {len(services)}")
        target = CBUUID.UUIDWithString_(A00A_SERVICE_UUID)
        for svc in services:
            if svc.UUID().isEqual_(target):
                print(f"[CB]   service {svc.UUID()}  ← TARGET")
                peripheral.discoverCharacteristics_forService_(None, svc)
            else:
                print(f"[CB]   service {svc.UUID()}")

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        b002 = CBUUID.UUIDWithString_(A00A_B002_TX_UUID)
        b003 = CBUUID.UUIDWithString_(A00A_B003_RX_UUID)
        for ch in (service.characteristics() or []):
            cuid = ch.UUID()
            props = ch.properties()
            prop_str = []
            if props & 0x02: prop_str.append("read")
            if props & 0x04: prop_str.append("write")
            if props & 0x08: prop_str.append("write-no-response")
            if props & 0x10: prop_str.append("notify")
            print(f"[CB]   A00A  {cuid.UUIDString()}  props={','.join(prop_str) or '(none)'}")
            if cuid.isEqual_(b002):
                self.b002_char = ch
                print(f"[CB]   → B002 captured (write target)")
            elif cuid.isEqual_(b003):
                self.b003_char = ch
                self.b003_state = "pending"
                print(f"[CB]   → B003 captured, subscribing")
                peripheral.setNotifyValue_forCharacteristic_(True, ch)
        if self.b002_char is not None and self.b003_char is not None:
            self.state = "ready"

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            self.b003_state = f"err: {error}"
            print(f"[CB] notify-state ERR: {error}")
            return
        on = characteristic.isNotifying()
        self.b003_state = "on" if on else "off"
        print(f"[CB] NOTIFY {'ON' if on else 'OFF'} for B003")

    def peripheral_didWriteValueForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            print(f"[CB] write error: {error}")
        else:
            print(f"[CB] write OK to {characteristic.UUID()}")

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
        sn = characteristic.UUID().UUIDString().upper().replace("0000", "")[:4]
        print(f"[RX] NOTIFY on {sn}: {len(raw)} bytes  {hex_str}")
        self.notifications.append({"ts": _now(), "char": sn, "len": len(raw), "raw_hex": hex_str})


def _send(central, packet: bytes) -> None:
    data = NSDataClass.dataWithBytes_length_(packet, len(packet))
    central.peripheral.writeValue_forCharacteristic_type_(data, central.b002_char, 0)
    print(f"[TX→B002] {packet.hex()}  (cmd=0x{packet[0]:02x})")


def main() -> int:
    print(f"[probe_a00a] target SR16 {SR16_UUID}")
    print(f"[probe_a00a] write→A00A:B002, subscribe→A00A:B003")
    print(f"[probe_a00a] opcodes: {[hex(x) for x in OPCODES_TO_TEST]}")

    c = _Central.alloc().init()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + 30
    while time.time() < deadline:
        _pump(0.3)
        if c.state in ("ready", "failed"):
            break

    if c.b002_char is None or c.b003_char is None:
        print(f"\n[probe_a00a] FAIL: state={c.state}, b002={c.b002_char}, b003={c.b003_char}")
        return 1

    _pump(1.5)
    print(f"\n[probe_a00a] READY. b003_state={c.b003_state}")

    for opcode in OPCODES_TO_TEST:
        pkt = _make_packet(opcode)
        print(f"\n--- write 0x{opcode:02x} to B002 ---")
        _send(c, pkt)
        _pump(1.5)
        if c.notifications:
            print(f"!!! HIT after 0x{opcode:02x}: {len(c.notifications)} total notifies")
            break

    if c.peripheral is not None:
        try:
            c.manager.cancelPeripheralConnection_(c.peripheral)
        except Exception:
            pass
    _pump(0.5)

    print("\n=== SUMMARY ===")
    print(f"b003_state: {c.b003_state}")
    print(f"notifications: {len(c.notifications)}")
    for n in c.notifications[:20]:
        print(f"  {n['ts']}  char={n['char']}  len={n['len']}  {n['raw_hex']}")
    return 0 if c.notifications else 3


if __name__ == "__main__":
    sys.exit(main())