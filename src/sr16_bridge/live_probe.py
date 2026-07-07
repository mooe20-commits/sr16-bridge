"""sr16-bridge live_probe: one-shot protocol probe against the live SR16.

Connect via CoreBluetooth, subscribe to FF01 notifications, send CMD_BATTERY
(0x03), capture the response, then optionally probe set_time + HR-log.

This bypasses the bleak+system-Profiler path entirely. Designed to run during
the ring's 10-30s advertise window.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.live_probe
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.live_probe --battery-only
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.live_probe --sync 1
"""
from __future__ import annotations

import argparse
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
from Foundation import NSObject, NSData as NSDataClass

# Vendor UUIDs confirmed via capture_sr16.py — see session 6 BREAKTHROUGH.
SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"
FF00_SERVICE_UUID = "0000FF00-0000-1000-8000-00805F9B34FB"
FF01_TX_CHAR_UUID = "0000FF01-0000-1000-8000-00805F9B34FB"  # notify (ring→us)
FF02_RX_CHAR_UUID = "0000FF02-0000-1000-8000-00805F9B34FB"  # write (us→ring)

# Colmi R02 opcodes (tahnok reference) — same packet shape, 16-byte packets
CMD_BATTERY             = 0x03
CMD_SET_TIME            = 0x01
CMD_READ_HEART_RATE_LOG = 0x15

PACKET_LEN = 16


def _pump(seconds: float) -> None:
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    NSRunLoop.currentRunLoop().runUntilDate_(end)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cbuuid(s: str) -> objc.objc_object:
    from CoreBluetooth import CBUUID as _CBUUID
    return _CBUUID.UUIDWithString_(s)


def _checksum(packet: bytes) -> int:
    return sum(packet) & 0xFF


def _make_packet(command: int, sub_data: bytes = b"") -> bytes:
    pkt = bytearray(PACKET_LEN)
    pkt[0] = command
    pkt[1:1 + len(sub_data)] = sub_data
    pkt[-1] = _checksum(pkt[:15])
    return bytes(pkt)


def _set_time_subdata() -> bytes:
    """BCD timestamp + language flag, matching tahnok/colmi_r02_client."""
    now = datetime.now(timezone.utc)
    yy = now.year % 100
    bcd = lambda x: ((x // 10) << 4) | (x % 10)
    return bytes([
        bcd(yy), bcd(now.month), bcd(now.day),
        bcd(now.hour), bcd(now.minute), bcd(now.second),
        0x01,  # 0 = Chinese, 1 = English
    ])


def _decode_battery_response(pkt: bytes) -> dict:
    """Decode CMD_BATTERY (0x03) response. Layout per tahnok/Colmi:
    byte 0 = 0x03 (echo) or 0x83 (error)
    byte 1 = battery level (%)
    byte 2..15 = padding
    """
    if len(pkt) != PACKET_LEN:
        return {"raw_hex": pkt.hex(), "error": f"unexpected length {len(pkt)}"}
    return {
        "raw_hex": pkt.hex(),
        "cmd": pkt[0],
        "battery_pct": pkt[1],
        "is_error": bool(pkt[0] & 0x80),
    }


class _Central(NSObject):
    def init(self):
        self = objc.super(_Central, self).init()
        if self is None:
            return None
        self.manager = None
        self.peripheral = None
        self.ff01_char = None
        self.ff02_char = None
        self.notifications = []
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
        print(f"[CB] CONNECTED")
        self.connected_at = time.time()
        self.state = "discovering"
        # Find FF00 service.
        ff00_uuid = _cbuuid(FF00_SERVICE_UUID)
        peripheral.discoverServices_([ff00_uuid])

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
        target = _cbuuid(FF00_SERVICE_UUID)
        found = False
        for svc in services:
            print(f"[CB]   service {svc.UUID()}")
            if svc.UUID().isEqual_(target):
                found = True
                peripheral.discoverCharacteristics_forService_(None, svc)
        if not found:
            print(f"[CB] WARNING: FF00 service not found — protocol will not work")

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ):
        if error:
            print(f"[CB] discoverChars error: {error}")
            return
        ff01_uuid = _cbuuid(FF01_TX_CHAR_UUID)
        ff02_uuid = _cbuuid(FF02_RX_CHAR_UUID)
        for ch in (service.characteristics() or []):
            cuid = ch.UUID()
            if cuid.isEqual_(ff01_uuid):
                self.ff01_char = ch
                print(f"[CB]   FF01 (TX, notify) found")
                peripheral.setNotifyValue_forCharacteristic_(True, ch)
            elif cuid.isEqual_(ff02_uuid):
                self.ff02_char = ch
                print(f"[CB]   FF02 (RX, write) found")
        self.state = "ready"

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(
        self, peripheral, characteristic, error
    ):
        if error:
            print(f"[CB] notify-state error: {error}")
            return
        if characteristic.isNotifying():
            print(f"[CB] NOTIFY ON for {characteristic.UUID()}")

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
            raw = data
        hex_str = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)
        print(f"[CB] NOTIFY {len(raw)} bytes: {hex_str}")
        self.notifications.append({"ts": _now_iso(), "raw_hex": hex_str})


