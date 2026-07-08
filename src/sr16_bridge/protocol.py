"""Vendor 0xAB protocol layer for the SR16 smart ring.

Reversed 2026-07-08 from an Android HCI snoop. The SR16 (and the Yawell/QRing/
Rogbid/RWfit family) does NOT use the Nordic UART-over-BLE transport or the
Colmi R02 packet format. It uses a vendor-specific 0xAB-prefixed frame on a
pair of GATT chars (handle 0x003e WRITE, handle 0x0040 NOTIFY) inside a
HID-over-GATT service.

Wire format:
    ab <dir> <type=0x00> <cmd> [payload]

- ab        vendor magic (constant, every packet)
- dir       0x01 = phone->ring (write), 0x11 = ring->phone (notify)
- type      0x00 always in captures — constant or flag, not a length
- cmd       1B opcode
- payload   cmd-specific. Bulk data opcodes share a 5B segment header:
              [2B frame_seq LE][1B category][1B sub_type][1B status_flag]
            followed by N x 8B or N x 16B records (or a 100B raw byte grid).

11 cmds (sessions 9-10 fully mapped from 78 ring->phone notifies):

    cmd  role                          payload shape
    ---- ----------------------------- -----------------------------------
    0x03 echo/ack (51x)                9B: transport + cmd + 5B seg hdr
    0x04 status response (3x)          10B
    0x05 status response (2x)          11B
    0x06 status response (4x)          12B
    0x13 device info (5x)              25B - last 8B are ASCII serial
    0x43 16B-record block, 4 records   73B - older day
    0x53 16B-record block, 5 records   89B - older day
    0x67 100B raw byte grid            109B - day bitmap
    0x6B 8B-record block, 13 records   113B - older day
    0x73 8B-record block, 14 records   121B - older day
    0xA3 16B-record block, 10 records  169B - today (retransmits)

Bulk record structure (uniform across 0x43/0x53/0x6B/0x73/0xA3):
    [2B marker LE = 0xE031 (regular) or 0xE131 (day summary)]
    [2B val16 LE   = seconds since midnight UTC of that day]
    [4B or 12B data tail]
        8B record  -> 4B data (u32, mostly zero)
        16B record -> 12B data = 6 x u16 metrics (HR avg/max/min, steps, cal, dist)

Phone->ring write pattern (sessions 9-10 observed):
    ab 01 00 03 <frame_seq LE> <category=0x05> <sub_type> <status_flag>
        sub_type 0x1A  -> triggers 0x43 / 0xA3 (16B-record blocks)
        sub_type 0x17  -> triggers 0x6B / 0x73 (8B-record blocks)
        sub_type 0x63  -> triggers 0x67 (byte grid)
        sub_type 0x02-0x0D -> triggers per-hour 16B blocks (one per metric)
        status_flag 0x10 = initial fetch, 0x30 = retry
    frame_seq increments per retry; ring matches it to the response.

Reference: ~/projects/sr16-bridge/src/sr16_bridge/decode_0ab.py for the
canonical record-segment parser.

This module is pure logic (zero IO). The async BLE transport lives in
`history_sync.py`. To send a packet to the ring:
    pkt = make_write_packet(CMD_FETCH, sub_type=SUB_DATA_16B, frame_seq=1)
    await client.write_gatt_char(UART_TX_HANDLE, pkt)   # 0x003e WRITE handle
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .decode_0ab import (
    DecodedNotify,
    Record,
    SegmentHeader,
    TransportHeader,
    decode_notify,
)


# --- Transport constants ------------------------------------------------

MAGIC = 0xAB
DIR_WRITE = 0x01   # phone -> ring
DIR_NOTIFY = 0x11  # ring  -> phone
TYPE_CONST = 0x00  # always 0x00 in captures

# GATT handles + 128-bit UUIDs (from session-9 snoop, frame 367+372+466 —
# ATT Read By Group Type Response). Resolution: 2026-07-09, option C).
#
# Phone sees 3 services on the SR16 in primary-service-discovery order:
#   0x1800 (GAP)        handles 0x0001..0x0007
#   0x1801 (GATT)       handles 0x0008..0x000B
#   0x1812 (HID / Hogp) handles 0x000C..0x0033   ← ring's HID-over-GATT
#   0xFF00              handles 0x0034..0x003B   ← vendor secondary
#   0xA00A              handles 0x003C..0x0041   ← vendor primary (THE ONE)
#   0x0BC0              handles 0x0042..0xFFFF
#
# The vendor transport rides on 0xA00A:
#   0x003E  16-bit UUID 0xB002  props=0x1E (R+W+WNR+N)  phone -> ring WRITE
#   0x0040  16-bit UUID 0xB003  props=0x12 (R+N)          ring  -> phone NOTIFY
#
# Full 128-bit form is the standard SIG base-UUID alias:
#   0000XXXX-0000-1000-8000-00805f9b34fb
UART_SERVICE_UUID = "0000a00a-0000-1000-8000-00805f9b34fb"
UART_TX_CHAR_UUID = "0000b002-0000-1000-8000-00805f9b34fb"  # phone -> ring (handle 0x003E)
UART_RX_CHAR_UUID = "0000b003-0000-1000-8000-00805f9b34fb"  # ring  -> phone (handle 0x0040)
UART_WRITE_HANDLE = 0x003E
UART_NOTIFY_HANDLE = 0x0040

# UUID_KNOWN is now True. Lets connect_pull.py run without --force.
UUID_KNOWN = True


# --- Command opcodes (all 11 captured) ----------------------------------

# Transport-shape opcodes
CMD_ACK         = 0x03   # echo of a phone->ring write (also a fetch trigger)
CMD_STATUS_A    = 0x04   # 10B status payload
CMD_STATUS_B    = 0x05   # 11B status payload
CMD_STATUS_C    = 0x06   # 12B status payload
CMD_DEVICE_INFO = 0x13   # 25B - last 8B are the ASCII serial number

# Bulk-data opcodes (16B records: 6 x u16 metrics)
CMD_BLOCK_16B_4REC = 0x43   # 73B
CMD_BLOCK_16B_5REC = 0x53   # 89B
CMD_TODAY_BLOCK    = 0xA3   # 169B - today (with 0xE131 day summary first)

# Bulk-data opcodes (8B records: u32 data tail)
CMD_BLOCK_8B_13REC = 0x6B   # 113B
CMD_BLOCK_8B_14REC = 0x73   # 121B

# Bulk-data opcodes (raw byte grid)
CMD_BYTE_GRID = 0x67        # 109B - 100B day bitmap

# Phone->ring cmds (not observed as notifies)
CMD_BEGIN_SYNC = 0x09       # start-of-sync marker
CMD_QUERY      = 0x04       # also used as a status-query request
CMD_CONFIG     = 0x05       # also used as a config-set request


# --- Sub-types for fetch requests ---------------------------------------

# Each fetch write uses cmd=0x03 + one of these sub_types to ask the ring
# for a specific block. The ring's response carries the matching cmd.
SUB_DATA_16B     = 0x1A   # triggers 0x43 / 0xA3 (16B records)
SUB_DATA_8B      = 0x17   # triggers 0x6B / 0x73 (8B records)
SUB_BYTE_GRID    = 0x63   # triggers 0x67
# Per-hour 16B blocks (one sub_type per metric family; observed 0x02-0x0D)
SUB_HOUR_02 = 0x02
SUB_HOUR_03 = 0x03
SUB_HOUR_04 = 0x04
SUB_HOUR_05 = 0x05
SUB_HOUR_06 = 0x06
SUB_HOUR_07 = 0x07
SUB_HOUR_08 = 0x08
SUB_HOUR_09 = 0x09
SUB_HOUR_0A = 0x0A
SUB_HOUR_0D = 0x0D

STATUS_FLAG_INITIAL = 0x10
STATUS_FLAG_RETRY   = 0x30
STATUS_FLAG_QUERY   = 0x00


# --- Markers in bulk records --------------------------------------------

MARKER_REGULAR = 0xE031   # hourly record
MARKER_DAY_SUMMARY = 0xE131  # day-summary record (start of in-progress day,
                              # or end of completed day)

# Slot duration between regular records: 4110 sec = 68.5 min.
# The vendor's "hourly" cadence is not a clean multiple of 60.
SLOT_SECONDS = 4110


# --- Packet builders ----------------------------------------------------

def make_write_packet(
    cmd: int,
    sub_data: bytes | bytearray | None = None,
) -> bytearray:
    """Build a phone->ring write packet.

    Format: ab 01 00 <cmd> [sub_data]
    No length byte, no checksum. sub_data is raw payload bytes (cmd-specific).
    """
    sub = bytearray(sub_data) if sub_data else bytearray()
    pkt = bytearray()
    pkt.append(MAGIC)
    pkt.append(DIR_WRITE)
    pkt.append(TYPE_CONST)
    pkt.append(cmd & 0xFF)
    pkt.extend(sub)
    return pkt


def make_fetch_request(
    sub_type: int,
    frame_seq: int = 1,
    status_flag: int = STATUS_FLAG_INITIAL,
    category: int = 0x05,
) -> bytearray:
    """Build a generic 0x03 fetch-request packet.

    Phone->ring format:  ab 01 00 03 <frame_seq LE u16> <category> <sub_type> <status_flag>

    sub_type selects which block the ring should return. The frame_seq must
    match between request and response (the ring echoes it in its segment
    header); the caller is responsible for picking a unique sequence per
    in-flight request.

    Returns 9 bytes.
    """
    payload = bytearray()
    payload.extend(struct.pack("<H", frame_seq & 0xFFFF))
    payload.append(category & 0xFF)
    payload.append(sub_type & 0xFF)
    payload.append(status_flag & 0xFF)
    return make_write_packet(CMD_ACK, payload)


def make_begin_sync(now: Optional[datetime] = None) -> bytearray:
    """Build the 0x09 start-of-sync marker.

    Phone->ring format: ab 01 00 09 <frame_seq LE u16> <category> <sub_type>
                                     <status_flag> [BCD datetime]

    Observed: ab01000957380201001a07080c3206  (15B)
        frame_seq=0x3857  category=0x02  sub_type=0x01  status=0x00
        date payload: 1a 07 08 0c 32 06
            = 0x1A=26, 0x07=7, 0x08=8, 0x0C=12, 0x32=50, 0x06=6
              (year mod 100, month, day, hour, min, sec — all BCD)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    payload = bytearray()
    payload.extend(struct.pack("<H", 0x3857))  # observed frame_seq (constant in capture)
    payload.append(0x02)                       # category
    payload.append(0x01)                       # sub_type
    payload.append(0x00)                       # status_flag
    # 6B BCD: year%100, month, day, hour, min, sec
    payload.extend(_bcd_datetime(now))
    return make_write_packet(CMD_BEGIN_SYNC, payload)


