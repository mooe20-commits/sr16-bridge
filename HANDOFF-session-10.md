# sr16-bridge Handoff — session 10 (2026-07-08 evening)

Read this first when picking up sr16-bridge in a new session.

## TL;DR — session 10 status

**The 0xAB protocol structure is now fully decoded.** We can parse every
captured packet. The handoff's "4-byte LE timestamp + 1-byte BPM grid"
hypothesis was **wrong** — the actual structure is hourly records with
multi-metric data tails, and **there is no 5-min HR grid in this protocol
at all**.

What's now KNOWN:
- Wire format: `ab <dir> <type> <cmd> [5B segment header] [N x record]`
- No LEN byte, no checksum (the "LEN byte" from the session-9 handoff was
  actually the `<type>` byte of the transport, always 0x00)
- All 11 cmds mapped: 0x03=ack, 0x04/0x05/0x06=status, 0x13=device-info,
  0x43/0x53/0x67/0x6B/0x73/0xA3=data
- Bulk record structure: `[2B marker] [2B val16=sec-since-midnight] [4B or 12B data]`
- Markers: 0xE031 (regular hourly) / 0xE131 (day-summary)
- val16 deltas = 4110 sec/record (the vendor's "slot" duration)
- 0xA3 retransmits are byte-identical except for segment header

What's NOT yet known:
- 128-bit service UUID for char handles 0x003e/0x0040
- 0xA3 record 12B data tail semantics (6×u16 metrics — likely HR, steps, cal)
- 0x6B/0x73 record 4B data tail semantics (small-int metric, values 21/76/88)
- 0x67 byte grid [0,1,2] slot semantics (wear bitmap? activity grid?)

## What we tried in session 10

### 1. Decoded 250 ATT frames from session 9's snoop
- Loaded `~/health/sr16_captures/packets_20260708T100733Z.json`
- Filtered to 78 ring->phone notifies (ab11 prefix, src 38:00:00:00:DE:90)
- Grouped by length and cmd byte to map packet types

### 2. Discovered the correct wire format
- Earlier handoff claimed: `ab <dir> <LEN> <cmd> [data]` with LEN=payload length
- tshark shows LEN byte is always 0x00 — the LEN hypothesis was wrong
- **Correct format**: `ab <dir> <type> <cmd> [data]` where `<type>` is 0x00
  always (a constant, not a length)
- The "LEN" byte is just the `<type>` byte, which tshark displays as part
  of the value

### 3. Decoded all 6 bulk-data cmds
- 0x43 (73B): 4 records of 16B — older day, 4 hours
- 0x53 (89B): 5 records of 16B — older day, 5 hours
- 0x67 (109B): 100B raw byte grid (no records) — wear/activity bitmap
- 0x6B (113B): 13 records of 8B — older day, 13 hours
- 0x73 (121B): 14 records of 8B — older day, 14 hours
- 0xA3 (169B): 10 records of 16B — TODAY, with rich 12B data tail

### 4. Mapped record structure
- All records: `[2B marker "31 e0" or "31 e1"] [2B val16 LE] [4B or 12B data]`
- val16 = seconds-since-midnight-UTC (16-bit, wraps at 0xFFFF=18:12:15)
- Deltas between regular records: 4110 sec (= 68.5 min) — the vendor's slot duration
- Marker 0xE131 (bytes "31 e1") = day-summary record (carries total-day metrics)
  - At START of in-progress day (0xA3 = today)
  - At END of completed day (0x43/0x53/0x6B/0x73)
- Marker 0xE031 (bytes "31 e0") = regular hourly record

### 5. The 0xA3 data tail is NOT a 5-min HR grid
- 0xA3 records have 12B data = 6×u16
- Values like [0, 6144, 0, 57901, 768, 32823] — too large to be BPM (max ~200)
- More likely: aggregated hourly metrics (HR avg, HR max, HR min, steps, cal, dist)
- The 6×u16 layout strongly suggests [reserved, hr_avg, hr_min, hr_max, steps, cal] but
  this is **unconfirmed** — needs a second capture during known activity to validate

### 6. 0xA3 retransmits are byte-identical
- All 4 0xA3 packets in the snoop differ ONLY in the 2B frame_seq (segment header bytes 0-1)
- The body (everything after segment header) is 100% identical between retransmits
- This is a packet retransmit/ack loop, NOT 4 different time samples
- Decoders must dedupe by body hash, not by frame number

## Files shipped this session

| File | Purpose | Status |
|---|---|---|
| `src/sr16_bridge/decode_0ab.py` | Offline 0xAB protocol decoder. CLI + Python API. | ✅ shipped + tested |
| `tests/test_decode_0ab.py` | 5 unit tests, all pass (loads real snoop data) | ✅ |
| `sys/PROBE-LOG.md` | session 10 block added with full schema + open blockers | ✅ |
| `HANDOFF-session-10.md` | this file | ✅ |

Total: +290 lines, 0 new external deps.

## What session 11 should pick up

**Two parallel paths; do whichever has the most leverage first:**

### Path A: rewrite `protocol.py` to the 0xAB model (1-2 hours)
The `protocol.py` module from session 4 is **completely wrong** about this ring.
Replace the Colmi R02 model with the 0xAB model from session 10.

Specifically:
1. New constants: `UART_SERVICE_UUID` (still unknown — needs path B),
   `UART_RX_CHAR_UUID`, `UART_TX_CHAR_UUID`
2. New `make_packet(cmd, data)` that emits `ab <dir=01> <type=00> <cmd> [data]`
3. New `parse_notify(value)` that uses `decode_0ab.py` (just import it)
4. New opcodes: 0x03 (sync request), 0x13 (device info), 0xA3 (today's hourly block)
5. The 5 unit tests in `tests/test_protocol.py` need a complete rewrite — they
   encode Colmi R02 behavior

**This path does NOT require the ring to be awake** — protocol.py is pure logic.

### Path B: new phone capture for service-discovery + 0xA3 disambiguation (30-60 min)
1. Operator: enable Dev Options snoop on Galaxy (already done from session 9)
2. **Trigger a fresh sync at the START of a connection** (so the snoop catches
   the service-discovery phase)
3. Pull bugreport, decode with `decode_snoop.py`
4. Extract 128-bit service UUID for handles 0x003e/0x0040
5. Bonus: if operator does a 5-min walk or run before triggering sync, the
   0xA3 records' 6×u16 values can be cross-referenced against known activity
   to disambiguate the metric layout

**This path requires operator action (phone in hand, doing a known activity).**

### Recommended order: A first, B in parallel
- A is unblocked and produces a working `protocol.py` + `history_sync.py` that
  we can at least run on synthetic data
- B unblocks the live ring path AND the 0xA3 metric disambiguation
- A and B can be done in parallel if operator has time for a phone capture

## What's NOT done / open blockers

- ❌ `protocol.py` rewrite (the Colmi R02 model is still wrong)
- ❌ 128-bit service UUID for 0x003e/0x0040
- ❌ 0xA3 data12 metric semantics
- ❌ Live HR streaming into `hr_readings` from any source
- ❌ Launchd plist for hr_live.py daemon
- ❌ `history_sync.py` end-to-end against the real ring

## Pitfalls captured (additions to handbook)

### 32. tshark's GATT value field INCLUDES the transport header (2026-07-08)
Earlier writeups assumed a `LEN` byte separated the 4-byte transport header
from the payload. tshark's "value" field shows the FULL bytes the application
wrote to the characteristic, which INCLUDES the ab/dir/type/cmd header. The
"LEN" byte we see is actually the `<type>` byte of the transport, and is
always 0x00 in this vendor protocol.

### 33. On-wire bytes "31 e0" / "31 e1" are LE u16 = 0xE031 / 0xE131, not 0x31E0 / 0x31E1 (2026-07-08)
If you read the bytes as BE u16 you get the wrong value. The marker constant
for the day-summary record is 0xE131, not 0x31E1. This caused a unit-test
failure in session 10 and was the source of the confusion in the handoff.

### 34. 0xA3 packets are retransmitted 4x with identical body (2026-07-08)
The 2B frame_seq in the segment header is the only thing that varies. When
decoding, dedupe by body hash (everything after the segment header) — don't
treat each frame as a unique sample.

## Reference projects used in session 10
- None — session 10 was pure packet analysis on the existing snoop data
- Previous session references (Oura RE, Gadgetbridge wiki, etc.) still apply
  but session 10's work was self-contained
