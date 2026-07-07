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
