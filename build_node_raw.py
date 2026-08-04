"""
build_node_raw.py -- ADD a native-resolution IPMI trace table to an existing demo store.

Run:  python build_node_raw.py                 # both stores, with integrity verification
      python build_node_raw.py --db data/demo_lite.sqlite
      python build_node_raw.py --dry-run       # project rows/bytes, write nothing

STRICTLY ADDITIVE, BY CONSTRUCTION
This opens an existing SQLite file and creates exactly one new table, `node_raw`. It never touches
`jobs`, `node_slots`, `node_events` or `demo_meta`, never re-runs precompute's scoring path, and
never regenerates the curated window. Before and after the write it fingerprints every pre-existing
table (row count + a checksum over the ordered rows) and ABORTS if any of them moved. The curated
three-day window, the job and node rows, and the demo timestamps are therefore provably unaffected.

WHY A SEPARATE TABLE RATHER THAN A COLUMN ON node_slots
`node_slots` is one row per node per 15-minute slot. This is one row per node per 20 SECONDS. They
are different grains; forcing them together would either explode node_slots or destroy the
resolution that is the entire point of the table.

WHAT IS IN IT
Source: the raw ExaData parquet under plugin=ipmi_pub, queried through DuckDB with the node and
time predicates pushed into the scan -- no metric file is ever loaded whole (total_power alone is
93 MB and ps0_input_power 83 MB).

  ps0_w    <- metric=ps0_input_power (PSU-0 input power, W)  ** the P2 triage indicator **
  power_w  <- metric=total_power     (node total power, W)
  temp_c   <- metric=gpu0_core_temp  (GPU-0 core temperature, °C)

THE PSU SERIES, AND THE EARLIER CLAIM THAT IT DID NOT EXIST.
This file used to state that there was no PSU input-power metric anywhere in the raw extract, and
the chart said so in its subtitle. That was true of the EXTRACTED DIRECTORY and false of the source.
D:/M100/data/raw_2209_extracted/ holds 24 of the 104 ipmi_pub metrics in D:/M100/data/22-09.tar;
`ps0_input_power` is one of the 80 that were never unpacked. It is present in the tar at the same
20-second native cadence as total_power, 980 nodes, 83.4 MB.

The trail, backwards from the feature name, because the naming is compositional and the sensor is
not spelled the way the feature is:
  cur_ps0_inputP            the P2 model feature (precompute.py reads it from p09_test.parquet)
  ps0_input_power_avg       the column in D:/M100/data/aggregated_extracted/rack_*/NN.parquet;
                            P2_09_full_scale_rebuild.ipynb cell 17 lists it in BROAD, and its
                            short() helper maps _avg -> "" and _power -> "P", giving ps0_inputP
  ps0_input_power           the raw ipmi_pub metric -- the same name, never renamed, simply absent
                            from the extract that was searched
So the indicator the P2 triage runs on IS available at native resolution, and it is now the primary
series on the trace. Total node power is kept alongside it as context, because the two answer
different questions and the earlier substitution was the only thing available at the time.

COVERAGE, and why it is shaped this way
  * the 10 nodes with a recorded onset in the curated window, +/- ONSET_SPAN_H around their onset --
    the case the chart exists for, where there is a real event to mark;
  * a small set of baseline nodes across the whole window, so a quiet shift can still show a trace.
    This set is deliberately small: measured across a 40-point sweep, the highest-scoring watch-list
    node was 33 DIFFERENT nodes, and the most frequent held the slot 4 times out of 40. Pre-covering
    "whatever node happens to be top" would mean covering most of the fleet. The renderer therefore
    picks the highest-scoring node THAT HAS a trace and says so, rather than implying it is the
    highest-scoring node overall.
Resolution is never reduced to save space -- if this needs to shrink, shrink the span or the roster.
"""
import argparse, hashlib, os, sqlite3, sys, time

RAW = "D:/M100/data/raw_2209_extracted/year_month=22-09/plugin=ipmi_pub"
POWER_PARQUET = f"{RAW}/metric=total_power/a_0.parquet"
TEMP_PARQUET  = f"{RAW}/metric=gpu0_core_temp/a_0.parquet"
PS0_PARQUET   = f"{RAW}/metric=ps0_input_power/a_0.parquet"

