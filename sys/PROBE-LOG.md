# SR16 BLE Probe Log
*All probes: scan 30s, bleak >=0.22 via CoreBluetooth on darwin. Output: device.name, adv.service_uuids, manufacturer keys, RSSI.*


## 2026-07-07 10:53:30Z — first scan (30s, bleak via CoreBluetooth)

### Device (the one we want)
```
name          : SR16
BT UUID       : 36BE6673-1486-2E90-38E9-3E097DB4CC43
RSSI          : -80 dBm
service_uuids :
  0000180d-0000-1000-8000-00805f9b34fb   ← BT SIG Heart Rate Service (standard, free to read)
  0000a00a-0000-1000-8000-00805f9b34fb   ← VENDOR 0xA00A (proprietary data channel)
manufacturer : company_id 0x06D6 (1750) ← Telink Semiconductor (Colmi / QRing chip family)
```
→ standard HR service means live heart-rate notifications are achievable **without
  any reverse-engineering**. The 0xA00A service is the vendor protocol we need to
  crack to read historical sleep/HRV/SpO2 — almost certainly a re-skinned Colmi
  protocol.

### Other devices seen (NOT the ring, context only)
- Dryer (BLE moisture sensor)
- iO Sense (BLE thermometer)
- M89-S-3217 (the smart-glasses from glasses-bridge — still alive)
- Samsung TV (BLE remote target)

