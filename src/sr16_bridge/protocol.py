"""Vendor protocol layer for the SR16 smart ring (Yawell/QRing/Colmi re-skin family).

Reversed from the publicly documented Colmi R02 protocol — the SR16 ships the same
BlueX RF03 SoC + Nordic UART-over-BLE transport + 16-byte packet format. SR16
*may* differ on command opcodes (e.g. its vendor code is 0xA00A per the original
HANDOFF notes), but the transport, packet shape, checksum, and HR-log layout are
shared.

Reference: https://github.com/tahnok/colmi_r02_client  (Apache-2.0, ported patterns)
           https://github.com/atc1441/ATC_RF03_Ring   (chip-level + OTA flasher)

Packet format (16 bytes, Nordic UART transport):
    byte 0       = command (0x01=set_time, 0x03=battery, 0x15=read_hr_log, 0x16=hr_log_settings, ...)
    bytes 1..14  = sub-data (≤14 bytes, command-specific)
    byte 15      = checksum = sum(byte 0..14) & 0xFF  (NOT XOR — tahnok confirms)

Error responses: packet[0] has bit 7 set (≥ 0x80). HR-log specifically returns
sub-type 0xFF in byte 1 when the requested day has no data.

HR-log response layout (CMD 0x15):
    byte 1 = sub_type:
        0x00   header — byte 2 = expected-packet-count, byte 3 = range in minutes (typically 5)
        0x01   first data chunk — bytes 2..5 = unix ts (LE u32), bytes 6..14 = 9 HR bytes
        0x02..N subsequent data chunks — bytes 2..14 = 13 HR bytes each
        sub_type == size-1  →  last chunk, parser returns the HeartRateLog
        0x17 (23)           →  end-of-day marker (only when is_today())
        0xFF                →  no-data error for this day

HR semantics:
    0      = no measurement (slot didn't fire / ring off-finger)
    1..254 = valid BPM
    255    = tahnok treats as -1 / sentinel (parser normalises to 0)

Day storage: 288 slots @ 5-min interval = exactly 24h. Range byte tells you the
interval in minutes; the parser ignores it and uses 5 (which is what the SR16 ships).

This module is pure logic + zero IO — no bleak, no asyncio. The async transport
lives in `history_sync.py` and `enumerate_vendor.py`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------- Transport constants (Nordic UART-over-BLE, same as Colmi) ----------
# Confirmed 2026-07-07 via capture_sr16.py: SR16 advertises 16-bit UUID 0xA00A
# (vendor service) + 0000180D (standard Heart Rate). NOT Nordic UART 6E40FFF0.
# Base UUID: 0000XXXX-0000-1000-8000-00805F9B34FB → 0xA00A → "0000A00A-0000-1000-8000-00805F9B34FB".
# We will discover the RX/TX chars on next run (peripheral must walk service).
UART_SERVICE_UUID = "0000A00A-0000-1000-8000-00805F9B34FB"
UART_RX_CHAR_UUID = "PLACEHOLDER_RX"  # TBD: discover via capture_sr16
UART_TX_CHAR_UUID = "PLACEHOLDER_TX"  # TBD: discover via capture_sr16

# ---------- Command opcodes (Colmi R02 — assumed identical for SR16 until proven otherwise) ----------

CMD_SET_TIME            = 0x01
CMD_BATTERY             = 0x03
CMD_READ_HEART_RATE_LOG = 0x15   # daily HR log (288 pts/day @ 5 min)
CMD_HR_LOG_SETTINGS     = 0x16
CMD_START_REAL_TIME_HR  = 0x19
CMD_STOP_REAL_TIME_HR   = 0x1A
CMD_GET_STEP_SOMEDAY    = 0x0E   # not in HR module but present in steps.py; harmless to reference
CMD_BLINK_TWICE         = 0x13
CMD_REBOOT              = 0x12

# Sub-types for CMD_READ_HEART_RATE_LOG (0x15) responses
HR_SUB_HEADER     = 0x00
HR_SUB_FIRST_DATA = 0x01
HR_SUB_END_OF_DAY = 0x17   # 23, only on is_today()
HR_SUB_NO_DATA    = 0xFF

# Day layout
HR_POINTS_PER_DAY = 288
HR_RANGE_MINUTES  = 5   # what SR16 ships; range byte in header is advisory

PACKET_LEN = 16


# ---------- Packet helpers ----------

def checksum(packet: bytes) -> int:
    """tahnok's checksum: sum of bytes & 0xFF. Not XOR (Mats.coffee had it wrong)."""
    return sum(packet) & 0xFF


