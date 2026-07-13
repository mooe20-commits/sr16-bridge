"""
ingest_snoop_to_db — parse a btsnoop_hci.log and write 0xA3 hourly records
into ~/health/sr16.db (a3_hourly table).

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.ingest_snoop_to_db <btsnoop_hci.log>

Updated 2026-07-13 to handle firmware variants:
  - session-9 markers: 0xE031 (regular) / 0xE131 (day-summary)
  - session-14 markers: 0xE631 (regular) / 0xE731 (day-summary)

The marker high byte differs (0xE0/0xE1 vs 0xE6/0xE7). Both are written to
a3_hourly.marker as raw 16-bit ints and the UNIQUE constraint surfaces any
real duplicate. Day-summaries (high byte 0xE7 or 0xE1) are skipped from hourly
ingestion — they are day rollups, not per-hour.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import subprocess
import sys
from collections import Counter

from .decode_0ab import decode_notify


DB_PATH = os.path.expanduser("~/health/sr16.db")
DEVICE_UUID = "36BE6673-1486-2E90-38E9-3E097DB4CC43"

# Day-summary high-byte values (skip from hourly ingestion).
# NB (2026-07-13): on the firmware variant the ring ships with RWfit,
# BOTH 0xE631 and 0xE731 are present per packet with 32 distinct hourly
# val16s across 0xE731 (00:00-17:00 UTC) and 2 in 0xE631. So neither marker
# is unambiguously "summary" here. We ingest ALL records and let the
# (device, val16, marker) UNIQUE constraint dedupe across retransmits.
DAY_SUMMARY_HIGH_BYTES: tuple = ()


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


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: ingest_snoop_to_db.py <btsnoop_hci.log>")
        return 1
    log = sys.argv[1]
    if not os.path.exists(log):
        print(f"NOT FOUND: {log}")
        return 1

    notifs = pull_snoop_values(log)
    print(f"Pulled {len(notifs)} notifications from {log}")

    # Per-record dedupe by (marker, val16, data)
    bulk_seen = set()
    bulk_records = []  # (cmd, marker, val16, data)

    for v in notifs:
        d = decode_notify(v)
        if not d.is_bulk:
            continue
        for r in d.records:
            key = (r.marker, r.val16, r.data)
            if key in bulk_seen:
                continue
            bulk_seen.add(key)
            bulk_records.append((d.cmd, r.marker, r.val16, r.data))

    print(f"Unique bulk records: {len(bulk_records)}")

    rows_16b = []  # 12B data tail = 6 metrics
    rows_8b = []   # 4B data tail = 1 u32
    for cmd, marker, val16, data in bulk_records:
        if len(data) == 12:
            u16s = parse_u16_pair(data)
            rows_16b.append((cmd, marker, val16, *u16s, data.hex()))
        elif len(data) == 4:
            u32 = int.from_bytes(data, "little")
            rows_8b.append((cmd, marker, val16, u32, data.hex()))
        else:
            print(f"  weird record: cmd=0x{cmd:02x} marker=0x{marker:04x} "
                  f"val16={val16} data_len={len(data)}")

    print(f"  16B (hourly+summary with 6 metrics): {len(rows_16b)}")
    print(f"   8B (older-day, u32 only):           {len(rows_8b)}")

    by_marker = Counter(m for _, m, *_ in rows_16b)
    print("  16B markers present: " +
          ", ".join(f"0x{m:04x}={c}" for m, c in sorted(by_marker.items())))

    # Per-hour dedupe: same val16 can appear under both 0xE631 and 0xE731 in
    # the same packet on this firmware. SQLite UNIQUE constraint is on
    # (device, val16, marker) so two rows with same val16+different marker is
    # allowed. That's fine — we get a flag in `marker` for which sub-type the
    # row came from. If the row is duplicated exactly across retransmits the
    # UNIQUE on (device, val16, marker) catches it.

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Reference date = the snoop's mtime (UTC).
    mtime = _dt.datetime.fromtimestamp(os.path.getmtime(log), tz=_dt.timezone.utc)
    date_local = mtime.date().isoformat()

    inserted = 0
    skipped_dup = 0
    skipped_summary = 0
    for cmd, marker, val16, u0, u1, u2, u3, u4, u5, raw_hex in rows_16b:
        high_byte = (marker >> 8) & 0xFF
        if high_byte in DAY_SUMMARY_HIGH_BYTES:
            skipped_summary += 1
            continue
        hh = val16 // 3600
        mm = (val16 % 3600) // 60
        ss = val16 % 60
        ts_utc = mtime.replace(hour=hh % 24, minute=mm, second=ss,
                               microsecond=0).isoformat()
        try:
            cur.execute("""
                INSERT OR IGNORE INTO a3_hourly
                (ts_utc, device_uuid, date_local, hour_local, val16, marker,
                 reserved_u16_0, steps_raw, cal_raw, hr_agg_raw,
                 intensity_raw, dist_raw, raw_hex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts_utc, DEVICE_UUID, date_local, hh % 24, val16, marker,
                  u0, u1, u2, u3, u4, u5, raw_hex))
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped_dup += 1
        except sqlite3.Error as e:
            print(f"  insert error: {e}")

    con.commit()

    print("\n=== Result ===")
    print(f"  Inserted:            {inserted}")
    print(f"  Skipped (duplicate): {skipped_dup}")
    print(f"  Skipped (day-sum):   {skipped_summary}")

    cur.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM a3_hourly")
    total, min_t, max_t = cur.fetchone()
    print(f"  Total a3_hourly rows now: {total}")
    print(f"  Date range: {min_t} → {max_t}")

    cur.execute("""
        SELECT date_local, hour_local, val16, marker, steps_raw,
               cal_raw, dist_raw, hr_agg_raw, intensity_raw
        FROM a3_hourly
        ORDER BY val16 DESC LIMIT 12
    """)
    print("\n  Latest 12 rows (val16 desc):")
    for r in cur.fetchall():
        d, h, v, m, st, ca, di, hr, it = r
        print(f"    {d} {h:02d}h  val16={v:5d}  marker=0x{m:04x}  "
              f"steps={st:5d} cal={ca:4d} dist={di:5d} "
              f"hr_agg={hr:5d} intensity={it:5d}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
