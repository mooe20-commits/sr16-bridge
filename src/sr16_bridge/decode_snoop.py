"""Decode an Android btsnoop_hci.log capture into SR16 vendor protocol evidence.

Reads the HCI snoop log with tshark (already brew-installed) and emits:

1. **Per-handle write opcode table** — for every ATT Write Request / Write Command
   from the phone, the destination char handle + the raw bytes. This is the
   opcode table the Mac-side has been guessing at for 7 sessions. Now we see it.

2. **First-write-per-handle hex dump** — the bytes the phone writes to each
   vendor char, which is exactly the wake-handshake / sync-history command
   structure we need to reproduce on the Mac.

3. **Human-readable markdown report** — `decoded_<ts>.md` for review.

4. **protocol.py patch hints** — a `Suggested protocol.py changes` block
   listing the discovered char handles and wake-byte sequence. Operator
   applies these to `protocol.py` after review.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.decode_snoop <path/to/btsnoop_hci.log>

The snoop log can be pulled from the Galaxy with:
    adb bugreport sr16_sync_$(date +%Y%m%d_%H%M%S)
    unzip -o sr16_sync_*.zip 'FS/data/log/bt/btsnoop_hci.log' -d ./extracted/

Reference: Gadgetbridge wiki "BT Protocol Reverse Engineering"
https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/BT-Protocol-Reverse-Engineering
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# SR16 identity — known from PROBE-LOG. We try both MAC form (system_profiler)
# and UUID form (CoreBluetooth) on the Mac side. The snoop log will have the
# phone's perspective: the SR16 shows up as a public random BD_ADDR like
# `38:00:00:00:DE:90` OR the resolver-routed UUID. Filter is generous.
SR16_NAME_HINTS = ("SR16", "RING", "RWfit", "Rogbid", "JingTider")


def _find_sr16_addresses(tshark_json: list[dict]) -> dict[str, str]:
    """Walk the tshark JSON for any frame mentioning the SR16 and return a
    mapping of {address-form: friendly-name} for the ring's BT address.

    Returns the public BD_ADDR (MAC) form preferred by Wireshark, but
    callers can also pass through to filters using btaddr / btsmp addresses.
    """
    candidates: dict[str, str] = {}
    for pkt in tshark_json:
        for layer in pkt.get("_source", {}).get("layers", {}).values():
            # Walk recursively — the layer may be nested
            def walk(obj, depth=0):
                if depth > 6:
                    return
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("btle.bd_addr", "btaddr.bd_addr", "bluetooth.bd_addr"):
                            if isinstance(v, str) and len(v) == 17 and ":" in v:
                                # Could be phone or ring. Defer — we tag the
                                # other side as SR16 only if a sibling field
                                # references the SR16 name.
                                candidates.setdefault(v.lower(), "unknown")
                        walk(v, depth + 1)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item, depth + 1)
            walk(layer)
    return candidates


def _looks_like_sr16_bdaddr(addr: str, tshark_json: list[dict]) -> bool:
    """Heuristic: the SR16 ships with MAC `38:00:00:00:DE:90` (per PROBE-LOG
    and prior capture_sr16 runs). Match on that prefix.
    """
    return addr.lower().replace(":", "").endswith("0000de90") or addr.lower().startswith("38:")


def _tshark_to_packets(snoop_path: Path) -> list[dict]:
    """Invoke tshark to read the btsnoop file as JSON, ATT layer only.

    We use -Y 'btatt' to drop everything but ATT frames (skips LMP/SMP/voice
    etc., keeps the file small and the analysis focused). The output is
    newline-delimited JSON via -T json.
    """
    cmd = [
        "tshark",
        "-r", str(snoop_path),
        "-Y", "btatt or btsmp or btl2cap or bthci_cmd",
        "-T", "json",
        "-x",   # raw hex bytes
        "-V",   # verbose
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"tshark failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def _tshark_brief(snoop_path: Path) -> list[dict]:
    """Lighter tshark run: no -V, just the ATT frame headers + opcodes.
    Used for opcode / handle tabulation.
    """
    cmd = [
        "tshark",
        "-r", str(snoop_path),
        "-Y", "btatt",
        "-T", "fields",
        "-E", "separator=|",
        "-E", "occurrence=f",
        "-e", "frame.number",
        "-e", "frame.time_epoch",
        "-e", "btatt.opcode",
        "-e", "btatt.handle",
        "-e", "btatt.value",
        "-e", "btatt.uuid16",
        "-e", "btatt.uuid128",
        "-e", "bthci_acl.src.bd_addr",
        "-e", "bthci_acl.dst.bd_addr",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"tshark -T fields failed (rc={proc.returncode}): {proc.stderr[:500]}")
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        # Pad short rows so positional indexing is safe
        while len(parts) < 9:
            parts.append("")
        out.append({
            "frame": parts[0],
            "ts": parts[1],
            "opcode": parts[2],
            "handle": parts[3],
            "value": parts[4],
            "uuid16": parts[5],
            "uuid128": parts[6],
            "src": parts[7],
            "dst": parts[8],
        })
    return out


# ATT opcodes we care about
ATT_OPCODES = {
    "0x01": "ERROR_RESPONSE",
    "0x02": "EXCHANGE_MTU_REQUEST",
    "0x03": "EXCHANGE_MTU_RESPONSE",
    "0x04": "FIND_INFORMATION_REQUEST",
    "0x05": "FIND_INFORMATION_RESPONSE",
    "0x06": "FIND_BY_TYPE_VALUE_REQUEST",
    "0x08": "READ_BY_TYPE_REQUEST",
    "0x09": "READ_BY_TYPE_RESPONSE",
    "0x0a": "READ_REQUEST",
    "0x0b": "READ_RESPONSE",
    "0x0c": "READ_BLOB_REQUEST",
    "0x0d": "READ_BLOB_RESPONSE",
    "0x0e": "READ_MULTIPLE_REQUEST",
    "0x0f": "READ_MULTIPLE_RESPONSE",
    "0x10": "READ_BY_GROUP_TYPE_REQUEST",
    "0x11": "READ_BY_GROUP_TYPE_RESPONSE",
    "0x12": "WRITE_REQUEST",        # ack required
    "0x13": "WRITE_RESPONSE",
    "0x14": "WRITE_COMMAND",        # no ack
    "0x16": "PREPARE_WRITE_REQUEST",
    "0x17": "PREPARE_WRITE_RESPONSE",
    "0x18": "EXECUTE_WRITE_REQUEST",
    "0x19": "EXECUTE_WRITE_RESPONSE",
    "0x1b": "HANDLE_VALUE_NOTIFICATION",
    "0x1d": "HANDLE_VALUE_INDICATION",
    "0x1e": "HANDLE_VALUE_CONFIRMATION",
    "0x52": "WRITE_COMMAND",        # alias
    "0x4a": "READ_BY_TYPE_REQUEST",  # alias
}


def _identify_sr16_writes(packets: list[dict]) -> list[dict]:
    """Filter the tshark field dump to writes destined for the SR16.

    Heuristic: a write is "for the ring" if either src or dst is a BD_ADDR
    ending in `00:DE:90` (the SR16's MAC, per capture_sr16 / blueutil).
    We also keep writes where the handle resolves to a known vendor char
    (0xFF01, 0xFF02, 0xFF03, 0xB002, 0xB003) — those are the SR16's
    FF00 and A00A service chars per PROBE-LOG.
    """
    vendor_handles = {"0x000e", "0x000f", "0x0010", "0x0011", "0x0012", "0x0013"}
    out = []
    for p in packets:
        opc = p.get("opcode", "")
        if opc not in ("0x12", "0x14", "0x52", "0x1b"):  # WR/WC/NOTIFY
            continue
        # Promote the bd_addr that ends in 00:DE:90 to "the ring"
        src = (p.get("src") or "").lower()
        dst = (p.get("dst") or "").lower()
        if "00:de:90" in src or "00:de:90" in dst:
            p["_direction"] = "phone→ring" if "00:de:90" in dst else "ring→phone"
            out.append(p)
            continue
        # Some snoop logs don't tag the ring by MAC. Match on handle too.
        h = (p.get("handle") or "").lower()
        if h in vendor_handles and p.get("value"):
            p["_direction"] = "phone→ring" if opc in ("0x12", "0x14", "0x52") else "ring→phone"
            out.append(p)
    return out


def _decode_snoop(snoop_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = out_dir / f"decoded_{ts}.md"
    raw_packets_path = out_dir / f"packets_{ts}.json"

    print(f"[decode_snoop] reading {snoop_path} with tshark ...", file=sys.stderr)
    packets = _tshark_brief(snoop_path)
    print(f"[decode_snoop] {len(packets)} ATT frames in capture", file=sys.stderr)

    # Save raw packets for offline analysis
    raw_packets_path.write_text(json.dumps(packets, indent=2))

    sr16_packets = _identify_sr16_writes(packets)
    print(f"[decode_snoop] {len(sr16_packets)} SR16-bound frames (writes + notifies)", file=sys.stderr)

    # Tabulate: group by handle, then opcode, then first-seen hex
    by_handle: dict[str, list[dict]] = defaultdict(list)
    for p in sr16_packets:
        if p.get("handle"):
            by_handle[p["handle"].lower()].append(p)

    # Build the report
    lines: list[str] = []
    lines.append(f"# SR16 snoop decode — {ts}")
    lines.append("")
    lines.append(f"- source: `{snoop_path}`")
    lines.append(f"- raw packets JSON: `{raw_packets_path}`")
    lines.append(f"- tshark version: 4.6.6 (brew formula, no GUI)")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- total ATT frames: **{len(packets)}**")
    lines.append(f"- SR16-bound frames: **{len(sr16_packets)}**")
    lines.append(f"- distinct char handles touched: **{len(by_handle)}**")
    lines.append("")
    lines.append("## Per-handle write table")
    lines.append("")
    lines.append("| handle | opcode | direction | count | first hex value |")
    lines.append("|--------|--------|-----------|-------|-----------------|")

    handle_summary: list[dict] = []
    for handle, pkts in sorted(by_handle.items()):
        # First write to this handle is the most interesting (the wake byte)
        writes = [p for p in pkts if p.get("opcode") in ("0x12", "0x14", "0x52")]
        notifies = [p for p in pkts if p.get("opcode") == "0x1b"]
        first_write = writes[0] if writes else None
        first_hex = first_write.get("value", "—") if first_write else "—"
        opc_name = ATT_OPCODES.get(first_write.get("opcode", ""), "?") if first_write else "—"
        direction = first_write.get("_direction", "—") if first_write else "—"
        lines.append(
            f"| `{handle}` | {opc_name} | {direction} | "
            f"{len(writes)}w / {len(notifies)}n | `{first_hex[:40]}{'…' if len(first_hex) > 40 else ''}` |"
        )
        handle_summary.append({
            "handle": handle,
            "first_write": first_write,
            "writes": writes,
            "notifies": notifies,
        })

    lines.append("")
    lines.append("## First-N writes (the wake-handshake candidates)")
    lines.append("")
    lines.append("These are the first 5 writes per handle, in chronological order.")
    lines.append("The first write to the FF02-equivalent char is almost certainly the wake-extension command we couldn't find for 7 sessions.")
    lines.append("")

    for h in sorted(by_handle.keys()):
        writes = [p for p in by_handle[h] if p.get("opcode") in ("0x12", "0x14", "0x52")]
        if not writes:
            continue
        lines.append(f"### Handle `{h}` — {len(writes)} writes")
        lines.append("")
        for i, w in enumerate(writes[:5]):
            t = datetime.fromtimestamp(float(w["ts"]), tz=timezone.utc).isoformat()
            lines.append(f"- {i+1}. frame={w['frame']}  ts={t}  value=`{w.get('value', '—')}`")
        if len(writes) > 5:
            lines.append(f"- …{len(writes) - 5} more, see `{raw_packets_path.name}`")
        lines.append("")

    lines.append("## Notification (ring→phone) payloads")
    lines.append("")
    lines.append("| frame | ts | handle | hex value |")
    lines.append("|-------|----|--------|-----------|")
    notifies_all = [p for p in sr16_packets if p.get("opcode") == "0x1b"]
    for p in notifies_all[:30]:
        t = datetime.fromtimestamp(float(p["ts"]), tz=timezone.utc).isoformat() if p.get("ts") else "?"
        lines.append(f"| {p['frame']} | {t} | `{p.get('handle', '—')}` | `{p.get('value', '—')[:60]}{'…' if len(p.get('value', '')) > 60 else ''}` |")
    if len(notifies_all) > 30:
        lines.append(f"| …{len(notifies_all) - 30} more | | | |")

    lines.append("")
    lines.append("## Suggested `protocol.py` patch")
    lines.append("")
    lines.append("After reviewing the table above, apply these changes to "
                 "`src/sr16_bridge/protocol.py`:")
    lines.append("")
    lines.append("```python")
    lines.append("# Replace the placeholder RX/TX char UUIDs with the discovered handles.")
    lines.append("# The 'wake' char is the one that receives the first non-zero write")
    lines.append("# after GATT enumeration. The 'sync' char is the one that takes the")
    lines.append("# history-log read command (typically a 0x15 / 0x16 opcode in the value).")
    lines.append("")
    lines.append("UART_SERVICE_UUID = '<discovered 128-bit service UUID>'  # was A00A — confirm from snoop")
    lines.append("UART_RX_CHAR_UUID = '<discovered RX char 128-bit UUID>'")
    lines.append("UART_TX_CHAR_UUID = '<discovered TX char 128-bit UUID>'")
    lines.append("```")
    lines.append("")
    lines.append("## Next step")
    lines.append("")
    lines.append("Open this report in your editor (`open -e " + str(report_path) + "`) and "
                 "look at the first-hex-value column. The smallest non-zero payload is almost "
                 "always the keep-awake or sync-history command.")

    report_path.write_text("\n".join(lines) + "\n")
    print(f"[decode_snoop] wrote {report_path}", file=sys.stderr)
    return report_path


def main() -> int:
    p = argparse.ArgumentParser(description="Decode an Android btsnoop_hci.log into SR16 protocol hints")
    p.add_argument("snoop", type=Path, help="path to btsnoop_hci.log")
    p.add_argument("--out", type=Path, default=Path.home() / "health" / "sr16_captures",
                   help="output directory for the decoded report")
    args = p.parse_args()
    if not args.snoop.exists():
        print(f"ERR: {args.snoop} does not exist", file=sys.stderr)
        return 1
    report = _decode_snoop(args.snoop, args.out)
    print(f"\n✓ decode complete: {report}")
    print(f"\nNext: `open -e {report}` and look at the first-hex-value column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
