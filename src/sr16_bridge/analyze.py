"""sr16-bridge analyze: local-AI analysis of accumulated HR readings.

Reads unanalyzed hr_readings rows, batches them into rolling windows, calls a local
Ollama model (Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC per USER profile), and writes
one row to analysis_runs per window. Idempotent — re-running on the same DB picks
up only newly-ingested rows.

Sources of HR rows (any of these feed into the same DB):
- `hr_live.py`                    real-time BLE notification stream
- `history_sync.py`               vendor 0xA00A bulk dump (session-2 work — RE needed)
- test/synthetic inserts          for acceptance testing without a live ring

Designed to be cron-able. Lives at:
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.analyze [--window-min 5] [--dry-run]

Acceptance test:
    cd ~/projects/sr16-bridge
    sqlite3 ~/health/sr16.db "INSERT INTO hr_readings(ts_utc,device_uuid,bpm,rr_intervals,sensor_contact) VALUES (datetime('now'),'test',72,'800,810',2),(datetime('now','+1 second'),'test',75,'',2)"
    PYTHONPATH=src .venv/bin/python -m sr16_bridge.analyze --window-min 5
    sqlite3 ~/health/sr16.db "SELECT id, rows_analyzed, summary_json FROM analysis_runs ORDER BY id DESC LIMIT 1;"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROBE_LOG = Path(__file__).resolve().parents[2] / "sys" / "PROBE-LOG.md"

# Per USER profile (memory): Twitter-style Heretic model is pinned; never substitute
DEFAULT_MODEL = "Qwen3.5-9B-Claude-4.6-HighIQ-HERETIC:latest"
OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_WINDOW_MIN = 5   # one analysis pass covers 5 minutes of HR data


def _iso_to_dt(s: str) -> datetime:
    """Parse ISO8601 (we always emit timezone-aware UTC). Tolerates Z suffix."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ---------- DB ----------

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Idempotent schema bootstrap: enable keys, create tables. ALTERs are guarded by PRAGMA.
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate() -> None:
    """Apply migrations that aren't safe to leave in schema.sql because CREATE TABLE IF NOT EXISTS
    is a no-op on existing tables. Idempotent — re-running on an already-migrated DB is fine."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _has_column(conn, "hr_readings", "analyzed_at"):
        conn.execute("ALTER TABLE hr_readings ADD COLUMN analyzed_at TEXT")
        conn.commit()
    conn.close()


def fetch_unanalyzed(device_uuid: str | None = None) -> list[sqlite3.Row]:
    """All hr_readings rows where analyzed_at IS NULL, oldest first.

    Optionally constrained to a single device_uuid.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if device_uuid:
        rows = list(conn.execute(
            "SELECT * FROM hr_readings WHERE analyzed_at IS NULL AND device_uuid = ? ORDER BY ts_utc ASC",
            (device_uuid,),
        ))
    else:
        rows = list(conn.execute(
            "SELECT * FROM hr_readings WHERE analyzed_at IS NULL ORDER BY ts_utc ASC",
        ))
    conn.close()
    return rows


def batch_into_windows(rows: list[sqlite3.Row], window_seconds: int) -> list[list[sqlite3.Row]]:
    """Group rows into adjacent windows of <=window_seconds. Window boundaries ride on row timestamps.

    Each input row carries ts_utc ISO8601. We start the first window at the first row's ts, and any
    subsequent row whose ts is within window_seconds of the window-start goes into the same bucket.
    """
    if not rows:
        return []
    windows: list[list[sqlite3.Row]] = []
    cur: list[sqlite3.Row] = []
    cur_start = _iso_to_dt(rows[0]["ts_utc"])
    for r in rows:
        ts = _iso_to_dt(r["ts_utc"])
        if not cur:
            cur = [r]
            cur_start = ts
        elif (ts - cur_start).total_seconds() <= window_seconds:
            cur.append(r)
        else:
            windows.append(cur)
            cur = [r]
            cur_start = ts
    if cur:
        windows.append(cur)
    return windows