def make_packet(command: int, sub_data: bytes | bytearray | None = None) -> bytearray:
    """Build a 16-byte packet. sub_data must be ≤ 14 bytes (else AssertionError)."""
    assert 0 <= command <= 255, f"command out of range: {command}"
    sub = bytearray(sub_data) if sub_data else bytearray()
    assert len(sub) <= 14, f"sub_data too long ({len(sub)} > 14)"
    pkt = bytearray(PACKET_LEN)
    pkt[0] = command
    pkt[1:1 + len(sub)] = sub
    pkt[-1] = checksum(pkt[:15])
    return pkt


def verify_packet(pkt: bytearray) -> bool:
    """Sanity-check: right length + checksum matches + error bit not set."""
    if len(pkt) != PACKET_LEN:
        return False
    if pkt[-1] != checksum(pkt[:15]):
        return False
    if pkt[0] >= 0x80:
        return False   # error bit
    return True


# ---------- Command packet builders (re-exported for callers) ----------

def set_time_packet(target: datetime) -> bytearray:
    """CMD_SET_TIME — 7-byte BCD timestamp + language flag.

    Format: yy MM dd hh mm ss L  (BCD for date/time, 0=Chinese 1=English)
    """
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    if target.tzinfo != timezone.utc:
        target = target.astimezone(timezone.utc)
    sub = bytearray(7)
    sub[0] = _bcd(target.year % 100)
    sub[1] = _bcd(target.month)
    sub[2] = _bcd(target.day)
    sub[3] = _bcd(target.hour)
    sub[4] = _bcd(target.minute)
    sub[5] = _bcd(target.second)
    sub[6] = 1  # 0=Chinese, 1=English
    return make_packet(CMD_SET_TIME, sub)


def battery_packet() -> bytearray:
    return make_packet(CMD_BATTERY)


def read_hr_log_packet(target: datetime) -> bytearray:
    """CMD_READ_HEART_RATE_LOG — request the HR log for the day containing `target`.

    `target` is normalised to start-of-day (UTC) per tahnok. Pass a unix-ts (LE u32)
    as 4-byte sub-data. Day rollover on the ring follows whatever timezone the ring's
    internal clock is set to (which we set via set_time_packet on each sync).
    """
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    if target.tzinfo != timezone.utc:
        target = target.astimezone(timezone.utc)
    start_of_day = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
    sub = struct.pack("<L", int(start_of_day.timestamp()))
    return make_packet(CMD_READ_HEART_RATE_LOG, sub)


# ---------- HR-log chunk parser ----------

@dataclass
class HeartRateLog:
    """One day's worth of HR data returned by the ring.

    `bpm` is always exactly 288 ints long. 0 means no measurement in that slot;
    1..254 are real BPM. We do not preserve tahnok's -1 sentinel — the ring itself
    seems to send 0 for missing, and we treat anything > 0 and < 255 as BPM.
    """
    timestamp: datetime    # start-of-day the ring reported
    bpm: list[int] = field(default_factory=list)
    raw_packets: list[bytearray] = field(default_factory=list)

    @property
    def valid(self) -> list[int]:
        return [b for b in self.bpm if 1 <= b <= 254]

    @property
    def mean_bpm(self) -> Optional[float]:
        v = self.valid
        return sum(v) / len(v) if v else None

    @property
    def peak_bpm(self) -> Optional[int]:
        v = self.valid
        return max(v) if v else None


class NoData:
    """Ring replied with HR_SUB_NO_DATA for this day."""


