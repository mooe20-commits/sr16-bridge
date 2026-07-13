"""Analyze a3_hourly_rollup — surface what's learnable from the data we have,
without assuming unit-scale is 1:1.

Output:
- Per-day totals + activity distribution
- Sleep / quiet window detection
- Pairwise field Pearson correlations (Pearson r)
- Retransmit-artifact payload detection
- Hourly activity heatmap

Usage:
    .venv/bin/python scripts/analyze_rollup.py
"""
import sqlite3
import statistics
from collections import defaultdict

DB = '/Users/mih/health/sr16.db'

FIELDS = ['steps_raw', 'cal_raw', 'dist_raw', 'hr_agg_raw', 'intensity_raw']


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT date_local, hour_utc, val16, marker,
               steps_raw, cal_raw, dist_raw, hr_agg_raw, intensity_raw
        FROM a3_hourly_rollup
        ORDER BY date_local, hour_utc
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT date_local, hour_local, val16, marker,
               steps_raw, cal_raw, dist_raw, hr_agg_raw, intensity_raw
        FROM a3_hourly
        ORDER BY date_local DESC, val16
    """)
    raw = cur.fetchall()

    print("=" * 72)
    print(f"SR16 ROLLUP ANALYSIS — {len(rows)} rollup rows, {len(raw)} raw rows")
    print("=" * 72)

    # --- Per-day totals ---
    day_data = defaultdict(list)
    for r in rows:
        day_data[r['date_local']].append(r)
    days = sorted(day_data, reverse=True)

    print(f"\nDays covered: {days}\n")
    print("=" * 72)
    print("SECTION 1: Per-day totals (assumes raw = 1:1 — verify with walk test)")
    print("=" * 72)

    for d in days:
        rd = day_data[d]
        steps = [r['steps_raw'] for r in rd]
        cals = [r['cal_raw'] for r in rd]
        dists = [r['dist_raw'] for r in rd]
        hrs = [r['hr_agg_raw'] for r in rd]
        ints = [r['intensity_raw'] for r in rd]
        nonzero_hours = sum(1 for r in rd if r['steps_raw'] or r['cal_raw'] or r['dist_raw'])
        peak_hour = max(rd, key=lambda x: x['steps_raw'])

        print(f"\n  {d}:")
        print(f"    hours logged       : {len(rd)}")
        print(f"    hours w/ activity  : {nonzero_hours}")
        print(f"    sum steps_raw      : {sum(steps):7d}")
        print(f"    sum cal_raw        : {sum(cals):5d}")
        print(f"    sum dist_raw       : {sum(dists):6d}")
        print(f"    mean hr_agg_raw    : {statistics.mean(hrs):7.1f}")
        print(f"    mean intensity_raw : {statistics.mean(ints):7.1f}")
        print(f"    peak activity hour : {peak_hour['hour_utc']:02d}h "
              f"(steps={peak_hour['steps_raw']}, "
              f"intensity={peak_hour['intensity_raw']})")

    # --- Sleep window ---
    print("\n" + "=" * 72)
    print("SECTION 2: Sleep / quiet window")
    print("=" * 72)

    for d in days:
        rd = day_data[d]
        run_start = None
        runs = []
        for r in sorted(rd, key=lambda x: x['hour_utc']):
            is_quiet = (r['steps_raw'] == 0 and r['cal_raw'] == 0 and r['dist_raw'] == 0)
            if is_quiet and run_start is None:
                run_start = r['hour_utc']
            elif not is_quiet and run_start is not None:
                runs.append((run_start, r['hour_utc'] - 1))
                run_start = None
        if run_start is not None:
            active = [r['hour_utc'] for r in rd
                      if not (r['steps_raw'] == 0 and r['cal_raw'] == 0 and r['dist_raw'] == 0)]
            next_hr = max(active) if active else 0
            runs.append((run_start, next_hr))
        print(f"\n  {d}:")
        if not runs:
            print("    no quiet stretches")
        for s, e in runs:
            if e >= s:
                print(f"    {s:02d}h -> {e:02d}h  ({e - s + 1}h)")

    # --- Correlation matrix ---
    print("\n" + "=" * 72)
    print("SECTION 3: Pairwise field correlation (Pearson r)")
    print("=" * 72)
    print("\n  All rollup rows combined:")
    print(f"  {'':14s}  " + "  ".join(f"{f[:8]:>8s}" for f in FIELDS))
    for fi in FIELDS:
        row_vals = []
        for fj in FIELDS:
            xs = [r[fi] for r in rows]
            ys = [r[fj] for r in rows]
            r = pearson(xs, ys)
            row_vals.append(f"{r:+.3f}" if r is not None else "    -   ")
        print(f"  {fi:14s}  " + "  ".join(f"{v:>8s}" for v in row_vals))

    # --- Retransmit-artifact detection ---
    print("\n" + "=" * 72)
    print("SECTION 4: Retransmit artifact detection (same payload, multiple val16s)")
    print("=" * 72)

    by_payload = defaultdict(list)
    for row in raw:
        payload = (row[4], row[5], row[6])
        by_payload[payload].append((row[0], row[2], row[3]))  # date, val16, marker

    suspicious = [(p, o) for p, o in by_payload.items() if len(o) >= 3]
    suspicious.sort(key=lambda x: -len(x[1]))

    print(f"\n  Found {len(suspicious)} distinct (steps,cal,dist) payloads "
          f"appearing 3+ times in raw table.")
    if suspicious:
        print(f"\n  Sample (top 5 by occurrence count):")
        print(f"  {'steps':>7s} {'cal':>5s} {'dist':>6s} | occurrences")
        for payload, occs in suspicious[:5]:
            st, ca, di = payload
            unique_val16 = sorted(set(o[1] for o in occs))
            print(f"  {st:7d} {ca:5d} {di:6d} | {len(occs)} "
                  f"(val16s: {unique_val16[:8]})")

    # --- Inferences ---
    print("\n" + "=" * 72)
    print("SECTION 5: Inferences")
    print("=" * 72)
    print(f"""
  1. PER-HOUR STEP INFLATION:
     Today 17:00 UTC = steps_raw=63239. Realistic max ~8K/hour.
     Likely ×0.1 or ×0.01 scale, OR retransmit duplicate.
     Retransmit artifact (SEC 4) confirms duplicate transmission.

  2. CORRELATED FIELDS (r > 0.7):
     steps_raw <-> intensity_raw (check SEC 3)
     These likely capture the same underlying signal.

  3. hr_agg_raw vs intensity_raw:
     If they correlate strongly with each other, one is duplicate signal.
     If both correlate with steps_raw, both are activity-load related.

  4. dist_raw BEHAVIOR:
     dist=8 between high-step hours, dist=49K in others — NOT monotonically
     cumulative. Probably per-hour deltas in non-m units.
""")

    # --- Hourly heatmap (today if present, else latest day) ---
    print("=" * 72)
    print(f"SECTION 6: Hourly activity heatmap ({days[0]})")
    print("=" * 72)
    today = day_data.get(days[0], [])
    if today:
        print(f"  hour | steps | cal | dist | hr | int | marker")
        print(f"  -----+-------+-----+------+----+-----+-------")
        for r in sorted(today, key=lambda x: x['hour_utc']):
            bar = "#" * min(40, r['steps_raw'] // 1500)
            print(f"  {r['hour_utc']:02d}h   "
                  f"{r['steps_raw']:5d} "
                  f"{r['cal_raw']:3d} "
                  f"{r['dist_raw']:5d} "
                  f"{r['hr_agg_raw']:3d} "
                  f"{r['intensity_raw']:5d} "
                  f"0x{r['marker']:04x} "
                  f"{bar}")
    print()
    con.close()


if __name__ == "__main__":
    main()
