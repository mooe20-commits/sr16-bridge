"""
0xAB protocol decoder for SR16 bulk notify payloads.

Discovered 2026-07-08 from the Android HCI snoop. The vendor protocol uses
a 0xAB-prefixed packet structure (NOT the Colmi R02 model previously assumed):

    ab <dir> <type> <cmd> [data...]    (no LEN byte in the GATT value)

- 0xAB: vendor magic (constant, every packet)
- <dir>: 0x01 = phone->ring (write), 0x11 = ring->phone (notify)
- <type>: 0x00 in every captured packet — may be a constant or a flag
- <cmd>: 1B opcode
- [data...]: opcode-specific payload

No checksum. The LEN byte hypothesized in earlier session-9 writeups is NOT
present in tshark's GATT value dump — the wire format is 3B transport
(`ab <dir> <type>`) + CMD + data.

Bulk notifies (0x43, 0x53, 0x67, 0x6B, 0x73, 0xA3) all share a common
record structure:

    3B transport:    ab <dir> <type>
    1B cmd:          <opcode>
    5B segment hdr:  <2B frame-seq> <1B type> <1B sub-type> <1B const 0x10>
    N x 8B or 16B records:
        [2B marker, on-the-wire bytes "31 e0" (LE u16 = 0xE031) or "31 e1" (LE u16 = 0xE131)]
        [2B val16 LE = seconds-since-midnight-UTC-of-day]
        [4B or 12B data, depending on record size]

0xE031 = regular hourly record.
0xE131 = day-summary record (carries total-day metrics like total steps,
total calories). Position varies: at the START of an in-progress day
(0xA3) or at the END of a completed day (0x43/0x53/0x6B/0x73).

Deltas between consecutive val16 are exactly 4110 seconds (68.5 min) per record.
4110 is the ring's "slot" duration in this vendor protocol — NOT a clean
multiple of 60, but consistent across all captured packets.

Per-cmd layouts (per packet, retransmits are byte-identical):
    0x43 (73B)  - 4 records of 16B, body hdr 'cb 64 05 1a 10' -> 4 hours
    0x53 (89B)  - 5 records of 16B, body hdr '26 93 05 1a 10' -> 5 hours
    0x67 (109B) - 100 raw data bytes (no records), body hdr 'dc 83 02 63 10'
                  byte values: {0, 1, 2} - day-long bitmap (semantics unconfirmed)
    0x6B (113B) - 13 records of 8B, body hdr '92 05 05 17 10' -> 13 hours
    0x73 (121B) - 14 records of 8B, body hdr '56 73 05 17 10' -> 14 hours
    0xA3 (169B) - 10 records of 16B, body hdr 'e7 1e 05 1a 10' -> 10 hours (today)

The 8B record has a 4B data tail (u32, mostly zero, non-zero values are small
integers like 21, 76, 88). The 16B record has a 12B data tail = 6 x u16
metrics. The 16B records' 12B data semantics are NOT fully decoded — they are
likely the HR/activity summary metrics for that hour (HR avg, HR max, HR min,
steps, calories, distance, etc.), but this needs corroboration from a second
capture with known activity.

This decoder is offline (no IO). It takes a hex string (the `value` field from
packets_*.json) and returns a structured dict. CLI: prints a per-cmd summary.

Usage:
    from decode_0ab import decode_notify
    decoded = decode_notify(p['value'])

    # CLI
    python decode_0ab.py <hex-string>
    python decode_0ab.py --json packets_20260708T100733Z.json
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# --- Data classes --------------------------------------------------------

@dataclass
class TransportHeader:
    magic: int       # 0xAB
    direction: int   # 0x01 = write, 0x11 = notify
    type: int        # 0x00 in every captured packet


@dataclass
class SegmentHeader:
    frame_seq: int       # 2B LE - vendor-internal sequence counter
    category: int        # 1B - 0x02 = metadata/status, 0x05 = measurement data
    sub_type: int        # 1B - specific metric within category
    status_flag: int     # 1B - mostly 0x10; for 0x03 echo responses it's 0x00 or 0x30


@dataclass
class Record:
    marker: int      # LE u16 reading of the on-wire "31 e0" / "31 e1" bytes
                     # -> 0xE031 = regular, 0xE131 = day-summary
    val16: int       # LE u16 - seconds since midnight
    data: bytes      # 4B (8B record) or 12B (16B record)


@dataclass
class DecodedNotify:
    cmd: int                       # 1B opcode
    transport: TransportHeader
    segment: SegmentHeader
    records: List[Record] = field(default_factory=list)
    raw_data: Optional[bytes] = None  # for 0x67-style raw byte grids

    @property
    def is_bulk(self) -> bool:
        return self.cmd in (0x43, 0x53, 0x6B, 0x73, 0xA3)

    @property
    def is_byte_grid(self) -> bool:
        return self.cmd == 0x67

    @property
    def record_size(self) -> int:
        """8 or 16 bytes per record. 0 for byte-grid packets."""
        if self.cmd in (0x43, 0x53, 0xA3):
            return 16
        if self.cmd in (0x6B, 0x73):
            return 8
        return 0

    @property
    def duration_seconds(self) -> int:
        """Total time span covered by the records (val16 delta from first to last)."""
        if not self.records:
            return 0
        first, last = self.records[0].val16, self.records[-1].val16
        if last >= first:
            return last - first
        # 16-bit wrap
        return (last + 0x10000) - first


# --- Parser --------------------------------------------------------------

def parse_transport(b: bytes) -> Tuple[TransportHeader, int]:
    """Parse the 3-byte transport header. Returns (header, payload_offset=3).

    Format: ab <dir> <type>. The CMD byte is parsed separately.
    """
    if len(b) < 3:
        raise ValueError(f"packet too short for transport header: {len(b)}B")
    if b[0] != 0xAB:
        raise ValueError(f"bad magic: 0x{b[0]:02x} (expected 0xAB)")
    if b[1] not in (0x01, 0x11):
        raise ValueError(f"bad direction: 0x{b[1]:02x} (expected 0x01 or 0x11)")
    t = TransportHeader(magic=b[0], direction=b[1], type=b[2])
    return t, 3


def parse_cmd(b: bytes, offset: int) -> Tuple[int, int]:
    """Parse the 1-byte cmd. Returns (cmd, new_offset)."""
    if len(b) < offset + 1:
        raise ValueError(f"packet too short for cmd at offset {offset}")
    return b[offset], offset + 1


def parse_segment_header(b: bytes, offset: int = 0) -> Tuple[SegmentHeader, int]:
    """Parse the 5-byte segment header. Returns (header, new_offset)."""
    if len(b) < offset + 5:
        raise ValueError(f"packet too short for segment header at offset {offset}")
    seg = SegmentHeader(
        frame_seq=b[offset] | (b[offset + 1] << 8),
        category=b[offset + 2],
        sub_type=b[offset + 3],
        status_flag=b[offset + 4],
    )
    return seg, offset + 5


def parse_record(b: bytes, offset: int, record_size: int) -> Record:
    """Parse a single record at the given offset. record_size must be 8 or 16."""
    if record_size not in (8, 16):
        raise ValueError(f"invalid record size: {record_size}")
    if len(b) < offset + record_size:
        raise ValueError(
            f"packet too short for {record_size}B record at offset {offset}"
        )
    marker = b[offset] | (b[offset + 1] << 8)
    # Note: the constant is the LE u16 reading the on-the-wire bytes "31 e1" /
    # "31 e0" — which yields 0xE131 / 0xE031 as the numeric value, NOT 0x31E1/0x31E0.
    val16 = b[offset + 2] | (b[offset + 3] << 8)
    data = b[offset + 4:offset + record_size]
    return Record(marker=marker, val16=val16, data=data)


def decode_notify(value: str | bytes) -> DecodedNotify:
    """Decode a vendor notify payload. Accepts hex string or bytes."""
    if isinstance(value, str):
        b = bytes.fromhex(value)
    else:
        b = value
    t, off = parse_transport(b)
    cmd, off = parse_cmd(b, off)
    seg, off = parse_segment_header(b, off)
    d = DecodedNotify(cmd=cmd, transport=t, segment=seg)
    if d.is_byte_grid:
        d.raw_data = bytes(b[off:])
    elif d.is_bulk:
        rec_size = d.record_size
        nrec = (len(b) - off) // rec_size
        for i in range(nrec):
            d.records.append(parse_record(b, off + i * rec_size, rec_size))
    return d


# --- Pretty-printing -----------------------------------------------------

def format_val16(v: int) -> str:
    """Format a val16 (seconds since midnight) as HH:MM:SS."""
    if v < 0 or v > 0xFFFF:
        return f"0x{v:04x}"
    h, rem = divmod(v, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_record(r: Record, rec_size: int) -> str:
    marker_str = "31e1" if r.marker == 0xE131 else "31e0"
    time_str = format_val16(r.val16)
    data_str = r.data.hex()
    if rec_size == 16:
        pairs = [
            int.from_bytes(r.data[i * 2:(i + 1) * 2], "little")
            for i in range(6)
        ]
        data_str += f"  6xu16={pairs}"
    else:  # 8
        u32 = int.from_bytes(r.data, "little")
        data_str += f"  u32={u32}"
    return f"marker={marker_str} val16=0x{r.val16:04x} ({time_str}) data={data_str}"


def format_decoded(d: DecodedNotify) -> str:
    lines = []
    t = d.transport
    s = d.segment
    lines.append(
        f"transport: ab {t.direction:02x} type=0x{t.type:02x}"
    )
    lines.append(f"cmd:       0x{d.cmd:02x}")
    lines.append(
        f"segment:   frame_seq=0x{s.frame_seq:04x} category=0x{s.category:02x} "
        f"sub_type=0x{s.sub_type:02x} status=0x{s.status_flag:02x}"
    )
    if d.is_byte_grid:
        assert d.raw_data is not None
        bits = d.raw_data
        unique = sorted(set(bits))
        lines.append(
            f"byte grid: {len(bits)}B unique={unique} "
            f"1s={sum(bits)}, 2s={sum(1 for x in bits if x == 2)}"
        )
    elif d.is_bulk:
        lines.append(f"records:   {len(d.records)} x {d.record_size}B")
        for i, r in enumerate(d.records):
            lines.append(f"  rec {i:2d}: {format_record(r, d.record_size)}")
        lines.append(
            f"duration:  {d.duration_seconds}s = {d.duration_seconds / 60:.1f}min"
        )
    return "\n".join(lines)


# --- CLI -----------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: decode_0ab.py <hex-string>")
        print("       decode_0ab.py --json <packets.json> [--src <mac>]")
        return 1
    if sys.argv[1] == "--json":
        path = sys.argv[2]
        with open(path) as f:
            pkts = json.load(f)
        target_mac = None
        if "--src" in sys.argv:
            target_mac = sys.argv[sys.argv.index("--src") + 1].lower()
        for p in pkts:
            v = p.get("value", "")
            if not v or not v.startswith("ab11"):
                continue
            if target_mac and p.get("src", "").lower() != target_mac:
                continue
            print(f"--- frame={p['frame']} ts={p['ts']} ---")
            try:
                d = decode_notify(v)
                print(format_decoded(d))
            except Exception as e:
                print(f"  decode error: {e}")
            print()
        return 0
    print(format_decoded(decode_notify(sys.argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
