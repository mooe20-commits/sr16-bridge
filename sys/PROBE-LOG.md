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