def _send_packet(central, packet: bytes) -> None:
    """Write a packet to FF02 (RX char). NSData wraps the bytes."""
    data = NSDataClass.dataWithBytes_length_(packet, len(packet))
    central.peripheral.writeValue_forCharacteristic_withResponse_(data, central.ff02_char, True)
    print(f"[TX] {packet.hex()}")


def _battery_probe(central) -> None:
    print("\n=== CMD_BATTERY (0x03) ===")
    pkt = _make_packet(CMD_BATTERY)
    _send_packet(central, pkt)
    _pump(3.0)
    if central.notifications:
        last = central.notifications[-1]["raw_hex"]
        try:
            raw = bytes.fromhex(last)
            decoded = _decode_battery_response(raw)
            print(f"[RX] decoded: {decoded}")
        except Exception as exc:
            print(f"[RX] decode error: {exc}")


def _set_time_probe(central) -> None:
    print("\n=== CMD_SET_TIME (0x01) ===")
    pkt = _make_packet(CMD_SET_TIME, _set_time_subdata())
    _send_packet(central, pkt)
    _pump(3.0)
    if central.notifications:
        last = central.notifications[-1]["raw_hex"]
        print(f"[RX] last notify: {last}")


def main() -> int:
    p = argparse.ArgumentParser(description="Live SR16 protocol probe (one-shot)")
    p.add_argument("--battery-only", action="store_true", help="only run CMD_BATTERY probe")
    p.add_argument("--timeout", type=int, default=60, help="seconds total")
    p.add_argument("--sync", type=int, default=0, metavar="DAYS",
                   help="if >0, attempt a CMD_READ_HEART_RATE_LOG for DAYS days after set_time")
    args = p.parse_args()

    print(f"[live_probe] target SR16 {SR16_UUID}")
    print(f"[live_probe] FF00 service, FF01=notify (TX), FF02=write (RX)")
    print(f"[live_probe] timeout {args.timeout}s, battery_only={args.battery_only}, sync_days={args.sync}")

    c = _Central.alloc().init()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        _pump(0.3)
        if c.state == "ready" and c.ff01_char is not None and c.ff02_char is not None:
            break
        if c.state == "failed":
            return 2

    if c.state != "ready" or c.ff02_char is None:
        print(f"[live_probe] TIMEOUT — state={c.state}, ff01={c.ff01_char}, ff02={c.ff02_char}")
        return 1

    # Run probes in order.
    _battery_probe(c)
    if not args.battery_only:
        _set_time_probe(c)
        if args.sync > 0:
            print(f"\n=== CMD_READ_HEART_RATE_LOG (0x15) days={args.sync} ===")
            # First day = today. Sub-data: 4-byte big-endian timestamp? Tahnok uses
            # one packet per day. Try simplest: just request day 0 (today).
            pkt = _make_packet(CMD_READ_HEART_RATE_LOG, bytes([0]))
            _send_packet(c, pkt)
            _pump(5.0)
            for n in c.notifications[-3:]:
                print(f"  notify: {n['raw_hex']}")

    # Disconnect cleanly so we don't leave a phantom GATT connection.
    if c.peripheral is not None and c.manager is not None:
        try:
            c.manager.cancelPeripheralConnection_(c.peripheral)
        except Exception:
            pass

    print("\n=== SUMMARY ===")
    print(f"notifications received: {len(c.notifications)}")
    for n in c.notifications[:5]:
        print(f"  {n['ts']}  {n['raw_hex']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())