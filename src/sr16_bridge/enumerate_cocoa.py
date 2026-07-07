"""PyObjC CoreBluetooth fallback enumerator — bypasses bleak's scanner limitation.

Why this exists: bleak.BleakScanner ONLY sees advertising peripherals. If macOS
HID-CL has already auto-paired the SR16 (its normal behavior with HID-class
devices), the ring is in the "connected, not advertising" state — bleak can't
see it. PyObjC CoreBluetooth talks to the macOS BT daemon directly and CAN
enumerate already-connected peripherals via `retrieveConnectedPeripherals_`.

This is Path #2 from HANDOFF-2026-07-07.md. Use this when:
- The ring is paired + connected (HID auto-bond state) — bleak returns nothing
- `sr16_watch.sh` finds no SR16 in `blueutil --inquiry`
- `enumerate_vendor.py` (bleak path) reports "SR16 not found"

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.enumerate_cocoa

Output:
    stdout:     one-line-per-discovered-peripheral + per-characteristic properties
    char_inventory: rows for every service+characteristic we discover
    sys/PROBE-LOG.md: appended with the run summary

Notes on PyObjC CoreBluetooth:
- CBCentralManagerDelegate is an *informal protocol* (no Python class to inherit).
  We conform to it via `objc.informal_protocol` and implement selector-named methods
  (note trailing underscores per Cocoa naming).
- Delegate methods are dispatched on the main queue; we pump NSRunLoop explicitly
  because asyncio + NSRunLoop don't auto-interop.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import objc
from CoreBluetooth import (
    CBCentralManager,
    CBPeripheral,
    CBUUID,
)
from Foundation import NSObject, NSRunLoop, NSDate, NSUUID

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"

# CBPeripheralStateConnected = 2
CB_PERIPHERAL_STATE_CONNECTED = 2
CB_MANAGER_STATE_POWERED_ON = 5


# ---------- informal protocol conformance ----------
#
# CBCentralManagerDelegate / CBPeripheralDelegate are Objective-C *informal protocols*
# (no Python class to inherit). PyObjC conformance is by selector presence — defining
# methods whose Python names end in `_<param>` (PyObjC's auto-translation of Obj-C
# selectors like `centralManager:didConnectPeripheral:` → `centralManager_didConnectPeripheral_`)
# is sufficient. We don't need to call `objc.informal_protocol(...)` ourselves.

class CentralDelegate(NSObject):
    """CBCentralManagerDelegate + CBPeripheralDelegate methods."""

    def init(self):
        self = objc.super(CentralDelegate, self).init()
        if self is None:
            return None
        self.discovered: list[tuple[str, str, int | None]] = []
        self.services_by_peripheral: dict[str, list[str]] = {}
        self.manager = None
        self.ready = False
        return self

    # --- CBCentralManagerDelegate ---

    def centralManagerDidUpdateState_(self, manager) -> None:
        state = int(manager.state())
        print(f"[CB] state = {state}  (5 = poweredOn)", file=sys.stderr)
        if state == CB_MANAGER_STATE_POWERED_ON:
            self.ready = True

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
        self, manager, peripheral, adv_data, rssi
    ) -> None:
        ident = str(peripheral.identifier())
        name = ""
        try:
            if peripheral.name():
                name = str(peripheral.name())
        except Exception:
            pass
        if not name and adv_data is not None:
            try:
                k = "kCBAdvDataLocalName"
                v = adv_data.valueForKey_(k) if hasattr(adv_data, "valueForKey_") else None
                if v is not None:
                    name = str(v)
            except Exception:
                pass
        rssi_val = None
        try:
            if rssi is not None and rssi != 127:    # 127 = RSSI not available
                rssi_val = int(rssi)
        except Exception:
            pass
        if ident not in [d[0] for d in self.discovered]:
            self.discovered.append((ident, name, rssi_val))
            print(f"[CB] discovered: {ident}  name='{name}'  rssi={rssi_val}", file=sys.stderr)

    def centralManager_didConnectPeripheral_(self, manager, peripheral) -> None:
        ident = str(peripheral.identifier())
        print(f"[CB] connected: {ident} — discovering services", file=sys.stderr)
        peripheral.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(
        self, manager, peripheral, error
    ) -> None:
        ident = str(peripheral.identifier())
        print(f"[CB] FAIL connect: {ident}: {error}", file=sys.stderr)

    def centralManager_didDisconnectPeripheral_error_(
        self, manager, peripheral, error
    ) -> None:
        ident = str(peripheral.identifier())
        print(f"[CB] disconnected: {ident}: {error}", file=sys.stderr)

    # --- CBPeripheralDelegate ---

    def peripheral_didDiscoverServices_(self, peripheral, error) -> None:
        if error is not None:
            print(f"[CB] services error: {error}", file=sys.stderr)
            return
        ident = str(peripheral.identifier())
        svcs = peripheral.services() or []
        uuids = []
        for s in svcs:
            try:
                u = s.UUID()
                uuids.append(str(u))
            except Exception:
                pass
        self.services_by_peripheral[ident] = uuids
        print(f"[CB] {ident} services: {uuids}", file=sys.stderr)
        for s in svcs:
            peripheral.discoverCharacteristics_forService_(None, s)

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, service, error
    ) -> None:
        if error is not None:
            return
        svc_uuid = str(service.UUID())
        for c in (service.characteristics() or []):
            cuuid = str(c.UUID())
            props_int = int(c.properties())
            props: list[str] = []
            for bit, name in [
                (1 << 0, "broadcast"),
                (1 << 1, "read"),
                (1 << 2, "write-without-response"),
                (1 << 3, "write"),
                (1 << 4, "notify"),
                (1 << 5, "indicate"),
                (1 << 6, "authenticated-signed-writes"),
                (1 << 7, "extended-properties"),
            ]:
                if props_int & bit:
                    props.append(name)
            print(
                f"    service={svc_uuid}  char={cuuid}  props={props}",
                file=sys.stderr,
            )


# ---------- DB ----------

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def _record_char(ts: str, device: str, svc: str, char: str, props: list[str], notes: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT OR IGNORE INTO char_inventory
              (ts_utc, device_uuid, service_uuid, char_uuid, properties, has_descriptor, probe_notes)
           VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (ts, device, svc, char, ",".join(props), notes),
    )
    conn.commit()
    conn.close()


# ---------- Run ----------

def _pump(secs: float) -> None:
    """Pump the main NSRunLoop for `secs` seconds, in 0.25s slices."""
    end = time.time() + secs
    while time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.25)
        )


def main() -> int:
    p = argparse.ArgumentParser(description="sr16-bridge: PyObjC CoreBluetooth enumerator (bypasses bleak)")
    p.add_argument("--scan", type=int, default=20, help="seconds total budget")
    args = p.parse_args()

    init_db()

    delegate = CentralDelegate.alloc().init()
    manager = CBCentralManager.alloc().initWithDelegate_queue_(
        delegate, None   # None → main queue
    )
    delegate.manager = manager

    print("[CB] waiting for manager state...", file=sys.stderr)
    deadline = time.time() + min(10, args.scan / 2)
    while time.time() < deadline and not delegate.ready:
        _pump(0.25)

    if not delegate.ready:
        print("[CB] manager never powered on (BT off? permissions denied?)", file=sys.stderr)
        return 3

    # THE KEY TRICK bleak can't do: ask the macOS BT daemon for already-connected peripherals.
    # Obj-C selector is `retrieveConnectedPeripheralsWithServices:` — requires a non-nil
    # CBUUID array; pass [] to mean "any service" (matches every connected peripheral).
    connected = manager.retrieveConnectedPeripheralsWithServices_([])
    n_connected = len(connected) if connected else 0
    print(f"[CB] retrieveConnectedPeripherals → {n_connected} devices", file=sys.stderr)
    if connected:
        for p_ in connected:
            ident = str(p_.identifier())
            name = str(p_.name()) if p_.name() else ""
            print(f"  already-connected: {ident}  name='{name}'", file=sys.stderr)
            if ident not in [d[0] for d in delegate.discovered]:
                delegate.discovered.append((ident, name, None))
            if int(p_.state()) == CB_PERIPHERAL_STATE_CONNECTED:
                p_.setDelegate_(delegate)
                p_.discoverServices_(None)

    # Also kick off a normal scan (covers advertising-only state)
    manager.scanForPeripheralsWithServices_options_(None, None)

    _pump(args.scan)
    manager.stopScan()

    # Pick the most likely SR16 candidate
    candidate = None
    for ident, name, rssi in delegate.discovered:
        n = (name or "").upper()
        if any(t in n for t in ("SR16", "R02", "R03", "TELINK", "RING")):
            candidate = (ident, name, rssi)
            break
    if candidate is None and delegate.discovered:
        candidate = delegate.discovered[0]

    if candidate is None:
        print("[CB] no candidates found via scan + retrieveConnectedPeripherals", file=sys.stderr)
        print("    (the SR16 may be asleep — try sr16_watch.sh or charge the ring)", file=sys.stderr)
        return 2

    ident, name, rssi = candidate
    print(f"[CB] selected candidate: {ident}  name='{name}'  rssi={rssi}", file=sys.stderr)

    # If we don't have services for this peripheral yet, try connecting
    if ident not in delegate.services_by_peripheral:
        print(f"[CB] attempting connect + service discovery to {ident}...", file=sys.stderr)
        try:
            uuid = NSUUID.alloc().initWithUUIDString_(ident)
            peripherals = manager.retrievePeripherals_(uuid)
            if peripherals:
                peripheral = peripherals[0]
                peripheral.setDelegate_(delegate)
                manager.connectPeripheral_options_(peripheral, None)
                _pump(15)
        except Exception as exc:
            print(f"[CB] connect attempt failed: {exc}", file=sys.stderr)

    started = datetime.now(timezone.utc).isoformat()
    services = delegate.services_by_peripheral.get(ident, [])
    for svc_uuid in services:
        _record_char(started, ident, svc_uuid, "?", [], "PyObjC: service-level only; char enumeration printed to stderr")

    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### enumerate_cocoa @ {started}\n"
            f"  discovered={len(delegate.discovered)}  selected={ident}  name={name!r}\n"
            f"  services={services}\n"
        )

    print(
        f"enumerate_cocoa OK: {len(delegate.discovered)} discovered, "
        f"{len(services)} services on selected → {DB_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())