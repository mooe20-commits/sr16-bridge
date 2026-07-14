"""
ingest_snoop_to_db — parse a btsnoop_hci.log and write SR16 vendor-protocol
records into ~/health/sr16.db.

Writes to:
  - a3_hourly   — 16B / 8B bulk records (steps, cal, distance, HR aggregate)
  - status_events — cmd 0x04/05/06 status responses + cmd 0x13 device-info

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.ingest_snoop_to_db <btsnoop_hci.log>
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.ingest_snoop_to_db --tz-offset 3 <btsnoop>

TZ offset semantics (handoff-2026-07-13-night finding #3):
  - val16 = ring-internal "seconds since midnight" — NOT aligned to UTC nor
    to local. Cross-comparison with RWfit's HR detail page showed a consistent
    3-hour offset for the operator (EEST, UTC+3).
  - Anchor = the timestamp from the first cmd 0x09 (begin-sync) BCD payload
    in the snoop, falling back to snoop mtime. This anchors the date.
  - hour_utc = (val16 // 3600) % 24 — used for ordering and dedupe.
  - hour_local = (hour_utc + tz_offset) % 24 — what the user sees.
  - date_local = anchor.date() (with TZ offset applied if you want it strict).

Smuggled records (handoff-2026-07-13-night finding #5):
  cmd 0x13 device-info packets can carry one or more 16B bulk records inside
  their body (the "31 e7" / 0xE731 marker smuggled inside what looks like a
  25B device-info response). extract_smuggled_records() pulls those out so
  they get ingested into a3_hourly just like 0xA3 records.

Status responses (handoff-2026-07-13-night finding #4):
  cmd 0x04 / 0x05 / 0x06 are dropped by the bulk-only path. They're written
  to status_events instead, with payload_u16 capturing the last 2 bytes —
  which for cmd 0x06 is the live counter (possibly live HR / live steps).
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import subprocess
import sys
from collections import Counter

from .decode_0ab import decode_notify
from .protocol import (
    CMD_BLOCK_16B_4REC,
    CMD_BLOCK_16B_5REC,
    CMD_BLOCK_8B_13REC,
    CMD_BLOCK_8B_14REC,
    CMD_BYTE_GRID,
    CMD_DEVICE_INFO,
    CMD_STATUS_A,
    CMD_STATUS_B,
    CMD_STATUS_C,
    CMD_TODAY_BLOCK,
    extract_smuggled_records,
)


DB_PATH = os.path.expanduser("~/health/sr16.db")
DEVICE_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"

# Operator is in EEST (UTC+3). Configurable via --tz-offset.
DEFAULT_TZ_OFFSET = 3


def pull_snoop_values(logfile: str) -> list:
    """Extract notification values from a btsnoop log via tshark."""
    r = subprocess.run(
        ["tshark", "-r", logfile,
         "-Y", 'btatt.opcode==0x1b and btatt.handle==0x0040',
         "-T", "fields", "-e", "btatt.value"],
        capture_output=True, text=True)
    return [w for w in r.stdout.strip().split("\n") if w]


def parse_u16_pair(data: bytes) -> tuple[int, ...]:
    """Parse 12B data tail as 6 x u16 LE."""
    if len(data) != 12:
        raise ValueError(f"data tail is not 12B: {len(data)}")
    return tuple(int.from_bytes(data[i*2:(i+1)*2], "little") for i in range(6))


def _bcd_decode(b: int) -> int:
    """Decode one BCD byte (0..99)."""
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def parse_begin_sync_timestamp(value: str) -> _dt.datetime | None:
    """Extract the BCD datetime from a cmd 0x09 begin-sync packet.

    Layout (per protocol.py): ab 01 00 09 <frame_seq LE u16> <category>
    <sub_type> <status_flag> <6B BCD: YY MM DD HH MM SS>
    Hex string is the value field from tshark.
    """
    try:
        b = bytes.fromhex(value)
    except ValueError:
        return None
    if len(b) < 15 or b[0] != 0xAB or b[3] != 0x09:
        return None
    # bytes 9..14 = BCD YYMMDDHHMMSS
    yy = _bcd_decode(b[9])
    mo = _bcd_decode(b[10])
    da = _bcd_decode(b[11])
    hh = _bcd_decode(b[12])
    mi = _bcd_decode(b[13])
    se = _bcd_decode(b[14])
    try:
        return _dt.datetime(2000 + yy, mo, da, hh, mi, se,
                            tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def find_anchor(notif_hexes: list, fallback_mtime: _dt.datetime) -> _dt.datetime:
    """Pick the canonical UTC date-anchor for this snoop.

    Prefer the first cmd 0x09 begin-sync timestamp; fall back to the snoop's
    mtime. Returns a UTC-aware datetime.
    """
    for v in notif_hexes:
        ts = parse_begin_sync_timestamp(v)
        if ts is not None:
            return ts
    return fallback_mtime


def compute_hours(val16: int, tz_offset: int) -> tuple[int, int]:
    """Convert a ring val16 to (hour_utc, hour_local).

    val16 is "seconds since midnight" of some ring-internal day (NOT
    necessarily UTC). hour_utc is the naive UTC hour boundary; hour_local
    applies the operator's TZ offset.
    """
    hour_utc = (val16 // 3600) % 24
    hour_local = (hour_utc + tz_offset) % 24
    return hour_utc, hour_local


def is_bulk_cmd(cmd: int) -> bool:
    return cmd in (
        CMD_BLOCK_16B_4REC,   # 0x43
        CMD_BLOCK_16B_5REC,   # 0x53
        CMD_BLOCK_8B_13REC,   # 0x6B
        CMD_BLOCK_8B_14REC,   # 0x73
        CMD_TODAY_BLOCK,      # 0xA3
    )


def is_status_cmd(cmd: int) -> bool:
    return cmd in (CMD_STATUS_A, CMD_STATUS_B, CMD_STATUS_C, CMD_DEVICE_INFO)


def main() -> int:
    args = sys.argv[1:]
    tz_offset = DEFAULT_TZ_OFFSET
    if "--tz-offset" in args:
        i = args.index("--tz-offset")
        tz_offset = int(args[i + 1])
        args = args[:i] + args[i + 2:]

    if not args:
        print("Usage: ingest_snoop_to_db.py [--tz-offset N] <btsnoop_hci.log>")
        return 1
    log = args[0]
    if not os.path.exists(log):
        print(f"NOT FOUND: {log}")
        return 1

    notifs = pull_snoop_values(log)
    print(f"Pulled {len(notifs)} notifications from {log}")
    print(f"TZ offset: UTC{tz_offset:+d}h (operator-local)")

    # Find anchor (cmd 0x09 begin-sync) before iterating
    fallback_mtime = _dt.datetime.fromtimestamp(
        os.path.getmtime(log), tz=_dt.timezone.utc)
    anchor = find_anchor(notifs, fallback_mtime)
    date_local = anchor.date().isoformat()
    print(f"Anchor (UTC): {anchor.isoformat()}  →  date_local={date_local}")

    # Pass 1: classify every notification by cmd
    bulk_records = []      # (cmd, marker, val16, data_hex, source)
    status_records = []    # (cmd, frame_seq, cat, sub, flag, body_hex, payload_hex, payload_u16, raw_hex)
    cmd_counter: Counter = Counter()
    skipped_non_bulk = 0

    for v in notifs:
        d = decode_notify(v)
        cmd_counter[d.cmd] += 1
        seg = d.segment

        if is_bulk_cmd(d.cmd):
            for r in d.records:
                bulk_records.append(
                    (d.cmd, r.marker, r.val16, r.data.hex(), "0x{:02x}".format(d.cmd))
                )

        elif d.cmd == CMD_DEVICE_INFO:
            # Device info body may smuggle one or more 16B records
            # (handoff-2026-07-13-night finding #5)
            smuggled = extract_smuggled_records(d)
            for r in smuggled:
                bulk_records.append(
                    (d.cmd, r.marker, r.val16, r.data.hex(), "0x13-smuggled")
                )
            # And log the device-info itself as a status event for forensics
            body_hex = d.body.hex()
            payload_hex = body_hex[-4:] if len(body_hex) >= 4 else body_hex
            payload_u16 = int.from_bytes(d.body[-2:], "little") if len(d.body) >= 2 else None
            status_records.append(
                (d.cmd, seg.frame_seq, seg.category, seg.sub_type, seg.status_flag,
                 body_hex, payload_hex, payload_u16, v)
            )

        elif d.cmd in (CMD_STATUS_A, CMD_STATUS_B, CMD_STATUS_C):
            body_hex = d.body.hex()
            payload_hex = body_hex[-4:] if len(body_hex) >= 4 else body_hex
            payload_u16 = int.from_bytes(d.body[-2:], "little") if len(d.body) >= 2 else None
            status_records.append(
                (d.cmd, seg.frame_seq, seg.category, seg.sub_type, seg.status_flag,
                 body_hex, payload_hex, payload_u16, v)
            )

        else:
            skipped_non_bulk += 1

    print(f"\nCmd distribution: " +
          ", ".join(f"0x{c:02x}={n}" for c, n in sorted(cmd_counter.items())))
    print(f"  → bulk records:    {len(bulk_records)}")
    print(f"  → status records:  {len(status_records)}")
    print(f"  → skipped (other): {skipped_non_bulk}")

    # Per-record dedupe by (cmd, marker, val16, data) — cmd prevents
    # bulk vs. smuggled collisions if both happen to share a val16.
    seen = set()
    deduped_bulk = []
    for rec in bulk_records:
        key = rec[:4]
        if key in seen:
            continue
        seen.add(key)
        deduped_bulk.append(rec)
    if len(deduped_bulk) < len(bulk_records):
        print(f"  → bulk deduped:     {len(bulk_records) - len(deduped_bulk)} retransmits dropped")

    rows_16b = []  # 12B data tail = 6 metrics
    rows_8b = []   # 4B data tail = 1 u32
    for cmd, marker, val16, data_hex, src in deduped_bulk:
        data = bytes.fromhex(data_hex)
        if len(data) == 12:
            u16s = parse_u16_pair(data)
            rows_16b.append((cmd, marker, val16, *u16s, data_hex))
        elif len(data) == 4:
            u32 = int.from_bytes(data, "little")
            rows_8b.append((cmd, marker, val16, u32, data_hex))

    print(f"\n  16B (hourly+summary with 6 metrics): {len(rows_16b)}")
    print(f"   8B (older-day, u32 only):           {len(rows_8b)}")

    by_marker = Counter(m for _, m, *_ in rows_16b)
    print("  16B markers present: " +
          ", ".join(f"0x{m:04x}={c}" for m, c in sorted(by_marker.items())))

    by_source = Counter(s for *_, s in deduped_bulk)
    print("  bulk record source: " +
          ", ".join(f"{s}={n}" for s, n in sorted(by_source.items())))

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ---- a3_hourly insertion ----
    inserted = 0
    skipped_dup = 0
    # The ring's val16 is "seconds since midnight of the ring's reference
    # day". We assume the anchor's UTC date IS that reference day. Wrap
    # backwards by one day only if the val16 hour is clearly in the past
    # AND the anchor hour is late in the day (anchor captured late-evening,
    # record is from earlier in the same reference day — no wrap needed).
    # Wrap forwards by one day if hour_utc > anchor.hour+2 (record is from
    # the next ring-day, which shouldn't normally happen in a single snoop).
    for cmd, marker, val16, u0, u1, u2, u3, u4, u5, raw_hex in rows_16b:
        hour_utc, hour_local = compute_hours(val16, tz_offset)
        day_offset = 0
        if hour_utc > (anchor.hour + 2) % 24:
            # Future ring-day — keep same date; safer than guessing
            day_offset = 0
        ts_utc = (anchor + _dt.timedelta(days=day_offset)).replace(
            hour=hour_utc, minute=(val16 % 3600) // 60,
            second=val16 % 60, microsecond=0)
        try:
            cur.execute("""
                INSERT OR IGNORE INTO a3_hourly
                (ts_utc, device_uuid, date_local, hour_local, hour_utc, val16, marker,
                 reserved_u16_0, steps_raw, cal_raw, hr_agg_raw,
                 intensity_raw, dist_raw, raw_hex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts_utc.isoformat(), DEVICE_UUID, date_local,
                  hour_local, hour_utc, val16, marker,
                  u0, u1, u2, u3, u4, u5, raw_hex))
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped_dup += 1
        except sqlite3.Error as e:
            print(f"  insert error: {e}")

    # ---- status_events insertion ----
    # Dedupe by (cmd, frame_seq, body_hex) — same status notification
    # delivered twice within the snoop is a retransmit.
    seen_status = set()
    deduped_status = []
    for s in status_records:
        key = (s[0], s[1], s[5])
        if key in seen_status:
            continue
        seen_status.add(key)
        deduped_status.append(s)

    inserted_status = 0
    skipped_status_dup = 0
    # Stamp each status row with a timestamp anchored on the snoop's
    # first frame_seq order — simplest stable anchor.
    base_ts = anchor
    for i, (cmd, frame_seq, cat, sub, flag, body_hex, payload_hex,
            payload_u16, raw_hex) in enumerate(deduped_status):
        # Increment by 1 second per row to keep monotonic ordering
        ts = base_ts + _dt.timedelta(seconds=i)
        try:
            cur.execute("""
                INSERT OR IGNORE INTO status_events
                (ts_utc, device_uuid, cmd, frame_seq, category, sub_type,
                 status_flag, body_hex, payload_hex, payload_u16,
                 raw_hex, snoop_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts.isoformat(), DEVICE_UUID, cmd, frame_seq, cat, sub,
                  flag, body_hex, payload_hex, payload_u16, raw_hex, log))
            if cur.rowcount > 0:
                inserted_status += 1
            else:
                skipped_status_dup += 1
        except sqlite3.Error as e:
            print(f"  status insert error: {e}")

    con.commit()

    print("\n=== Result ===")
    print(f"  a3_hourly inserted:     {inserted}")
    print(f"  a3_hourly duplicates:   {skipped_dup}")
    print(f"  status_events inserted: {inserted_status}")
    print(f"  status_events dups:     {skipped_status_dup}")

    cur.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM a3_hourly")
    total, min_t, max_t = cur.fetchone()
    print(f"\n  Total a3_hourly rows now: {total}")
    print(f"  Date range: {min_t} → {max_t}")

    # Spot-check: 12 most recent a3_hourly rows
    cur.execute("""
        SELECT date_local, hour_local, hour_utc, val16, marker, steps_raw,
               cal_raw, dist_raw, hr_agg_raw, intensity_raw
        FROM a3_hourly
        ORDER BY ts_utc DESC LIMIT 12
    """)
    print("\n  Latest 12 a3_hourly rows (ts_utc desc):")
    print(f"    {'date':<10} {'HL':>3} {'HU':>3} {'val16':>6} {'marker':>6} "
          f"{'steps':>6} {'cal':>5} {'dist':>6} {'hr_a':>6} {'int':>5}")
    for r in cur.fetchall():
        d, hl, hu, v, m, st, ca, di, hr, it = r
        print(f"    {d:<10} {hl:>3d} {hu:>3d} {v:>6d} 0x{m:04x} "
              f"{st:>6d} {ca:>5d} {di:>6d} {hr:>6d} {it:>5d}")

    # Spot-check cmd 0x06 payload values (the "live counter")
    cur.execute("""
        SELECT cmd, COUNT(*) AS n, MIN(payload_u16), MAX(payload_u16),
               MIN(ts_utc), MAX(ts_utc)
        FROM status_events
        GROUP BY cmd
    """)
    print("\n  status_events by cmd:")
    print(f"    {'cmd':>4} {'n':>5} {'min_u16':>8} {'max_u16':>8} "
          f"{'first_ts':<25} {'last_ts':<25}")
    for r in cur.fetchall():
        cmd, n, mn, mx, fts, lts = r
        print(f"    0x{cmd:02x} {n:>5d} {mn!s:>8} {mx!s:>8} "
              f"{fts:<25} {lts:<25}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())