def mark_rows_analyzed(row_ids: list[int], when_iso: str) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"UPDATE hr_readings SET analyzed_at = ? WHERE id IN ({placeholders})",
        [when_iso, *row_ids],
    )
    conn.commit()
    conn.close()


def write_analysis_run(started_at: str, finished_at: str | None, rows_analyzed: int,
                       window_start: str, window_end: str, model_id: str,
                       prompt: str, response: str | None,
                       summary_json: str | None, error: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO analysis_runs
              (started_at, finished_at, rows_analyzed, window_start_ts, window_end_ts,
               model_id, prompt, response, summary_json, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (started_at, finished_at, rows_analyzed, window_start, window_end,
         model_id, prompt, response, summary_json, error),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def set_kv(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO gateway_state(key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_kv(key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM gateway_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------- HR math ----------

def compute_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Pure-stats rollup of an HR window. Used to build the prompt AND to validate the LLM response."""
    bpms = [r["bpm"] for r in rows if r["bpm"] is not None]
    if not bpms:
        return {"n": 0}
    bpms_sorted = sorted(bpms)
    resting = bpms_sorted[0]                       # cheap proxy: minimum = resting
    # SDNN (ms): stdev of RR intervals (in seconds, then *1000)
    rr_ms: list[float] = []
    for r in rows:
        rr = r["rr_intervals"] or ""
        for chunk in rr.split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    rr_ms.append(int(chunk) * 1000.0 / 1024.0)
                except ValueError:
                    pass
    n = len(bpms)
    avg = sum(bpms) / n
    peak = max(bpms)
    stats = {
        "n": n,
        "avg_bpm": round(avg, 1),
        "peak_bpm": peak,
        "min_bpm": bpms_sorted[0],
        "resting_bpm_estimate": resting,
        "delta_peak_resting": peak - resting,
    }
    if len(rr_ms) >= 2:
        mean = sum(rr_ms) / len(rr_ms)
        var = sum((x - mean) ** 2 for x in rr_ms) / (len(rr_ms) - 1)
        sdnn = var ** 0.5
        # RMSSD: root mean square of successive differences
        diffs_sq = [(rr_ms[i] - rr_ms[i - 1]) ** 2 for i in range(1, len(rr_ms))]
        rmssd = (sum(diffs_sq) / len(diffs_sq)) ** 0.5 if diffs_sq else 0.0
        stats["sdnn_ms"] = round(sdnn, 1)
        stats["rmssd_ms"] = round(rmssd, 1)
        stats["rr_count"] = len(rr_ms)
    return stats


# ---------- Prompt + Ollama ----------

PROMPT_TEMPLATE = """You are a concise health-data analyst reviewing a window of heart-rate (HR) data from a smart ring.

Window: {n} samples covering {window_start} → {window_end} ({duration_min} min)
Device: {device_uuid}

Computed stats (these are ground truth, trust them):
{stats_json}

Return ONLY valid JSON matching this schema — no prose, no markdown fences:
{{
  "avg_bpm":         <number>,
  "peak_bpm":        <number>,
  "resting_bpm":     <number>,
  "rmssd_ms":        <number or null>,
  "anomaly":         "normal" | "elevated" | "resting_high" | "erratic",
  "note":            "<one short sentence, <= 200 chars, second-person voice>"
}}

Where rules:
- anomaly "erratic"      if RMSSD < 20ms or |peak-resting| > 50
- anomaly "elevated"     if peak > 100 or avg > 90
- anomaly "resting_high" if resting_bpm_estimate > 85
- anomaly "normal"       otherwise
- "note" is for the wearer — not the lab tech. Be direct.
"""


def build_prompt(rows: list[sqlite3.Row], stats: dict[str, Any]) -> tuple[str, str, str]:
    """Build the prompt and return (prompt, window_start, window_end)."""
    window_start = rows[0]["ts_utc"]
    window_end = rows[-1]["ts_utc"]
    duration_min = round((_iso_to_dt(window_end) - _iso_to_dt(window_start)).total_seconds() / 60, 1)
    device_uuid = rows[0]["device_uuid"] or "unknown"
    return (
        PROMPT_TEMPLATE.format(
            n=len(rows),
            window_start=window_start,
            window_end=window_end,
            duration_min=duration_min,
            device_uuid=device_uuid,
            stats_json=json.dumps(stats, indent=2),
        ),
        window_start,
        window_end,
    )


def call_ollama(model: str, prompt: str, timeout: int = 120) -> str:
    """POST to /api/generate, return raw response text. NO markdown-fence stripping here —
    callers that want JSON should validate with extract_summary().
    """
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 400},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("response", "")


def extract_summary(response: str) -> dict[str, Any] | None:
    """Pull a JSON object out of an LLM response. Tolerates ```json fences and leading prose."""
    s = response.strip()
    # strip code fences
    if s.startswith("```"):
        # find first newline; skip it
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # find first { and last }
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = s[start:end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------- Main ----------

def analyze_window(rows: list[sqlite3.Row], model: str, dry_run: bool) -> dict[str, Any]:
    """Analyze one window. Returns a dict with the analysis_runs row id + summary."""
    stats = compute_stats(rows)
    prompt, ws, we = build_prompt(rows, stats)
    if dry_run:
        return {"prompt": prompt, "stats": stats, "window": (ws, we), "dry": True}
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        raw = call_ollama(model, prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        rid = write_analysis_run(started_at, finished_at, len(rows), ws, we, model, prompt, None, None,
                                  f"{type(exc).__name__}: {exc}")
        mark_rows_analyzed([r["id"] for r in rows], finished_at)
        return {"id": rid, "error": str(exc), "stats": stats}
    summary = extract_summary(raw) or {}
    finished_at = datetime.now(timezone.utc).isoformat()
    rid = write_analysis_run(started_at, finished_at, len(rows), ws, we, model, prompt, raw,
                              json.dumps(summary) if summary else None, None)
    mark_rows_analyzed([r["id"] for r in rows], finished_at)
    return {"id": rid, "stats": stats, "summary": summary, "raw": raw}


def main() -> int:
    p = argparse.ArgumentParser(description="sr16-bridge gateway: analyze accumulated HR with local Ollama")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    p.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN,
                   help="minutes per analysis window (default 5)")
    p.add_argument("--device-uuid", default=None, help="only analyze rows from this device")
    p.add_argument("--dry-run", action="store_true", help="build the prompt but don't call Ollama or write")
    p.add_argument("--quiet", action="store_true", help="suppress per-window line printing")
    args = p.parse_args()

    init_db()
    migrate()
    rows = fetch_unanalyzed(args.device_uuid)
    if not rows:
        if not args.quiet:
            print("no unanalyzed rows; nothing to do")
        return 0

    window_seconds = args.window_min * 60
    windows = batch_into_windows(rows, window_seconds)
    if not args.quiet:
        print(f"found {len(rows)} unanalyzed rows → {len(windows)} window(s) of <= {args.window_min} min")

    results = []
    for win in windows:
        result = analyze_window(win, args.model, args.dry_run)
        results.append(result)
        if not args.quiet and not args.dry_run:
            mid = result.get("id")
            summary = result.get("summary") or {}
            err = result.get("error")
            if err:
                print(f"  window #{mid or '?'} rows={len(win)} ERROR: {err}")
            else:
                print(f"  window #{mid} rows={len(win)} anomaly={summary.get('anomaly')} "
                      f"avg={summary.get('avg_bpm')} peak={summary.get('peak_bpm')} "
                      f"note='{summary.get('note', '')[:60]}'")

    set_kv("last_analyze_ts_utc", datetime.now(timezone.utc).isoformat())
    set_kv("last_analyze_window_count", str(len(windows)))
    set_kv("last_analyze_model", args.model)

    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(
            f"\n#### analyze run @ {datetime.now(timezone.utc).isoformat()}\n"
            f"  device={args.device_uuid or 'ALL'}  model={args.model}\n"
            f"  rows_in={len(rows)}  windows={len(windows)}  dry_run={args.dry_run}\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
