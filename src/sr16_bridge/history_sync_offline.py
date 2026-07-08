"""Offline history-sync demo — replays captured notify packets as if they
arrived over BLE, then runs the protocol layer on them to produce a
deduplicated view of the day's hourly history.

This is the Path-A (no ring needed) workflow: we already have one full
sync captured in ~/health/sr16_captures/packets_20260708T100733Z.json. The
0xAB protocol layer should be able to extract everything meaningful
from it without touching BLE.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync_offline
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync_offline --json
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.history_sync_offline \
        --packets /path/to/packets_*.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from .protocol import (
    CMD_TODAY_BLOCK, CMD_BYTE_GRID, CMD_DEVICE_INFO,
    CMD_BLOCK_16B_4REC, CMD_BLOCK_16B_5REC,
    CMD_BLOCK_8B_13REC, CMD_BLOCK_8B_14REC,
    ParsedFetch, Record, parse_notify, parse_fetch, merge_fetches,
    parse_byte_grid, parse_device_info,
    record_u16_metrics,
    dedupe_retransmits,
)

PACKETS_JSON = Path("/Users/mih/health/sr16_captures/packets_20260708T100733Z.json")
RING_MAC = "38:00:00:00:de:90"
BULK_CMDS = {
    CMD_TODAY_BLOCK, CMD_BLOCK_16B_4REC, CMD_BLOCK_16B_5REC,
    CMD_BLOCK_8B_13REC, CMD_BLOCK_8B_14REC, CMD_BYTE_GRID,
}


@dataclass
class SyncReport:
    """Output of an offline sync replay — the per-day view of what the
    protocol layer can extract from the captured notifies."""

    device_serial: str | None
    today_records: List[Record]
    today_summary: dict | None
    older_16b_blocks: list  # list of ParsedFetch grouped by day
    older_8b_blocks: list
    byte_grid: dict | None
    bulk_notify_count: int
    raw_notify_count: int


def _is_ring_notify(p: dict) -> bool:
    v = p.get("value", "")
    return v.startswith("ab11") and p.get("src", "").lower() == RING_MAC


def replay_capture(packets_path: Path) -> SyncReport:
    """Replay a captured packets_*.json as if it were a live sync.

    Groups notifies by opcode, decodes each, applies the per-opcode dedupe
    (handles 0xA3 retransmits), and returns a structured SyncReport.
    """
    with packets_path.open() as f:
        pkts = json.load(f)
    notifies = [p for p in pkts if _is_ring_notify(p)]
    by_cmd: dict[int, list] = defaultdict(list)
    for p in notifies:
        v = p["value"]
        cmd = int(v[6:8], 16)
        by_cmd[cmd].append(p)

    # Device info
    device_serial = None
    if CMD_DEVICE_INFO in by_cmd:
        try:
            device_serial = parse_device_info(by_cmd[CMD_DEVICE_INFO][0]["value"]).serial
        except Exception:
            pass

    # Today block (0xA3)
    today_records: list = []
    today_summary: dict | None = None
    if CMD_TODAY_BLOCK in by_cmd:
        decoded = [parse_notify(p["value"]) for p in by_cmd[CMD_TODAY_BLOCK]]
        merged = merge_fetches([parse_fetch(d) for d in decoded])
        today_records = merged.records
        if merged.day_summary is not None:
            today_summary = {
                "marker": "0xE131",
                "val16": f"0x{merged.day_summary.val16:04x}",
                "metrics": record_u16_metrics(merged.day_summary),
            }

    # 16B-record blocks (older days)
    older_16b = []
    for cmd in (CMD_BLOCK_16B_4REC, CMD_BLOCK_16B_5REC):
        if cmd not in by_cmd:
            continue
        decoded = dedupe_retransmits(
            [parse_notify(p["value"]) for p in by_cmd[cmd]]
        )
        if not decoded:
            continue
        merged = merge_fetches([parse_fetch(d) for d in decoded])
        older_16b.append({
            "cmd": f"0x{cmd:02x}",
            "record_count": len(merged.records),
            "has_day_summary": merged.day_summary is not None,
        })

    # 8B-record blocks (older days)
    older_8b = []
    for cmd in (CMD_BLOCK_8B_13REC, CMD_BLOCK_8B_14REC):
        if cmd not in by_cmd:
            continue
        decoded = dedupe_retransmits(
            [parse_notify(p["value"]) for p in by_cmd[cmd]]
        )
        if not decoded:
            continue
        merged = merge_fetches([parse_fetch(d) for d in decoded])
        older_8b.append({
            "cmd": f"0x{cmd:02x}",
            "record_count": len(merged.records),
            "has_day_summary": merged.day_summary is not None,
        })

    # Byte grid
    byte_grid = None
    if CMD_BYTE_GRID in by_cmd:
        g = parse_byte_grid(by_cmd[CMD_BYTE_GRID][0]["value"])
        byte_grid = {
            "bytes": len(g.data),
            "ones": g.ones,
            "twos": g.twos,
        }

    return SyncReport(
        device_serial=device_serial,
        today_records=today_records,
        today_summary=today_summary,
        older_16b_blocks=older_16b,
        older_8b_blocks=older_8b,
        byte_grid=byte_grid,
        bulk_notify_count=sum(len(by_cmd[c]) for c in BULK_CMDS),
        raw_notify_count=len(notifies),
    )


def print_report(report: SyncReport, as_json: bool = False) -> None:
    if as_json:
        out = {
            "device_serial": report.device_serial,
            "today_summary": report.today_summary,
            "today_records": [
                {
                    "val16": f"0x{r.val16:04x}",
                    "marker": f"0x{r.marker:04x}",
                    "metrics": record_u16_metrics(r),
                }
                for r in report.today_records
            ],
            "older_16b_blocks": report.older_16b_blocks,
            "older_8b_blocks": report.older_8b_blocks,
            "byte_grid": report.byte_grid,
            "bulk_notify_count": report.bulk_notify_count,
            "raw_notify_count": report.raw_notify_count,
        }
        print(json.dumps(out, indent=2))
        return

    print(f"=== sr16-bridge offline sync replay ===")
    print(f"raw notify count:     {report.raw_notify_count}")
    print(f"bulk-notify count:    {report.bulk_notify_count}")
    print(f"device serial:        {report.device_serial!r}")
    print()
    if report.today_summary:
        print(f"TODAY (0xA3) day-summary:")
        print(f"  marker={report.today_summary['marker']}  "
              f"val16={report.today_summary['val16']}")
        print(f"  metrics[resv, hr_avg, hr_min, hr_max, steps, cal] = "
              f"{report.today_summary['metrics']}")
        print()
        print(f"TODAY (0xA3) hourly records: {len(report.today_records)}")
        for r in report.today_records:
            print(f"  val16=0x{r.val16:04x}  metrics={record_u16_metrics(r)}")
    if report.older_16b_blocks:
        print()
        print("Older-day 16B blocks:")
        for blk in report.older_16b_blocks:
            print(f"  cmd={blk['cmd']} records={blk['record_count']} "
                  f"day_summary={blk['has_day_summary']}")
    if report.older_8b_blocks:
        print()
        print("Older-day 8B blocks:")
        for blk in report.older_8b_blocks:
            print(f"  cmd={blk['cmd']} records={blk['record_count']} "
                  f"day_summary={blk['has_day_summary']}")
    if report.byte_grid:
        print()
        print(f"Byte grid (0x67): bytes={report.byte_grid['bytes']} "
              f"ones={report.byte_grid['ones']} twos={report.byte_grid['twos']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Offline replay of captured SR16 sync")
    p.add_argument("--packets", type=Path, default=PACKETS_JSON,
                   help="path to packets_*.json from decode_snoop")
    p.add_argument("--json", action="store_true", help="emit JSON instead of pretty")
    args = p.parse_args()
    if not args.packets.exists():
        print(f"ERR: {args.packets} not found", file=sys.stderr)
        return 1
    report = replay_capture(args.packets)
    print_report(report, as_json=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())