ONSET_SPAN_H = 3.0        # hours either side of an onset
# Budget for the whole addition. The committed store is ~48 MB and GitHub's hard limit is 100 MB per
# file, so this keeps the DB near 50 MB with a wide margin. Measured actual on demo_lite: ~1.2 MB.
SIZE_BUDGET_MB = 8.0
# Cap on how many onset spans are extracted. The curated 3-day store has 10 onsets and is unaffected;
# the full 10-day store has 840 across 823 nodes, which projects to 34 MB and would blow the budget.
# The MOST RECENT onsets are kept, because those are the ones inside (or nearest) the curated window
# the demo actually replays. Reducing the roster is the correct lever here -- never the 20s sampling
# resolution, which is the whole reason this table exists.
MAX_ONSET_SPANS = 40
BASELINE_NODES = [52, 933]   # most frequent top-watch nodes that are not onset nodes

HERE = os.path.dirname(os.path.abspath(__file__))
PRESERVE = ("jobs", "node_slots", "node_events", "demo_meta")


def fingerprint(cx, table):
    """(row count, checksum over the ordered rows). Detects any change to a pre-existing table."""
    try:
        n = cx.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    h = hashlib.sha256()
    for row in cx.execute(f"SELECT * FROM {table}"):
        h.update(repr(row).encode("utf-8", "replace"))
    return (n, h.hexdigest()[:16])


def roster(cx):
    """-> (onset_spans, window). onset_spans is [(node, lo, hi)] for this store's own window."""
    dm = {r[0]: r[1] for r in cx.execute("SELECT key, value FROM demo_meta")} \
        if cx.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='demo_meta'"
                      ).fetchone()[0] else {}
    if "window_start_ts" in dm:
        W0, W1 = int(dm["window_start_ts"]), int(dm["window_end_ts"])
    else:
        W0 = cx.execute("SELECT MIN(ts) FROM node_slots").fetchone()[0]
        W1 = cx.execute("SELECT MAX(ts) FROM node_slots").fetchone()[0]
    span = int(ONSET_SPAN_H * 3600)
    ev = cx.execute("SELECT node, ts FROM node_events WHERE ts>? AND ts<=? "
                    "ORDER BY ts DESC LIMIT ?", (W0, W1, MAX_ONSET_SPANS)).fetchall()
    ev = sorted(ev, key=lambda r: r[1])
    spans = [(int(n), int(ts) - span, int(ts) + span) for n, ts in ev]
    for n in BASELINE_NODES:
        spans.append((int(n), int(W0), int(W1)))
    return spans, (int(W0), int(W1))


def build_sql(spans):
    """One DuckDB statement. Predicates are pushed into the parquet scan, so only the matching row
    groups are read -- never the whole 93 MB metric file.

    A chain of FULL OUTER JOINs is the wrong shape for three sources: the join key has to be
    COALESCEd at every step and a row present only in the third source acquires nulls in the key.
    So the (node, timestamp) grid is built ONCE as a UNION of all three, and each metric is joined
    onto it. A sensor that stopped reporting therefore appears as a NULL at a real timestamp rather
    than as a missing row -- which is exactly what the chart needs, because a node going silent
    before its own onset is the P2 signal, not a gap to be closed.
    """
    where = " OR ".join(
        f"(node='{n}' AND epoch(timestamp) BETWEEN {lo} AND {hi})" for n, lo, hi in spans)
    def src(path):
        return f"(SELECT node, timestamp, value FROM read_parquet('{path}') WHERE {where})"
    return f"""
        WITH p AS {src(POWER_PARQUET)},
             t AS {src(TEMP_PARQUET)},
             s AS {src(PS0_PARQUET)},
             k AS (SELECT node, timestamp FROM p
                   UNION SELECT node, timestamp FROM t
                   UNION SELECT node, timestamp FROM s)
        SELECT CAST(k.node AS INTEGER)        AS node,
               CAST(epoch(k.timestamp) AS BIGINT) AS ts,
               CAST(p.value AS DOUBLE)        AS power_w,
               CAST(t.value AS DOUBLE)        AS temp_c,
               CAST(s.value AS DOUBLE)        AS ps0_w
        FROM k
        LEFT JOIN p ON p.node = k.node AND p.timestamp = k.timestamp
        LEFT JOIN t ON t.node = k.node AND t.timestamp = k.timestamp
        LEFT JOIN s ON s.node = k.node AND s.timestamp = k.timestamp
        ORDER BY 1, 2
    """


