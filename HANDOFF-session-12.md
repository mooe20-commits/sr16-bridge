# sr16-bridge Handoff — session 12 (2026-07-09)

Read this first when picking up sr16-bridge in a new session.

## TL;DR — session 12 status

**Path B (the service-UUID blocker) is RESOLVED without a fresh bugreport.**

The session-9 snoop *did* capture primary-service-discovery frames — we just
hadn't queried them. `tshark -Y 'btatt.opcode==0x11'` on the existing
`btsnoop_hci.log` showed two `Read By Group Type Response` frames (367, 372)
listing all services on the SR16:

| Service (16-bit) | Handle range | Owned chars |
|---|---|---|
| 0x1800 (GAP) | 0x0001..0x0007 | (standard) |
| 0x1801 (GATT) | 0x0008..0x000B | (standard) |
| 0x1812 (HID / Hogp) | 0x000C..0x0033 | (HID) |
| 0xFF00 | 0x0034..0x003B | FF01/FF02/FF03 |
| **0xA00A** | **0x003C..0x0041** | **0x003E (0xB002, R+W+WNR+N), 0x0040 (0xB003, R+N)** |
| 0x0BC0 | 0x0042..0xFFFF | 0BC1/0BC2 |

Full 128-bit form (SIG base-UUID alias scheme):

```
0000a00a-0000-1000-8000-00805f9b34fb   # service
0000b002-0000-1000-8000-00805f9b34fb   # TX (phone -> ring, handle 0x003E)
0000b003-0000-1000-8000-00805f9b34fb   # RX (ring  -> phone, handle 0x0040)
```

This matches what session 6.5's GATT enumerate already observed (`services:
3 — A00A, 180D, FF00` — except session 6 missed 0x1812/0x0BC0 because those
are visible post-connect only). The 0x1812 / 0x0BC0 pair weren't on session 6.5's
radar because the script returned services before full primary-discovery completed.

**Path C is now unblocked.** `connect_pull.py` will run against the live ring
without `--force`.

## What we shipped this session

| File | Status | Purpose |
|---|---|---|
| `src/sr16_bridge/protocol.py` | patched | `UART_SERVICE_UUID`, `UART_TX_CHAR_UUID`, `UART_RX_CHAR_UUID` set to real values; new `UUID_KNOWN = True` exported |
| `src/sr16_bridge/connect_pull.py` | patched | imports protocol-level `UUID_KNOWN`; placeholder string check is now an AND-gate |
| `tests/test_protocol_0ab.py` | patched | +2 tests pinning the UUIDs and SIG base format |
| `HANDOFF-session-12.md` | new | This file |

Total: +30 lines, 0 new deps.

## How we resolved Path B

```
# Extract the snoop from the bugreport zip (cached at ~/health/sr16_captures/sr16_cap_20260708_130025.zip)
unzip -p ~/health/sr16_captures/sr16_cap_20260708_130025.zip '*/FS/data/log/bt/btsnoop_hci.log' > /tmp/snoop.log

# Find ATT service-discovery (opcode 0x11 = Read By Group Type Response)
tshark -r /tmp/snoop.log -Y 'btatt.opcode==0x11' -V
# → Frame 367: services on 0x0001..0x0033 (GAP, GATT, HID)
# → Frame 372: services on 0x0034..0xFFFF (FF00, A00A, 0BC0)

# Find char declarations (opcode 0x09 = Read By Type Response)
tshark -r /tmp/snoop.log -Y 'btatt.opcode==0x09' -V | grep -A6 'Handle: 0x00(3c|3d|3e|3f|40|41)'
# → handle 0x003E declares char UUID 0xB002, props 0x1E (R+W+WNR+N)
# → handle 0x0040 declares char UUID 0xB003, props 0x12 (R+N)
```

The snoop was captured DURING a sync, not at its start, but ATT primary-
service-discovery (which is part of every fresh connection handshake) ran
as soon as the connection came up — so the discovery frames survived.

## Key takeaway (saving it for the skill)

**Always query the snoop for ATT primary-service-discovery frames before
declaring "we need a fresh capture." tshark's `-Y 'btatt.opcode==0x11'` is
the forgot-to-check filter; it surfaces `Read By Group Type Response` packets
which carry the 128-bit UUID → handle-range map for every GATT service on
the peripheral.**

## Test results

**29/29 tests pass** (was 27 — added 2 UUID pin tests):

```
test_uart_service_uuid_is_real PASSED     # pins the exact UUID strings
test_uart_uuids_match_bt_sig_base PASSED  # pins the SIG base-UUID format
... 27 prior tests pass
```

The new tests will fail loudly if anyone rotates the UUIDs back to
placeholders or fat-fingers a digit.

## What's open

Same list as session 11, minus the UUID blocker:

- ❌ **Live ring pull end-to-end (Path C)** — scaffold ready with real UUIDs; needs macOS-side HID/sleep gauntlet cleared first
- ❌ 0xA3 6×u16 metric semantics (needs corroborating capture during known activity)
- ❌ 0x67 byte grid value semantics (off-finger vs charging vs other)
- ❌ Port `hr_live.py` from 0x180D to 0xAB (unlocks aggregated metrics)
- ❌ Launchd plist for `hr_live` daemon
- ❌ DB schema for non-BPM metrics (steps, cal, dist per hour)
- ❌ `history_sync.py` rewrite — the old Colmi R02 version is still in
  the tree. Marked for deletion once `connect_pull` validates against the live ring.
- ❌ Known-activity capture (5-min walk/run) before next sync to disambiguate 0xA3 metrics

## Pitfalls captured this session

### P41. The session-9 snoop already contained primary-service-discovery (2026-07-09)

Symptom: Assumed the snoop didn't capture GATT service-discovery because it
landed in the middle of a sync. The very first thing a fresh BLE connection
does is run primary-service-discovery, so those frames live at the start
of the snoop regardless of what triggered the connection.

Fix: `tshark -r <log> -Y 'btatt.opcode==0x11' -V` always. Pair with
`btatt.opcode==0x09` for char declarations and `0x0a` for char value reads.
Together those three filters cover the entire GATT discovery surface.

Don't repeat the 7-session rathole of assuming you need a fresh capture
when one already has what you need — try the three discovery filters first.

## Recommended next session

The cheapest productive path: run `connect_pull.py --scan 30` against
the live ring and see what notifications come back. Even if the Mac-side
sleep blocker bites first (it will — P1, P3 in sr16-ring-mac-pitfalls
skill), we'll learn whether the protocol layer's packet builders are
correct before we invest more in the Mac-side glue. If the sleep blocker
hits, the second move is the "kill RWfit + cold-start" capture for the
0xA3 metric semantics — same snoop workflow, captures during a known-
activity window (operator does a 5-min run, then triggers sync).

</content>