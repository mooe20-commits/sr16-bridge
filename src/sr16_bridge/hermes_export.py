"""sr16-bridge hermes_export: render a daily HR summary into Obsidian.

Reads hr_readings + analysis_runs for a given local calendar day and writes
a single Markdown note to ~/Documents/Obsidian Vault/Health/<YYYY-MM-DD>.md.

Idempotent: re-running for the same day overwrites the note in place. The
note carries a YAML frontmatter (created/updated) so Obsidian's graph view
will treat each day as a node connected by tags.

Usage:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hermes_export --day 2026-07-07
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hermes_export --yesterday
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hermes_export --day 2026-07-07 --dry-run

Designed to be cron-able. Lives at:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hermes_export --yesterday

Acceptance test:
    cd ~/projects/sr16-bridge
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.hermes_export --day 2026-07-07
    cat ~/Documents/Obsidian\ Vault/Health/2026-07-07.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / "health" / "sr16.db"
VAULT = Path.home() / "Documents" / "Obsidian Vault"
OUT_DIR = VAULT / "Health"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# Per obsidian-vault-tagging-conventions: tags name SUBJECTS, not structure.
# health / heart-rate / hrv / sleep are real subjects that connect this
# note to others on the same topics in the graph view.
DEFAULT_TAGS = ["health", "heart-rate", "smart-ring"]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


# ---------- DB queries ----------

def fetch_hr_for_day(day: str) -> list[sqlite3.Row]:
    """All hr_readings whose date(ts_utc) == day. Sorted ASC."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """SELECT ts_utc, bpm, rr_intervals, sensor_contact, source
           FROM hr_readings
           WHERE date(ts_utc) = ?
           ORDER BY ts_utc ASC""",
        (day,),
    ))
    conn.close()
    return rows


def fetch_analysis_for_day(day: str) -> list[sqlite3.Row]:
    """All analysis_runs whose window_start_ts falls on `day`."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """SELECT id, started_at, rows_analyzed, window_start_ts, window_end_ts,
                  model_id, summary_json, error
           FROM analysis_runs
           WHERE date(window_start_ts) = ?
           ORDER BY window_start_ts ASC""",
        (day,),
    ))
    conn.close()
    return rows


# ---------- Rollups ----------

def bpm_stats(bpms: list[int]) -> dict[str, Any]:
    """Pure stats rollup. Mirrors analyze.compute_stats() but cheaper (no RR parse)."""
    if not bpms:
        return {}
    sorted_bpms = sorted(bpms)
    return {
        "n": len(bpms),
        "min": sorted_bpms[0],
        "max": sorted_bpms[-1],
        "avg": round(sum(bpms) / len(bpms), 1),
        "p50": sorted_bpms[len(sorted_bpms) // 2],
        "p90": sorted_bpms[int(len(sorted_bpms) * 0.90)],
    }


def rr_rmssd(rr_intervals_field: str) -> float | None:
    """RMSSD in ms from a comma-joined rr_intervals string. Same math as analyze.py."""
    if not rr_intervals_field:
        return None
    rr_ms: list[float] = []
    for chunk in rr_intervals_field.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            rr_ms.append(int(chunk) * 1000.0 / 1024.0)
        except ValueError:
            pass
    if len(rr_ms) < 2:
        return None
    diffs_sq = [(rr_ms[i] - rr_ms[i - 1]) ** 2 for i in range(1, len(rr_ms))]
    return round((sum(diffs_sq) / len(diffs_sq)) ** 0.5, 1)


def anomaly_summary(analyses: list[sqlite3.Row]) -> Counter:
    """Tally anomaly classes across the day's analyses."""
    c: Counter = Counter()
    for r in analyses:
        if r["error"]:
            c["error"] += 1
            continue
        if not r["summary_json"]:
            c["unknown"] += 1
            continue
        try:
            summary = json.loads(r["summary_json"])
        except json.JSONDecodeError:
            c["malformed"] += 1
            continue
        c[summary.get("anomaly", "unknown")] += 1
    return c


# ---------- Rendering ----------