class HeartRateLogParser:
    """Stateful chunk reassembler for HR-log responses.

    Usage:
        parser = HeartRateLogParser()
        for chunk in stream:
            result = parser.feed(chunk)
            if isinstance(result, (HeartRateLog, NoData)):
                handle(result)
                parser = HeartRateLogParser()   # ready for next day
    """

    def __init__(self, *, day_is_today: bool = False) -> None:
        self._reset()
        self._day_is_today = day_is_today

    def _reset(self) -> None:
        self._raw: list[int] = []
        self._size: int = 0       # expected chunk count from header
        self._range_min: int = HR_RANGE_MINUTES
        self._timestamp: Optional[datetime] = None
        self._index: int = 0      # how many HR bytes we've appended
        self._pkts: list[bytearray] = []

    def feed(self, pkt: bytearray) -> Optional[HeartRateLog | NoData]:
        """Feed one 16-byte packet. Returns a result when the day is complete,
        None while waiting for more chunks, or NoData on HR_SUB_NO_DATA."""
        if len(pkt) != PACKET_LEN:
            return None   # ignore malformed
        if pkt[0] != CMD_READ_HEART_RATE_LOG:
            return None   # not ours
        if pkt[0] >= 0x80:
            return None   # error bit set on the response

        self._pkts.append(bytearray(pkt))
        sub = pkt[1]

        if sub == HR_SUB_NO_DATA:
            r = NoData()
            self._reset()
            return r

        if sub == HR_SUB_HEADER:
            # byte 2 = expected chunk count (after this one); byte 3 = range (min)
            self._size = pkt[2] + 1   # tahnok's parser treats size as "data chunks after header"
            self._range_min = pkt[3] if pkt[3] > 0 else HR_RANGE_MINUTES
            self._raw = [0] * HR_POINTS_PER_DAY
            self._index = 0
            return None

        if sub == HR_SUB_FIRST_DATA:
            ts = struct.unpack_from("<l", pkt, offset=2)[0]
            self._timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            # 9 HR bytes after the 4-byte ts; packet[15] is checksum so we take 6..15
            chunk = list(pkt[6:15])
            self._raw[self._index:self._index + len(chunk)] = chunk
            self._index += len(chunk)
            return None

        # subsequent data chunks
        if sub >= 2:
            chunk = list(pkt[2:15])
            end = min(self._index + len(chunk), HR_POINTS_PER_DAY)
            self._raw[self._index:end] = chunk[: end - self._index]
            self._index = end
            # finished?
            if sub == (self._size - 1) or self._index >= HR_POINTS_PER_DAY:
                if self._timestamp is None:
                    self._timestamp = datetime.now(timezone.utc)
                log = HeartRateLog(
                    timestamp=self._timestamp,
                    bpm=list(self._raw),
                    raw_packets=self._pkts,
                )
                self._reset()
                return log
            return None

        # unknown sub_type — ignore but keep state
        return None


# ---------- Battery response parser ----------

@dataclass
class BatteryInfo:
    level: int        # 0..100
    charging: bool


def parse_battery(pkt: bytearray) -> Optional[BatteryInfo]:
    if len(pkt) != PACKET_LEN or pkt[0] != CMD_BATTERY:
        return None
    return BatteryInfo(level=int(pkt[1]), charging=bool(pkt[2]))


# ---------- HR-log settings response parser ----------

@dataclass
class HRLogSettings:
    enabled: bool
    interval_minutes: int


def parse_hr_log_settings(pkt: bytearray) -> Optional[HRLogSettings]:
    if len(pkt) != PACKET_LEN or pkt[0] != CMD_HR_LOG_SETTINGS:
        return None
    raw = pkt[2]
    enabled = raw == 1
    if raw not in (1, 2):
        enabled = False
    return HRLogSettings(enabled=enabled, interval_minutes=int(pkt[3]))


# ---------- internal ----------

def _bcd(n: int) -> int:
    """Pack a 0..99 integer into BCD (e.g. 23 -> 0x23)."""
    assert 0 <= n < 100, f"BCD out of range: {n}"
    return ((n // 10) << 4) | (n % 10)