def make_status_query(frame_seq: int = 1) -> bytearray:
    """Build a 0x04 status query. Observed: ab0100047b60020e0000"""
    payload = bytearray()
    payload.extend(struct.pack("<H", frame_seq & 0xFFFF))
    payload.append(0x02)  # category
    payload.append(0x0E)  # sub_type
    payload.append(0x00)  # status_flag
    payload.extend(b"\x00")
    return make_write_packet(CMD_QUERY, payload)


# --- Notify parsers -----------------------------------------------------

def parse_notify(value: str | bytes) -> DecodedNotify:
    """Parse a ring->phone notify payload. Thin wrapper over decode_0ab.decode_notify."""
    return decode_notify(value)


@dataclass
class ParsedFetch:
    """One full bulk fetch, deduplicated by record content.

    Each SR16 bulk opcode arrives as 1-N notify packets (0xA3 arrives 4x as
    a retransmit loop). This class:
      - decodes the raw notify
      - dedupes records by (marker, val16, data) so retransmits don't double-count
      - splits the day-summary record (marker=0xE131) from regular records
    """
    cmd: int
    frame_seq: int
    category: int
    sub_type: int
    status_flag: int
    day_summary: Optional[Record] = None
    records: List[Record] = field(default_factory=list)

    @property
    def is_today(self) -> bool:
        """True for 0xA3 (today's in-progress block)."""
        return self.cmd == CMD_TODAY_BLOCK

    @property
    def record_size(self) -> int:
        return 16 if self.cmd in (CMD_BLOCK_16B_4REC, CMD_BLOCK_16B_5REC,
                                  CMD_TODAY_BLOCK) else 8


