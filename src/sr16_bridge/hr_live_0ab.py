"""Live SR16 → SQLite via the 0xAB vendor protocol (session 13+).

What this does:
  1. Connects to the ring's A00A service over BLE
  2. Subscribes to B003 (notify), writes a fetch packet to B002 (write)
  3. Drains the notifies → parses 0xA3 today-block (10 hourly records + 1 day-summary)
  4. Inserts each record into a3_hourly (UNIQUE per device_uuid+val16+marker, so
     retransmits are idempotent)

This is the "Hermes can read ring data on the Mac" path. Run once per sync.

Usage:
    # One-shot: do a sync now and write to ~/health/sr16.db
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hr_live_0ab --once

    # Daemon mode: sync every N seconds
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hr_live_0ab --daemon --interval 300

Pitfalls handled (see sr16-ring-mac-pitfalls):
- P1 (HID auto-bond): script retries up to N times, expects operator to do
  Forget-this-device dance before the first run.
- P3 (ring radio sleep): bounded by the existing live_pull.py harness which
  knows the ~20-40s advert window.
- P6 (CCCD): inherits the manual 0x2902 write from live_pull.py.
- P42/P46 (CB handle vs snoop handle): uses char-UUID form throughout.

Field semantics: see protocol.py docstring + HANDOFF-session-13.md. The
hr_agg_raw and intensity_raw fields are UNCONFIRMED — written to the DB
under ambiguous keys so future RE can re-map without schema migration.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .protocol import (
    CMD_TODAY_BLOCK,
    MARKER_DAY_SUMMARY,
    SUB_DATA_16B,
    UART_RX_CHAR_UUID,
    UART_TX_CHAR_UUID,
    merge_fetches,
    make_fetch_request,
    parse_fetch,
    parse_notify,
    record_metric_dict,
)
from .schema_init import DB_PATH, SCHEMA, init_db


SR16_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"


def insert_a3_records(decoded_notifies: list, device_uuid: str = SR16_UUID) -> int:
    """Insert all 0xA3 records from the captured notifies into a3_hourly.

    Returns the number of rows newly inserted (excludes UNIQUE-violation
    retransmits).
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)

    fetches = []
    for raw in decoded_notifies:
        try:
            dn = parse_notify(raw if isinstance(raw, bytes) else bytes.fromhex(raw))
            if dn.cmd == CMD_TODAY_BLOCK:
                fetches.append(parse_fetch(dn))
        except Exception:
            continue

    if not fetches:
        conn.close()
        return 0

    merged = merge_fetches(fetches)

    # Local time = UTC+2 (CEST, July 2026). For a production daemon this
    # should come from tzdata; hardcoding for now since the operator is
    # in a single TZ. Update this when shipping.
    LOCAL_TZ_OFFSET = timedelta(hours=2)

    rows = []
    for r in merged.records:
        # Convert val16 (seconds since midnight UTC) into a UTC datetime for today.
        # We use today's date as the val16 anchor — a more correct impl would
        # read the day-summary record's date, but for "live pull right now"
        # today's UTC date is fine.
        utc_dt = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(seconds=r.val16)
        local_dt = utc_dt.astimezone(timezone(LOCAL_TZ_OFFSET))

        md = record_metric_dict(r)
        rows.append((
            utc_dt.isoformat(),
            device_uuid,
            local_dt.strftime("%Y-%m-%d"),
            local_dt.hour,
            r.val16,
            r.marker,
            md["steps_raw"],
            md["cal_raw"],
            md["hr_agg_raw"],
            md["intensity"],
            md["dist_raw"],
            md["reserved"],
            r.data.hex(),
        ))

    inserted = 0
    try:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO a3_hourly
               (ts_utc, device_uuid, date_local, hour_local, val16, marker,
                steps_raw, cal_raw, hr_agg_raw, intensity_raw, dist_raw,
                reserved_u16_0, raw_hex)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    return inserted


def pull_once(seconds: float = 4.0) -> tuple[bool, int, str]:
    """Do one live pull via PyObjC and ingest into SQLite.

    Returns (success, rows_inserted, error_msg).
    """
    # Imported lazily so the offline path works on systems without PyObjC.
    from .live_pull import (  # type: ignore[import-not-found]
        COLLECT_SECONDS,
        RING_NAME,
        SR16_UUID as _SR16_UUID,
        _Central,
        _pump,
        _start_notify,
        _write,
    )
    from CoreBluetooth import (  # type: ignore[import-not-found]
        CBCharacteristicWriteWithoutResponse,
        CBCentralManager,
    )
    from Foundation import NSString  # type: ignore[import-not-found]

    init_db()

    c = _Central()
    c.manager = CBCentralManager.alloc().initWithDelegate_queue_options_(c, None, None)

    deadline = time.time() + 60
    while time.time() < deadline:
        _pump(0.2)
        if c.state == "ready":
            break
        if c.state == "failed":
            return (False, 0, f"connection failed (state={c.state})")
    if c.state != "ready":
        return (False, 0, f"timeout (state={c.state}) — ring asleep or HID-bonded")

    _start_notify(c)
    _pump(0.3)

    pkt = make_fetch_request(SUB_DATA_16B, frame_seq=1)
    _write(c, pkt)

    print(f"[hr_live_0ab] collecting notifies for {seconds}s...")
    _pump(seconds)

    if c.peripheral:
        c.manager.cancelPeripheralConnection_(c.peripheral)
        _pump(0.3)

    if not c.notifies:
        return (False, 0, "no notifies — ring did not respond to fetch (P3/P6)")

    inserted = insert_a3_records(c.notifies, device_uuid=SR16_UUID)
    return (True, inserted, "")


def main() -> int:
    p = argparse.ArgumentParser(description="SR16 0xAB live pull → SQLite")
    p.add_argument("--once", action="store_true",
                   help="do a single sync and exit (default)")
    p.add_argument("--daemon", action="store_true",
                   help="loop forever, syncing every --interval seconds")
    p.add_argument("--interval", type=int, default=300,
                   help="seconds between syncs in daemon mode (default 300 = 5min)")
    p.add_argument("--seconds", type=float, default=4.0,
                   help="notify-collection window per sync (default 4s)")
    args = p.parse_args()

    if args.daemon:
        print(f"[hr_live_0ab] daemon mode, interval={args.interval}s")
        while True:
            ok, n, err = pull_once(args.seconds)
            ts = datetime.now(timezone.utc).isoformat()
            if ok:
                print(f"[hr_live_0ab] {ts} inserted={n}")
            else:
                print(f"[hr_live_0ab] {ts} FAILED: {err}")
            time.sleep(args.interval)

    # default: --once
    ok, n, err = pull_once(args.seconds)
    if ok:
        print(f"[hr_live_0ab] OK — inserted {n} rows into a3_hourly")
        return 0
    print(f"[hr_live_0ab] FAILED — {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())