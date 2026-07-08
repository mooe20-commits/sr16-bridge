"""sr16-bridge multi_notify_probe: subscribe to ALL notify-capable chars at once.

Session 7 strategy:
    Session 6.5 proved FF00/FF01/FF02 GATT path works end-to-end but the ring
    doesn't notify on FF01 after a CMD_BATTERY (0x03) write. We don't know if:
      (a) the SR16 uses a different opcode than Colmi R02, OR
      (b) the SR16 uses a different NOTIFY char (A00A:B002 or 0BC0:0BC1)

This probe subscribes to ALL notify-capable chars simultaneously, then sends
a single 1-byte write (0x00) to FF02. ANY notify received on any of the
subscribed chars tells us:
    * the ring DOES respond to single-byte writes
    * WHICH char the SR16 actually notifies on

Subsequent runs can sweep opcodes 0x00..0xFF to find which one gets a response.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.multi_notify_probe
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.multi_notify_probe --opcode 0x03
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.multi_notify_probe --sweep 0 16
"""
from __future__ import annotations

import argparse
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

# ALL notify-capable chars from the session-6 inventory.
NOTIFY_CHARS = [
    ("FF01", "0000FF01-0000-1000-8000-00805F9B34FB"),  # FF00 service TX
    ("B002", "0000B002-0000-1000-8000-00805F9B34FB"),  # A00A service main
    ("0BC1", "00000BC1-0000-1000-8000-00805F9B34FB"),  # 0BC0 service main
    ("0BC2", "00000BC2-0000-1000-8000-00805F9B34FB"),  # 0BC0 service sub
]
FF00_SERVICE_UUID = "0000FF00-0000-1000-8000-00805F9B34FB"
FF02_RX_CHAR_UUID = "0000FF02-0000-1000-8000-00805F9B34FB"  # write target

# Pad a write to 16 bytes — protocol.py uses 16-byte packets even if we only
# set 1 byte. The rest stay zero + checksum on the last byte.
PACKET_LEN = 16


def _pump(seconds: float) -> None:
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    NSRunLoop.currentRunLoop().runUntilDate_(end)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cbuuid(s: str):
    return CBUUID.UUIDWithString_(s)


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
        self.ff02_char = None
        # Map short char name → CBCharacteristic
        self.notify_chars: dict[str, object] = {}
        self.notify_state: dict[str, str] = {}  # short_name → "pending"/"on"/"off"/"error"
        self.notifications: list[dict] = []
        self.state = "init"
        self.connected_at = None
        return self

    # ---- CBCentralManagerDelegate ----

    def centralManagerDidUpdateState_(self, manager):
        st = manager.state()
        print(f"[CB] state = {st}  (5 = poweredOn)")
        if st != 5:
            return
        self.manager = manager
        self.state = "scanning"
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
        self.connected_at = time.time()
        self.state = "discovering"
        # Discover ALL services (no filter) so we can subscribe to notify chars
        # on A00A and 0BC0 too — not just FF00.
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
        print(f"[CB] services: {len(services)}")
        for svc in services:
            print(f"[CB]   service {svc.UUID()}")
            # Walk every service to find every notify char (FF00, A00A, 0BC0).
            peripheral.discoverCharacteristics_forService_(None, svc)

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        svc_uuid = service.UUID().UUIDString() if service.UUID() else "?"
        # Build target UUID set
        ff01 = _cbuuid("0000FF01-0000-1000-8000-00805F9B34FB")
        ff02 = _cbuuid(FF02_RX_CHAR_UUID)
        b002 = _cbuuid("0000B002-0000-1000-8000-00805F9B34FB")
        bc1 = _cbuuid("00000BC1-0000-1000-8000-00805F9B34FB")
        bc2 = _cbuuid("00000BC2-0000-1000-8000-00805F9B34FB")

        for ch in (service.characteristics() or []):
            cuid = ch.UUID()
            props = ch.properties()
            props_str = self._props_str(props)
            print(f"[CB]   {svc_uuid}  {cuid}  props={props_str}")

            if cuid.isEqual_(ff02):
                self.ff02_char = ch
                print(f"[CB]   → FF02 captured (write target)")

            # Subscribe to every char that has notify bit (0x10) set.
            if props & 0x10:
                # Match against our short-name set.
                short_name = None
                if cuid.isEqual_(ff01):
                    short_name = "FF01"
                elif cuid.isEqual_(b002):
                    short_name = "B002"
                elif cuid.isEqual_(bc1):
                    short_name = "0BC1"
                elif cuid.isEqual_(bc2):
                    short_name = "0BC2"
                if short_name and short_name not in self.notify_chars:
                    self.notify_chars[short_name] = ch
                    self.notify_state[short_name] = "pending"
                    print(f"[CB]   → subscribing {short_name} (notify)")
                    peripheral.setNotifyValue_forCharacteristic_(True, ch)
        # Heuristic: ready when FF02 found AND at least one notify char subscribed.
        if self.ff02_char is not None and self.notify_chars:
            self.state = "ready"

    @staticmethod
    def _props_str(props: int) -> str:
        out = []
        if props & 0x02: out.append("read")
        if props & 0x04: out.append("write")
        if props & 0x08: out.append("write-no-response")
        if props & 0x10: out.append("notify")
        if props & 0x20: out.append("indicate")
        return ",".join(out) or "(none)"

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        # Reverse-map the char back to its short name.
        cuid = characteristic.UUID()
        ff01 = _cbuuid("0000FF01-0000-1000-8000-00805F9B34FB")
        b002 = _cbuuid("0000B002-0000-1000-8000-00805F9B34FB")
        bc1 = _cbuuid("00000BC1-0000-1000-8000-00805F9B34FB")
        bc2 = _cbuuid("00000BC2-0000-1000-8000-00805F9B34FB")
        short_name = None
        if cuid.isEqual_(ff01): short_name = "FF01"
        elif cuid.isEqual_(b002): short_name = "B002"
        elif cuid.isEqual_(bc1): short_name = "0BC1"
        elif cuid.isEqual_(bc2): short_name = "0BC2"

        if error:
            self.notify_state[short_name or "?"] = f"error: {error}"
            print(f"[CB] notify-state ERROR on {short_name or cuid}: {error}")
            return
        is_notifying = characteristic.isNotifying()
        self.notify_state[short_name or "?"] = "on" if is_notifying else "off"
        print(f"[CB] NOTIFY {'ON' if is_notifying else 'OFF'} for {short_name or cuid}")

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
            raw = data
        hex_str = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)

        # Identify which char.
        cuid = characteristic.UUID()
        ff01 = _cbuuid("0000FF01-0000-1000-8000-00805F9B34FB")
        b002 = _cbuuid("0000B002-0000-1000-8000-00805F9B34FB")
        bc1 = _cbuuid("00000BC1-0000-1000-8000-00805F9B34FB")
        bc2 = _cbuuid("00000BC2-0000-1000-8000-00805F9B34FB")
        short_name = "?"
        if cuid.isEqual_(ff01): short_name = "FF01"
        elif cuid.isEqual_(b002): short_name = "B002"
        elif cuid.isEqual_(bc1): short_name = "0BC1"
        elif cuid.isEqual_(bc2): short_name = "0BC2"

        print(f"[RX] NOTIFY on {short_name}: {len(raw)} bytes  {hex_str}")
        self.notifications.append({
            "ts": _now_iso(),
            "char": short_name,
            "len": len(raw),
            "raw_hex": hex_str,
        })