def _packet_body_key(d: DecodedNotify) -> str:
    """Deterministic byte-string of everything in a notify *except* the
    segment header's frame_seq. Two packets with the same body_key are
    byte-identical retransmits.

    For 0xA3 this is NOT the correct dedupe boundary — the day-summary
    record varies in val16 across retransmits. Use parse_fetch() / merge_fetches()
    for per-record dedupe instead.
    """
    parts = bytearray()
    parts.append(d.transport.magic)
    parts.append(d.transport.direction)
    parts.append(d.transport.type)
    parts.append(d.cmd)
    parts.append(d.segment.category)
    parts.append(d.segment.sub_type)
    parts.append(d.segment.status_flag)
    for r in d.records:
        parts.extend(struct.pack("<HH", r.marker, r.val16))
        parts.extend(r.data)
    if d.raw_data is not None:
        parts.extend(d.raw_data)
    return parts.hex()


def dedupe_retransmits(decoded: List[DecodedNotify]) -> List[DecodedNotify]:
    """Drop notify packets whose body (excluding segment frame_seq) is a
    duplicate of an earlier packet's body.

    For 0xA3 the day-summary record varies across retransmits (val16 advances
    as "now" advances), so per-record dedupe matters more than per-packet
    dedupe — see parse_fetch() / merge_fetches() for that path.
    """
    seen: set[str] = set()
    out: List[DecodedNotify] = []
    for d in decoded:
        key = _packet_body_key(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def parse_fetch(decoded: DecodedNotify) -> ParsedFetch:
    """Parse one bulk-notify decode into a deduplicated ParsedFetch.

    Per-record dedupe: across retransmits of 0xA3, only the day-summary record
    (marker=0xE131) varies in val16. We keep the LATEST summary (highest val16)
    and union all regular records.
    """
    pf = ParsedFetch(
        cmd=decoded.cmd,
        frame_seq=decoded.segment.frame_seq,
        category=decoded.segment.category,
        sub_type=decoded.segment.sub_type,
        status_flag=decoded.segment.status_flag,
    )
    seen_regular: set[tuple[int, bytes]] = set()
    best_summary: Optional[Record] = None
    for r in decoded.records:
        if r.marker == MARKER_DAY_SUMMARY:
            # Keep the latest (highest val16) summary across retransmits.
            if best_summary is None or r.val16 > best_summary.val16:
                best_summary = r
        else:
            key = (r.val16, bytes(r.data))
            if key in seen_regular:
                continue
            seen_regular.add(key)
            pf.records.append(r)
    pf.day_summary = best_summary
    # Stable order by val16
    pf.records.sort(key=lambda r: r.val16)
    return pf


def merge_fetches(fetches: List[ParsedFetch]) -> ParsedFetch:
    """Merge multiple ParsedFetch blocks (e.g. across retransmits of 0xA3)."""
    if not fetches:
        raise ValueError("no fetches to merge")
    base = ParsedFetch(
        cmd=fetches[0].cmd,
        frame_seq=fetches[0].frame_seq,
        category=fetches[0].category,
        sub_type=fetches[0].sub_type,
        status_flag=fetches[0].status_flag,
    )
    seen_regular: set[tuple[int, bytes]] = set()
    best_summary: Optional[Record] = None
    for f in fetches:
        if f.day_summary is not None:
            if best_summary is None or f.day_summary.val16 > best_summary.val16:
                best_summary = f.day_summary
        for r in f.records:
            key = (r.val16, bytes(r.data))
            if key in seen_regular:
                continue
            seen_regular.add(key)
            base.records.append(r)
    base.day_summary = best_summary
    base.records.sort(key=lambda r: r.val16)
    return base


# --- High-level helpers -------------------------------------------------

def parse_device_info(value: str | bytes) -> "DeviceInfo":
    """Parse a 0x13 device-info payload.

    Layout (25B):
        transport(3) + cmd(1) + segment_header(5) + body(16B)
        last 8B of body are the ASCII serial number.
    """
    d = parse_notify(value)
    if d.cmd != CMD_DEVICE_INFO:
        raise ValueError(f"not a 0x13 packet: 0x{d.cmd:02x}")
    body = bytes([d.segment.category, d.segment.sub_type, d.segment.status_flag])
    # The segment header is 5B but only 3B above are user-meaningful fields.
    # The actual 16B body sits after the segment header.
    raw = bytes.fromhex(value) if isinstance(value, str) else value
    serial = raw[-8:]
    return DeviceInfo(serial=serial.decode("ascii", errors="replace").rstrip("\x00"),
                      raw=raw)


@dataclass
class DeviceInfo:
    serial: str
    raw: bytes


def parse_byte_grid(value: str | bytes) -> "ByteGrid":
    """Parse a 0x67 100B byte grid. Values are in {0, 1, 2}."""
    d = parse_notify(value)
    if d.cmd != CMD_BYTE_GRID:
        raise ValueError(f"not a 0x67 packet: 0x{d.cmd:02x}")
    assert d.raw_data is not None
    return ByteGrid(
        data=bytes(d.raw_data),
        ones=sum(d.raw_data),
        twos=sum(1 for x in d.raw_data if x == 2),
    )


@dataclass
class ByteGrid:
    data: bytes
    ones: int
    twos: int


# --- 0xA3 metric semantics (UNCONFIRMED) --------------------------------

# The 16B record's 12B data tail is 6 x u16. The most likely layout, per
# session-10 inference:
#   [reserved, hr_avg, hr_min, hr_max, steps, calories]
# These indices let callers reference metrics by name without hardcoding
# indices everywhere. UNCONFIRMED — needs a second capture with known
# activity to validate (see HANDOFF-session-10.md "What's NOT yet known").
A3_METRIC_RESERVED = 0
A3_METRIC_HR_AVG   = 1
A3_METRIC_HR_MIN   = 2
A3_METRIC_HR_MAX   = 3
A3_METRIC_STEPS    = 4
A3_METRIC_CALORIES = 5


def record_u16_metrics(r: Record) -> List[int]:
    """Decode a 16B record's 12B data tail as 6 x u16 LE."""
    if len(r.data) != 12:
        raise ValueError(f"expected 12B data tail, got {len(r.data)}")
    return [int.from_bytes(r.data[i * 2:(i + 1) * 2], "little") for i in range(6)]


def record_u32_metric(r: Record) -> int:
    """Decode an 8B record's 4B data tail as a u32 LE."""
    if len(r.data) != 4:
        raise ValueError(f"expected 4B data tail, got {len(r.data)}")
    return int.from_bytes(r.data, "little")


# --- Internal helpers ---------------------------------------------------

def _bcd(n: int) -> int:
    """Pack 0..99 into BCD (e.g. 23 -> 0x23)."""
    if not 0 <= n < 100:
        raise ValueError(f"BCD out of range: {n}")
    return ((n // 10) << 4) | (n % 10)


def _bcd_datetime(dt: datetime) -> bytes:
    """Pack a datetime as 6B BCD: YY MM DD HH MM SS."""
    return bytes([
        _bcd(dt.year % 100),
        _bcd(dt.month),
        _bcd(dt.day),
        _bcd(dt.hour),
        _bcd(dt.minute),
        _bcd(dt.second),
    ])


__all__ = [
    "MAGIC", "DIR_WRITE", "DIR_NOTIFY", "TYPE_CONST",
    "UART_WRITE_HANDLE", "UART_NOTIFY_HANDLE",
    "UART_SERVICE_UUID", "UART_TX_CHAR_UUID", "UART_RX_CHAR_UUID",
    "CMD_ACK", "CMD_STATUS_A", "CMD_STATUS_B", "CMD_STATUS_C",
    "CMD_DEVICE_INFO",
    "CMD_BLOCK_16B_4REC", "CMD_BLOCK_16B_5REC", "CMD_TODAY_BLOCK",
    "CMD_BLOCK_8B_13REC", "CMD_BLOCK_8B_14REC", "CMD_BYTE_GRID",
    "CMD_BEGIN_SYNC", "CMD_QUERY", "CMD_CONFIG",
    "SUB_DATA_16B", "SUB_DATA_8B", "SUB_BYTE_GRID",
    "SUB_HOUR_02", "SUB_HOUR_03", "SUB_HOUR_04", "SUB_HOUR_05",
    "SUB_HOUR_06", "SUB_HOUR_07", "SUB_HOUR_08", "SUB_HOUR_09",
    "SUB_HOUR_0A", "SUB_HOUR_0D",
    "STATUS_FLAG_INITIAL", "STATUS_FLAG_RETRY", "STATUS_FLAG_QUERY",
    "MARKER_REGULAR", "MARKER_DAY_SUMMARY", "SLOT_SECONDS",
    "ParsedFetch", "DeviceInfo", "ByteGrid",
    "make_write_packet", "make_fetch_request", "make_begin_sync",
    "make_status_query",
    "parse_notify", "parse_fetch", "parse_device_info", "parse_byte_grid",
    "dedupe_retransmits", "merge_fetches",
    "record_u16_metrics", "record_u32_metric",
    "A3_METRIC_RESERVED", "A3_METRIC_HR_AVG", "A3_METRIC_HR_MIN",
    "A3_METRIC_HR_MAX", "A3_METRIC_STEPS", "A3_METRIC_CALORIES",
]