### What I did NOT verify yet
- Whether the ring is also connected to another host (a phone, probably Rogbid's app).
  Most smart rings support only one BT bond at a time. If your phone is paired,
  Mac will fail to connect / GATT enumerate until the phone releases the ring.
- Historical data shape from the 0xA00A service.

### GATT enumerate run at 2026-07-07T10:58:11.602050+00:00

### GATT enumerate run at 2026-07-07T10:58:58.134148+00:00

### GATT enumerate run at 2026-07-07T11:11:24.592321+00:00

#### analyze run @ 2026-07-07T11:40:00.008855+00:00
  device=ALL  model=Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest
  rows_in=60  windows=1  dry_run=False

#### analyze run @ 2026-07-07T11:40:41.997983+00:00
  device=ALL  model=Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest
  rows_in=8  windows=1  dry_run=False

#### analyze run @ 2026-07-07T11:41:36.859285+00:00
  device=ALL  model=Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest
  rows_in=12  windows=1  dry_run=False

#### analyze run @ 2026-07-07T11:41:56.548213+00:00
  device=ALL  model=Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest
  rows_in=15  windows=1  dry_run=False

#### history_sync (synthetic) @ 2026-07-07T17:22:50.143973+00:00
  days=3  days_with=3  inserted=712  skipped=0

#### history_sync (synthetic) @ 2026-07-07T17:22:50.196100+00:00
  days=3  days_with=0  inserted=0  skipped=712

#### analyze run @ 2026-07-07T17:26:17.142568+00:00
  device=ALL  model=Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest
  rows_in=221  windows=1  dry_run=False

#### enumerate_cocoa @ 2026-07-07T17:44:24.396645+00:00
  discovered=13  selected=4D020B95-547D-323F-F26B-C259B0DC94DA  name='Dryer'
  services=[]

#### hermes_export @ 2026-07-07T18:20:06+00:00
  days=3  out_dir=/Users/mih/Documents/Obsidian Vault/Health
  files=3 (2026-07-05, 2026-07-06, 2026-07-07)
  rows: 236 / 242 / 329   analyses: 0 / 21 / 8

#### enumerate_cocoa @ 2026-07-07T18:36:17.213758+00:00
  discovered=15  selected=36BE6673-1486-2E90-38E9-3E097DB4CC43  name='SR16'
  services=[]

#### enumerate_cocoa @ 2026-07-07T18:41:45.492474+00:00
  discovered=17  selected=644E1846-6FB6-9407-E226-D9D12D77477D  name=''
  services=[]

#### session 6 BLE breakthrough @ 2026-07-07T21:38:00+00:00
  context: operator confirmed ring wakes but macOS HID-grabs it as a mouse
  capture_sr16 first successful scan: SR16 at 36BE6673-1486-2E90-38E9-3E097DB4CC43, rssi=-75
  advertised services: 0xA00A (vendor), 0x180D (Heart Rate)
  post-connect services: 3 — A00A, 180D, FF00 (FF00 only visible after connect)
  vendor service UUID confirmed: A00A → 0000A00A-0000-1000-8000-00805F9B34FB
  patched: src/sr16_bridge/protocol.py — UART_SERVICE_UUID set to A00A
  pending: RX/TX char UUIDs (need full char walk; script crashed on discoverCharacteristics_forService_ fix verified, restart pending)
  crashed on: read-only peripheral.delegate = self → fixed to setDelegate_
  crashed on: CBService.discoverCharacteristics_forService_ → fixed to peripheral.discoverCharacteristics_forService_
  operator paired via macOS HID popup → SR16 now stuck in HID-connected state
  blueutil --unpair / --disconnect are no-ops on HID connections
  PENDING OPERATOR: drop HID connection (System Settings disconnect OR sudo killall bluetoothd)

#### SESSION 6 BREAKTHROUGH — char_inventory @ 2026-07-07T18:48:57+00:00
  SR16 @ 36BE6673-1486-2E90-38E9-3E097DB4CC43  rssi=-83
  services: 3  (FF00, A00A, 0BC0)
  chars: 7
    FF00:FF01  read,write,write-no-response,notify   ← likely TX (notify, ring→us)
    FF00:FF02  read,write,write-no-response           ← likely RX (write, us→ring)
    FF00:FF03  read,write,write-no-response           ← control
    A00A:B002  read,write,write-no-response,notify    ← A00A main
    A00A:B003  read,notify
    0BC0:0BC1  write,write-no-response,notify
    0BC0:0BC2  read,notify
  capture path: scanner+connect+discoverServices+discoverCharacteristics
  vendor service = A00A confirmed (matches assumption in protocol.py)

#### session 6.5 closeout — protocol RE pending @ 2026-07-07T22:00:00+00:00
  HID-auto-bond dance worked: Forget in System Settings → ring re-advertises → CB connect races the grab
  capture_sr16 captured full inventory: 3 services, 7 chars
  live_probe writes succeed (write-ack) but ring never notifies back
  hypothesis: SR16 uses different opcodes than Colmi R02 tahnok reference
  next step: nRF Connect sniff OR brute-force 1-byte writes to find which opcode triggers notify

#### enumerate_cocoa @ 2026-07-08T08:11:30.208318+00:00
  discovered=15  selected=36BE6673-1486-2E90-38E9-3E097DB4CC43  name='SR16'
  services=[]

#### session 7 opcode sweep @ 2026-07-08T11:25:00+00:00
  context: ring paired as HID Mouse, dropped in Sys Settings, raced sweep in
  sweep 0x00..0xff (256 writes) to FF02, 0.4s wait per opcode
  subscribed: FF01 (NOTIFY ON), 0BC2 (NOTIFY ON)
  CCCD errors persist on A00A:B002 + 0BC0:0BC1 (Code=10 attribute not found)
  FINAL: notifications received = 0
  interpretation: SR16 does NOT respond to single-byte writes to FF02 on
    the FF00-service TX char. protocol shape is NOT Colmi R02 (16-byte
    cmd+subdata+checksum packet).
  next: pivot — try writing to A00A:B002 (the true "A00A" vendor channel),
    or use nRF Connect on phone to sniff opcode table.

#### session 7 A00A probe @ 2026-07-08T11:35:00+00:00
  context: ring disconnected again, raced probe_a00a in
  write target: A00A:B002, listen: A00A:B003 (NOTIFY ON, CCCD ok)
  opcodes sent: 0x01, 0x03, 0x12, 0x13, 0x15, 0x16, 0x19, 0x1A,
                0xA0, 0xA1, 0xB0, 0xC0
  result: 0x01 + 0x03 wrote-acked, ring dropped connection after ~6s
          10 opcodes silently drained (likely buffer-side, no notify ack)
  FINAL: 0 notifications on B003
  interpretation: SR16 does NOT respond to 16-byte Colmi-style packets
    on ANY of FF02 (FF00-RX) OR B002 (A00A-RX). The protocol is
    a fundamentally different shape.
  next: most likely cause is H3 — BLE encryption / LTK pairing required
    before the ring accepts vendor commands. Next session: phone + nRF
    Connect to sniff the actual vendor app packets.


#### subscribe_180d attempt @ 2026-07-08T09:27:43.585179+00:00
  context: post-session-7, Mac-side standard HR quick-win attempt
  ring unpaired from macOS (HID Forget in Sys Settings) to clear auto-rebond
  ring stays asleep after wake; advertise windows too short for our scanner
  tried: killall bluetoothd, Forget device, power-cycle + charger, tap wake
  bleak discover() saw SR16 once at UUID 36BE6673-1486-2E90-38E9-3E097DB4CC43
  scanner filter bug: rejected by MAC form vs BLE UUID form (fixed in code, never tested)
  scan timeout 180s, ring went back to sleep before connect
  vendor app = RWfit (Samsung Galaxy, paired once, LTK on phone)
  PIVOT DECISION: stop macOS 0x180D attempts — ring firmware sleep too aggressive
  PIVOT TO: Android HCI snoop via Developer Options → btsnoop_hci.log → Wireshark
  ref: Th0rgal/open_oura (Rust, Oura ring RE, same GATT pattern)
  ref: ringverse/protocol (multi-vendor ring RE)
  ref: Gadgetbridge BT Protocol RE wiki
  next: install Wireshark + adb, enable USB debugging + BT HCI snoop on Galaxy,
        trigger RWfit sync, capture btsnoop_hci.log via adb bugreport,
        open in Wireshark → discover SR16 opcodes → patch protocol.py

## 2026-07-08 — session 9 (pivot to Android HCI snoop)

### Tools installed
- `adb` via `brew install --cask android-platform-tools` → v37.0.0
- `tshark` via `brew install wireshark` → 4.6.6 (CLI only, no .app — sudo blocked)
- Both confirmed working.

### Phone state
- Galaxy S10e (SM_G970F), Android 12 (API 31)
- adb authorized over USB-C (RF8M31H582A)
- `mSnoopLogSettingAtEnable = full` — Dev Options toggle took
- `persist.bluetooth.btsnoopenabled` is empty (Samsung uses settings secure, not prop — snoop still works)
- App: `com.rw.revivalfit` (NOT `com.koepovksmart.rwfit` — earlier greps missed it; pkg path was different)
- Activity: `com.rw.revivalfit/com.example.test.ui.testui.TMainRingActivity`
- Ring state: `(Connected) 38:00:00:00:DE:90 [LE] SR16 (Hogp)` — ACL up for 10+ min

### New tool: decode_snoop.py
- `src/sr16_bridge/decode_snoop.py` shipped this session
- Uses tshark headlessly to extract ATT writes from btsnoop_hci.log
- Tags writes destined for the SR16 (by BD_ADDR 38:00:00:00:DE:90 or vendor char handles 0x000e–0x0013)
- Emits per-handle write table + first-N hex dumps + protocol.py patch hint
- Output: `~/health/sr16_captures/decoded_<ts>.md`

### Bugreport cycle
- First adb bugreport (proc_56c8496e5077) was killed by SIGTERM (process manager, not us)
- Second bugreport running: proc_1e3fb1135682
- Will extract + decode the btsnoop_hci.log from the resulting zip

## 2026-07-09 — session 10 (0xAB protocol decode)

### Goal
Decode the 158 SR16-bound notify packets captured in session 9's bugreport.
Discovered the 0xA3/0x67/0x73 payload structure and rewrote the wrong
Colmi-R02 model in protocol.py mentally (separate work in session 10+).

### Approach
- Load `/Users/mih/health/sr16_captures/packets_20260708T100733Z.json` (250 ATT frames, 158 SR16-bound)
- Filter to ab11-prefixed notifies from src 38:00:00:00:DE:90
- Group by length and cmd byte to map packet types

### Cmd distribution (78 ring->phone notifies, all ab11-prefixed)
| cmd | count | size   | what |
|-----|-------|--------|------|
| 0x03 | 51    | 9B     | write-ack echo (51x = 12-13 unique patterns, repeated 4x each) |
| 0x04 | 3     | 10B    | status response |
| 0x05 | 2     | 11B    | status response |
| 0x06 | 4     | 12B    | status response |
| 0x13 | 5     | 25B    | device info (ends with ASCII serial "3080000") |
| 0x43 | 3     | 73B    | medium data (4 records of 16B) |
| 0x53 | 1     | 89B    | medium data (5 records of 16B) |
| 0x67 | 1     | 109B   | 100B byte grid (values 0/1/2 — wear/activity bitmap) |
| 0x6B | 3     | 113B   | 13 records of 8B (older day's hourly) |
| 0x73 | 1     | 121B   | 14 records of 8B (older day's hourly) |
| 0xA3 | 4     | 169B   | 10 records of 16B (TODAY, with rich 12B data tail) |

### Wire format (CORRECTED — replaces earlier "LEN byte" hypothesis)
```
ab <dir> <type> <cmd> [5B segment header] [N x record]
```
- ab: 0xAB magic (constant)
- dir: 0x01 (phone->ring) or 0x11 (ring->phone)
- type: 0x00 in every observed packet (constant — may be a flag)
- cmd: 1B opcode
- segment header (5B): <2B frame-seq> <1B category> <1B sub_type> <1B status_flag>
  - category: 0x02 = metadata/status, 0x05 = measurement data
  - sub_type: 0x1a = today, 0x17 = older day, 0x04 = device info, etc.
  - status_flag: 0x10 mostly; 0x00/0x30 for 0x03 echo responses
- record (8B or 16B):
  - 2B marker (on-the-wire bytes "31 e0" or "31 e1")
  - 2B val16 LE (seconds-since-midnight-UTC-of-day)
  - 4B or 12B data

The LEN byte (0x00 always) seen in tshark's value field is actually
the `<type>` byte in the transport, NOT a length field. tshark's value
field includes the full transport.

### Markers
- 0xE031 (bytes "31 e0") = regular hourly record
- 0xE131 (bytes "31 e1") = day-summary record
  - Position: at START of in-progress day (0xA3 = today)
  - Or at END of completed day (0x43/0x53/0x6B/0x73)
  - Day summary's val16 is the END time of the day
  - Day summary's data carries TOTAL day metrics (steps, calories, etc.)

### val16 = seconds-since-midnight-UTC
- 16-bit unsigned (wraps at 0xFFFF = 18:12:15)
- Deltas between regular records: exactly 4110 seconds (68.5 min) — the ring's "slot" duration
  - One off-by-one jitter seen in 0x6B around the 0xFFFF wrap (4111 instead of 4110)

### Decoded values
- 0xA3 record 0 (summary): val16=0xFE03 (18:03:47) — strange, may be END of yesterday, not start of today
  - data12: 6xu16 = [0, 27916, 5888, 49344, 43521, 4188]
  - Plausible: total_steps=27916, cal=5888 (kcal*10?), dist=49344 (m), ?
- 0xA3 records 6-9 (afternoon hourly):
  - 14:49: data12 = [0, 6144, 0, 57901, 768, 32823]
  - 15:58: data12 = [0, 58114, 1280, 39556, 25344, 61452]
  - Could be: [0, hr_avg_bpm, hr_min, hr_max, steps, calories] — needs ring data to confirm
- 0x67 byte grid (100B): 25 ones, 1 two, 74 zeros
  - Likely 5-min slots (8.3h coverage) OR 6-min slots (10h)
  - 0=no measurement, 1=valid slot, 2=active/wear slot

### 0xA3 retransmits are byte-identical
- All 4 0xA3 packets differ only in bytes 4-7 (the 2B frame-seq in segment header)
- This is a packet retransmit loop, NOT 4 different time samples

### Tool shipped: `src/sr16_bridge/decode_0ab.py`
- Offline decoder, no IO
- 270 lines, 0 deps
- 5 unit tests in `tests/test_decode_0ab.py`, all pass
- CLI: `python3 -m sr16_bridge.decode_0ab --json <packets.json> --src <mac>`

### What's NOT yet decoded
- 0xA3 data12 6xu16: unknown field semantics (HR? steps? combined metrics?)
  - Need second capture with KNOWN activity (run, sleep, rest) to disambiguate
- 0x6B/0x73 data4 u32: small-int metric (21, 76, 88) — could be steps, cal, or HR samples
  - 21 BPM = dead, so likely not HR
  - 21 steps/min = walking pace? 76 cal = 1h of moderate activity?
- 0x67 byte grid [0,1,2] semantics — needs more captures to find pattern

### What's needed to fully decode
1. One more Android HCI snoop, captured at the START of a sync (so we get the
   service-discovery phase and the 128-bit service UUID for handles 0x003e/0x0040)
2. Ideally: one capture during known activity (running, sleeping) so we can
   cross-reference 0xA3 data12 values against the real-world activity
3. (Already known) The 4110 sec/hour slot is the vendor's slot — not a 5-min
   HR grid as the handoff hypothesized. There is NO 5-min HR grid in this protocol.

### Open blockers (carry to session 11+)
- 128-bit service UUID for 0x003e/0x0040 (needs new snoop)
- 0xA3 data12 metric semantics (needs corroborating capture)
- protocol.py rewrite (Colmi R02 model is still wrong — now we have a clear
  picture of what to replace it with)

### Pitfalls captured
- P35: tshark's GATT value field INCLUDES the transport header (ab <dir> <type> <cmd>).
  The LEN byte hypothesis from earlier sessions was a misread of the type byte.
- P36: On-wire bytes "31 e0" / "31 e1" read as LE u16 are 0xE031 / 0xE131, NOT 0x31E0 / 0x31E1.
  Easy to flip if you think of the bytes as a big-endian u16.
- P37: 0xA3 packets (and other bulks) are retransmitted 4x with identical
  body. Decoders must dedupe by body hash, not by frame number.

---

## Session 11 (2026-07-08 evening) — Path A: protocol.py rewrite

### What we shipped
- `src/sr16_bridge/protocol.py` — full rewrite to 0xAB model (~500 lines)
- `src/sr16_bridge/history_sync_offline.py` — replay harness (~200 lines)
- `src/sr16_bridge/connect_pull.py` — live BLE scaffold (~180 lines)
- `tests/test_protocol_0ab.py` — 22 unit tests, all pass
- `HANDOFF-session-11.md` — handoff for session 12

### What we discovered
1. **0xA3 retransmits are NOT byte-identical.** Only records 1-9 (regular
   hourly) match across the 4 retransmits. Record 0 (day-summary, marker
   0xE131) varies in val16 (advances with wall-clock). data12 IS identical.
   Fix: dedupe per-record, not per-packet. P37 in session 10 was wrong.

2. **Phone→ring writes are fully decoded now.** 78 writes break down as:
   - 75 × cmd=0x03 (fetch trigger, with sub_type picking the block)
   - 1 × cmd=0x04 (status query)
   - 1 × cmd=0x05 (config)
   - 1 × cmd=0x09 (begin-sync marker)
   The sub_type field in the segment header (after cmd) selects which
   block the ring returns:
   - 0x1A → 16B-record block (today or older day)
   - 0x17 → 8B-record block (older day)
   - 0x63 → byte grid
   - 0x02-0x0D → per-hour 16B blocks (one per metric family)

3. **0x09 begin-sync packet is 15B, not 16B or 17B.** Earlier writeup
   counted the segment header as 5B (matching bulk records); it's 6B
   for 0x09 specifically.

4. **Device serial from 0x13 may be bogus.** Last 8B decodes to ASCII
   "30380000" = "080000" — doesn't match any expected format. May need
   a different field offset for the real serial, or the ring simply
   doesn't populate it.

### Test results
27/27 tests pass:
- 5 session-10 tests (decode_0ab)
- 22 new tests (protocol_0ab): packet builders, parsers, dedupe,
  high-level helpers, constants

### Open for session 12
- Path B: fresh bugreport at connect-time → service UUID
- Path C: wire connect_pull.py against live ring (UUID must be real)
- Path D: 0x67 byte grid semantics (wear bitmap?)
- 0xA3 metric semantics still unconfirmed (needs corroborating capture)

---

## Session 12 (2026-07-09) — Path B resolved without a fresh bugreport

### Goal
Resolve the service-UUID blocker (Path B from session 11 handoff).

### What we shipped
- `src/sr16_bridge/protocol.py` — `UART_SERVICE_UUID`, `UART_TX_CHAR_UUID`, `UART_RX_CHAR_UUID` real (not placeholders)
- `src/sr16_bridge/connect_pull.py` — imports protocol-level `UUID_KNOWN`; placeholder string check now AND-gated
- `tests/test_protocol_0ab.py` — 2 new tests pin the UUIDs and SIG base format; suite is 29/29
- `HANDOFF-session-12.md` — handoff for session 13

### Discovery (option C: query existing snoop)
Ran `tshark -r btsnoop_hci.log -Y 'btatt.opcode==0x11'` against the
session-9 capture and found TWO `Read By Group Type Response` frames
(367, 372). Those frames contain primary-service-discovery responses —
listing every service the ring exposes, with its 128-bit UUID and
handle range. **We never needed a fresh bugreport.**

```
Frame 367: services 0x0001..0x0033 → GAP (0x1800), GATT (0x1801), HID (0x1812)
Frame 372: services 0x0034..0xFFFF → FF00, A00A (handles 0x003C..0x0041), 0BC0
```

The vendor transport rides on 0xA00A:
- handle 0x003E declares char 0xB002 (props 0x1E = R+W+WNR+N) ← phone writes here
- handle 0x0040 declares char 0xB003 (props 0x12 = R+N)       ← ring notifies here

Full 128-bit form (SIG base-UUID alias):
```
0000a00a-0000-1000-8000-00805f9b34fb   # service
0000b002-0000-1000-8000-00805f9b34fb   # TX (phone -> ring)
0000b003-0000-1000-8000-00805f9b34fb   # RX (ring  -> phone)
```

### Test results
29/29 pass (was 27 — added `test_uart_service_uuid_is_real` and
`test_uart_uuids_match_bt_sig_base`).

### Recommended next session
Path C step 2: `connect_pull.py --scan 30` against the live ring. See
P1, P3 in `~/.hermes/skills/devops/sr16-ring-mac-pitfalls/SKILL.md`
for the Mac-side HID/sleep gauntlet that will probably bite first.
If we hit it, the fallback is the known-activity capture for 0xA3
metric semantics — operator does a 5-min run, then triggers sync.

### Pitfalls captured (encoded in sr16-ring-mac-pitfalls skill)
- P41: Always query the snoop for ATT primary-service-discovery before declaring a fresh capture needed.

---

## Session 12.5 (2026-07-09 evening) — Path C attempt + closeout

### Goal
Live ring pull from Mac using the new UUIDs.

### What worked
- `src/sr16_bridge/live_pull.py` — new PyObjC-based live pull harness
- Scan → connect → discoverServices(A00A) → discoverCharacteristics(B002, B003)
- All 3 services reachable: FF00, A00A, 0BC0 (matches session 6.5)
- B002 props=30, B003 props=18 (R, Notify confirmed)

### What didn't
- `setNotifyValue_forCharacteristic_(True, B003)` returned success but **0 notifies
  arrived** after sending the fetch packet. Suspected: P6 CCCD write window too short,
  or ring needs vendor keep-alive (P3) to wake the data path.
- `retrievePeripherals_` returns 0 when phone's Hogp link is active, so scan-with-race
  is the only route.

### Diagnostic findings (saved as P42 in skill)
- CoreBluetooth re-numbers ATT handles: snoop 0x3E/0x40 → CB 0x3D/0x3F (off-by-one).
  Cause: likely HID-profile pairing vs Hogp-only pairing leads to different GATT DB
  enumeration. Fix: use char UUIDs, not handles, on Mac.

### Decision
Stop chasing Mac-side live pull — P1/P3 is a hardware-OS-level state, not a code bug.
The phone-side known-activity snoop is strictly better for the highest-value open
question (0xA3 6×u16 metric semantics) and avoids P1/P3 entirely.

### Next session
1. 5-min known-activity capture on phone (operator does walk/run)
2. Trigger RWfit sync during/right after the activity
3. adb bugreport → extract btsnoop_hci.log
4. decode_snoop.py + decode_0ab.py → pinpoint HR avg/min/max/steps/cal layout
5. Then port `hr_live.py` from 0x180D HR to 0xAB (now that we know the metrics)

