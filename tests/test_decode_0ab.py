"""Unit tests for the 0xAB protocol decoder. Locks in the schema discovered
in session 9-10 from the Android HCI snoop. Update the EXPECTED values
if the schema changes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sr16_bridge.decode_0ab import (  # noqa: E402
    decode_notify,
    format_val16,
)


# Real values from /Users/mih/health/sr16_captures/packets_20260708T100733Z.json
PACKETS_JSON = "/Users/mih/health/sr16_captures/packets_20260708T100733Z.json"


def _load_value(frame: int) -> str:
    """Load the value field of a specific packet frame from the snoop JSON."""
    with open(PACKETS_JSON) as f:
        for p in json.load(f):
            if int(p["frame"]) == frame:
                return p["value"]
    raise KeyError(f"frame {frame} not found in {PACKETS_JSON}")


# 0xA3 packet, frame 626 — today's hourly data, 10 records of 16B
A3_FRAME_626 = _load_value(626)

# 0x6B packet, frame 664 — 13 records of 8B
B6_FRAME_664 = _load_value(664)

# 0x67 packet, frame 610 — 100B byte grid
B67_FRAME_610 = _load_value(610)


def test_a3_packet_decodes_correctly():
    d = decode_notify(A3_FRAME_626)
    assert d.cmd == 0xA3, f"cmd: {d.cmd:#x}"
    assert d.transport.magic == 0xAB
    assert d.transport.direction == 0x11
    assert d.transport.type == 0x00
    assert d.segment.frame_seq == 0x1EE7
    assert d.segment.category == 0x05  # measurement data
    assert d.segment.sub_type == 0x1A  # today's data
    assert d.segment.status_flag == 0x10
    assert d.is_bulk
    assert not d.is_byte_grid
    assert d.record_size == 16
    assert len(d.records) == 10
    # Record 0 starts at 18:03:47 (val16 = 0xFE03 = 65027 sec)
    # In 0xA3, the FIRST record is 0x31E1 = day summary (today is in progress,
    # so the summary comes first, then the per-hour records follow).
    assert d.records[0].val16 == 0xFE03
    assert format_val16(0xFE03) == "18:03:47"
    assert d.records[0].marker == 0xE131  # day summary at start (in-progress day)
    # Record 9 (last) starts at 00:03:12 (val16 = 0x00C0 = 192 sec — next day wrap)
    assert d.records[9].val16 == 0x00C0
    assert format_val16(0x00C0) == "00:03:12"
    # Record 9 is a regular 0xE031 hourly record
    assert d.records[9].marker == 0xE031
    # data12 of record 6 is the first non-zero one
    r6 = d.records[6]
    pairs = [int.from_bytes(r6.data[i*2:(i+1)*2], "little") for i in range(6)]
    assert pairs == [0, 6144, 0, 57901, 768, 32823], f"got {pairs}"


def test_b6_packet_decodes_correctly():
    d = decode_notify(B6_FRAME_664)
    assert d.cmd == 0x6B
    assert d.segment.category == 0x05
    assert d.segment.sub_type == 0x17  # older day's data
    assert d.record_size == 8
    assert len(d.records) == 13
    # Last record is the day summary (0xE131 marker) — skip it for the
    # delta check, since the summary's val16 doesn't follow the regular grid.
    assert d.records[-1].marker == 0xE131
    regular = d.records[:-1]
    deltas = [
        (regular[i+1].val16 - regular[i].val16) & 0xFFFF
        for i in range(len(regular) - 1)
    ]
    # All deltas should be 4110 or 4111 (off-by-one jitter on the wrap around 0xFFFF)
    assert all(4109 <= delta <= 4111 for delta in deltas), f"deltas: {deltas}"


def test_b67_byte_grid_decodes_correctly():
    d = decode_notify(B67_FRAME_610)
    assert d.cmd == 0x67
    assert d.is_byte_grid
    assert d.raw_data is not None
    assert len(d.raw_data) == 100
    # unique values should be subset of {0, 1, 2}
    assert set(d.raw_data) <= {0, 1, 2}
    # The 100 bytes have 25 1s and 1 2 (per the snoop)
    ones = sum(d.raw_data)
    twos = sum(1 for x in d.raw_data if x == 2)
    assert ones == 25, f"ones: {ones}"
    assert twos == 1, f"twos: {twos}"


def test_val16_formatting():
    assert format_val16(0) == "00:00:00"
    assert format_val16(60) == "00:01:00"
    assert format_val16(3600) == "01:00:00"
    assert format_val16(3661) == "01:01:01"
    assert format_val16(0xFE03) == "18:03:47"
    assert format_val16(0xFFFF) == "18:12:15"  # 65535 = 18h 12m 15s
    # Out-of-range returns hex
    assert format_val16(0x10000).startswith("0x")


def test_short_notify_decodes():
    # 0x03 echo response: 9B total
    short = "ab110003a0a0020200"  # 9 bytes
    d = decode_notify(short)
    assert d.cmd == 0x03
    assert d.segment.category == 0x02  # metadata
    assert not d.is_bulk
    assert not d.is_byte_grid
    assert d.records == []


if __name__ == "__main__":
    test_a3_packet_decodes_correctly()
    print("PASS: test_a3_packet_decodes_correctly")
    test_b6_packet_decodes_correctly()
    print("PASS: test_b6_packet_decodes_correctly")
    test_b67_byte_grid_decodes_correctly()
    print("PASS: test_b67_byte_grid_decodes_correctly")
    test_val16_formatting()
    print("PASS: test_val16_formatting")
    test_short_notify_decodes()
    print("PASS: test_short_notify_decodes")
    print("\nAll tests passed.")