def process(db_path, dry_run):
    import duckdb
    print("=" * 96)
    print(f"{db_path}")
    print("=" * 96)
    if not os.path.exists(db_path):
        print("  file does not exist -- skipped"); return True

    before_bytes = os.path.getsize(db_path)
    cx = sqlite3.connect(db_path)
    pre = {t: fingerprint(cx, t) for t in PRESERVE}
    spans, (W0, W1) = roster(cx)
    onset_nodes = sorted({n for n, lo, hi in spans if n not in BASELINE_NODES})
    print(f"  window     : {W0} -> {W1}")
    print(f"  onset spans: {len(spans) - len(BASELINE_NODES)} at +/-{ONSET_SPAN_H}h "
          f"over nodes {onset_nodes}")
    print(f"  baseline   : {BASELINE_NODES} across the full window")

    t0 = time.time()
    df = duckdb.connect().execute(build_sql(spans)).df()
    print(f"  extracted  : {len(df):,} rows in {time.time() - t0:.1f}s "
          f"({df.node.nunique()} nodes)")
    if len(df):
        med = df.groupby("node").ts.apply(lambda s: s.diff().median()).median()
        print(f"  cadence    : {med:.0f}s median   "
              f"nulls power={int(df.power_w.isna().sum())} temp={int(df.temp_c.isna().sum())} "
              f"ps0={int(df.ps0_w.isna().sum())}")
        print(f"  ps0 range  : {df.ps0_w.min():.0f}-{df.ps0_w.max():.0f} W over "
              f"{int(df.ps0_w.notna().sum()):,} samples")
    projected_mb = len(df) * 42.5 / 1e6         # one more REAL column than the two-metric version
    print(f"  projected  : ~{projected_mb:.2f} MB  (budget {SIZE_BUDGET_MB} MB)")
    if projected_mb > SIZE_BUDGET_MB:
        print(f"  ABORT: projection exceeds the {SIZE_BUDGET_MB} MB budget. Reduce ONSET_SPAN_H or "
              f"the roster -- never the sampling resolution.")
        cx.close(); return False
    if dry_run:
        print("  dry run: nothing written"); cx.close(); return True

    # ---- the ONLY write: one new table -------------------------------------------------------
    cx.execute("DROP TABLE IF EXISTS node_raw")           # idempotent re-run; touches nothing else
    cx.execute("CREATE TABLE node_raw (node INTEGER, ts INTEGER, power_w REAL, temp_c REAL, "
               "ps0_w REAL)")
    cx.executemany("INSERT INTO node_raw VALUES (?,?,?,?,?)",
                   df[["node", "ts", "power_w", "temp_c", "ps0_w"]]
                   .itertuples(index=False, name=None))
    cx.execute("CREATE INDEX IF NOT EXISTS ix_raw_node_ts ON node_raw(node, ts)")
    cx.commit()

    # ---- prove nothing else moved --------------------------------------------------------------
    post = {t: fingerprint(cx, t) for t in PRESERVE}
    ok = True
    for t in PRESERVE:
        same = pre[t] == post[t]
        ok &= same
        state = "unchanged" if same else f"*** CHANGED *** {pre[t]} -> {post[t]}"
        print(f"  verify {t:<12}: {state}")
    n = cx.execute("SELECT COUNT(*) FROM node_raw").fetchone()[0]
    cx.close()

    after_bytes = os.path.getsize(db_path)
    added = (after_bytes - before_bytes) / 1e6
    print(f"  node_raw   : {n:,} rows")
    print(f"  size       : {before_bytes/1e6:.1f} -> {after_bytes/1e6:.1f} MB  (+{added:.2f} MB, "
          f"+{100*added/(before_bytes/1e6):.1f}%)")
    if added > SIZE_BUDGET_MB:
        print(f"  WARNING: actual growth exceeded the {SIZE_BUDGET_MB} MB budget")
        ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dbs = a.db or [os.path.join(HERE, "data", "demo_lite.sqlite"),
                   os.path.join(HERE, "data", "demo.sqlite")]
    allok = True
    for db in dbs:
        allok &= process(db if os.path.isabs(db) else os.path.join(HERE, db), a.dry_run)
        print()
    print("OK" if allok else "FAILED -- a pre-existing table changed, or the budget was exceeded")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
