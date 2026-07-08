"""Unit tests for the 0xAB protocol layer (protocol.py).

Locks in the schema discovered in sessions 9-10 from the Android HCI snoop.
Update EXPECTED values if the schema changes.

Tested areas:
- Packet builders (make_write_packet, make_fetch_request, make_begin_sync)
- Notify parser round-trip on real snoop data
- parse_fetch / merge_fetches: per-record dedupe across 0xA3 retransmits
- dedupe_retransmits: per-packet dedupe (used for older-day blocks)
- High-level helpers: parse_device_info, parse_byte_grid, record_u16_metrics
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sr16_bridge.protocol import (  # noqa: E402
    CMD_ACK, CMD_TODAY_BLOCK, CMD_BLOCK_16B_4REC, CMD_BLOCK_8B_13REC,
    CMD_BYTE_GRID, CMD_DEVICE_INFO,
    SUB_DATA_16B, SUB_DATA_8B, SUB_BYTE_GRID,
    MARKER_REGULAR, MARKER_DAY_SUMMARY, SLOT_SECONDS,
    STATUS_FLAG_INITIAL, STATUS_FLAG_RETRY,
    MAGIC, DIR_WRITE, TYPE_CONST,
    UART_SERVICE_UUID, UART_TX_CHAR_UUID, UART_RX_CHAR_UUID,
    UART_WRITE_HANDLE, UART_NOTIFY_HANDLE,
    make_write_packet, make_fetch_request, make_begin_sync, make_status_query,
    parse_notify, parse_fetch, merge_fetches, dedupe_retransmits,
    parse_device_info, parse_byte_grid,
    record_u16_metrics, record_u32_metric,
)


PACKETS_JSON = "/Users/mih/health/sr16_captures/packets_20260708T100733Z.json"


def _load_packets() -> list:
    with open(PACKETS_JSON) as f:
        return json.load(f)


def _ring_notifies_by_cmd() -> dict:
    out: dict = {}
    for p in _load_packets():
        v = p.get("value", "")
        if not v.startswith("ab11"):
            continue
        if p.get("src", "").lower() != "38:00:00:00:de:90":
            continue
        cmd = int(v[6:8], 16)
        out.setdefault(cmd, []).append(p)
    return out


def _load_value(frame: int) -> str:
    with open(PACKETS_JSON) as f:
        for p in json.load(f):
            if int(p["frame"]) == frame:
                return p["value"]
    raise KeyError(f"frame {frame} not found")


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def test_make_write_packet_format():
    pkt = make_write_packet(CMD_ACK, b"\x01\x02\x03\x04\x05")
    assert pkt[0] == MAGIC == 0xAB
    assert pkt[1] == DIR_WRITE == 0x01
    assert pkt[2] == TYPE_CONST == 0x00
    assert pkt[3] == CMD_ACK
    assert bytes(pkt[4:]) == b"\x01\x02\x03\x04\x05"
    # No checksum, no length byte
    assert len(pkt) == 9


def test_make_write_packet_no_payload():
    pkt = make_write_packet(CMD_ACK)
    assert len(pkt) == 4
    assert pkt.hex() == "ab010003"


def test_make_fetch_request_16b():
    pkt = make_fetch_request(SUB_DATA_16B, frame_seq=0x1AAD, status_flag=STATUS_FLAG_INITIAL)
    assert pkt[0:4] == bytes([MAGIC, DIR_WRITE, TYPE_CONST, CMD_ACK])
    assert pkt[4:6] == b"\xad\x1a"  # LE u16 0x1AAD
    assert pkt[6] == 0x05           # category
    assert pkt[7] == SUB_DATA_16B   # sub_type
    assert pkt[8] == STATUS_FLAG_INITIAL
    assert len(pkt) == 9


def test_make_fetch_request_matches_observed():
    """The captured 0xA3-triggering write was ab010003ad1a051a10.
    Verify our builder reproduces it exactly."""
    pkt = make_fetch_request(SUB_DATA_16B, frame_seq=0x1AAD)
    assert pkt.hex() == "ab010003ad1a051a10"


def test_make_begin_sync_format():
    pkt = make_begin_sync()
    assert pkt[0:4] == bytes([MAGIC, DIR_WRITE, TYPE_CONST, 0x09])
    # 15B = 4B transport + cmd + 6B seg header + 5B BCD... wait, let me just check observed
    assert len(pkt) == 15  # matches observed ab01000957380201001a07080c3206


def test_make_status_query_format():
    pkt = make_status_query(frame_seq=0x607B)
    # Matches observed: ab0100047b60020e0000
    assert pkt.hex() == "ab0100047b60020e0000"


# ---------------------------------------------------------------------------
# Parsing real notifies
# ---------------------------------------------------------------------------

def test_parse_0xa3_today_block():
    d = parse_notify(_load_value(626))
    assert d.cmd == CMD_TODAY_BLOCK == 0xA3
    assert d.segment.sub_type == 0x1A
    assert d.segment.category == 0x05
    assert len(d.records) == 10
    # First record is the day-summary for today (in-progress day)
    assert d.records[0].marker == MARKER_DAY_SUMMARY
    # Last record is a regular hourly
    assert d.records[-1].marker == MARKER_REGULAR


def test_parse_0x43_older_day_16b():
    d = parse_notify(_load_value(635))
    assert d.cmd == CMD_BLOCK_16B_4REC
    assert d.segment.sub_type == 0x1A
    assert len(d.records) == 4


def test_parse_0x6b_older_day_8b():
    d = parse_notify(_load_value(664))
    assert d.cmd == CMD_BLOCK_8B_13REC
    assert len(d.records) == 13
    # Last is day-summary
    assert d.records[-1].marker == MARKER_DAY_SUMMARY


def test_parse_0x67_byte_grid():
    d = parse_notify(_load_value(610))
    assert d.cmd == CMD_BYTE_GRID
    assert d.is_byte_grid
    assert d.raw_data is not None
    assert len(d.raw_data) == 100
    assert set(d.raw_data) <= {0, 1, 2}


def test_parse_0x13_device_info():
    info = parse_device_info(_load_value(569))
    assert isinstance(info.serial, str)
    # Serial is the last 8 bytes decoded as ASCII
    assert info.serial == info.raw[-8:].decode("ascii", errors="replace").rstrip("\x00")


# ---------------------------------------------------------------------------
# Per-record dedupe for 0xA3 retransmits
# ---------------------------------------------------------------------------

def test_0xa3_per_record_dedupe():
    """Four 0xA3 retransmits in the snoop. The day-summary record (record 0)
    varies in val16 (advances with wall-clock time). Records 1-9 are
    byte-identical across retransmits. After merge_fetches, we should see
    9 records (1 day-summary + 8 unique hourly) — wait, there are 10 records
    per packet, 1 is the summary and 9 are hourly, so 9+1=10 total split
    into summary + 9 records.
    """
    notifies = _ring_notifies_by_cmd()[CMD_TODAY_BLOCK]
    assert len(notifies) == 4
    decoded = [parse_notify(p["value"]) for p in notifies]
    merged = merge_fetches([parse_fetch(d) for d in decoded])
    # 10 records per packet, 1 is summary -> 9 in records, 1 in day_summary
    assert len(merged.records) == 9
    assert merged.day_summary is not None
    assert merged.day_summary.marker == MARKER_DAY_SUMMARY
    # Summary should be the latest val16 across retransmits
    summary_vals = [
        d.records[0].val16 for d in decoded if d.records[0].marker == MARKER_DAY_SUMMARY
    ]
    assert merged.day_summary.val16 == max(summary_vals)


def test_0xa3_summary_data12_is_identical_across_retransmits():
    """Although the day-summary's val16 varies, its 12B data tail is the
    same in every retransmit (it's the day's running totals)."""
    notifies = _ring_notifies_by_cmd()[CMD_TODAY_BLOCK]
    decoded = [parse_notify(p["value"]) for p in notifies]
    summaries = [d.records[0] for d in decoded]
    data12s = set(bytes(s.data).hex() for s in summaries)
    assert len(data12s) == 1


# ---------------------------------------------------------------------------
# Per-packet dedupe (for older-day blocks which don't retransmit the same way)
# ---------------------------------------------------------------------------

def test_dedupe_retransmits_for_0x43():
    """0x43 (older-day 16B block) appears 3x in the snoop. They should be
    byte-identical retransmits -> dedupe to 1 packet."""
    notifies = _ring_notifies_by_cmd()[CMD_BLOCK_16B_4REC]
    assert len(notifies) == 3
    decoded = [parse_notify(p["value"]) for p in notifies]
    deduped = dedupe_retransmits(decoded)
    # All 3 are byte-identical (frame_seq-only differs) -> deduped to 1
    assert len(deduped) == 1


def test_dedupe_keeps_distinct_packets():
    """0x43 (4 records) and 0x53 (5 records) are different payloads —
    dedupe must keep both."""
    by_cmd = _ring_notifies_by_cmd()
    decoded = (
        [parse_notify(p["value"]) for p in by_cmd[CMD_BLOCK_16B_4REC]] +
        [parse_notify(p["value"]) for p in by_cmd.get(0x53, [])]
    )
    deduped = dedupe_retransmits(decoded)
    cmds = sorted(d.cmd for d in deduped)
    assert cmds == [0x43, 0x53]


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def test_record_u16_metrics():
    """The 16B record's 12B data tail is 6 x u16 LE."""
    d = parse_notify(_load_value(626))
    # Record 6 has metrics [0, 6144, 0, 57901, 768, 32823] per session 10
    r6 = d.records[6]
    assert record_u16_metrics(r6) == [0, 6144, 0, 57901, 768, 32823]