def render_note(day: str, hr_rows: list[sqlite3.Row], analyses: list[sqlite3.Row],
               tags: list[str]) -> str:
    """Produce the full markdown body. Frontmatter included."""
    now_iso = datetime.now(timezone.utc).isoformat()
    bpms = [r["bpm"] for r in hr_rows]
    stats = bpm_stats(bpms)
    rmssds = [rr_rmssd(r["rr_intervals"]) for r in hr_rows]
    rmssds_valid = [r for r in rmssds if r is not None]
    rmssd_avg = round(sum(rmssds_valid) / len(rmssds_valid), 1) if rmssds_valid else None

    sources = Counter(r["source"] for r in hr_rows)
    anomalies = anomaly_summary(analyses)

    # Time range — note ts_utc is UTC; render the local wall-clock window if data spans it
    if hr_rows:
        first_ts = hr_rows[0]["ts_utc"]
        last_ts = hr_rows[-1]["ts_utc"]
        first_local = _iso_to_local_hhmm(first_ts)
        last_local = _iso_to_local_hhmm(last_ts)
    else:
        first_local = last_local = "—"

    fm_lines = [
        "---",
        f"created: {now_iso}",
        f"updated: {now_iso}",
        f"day: {day}",
        "type: health-summary",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
        f"# Health — {day}",
        "",
    ]
    body_lines: list[str] = []

    # ---- TL;DR ----
    body_lines.append("## TL;DR")
    body_lines.append("")
    if not hr_rows:
        body_lines.append("No heart-rate data on this day.")
        body_lines.append("")
    else:
        notes = []
        if stats:
            notes.append(f"**{stats['n']} samples**, "
                         f"avg **{stats['avg']} bpm**, "
                         f"range {stats['min']}–{stats['max']} bpm")
        if rmssd_avg is not None:
            notes.append(f"RMSSD avg **{rmssd_avg} ms**")
        if anomalies:
            top = ", ".join(f"{k} ×{v}" for k, v in anomalies.most_common())
            notes.append(f"analyses: {top}")
        body_lines.append(" · ".join(notes))
        body_lines.append("")

    # ---- HR rollup ----
    body_lines.append("## Heart rate")
    body_lines.append("")
    if stats:
        body_lines.append("| metric | value |")
        body_lines.append("|---|---|")
        body_lines.append(f"| samples | {stats['n']} |")
        body_lines.append(f"| min | {stats['min']} bpm |")
        body_lines.append(f"| avg | {stats['avg']} bpm |")
        body_lines.append(f"| p50 (median) | {stats['p50']} bpm |")
        body_lines.append(f"| p90 | {stats['p90']} bpm |")
        body_lines.append(f"| max | {stats['max']} bpm |")
        if rmssd_avg is not None:
            body_lines.append(f"| RMSSD (avg) | {rmssd_avg} ms |")
        body_lines.append(f"| coverage | {first_local} → {last_local} local |")
        body_lines.append(f"| sources | {', '.join(f'{k} ×{v}' for k, v in sources.items())} |")
        body_lines.append("")

    # ---- Hourly histogram (cheap ASCII) ----
    if hr_rows:
        body_lines.append("## Hourly distribution")
        body_lines.append("")
        buckets = _hourly_histogram(hr_rows)
        body_lines.append("```")
        body_lines.append("hour  count  hist")
        for h in range(24):
            n = buckets[h]
            bar = "█" * min(n, 60)
            body_lines.append(f"{h:02d}    {n:>4}  {bar}")
        body_lines.append("```")
        body_lines.append("")

    # ---- Anomaly rollup ----
    if analyses:
        body_lines.append("## Analyzer verdicts")
        body_lines.append("")
        body_lines.append("| id | window | rows | anomaly | note |")
        body_lines.append("|---|---|---|---|---|")
        for a in analyses:
            ws = a["window_start_ts"]
            we = a["window_end_ts"]
            window_label = f"{_iso_to_local_hhmm(ws)}–{_iso_to_local_hhmm(we)}"
            err = a["error"]
            if err:
                body_lines.append(f"| {a['id']} | {window_label} | {a['rows_analyzed']} | ERROR | `{err[:80]}` |")
                continue
            summary = {}
            try:
                summary = json.loads(a["summary_json"] or "{}")
            except json.JSONDecodeError:
                pass
            anomaly = summary.get("anomaly", "unknown")
            note = (summary.get("note") or "").replace("|", "\\|")[:120]
            body_lines.append(f"| {a['id']} | {window_label} | {a['rows_analyzed']} | {anomaly} | {note} |")
        body_lines.append("")

    # ---- Footer ----
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(f"_Generated by sr16-bridge hermes_export · model: "
                      f"see `analysis_runs.model_id` · db: `{DB_PATH}`_")
    body_lines.append("")

    return "\n".join(fm_lines + body_lines)


def _iso_to_local_hhmm(iso_ts: str) -> str:
    """Render an ISO8601 (UTC) timestamp as local HH:MM. Tolerates Z suffix."""
    if iso_ts.endswith("Z"):
        iso_ts = iso_ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts[:16]
    local = dt.astimezone()
    return local.strftime("%H:%M")


def _hourly_histogram(hr_rows: list[sqlite3.Row]) -> list[int]:
    """Bucket hr_readings by local hour-of-day. Returns 24-element list."""
    buckets = [0] * 24
    for r in hr_rows:
        ts = r["ts_utc"]
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        buckets[dt.astimezone().hour] += 1
    return buckets


# ---------- IO ----------

def write_note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# ---------- Main ----------

def export_day(day: str, tags: list[str], dry_run: bool, verbose: bool = True) -> dict[str, Any]:
    """Render + write one day's note. Returns a summary dict."""
    hr_rows = fetch_hr_for_day(day)
    analyses = fetch_analysis_for_day(day)
    body = render_note(day, hr_rows, analyses, tags)
    out_path = OUT_DIR / f"{day}.md"
    if not dry_run:
        write_note(out_path, body)
    if verbose:
        print(f"  day={day}  hr_rows={len(hr_rows)}  analyses={len(analyses)}  → {out_path}"
              + ("  [DRY RUN]" if dry_run else ""))
    return {
        "day": day,
        "hr_rows": len(hr_rows),
        "analyses": len(analyses),
        "path": str(out_path),
        "wrote": not dry_run,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="sr16-bridge gateway: render Obsidian daily HR notes")
    p.add_argument("--day", help="ISO date to export (YYYY-MM-DD). Mutually exclusive with --yesterday.")
    p.add_argument("--yesterday", action="store_true",
                   help="export yesterday's local date (for nightly cron)")
    p.add_argument("--tags", default=",".join(DEFAULT_TAGS),
                   help=f"comma-joined tags (default: {','.join(DEFAULT_TAGS)})")
    p.add_argument("--dry-run", action="store_true", help="render but don't write")
    p.add_argument("--quiet", action="store_true", help="suppress per-day output")
    args = p.parse_args()

    init_db()

    if args.yesterday:
        target_day = (date.today() - timedelta(days=1)).isoformat()
    elif args.day:
        target_day = args.day
    else:
        print("must specify --day YYYY-MM-DD or --yesterday", file=sys.stderr)
        return 2

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    result = export_day(target_day, tags=tags, dry_run=args.dry_run, verbose=not args.quiet)
    return 0 if result["wrote"] or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())