def _send(central, packet: bytes) -> None:
    data = NSDataClass.dataWithBytes_length_(packet, len(packet))
    central.peripheral.writeValue_forCharacteristic_type_(data, central.ff02_char, 0)
    print(f"[TX] {packet.hex()}  (cmd=0x{packet[0]:02x})")


def main() -> int:
    p = argparse.ArgumentParser(description="Subscribe to ALL notify chars + write")
    p.add_argument("--opcode", type=lambda x: int(x, 0), default=0x00,
                   help="1-byte opcode to write (default: 0x00)")
    p.add_argument("--sub-data", type=str, default="",
                   help="hex sub-data bytes to append after opcode (default: empty)")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--wait-after-write", type=float, default=5.0,
                   help="seconds to wait for notify after write (default: 5)")
    p.add_argument("--sweep-start", type=lambda x: int(x, 0), default=None,
                   help="if set with --sweep-end, sweep opcodes START..END inclusive")
    p.add_argument("--sweep-end", type=lambda x: int(x, 0), default=None,
                   help="end of sweep range (inclusive, paired with --sweep-start)")
    args = p.parse_args()

    print(f"[multi_notify_probe] target SR16 {SR16_UUID}")
    print(f"[multi_notify_probe] will subscribe to: {[n for n, _ in NOTIFY_CHARS]}")
    print(f"[multi_notify_probe] write target: FF02")
    print(f"[multi_notify_probe] timeout={args.timeout}s")
    if args.sweep_start is not None and args.sweep_end is not None:
        a, b = args.sweep_start, args.sweep_end
        print(f"[multi_notify_probe] SWEEP opcodes 0x{a:02x}..0x{b:02x} ({b - a + 1} writes)")
    else:
        sub = bytes.fromhex(args.sub_data) if args.sub_data else b""
        print(f"[multi_notify_probe] single write: opcode=0x{args.opcode:02x} sub={sub.hex() or '(empty)'}")

    c = _Central.alloc().init()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        _pump(0.3)
        if c.state == "ready" and c.ff02_char is not None:
            break
        if c.state == "failed":
            return 2

    if c.state != "ready" or c.ff02_char is None:
        print(f"[multi_notify_probe] TIMEOUT — state={c.state}, "
              f"ff02={c.ff02_char}, notify_chars={list(c.notify_chars.keys())}")
        return 1

    print(f"\n[multi_notify_probe] READY. subscribed to {list(c.notify_chars.keys())}. "
          f"states={c.notify_state}")
    # Small extra pump so all NOTIFY ON ack callbacks arrive.
    _pump(1.0)

    if args.sweep_start is not None and args.sweep_end is not None:
        a, b = args.sweep_start, args.sweep_end
        for opcode in range(a, b + 1):
            pkt = _make_packet(opcode)
            print(f"\n--- sweep 0x{opcode:02x} ---")
            _send(c, pkt)
            _pump(args.wait_after_write)
            if c.notifications:
                print(f"!!! HIT on 0x{opcode:02x}: {len(c.notifications)} notify(ies) total")
    else:
        sub = bytes.fromhex(args.sub_data) if args.sub_data else b""
        pkt = _make_packet(args.opcode, sub)
        _send(c, pkt)
        _pump(args.wait_after_write)

    # Disconnect cleanly.
    if c.peripheral is not None and c.manager is not None:
        try:
            c.manager.cancelPeripheralConnection_(c.peripheral)
        except Exception:
            pass
    _pump(0.5)

    print("\n=== SUMMARY ===")
    print(f"subscribed: {list(c.notify_chars.keys())}")
    print(f"final notify states: {c.notify_state}")
    print(f"notifications received: {len(c.notifications)}")
    for n in c.notifications[:20]:
        print(f"  {n['ts']}  char={n['char']}  len={n['len']}  {n['raw_hex']}")
    return 0 if c.notifications else 3


if __name__ == "__main__":
    sys.exit(main())