def test_record_u32_metric_for_8b_record():
    d = parse_notify(_load_value(664))
    # Last record of 0x6B is day-summary with data=00000058 (LE u32 = 0x58000000)
    last = d.records[-1]
    assert len(last.data) == 4
    assert int.from_bytes(bytes(last.data), "little") == 0x58000000
    assert record_u32_metric(last) == 0x58000000


def test_parse_byte_grid_helper():
    g = parse_byte_grid(_load_value(610))
    assert len(g.data) == 100
    assert g.ones == 25
    assert g.twos == 1


def test_unicode_decode_does_not_crash():
    """decode_notify should accept hex strings OR raw bytes."""
    hex_str = _load_value(626)
    d1 = parse_notify(hex_str)
    d2 = parse_notify(bytes.fromhex(hex_str))
    assert d1.cmd == d2.cmd
    assert len(d1.records) == len(d2.records)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_slot_seconds_constant():
    """4110s = 68.5 min — the vendor's slot duration between regular records."""
    assert SLOT_SECONDS == 4110


def test_markers_are_le_u16():
    """The on-wire bytes '31 e0' / '31 e1' decode to 0xE031 / 0xE131 as
    little-endian u16. This is the bit that tripped up session 10."""
    assert MARKER_REGULAR == 0xE031
    assert MARKER_DAY_SUMMARY == 0xE131


