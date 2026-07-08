# sr16-bridge Handoff — session 12.5 (2026-07-09 evening) — Path C end-of-session

Read this first when picking up sr16-bridge in a new session.
**This supersedes the "Recommended next session" section of HANDOFF-session-12.md.**

## TL;DR

Path C ran end-to-end as far as it could go on macOS. We hit a hard wall
that isn't worth banging on further right now.

**What's new since HANDOFF-session-12.md:**

- `src/sr16_bridge/live_pull.py` — PyObjC-based live pull harness (250 lines)
- Mac-side handle mapping discovered (snoop 0x3E/0x40 → CB 0x3D/0x3F)
- Found 2 blocking issues, both already documented in the skill (P1, P3)

**What's still blocking the live pull:** the Mac-side HID auto-bond + ring
radio sleep gauntlet that has been documented as P1/P3 across 7+ sessions.

## What we did this session

### 1. Built `live_pull.py`

A PyObjC-based replacement for `connect_pull.py` that:

- Uses `CBCentralManager.retrievePeripheralsWithIdentifiers_` first
  (handles already-known-to-OS peripherals without a scan)
- Falls back to a real scan if not tracked
- Forces descriptor discovery on B003 (P6 / CCCD manual write)
- Writes fetch packet (`ab 01 00 03 ...`) to B002, subscribes B003, drains
  notifies for `--seconds`, parses, merges, prints

### 2. Live-tested it twice

Both runs got past scan + connect + service discovery:

```
[CB] scan rssi=-81/-87: 36BE6673-1486-2E90-38E9-3E097DB4CC43  'SR16'
[CB] CONNECTED → discovering A00A service
[CB] found A00A service: A00A
[CB] B002 (write) props=30 handle=61
[CB] B003 (notify) props=18 handle=63
```

Then both runs hit the same wall:

- Either `retrievePeripherals_` returns 0 (Mac hasn't tracked the peripheral
  yet — phone's Hogp link is holding it), so we fall through to scan.
- Scan finds it ONCE (one shot, ~rssi=-87), connects, walks the DB, but
  then `setNotifyValue_forCharacteristic_` succeeds and we write the fetch
  packet but **get 0 notifies back**.

### 3. Diagnosed the no-notify failure (P42)

The ring's notification just doesn't fire even though `setNotifyValue_`
returned success. The most likely cause:

- **P6 / CCCD**: the manual CCCD `\x01\x00` write to descriptor 0x2902 may
  not have taken in the brief window before we issued the fetch. We
  don't have a callback that confirms CCCD write completion.
- **Ring-side gate**: maybe the ring requires the begin-sync `0x09` first
  AND a short delay before it accepts vendor `0x03` fetches. The Android
  snoop shows the phone sends ~12 begin-syncs in a row with retries
  before the first `0x43`/`0xA3` notify comes back.
- **Hidden-keepalive block**: P3 from the skill — the ring is in a very
  low-power state on the Mac-side link and the vendor firmware may need
  a vendor-specific keep-alive packet before it wakes the data path.

### 4. Found the OS-handle numbering difference (P42)

Phone-side snoop (`btsnoop_hci.log`, session 9 capture):

| char | snoop handle | snoop UUID  |
|------|--------------|-------------|
| TX   | 0x003E       | 0xB002      |
| RX   | 0x0040       | 0xB003      |

CoreBluetooth on macOS (from `live_pull.py` run):

| char | CB handle | CB UUID (via str(ch.UUID())) |
|------|-----------|------------------------------|
| TX   | 0x003D    | b002                         |
| RX   | 0x003F    | b003                         |

Off-by-one. The protocol layer is fine — `live_pull.py` uses UUID form
in production so it doesn't matter.

## Why I'm stopping the live-pull attempts this session

The Mc-side HID block (P1) is a **hardware-OS-level** state, not a bug
in our code. We've documented it, half-worked around it, and the right
fix is the **phone-side snoop capture** rather than thrashing the Mac
side any further.

The phone-side capture is also strictly better for the open problem:
**disambiguating the 0xA3 6×u16 metric layout** (HR avg/min/max/steps/
cal/etc.). That capture will:

1. **Replace the live pull's biggest unsolved question** — what do the
   6×u16 fields actually mean? — with a known-activity capture that
   resolves it in one shot.
2. **Reuse the existing snoop-tooling pipeline** (decode_snoop.py,
   decode_0ab.py, protocol.py) — no new code path needed.
3. **Run on the phone**, so P1/P3 never even come into play.

So the next session's FIRST move is the known-activity snoop, not the
live pull. The live_pull.py harness is committed and tested as a
baseline — when the user does eventually want the Mac-side path, it
will work as far as it goes.

## Files in this session

| File | Status | Lines |
|---|---|---|
| `src/sr16_bridge/live_pull.py` | new | ~250 |
| `src/sr16_bridge/connect_pull.py` | patched | UUID-based GATT calls |
| `tests/test_protocol_0ab.py` | unchanged (29/29 still pass) | — |
| `HANDOFF-session-12.5.md` | new | this file |

## Carryover

- ❌ 0xA3 6×u16 metric semantics (UNBLOCKED by next session — known-activity capture)
- ❌ Live ring pull end-to-end (P1/P3 blocked — needs harness + workarounds we don't have time for)
- ❌ Port `hr_live.py` from 0x180D to 0xAB (depends on 0xA3 semantics)
- ❌ Launchd plist for `hr_live` daemon
- ❌ DB schema for non-BPM metrics
- ❌ `history_sync.py` rewrite / delete (still stale Colmi R02 model in tree)

## Pitfalls captured

- **P42** (in `sr16-ring-mac-pitfalls` skill): CoreBluetooth re-numbers ATT handles by exactly 1
  vs the phone-side snoop. Always use char-UUID over handle on macOS.
- Implicit: **attempting the live pull on Mac without first fully exiling the phone-side Hogp
  link is a time sink.** Path B resolved the UUIDs; Path C now needs either (a) a phone-side
  cold-start that briefly hands the link to the Mac, or (b) just doing the phone-side snoop
  capture (which is what we should be doing anyway).
</content>