def test_gatt_handles():
    """Handle 0x003e = WRITE (phone->ring), 0x0040 = NOTIFY (ring->phone).
    These are the raw ATT handles, not the char UUIDs."""
    assert UART_WRITE_HANDLE == 0x003E
    assert UART_NOTIFY_HANDLE == 0x0040


def test_uart_service_uuid_is_real():
    """Pinned by session 12 — derived from the existing session-9 snoop
    (frame 367 / 372: ATT Read By Group Type Response). If anyone rotates
    this to a placeholder again, this test fails loudly."""
    assert UART_SERVICE_UUID == "0000a00a-0000-1000-8000-00805f9b34fb"
    assert "PLACEHOLDER" not in UART_SERVICE_UUID
    # Both chars live on the A00A service per primary-service-discovery.
    assert UART_TX_CHAR_UUID.startswith("0000b002")
    assert UART_RX_CHAR_UUID.startswith("0000b003")
    # The protocol module now exports UUID_KNOWN=True to gate live ring pulls.
    from sr16_bridge.protocol import UUID_KNOWN
    assert UUID_KNOWN is True


def test_uart_uuids_match_bt_sig_base():
    """All three UUIDs share the SIG base-UUID format. If someone fat-fingers
    one of these, the BT stack will silently miss the service."""
    BASE = "-0000-1000-8000-00805f9b34fb"
    for u in (UART_SERVICE_UUID, UART_TX_CHAR_UUID, UART_RX_CHAR_UUID):
        assert u.endswith(BASE), f"{u} does not end with the SIG base UUID"


if __name__ == "__main__":
    # Allow `python -m tests.test_protocol_0ab` to run without pytest
    failures = 0
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS: {name}")
            except Exception as exc:
                print(f"FAIL: {name}: {exc}")
                failures += 1
    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failure(s)")