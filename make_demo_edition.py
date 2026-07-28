"""
make_demo_edition.py -- build a slim, deploy-safe demo database from the full store.

Reads  data/demo.sqlite   (~490 MB: 748k node-slots, 34k jobs, 840 onsets, full test period)
Writes data/demo_lite.sqlite  (target < 80 MB) + data/hero_examples.json

It CURATES rather than samples, so the demo stays interesting:

  * Replay window narrowed to 3 days -- 2022-09-26 .. 2022-09-29 (UTC). Chosen for event density:
    it carries the HIGHEST confirmed-job-failure load of any 3-day span in the test period
    (~2,835 non-COMPLETED endings incl. ~1,105 TIMEOUTs) together with 10 genuine *isolated*
    node-anomaly onsets. We deliberately avoid the 2022-09-21 window: although it has ~814 onsets,
    they are a single machine-wide maintenance storm touching ~812 of 840 nodes, so "full cadence
    for every onset node" would keep almost the whole fleet (~166 MB) and show a mass event rather
    than the operationally interesting per-node failures.

  * Every job overlapping the window is kept with its full scored record and drill-down features.
  * All nodes are kept. Nodes that experience an onset keep FULL 15-min cadence (so the run-up to
    every real onset is intact); quiet nodes are thinned to ~hourly cadence. A 6-hour pre-roll is
    kept so the node board is populated from the first virtual minute (6h look-back).
  * Drill-down feature values (contrib_json + the curated IPMI/SLURM columns) are preserved for
    every retained row -- nothing is invented; a dropped row is simply dropped.
  * Hero rows are guaranteed and written to hero_examples.json with the virtual time to jump to.
  * VACUUM makes the file physically compact.

Run:  python make_demo_edition.py
"""
import os, sqlite3, json, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SRC  = os.path.join(HERE, "data", "demo.sqlite")
DST  = os.path.join(HERE, "data", "demo_lite.sqlite")
HERO = os.path.join(HERE, "data", "hero_examples.json")

def _ep(s):  # "YYYY-MM-DD HH:MM:SS" UTC -> epoch
    return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp())
def _iso(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

W0 = _ep("2022-09-26 00:00:00")     # replay window start (UTC)
W1 = _ep("2022-09-29 00:00:00")     # replay window end   (UTC) -- exactly 3 days
PRE = W0 - 6 * 3600                 # keep 6h of node slots before the window for the 6h look-back
QUIET_HOURS = 1                     # quiet nodes: at most one slot per this many hours
ALERT_THR = 0.30                    # P3 default alert threshold (for hero selection only)
NODE_ALERT = 0.50                   # P2 in-scope "at-risk" score
CUT25 = 285.77777777777777          # PSU-0 watts cutoff for the default 25% triage (from meta.json)


def build():
    assert os.path.exists(SRC), f"source db not found: {SRC}"
    if os.path.exists(DST):
        os.remove(DST)

    src = sqlite3.connect(SRC)
    tbl_sql = {n: s for n, s in src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('jobs','node_slots','node_events')")}
    slot_cols = [r[1] for r in src.execute("PRAGMA table_info(node_slots)")]
    src.close()
    collist = ", ".join(f'"{c}"' for c in slot_cols)

    db = sqlite3.connect(DST)
    db.execute(f"ATTACH DATABASE '{SRC}' AS s")
    for name in ("jobs", "node_slots", "node_events"):
        db.execute(tbl_sql[name])

    # ---- jobs: every job whose lifetime overlaps the window (submitted-in-window is a subset) ----
    db.execute("INSERT INTO jobs SELECT * FROM s.jobs WHERE submit_ts < ? AND end_ts >= ?", (W1, W0))

    # ---- node_events: onsets in the window (+ pre-roll) ----
    db.execute("INSERT INTO node_events SELECT * FROM s.node_events WHERE ts >= ? AND ts < ?", (PRE, W1))

    # onset nodes = nodes with an onset inside the replay window
    onset_nodes = [r[0] for r in db.execute(
        "SELECT DISTINCT node FROM s.node_events WHERE ts >= ? AND ts < ?", (W0, W1))]
    onset_in = "(" + ",".join(str(int(n)) for n in onset_nodes) + ")" if onset_nodes else "(-1)"

    # ---- node_slots: onset nodes at FULL cadence ----
    db.execute(f"INSERT INTO node_slots SELECT {collist} FROM s.node_slots "
               f"WHERE ts >= ? AND ts < ? AND node IN {onset_in}", (PRE, W1))

    # ---- node_slots: quiet nodes thinned to one slot per QUIET_HOURS hour ----
    bucket = QUIET_HOURS * 3600
    db.execute(
        f"INSERT INTO node_slots ({collist}) "
        f"SELECT {collist} FROM ("
        f"  SELECT {collist}, ROW_NUMBER() OVER (PARTITION BY node, ts/{bucket} ORDER BY ts) rn "
        f"  FROM s.node_slots WHERE ts >= ? AND ts < ? AND node NOT IN {onset_in}"
        f") WHERE rn = 1", (PRE, W1))

    # ---- indexes (time-window + ID lookups the app uses) ----
    db.execute("CREATE INDEX ix_jobs_submit ON jobs(submit_ts)")
    db.execute("CREATE INDEX ix_jobs_end    ON jobs(end_ts)")
    db.execute("CREATE INDEX ix_jobs_id     ON jobs(job_id)")
    db.execute("CREATE INDEX ix_slots_ts    ON node_slots(ts)")
    db.execute("CREATE INDEX ix_slots_node  ON node_slots(node, ts)")
    db.execute("CREATE INDEX ix_ev_ts       ON node_events(ts)")

    # ---- demo_meta: the replay window travels WITH the lite db (Store reads it) ----
    db.execute("CREATE TABLE demo_meta (key TEXT PRIMARY KEY, value INTEGER)")
    db.executemany("INSERT INTO demo_meta VALUES (?,?)",
                   [("window_start_ts", W0), ("window_end_ts", W1)])
    db.commit()
    db.execute("DETACH DATABASE s")

    heroes = _pick_heroes(db, onset_nodes)
    db.commit()
    db.isolation_level = None       # autocommit so VACUUM can run outside a transaction
    db.execute("VACUUM")
    db.isolation_level = ""
    db.close()

    json.dump(heroes, open(HERO, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    _report(onset_nodes, heroes)


def _pick_heroes(db, onset_nodes):
    db.row_factory = sqlite3.Row
    heroes = {"window_start_ts": W0, "window_end_ts": W1,
              "window_start_iso": _iso(W0), "window_end_iso": _iso(W1),
              "note": "Virtual timestamps to jump to for a guided tour. All rows are real "
                      "precomputed predictions on the Sept-2022 test period.",
              "examples": []}

    def clampjump(ts):   # keep the jump target strictly inside the replay window
        return int(max(W0 + 60, min(W1 - 60, ts)))

    # HERO 1 -- a high-risk job that genuinely TIMED OUT (prefer one whose TIMEOUT lands in-window)
    r = db.execute(
        "SELECT * FROM jobs WHERE state='TIMEOUT' AND risk>=? AND end_ts>=? AND end_ts<? "
        "ORDER BY risk DESC LIMIT 1", (ALERT_THR, W0, W1)).fetchone()
    if r is None:  # fall back to any overlapping high-risk TIMEOUT
        r = db.execute("SELECT * FROM jobs WHERE state='TIMEOUT' AND risk>=? ORDER BY risk DESC LIMIT 1",
                       (ALERT_THR,)).fetchone()
    if r is not None:
        jump = clampjump((r["end_ts"] - 1800) if r["end_ts"] <= W1 else (W0 + W1) // 2)  # flagged & still running
        heroes["examples"].append({
            "kind": "job_timeout_caught", "id": int(r["job_id"]), "entity": f"job {int(r['job_id'])}",
            "user": f"user_{int(r['user_id'])}", "risk_pct": round(float(r["risk"]) * 100, 1),
            "state": r["state"], "pred_type": r["pred_type"], "jump_ts": jump, "jump_iso": _iso(jump),
            "title_en": f"High-risk job caught: {int(r['job_id'])} predicted {r['pred_type']} at "
                        f"{round(float(r['risk'])*100)}% — then timed out.",
            "title_zh": f"高風險任務命中：{int(r['job_id'])} 於提交時預測 {r['pred_type']}（"
                        f"{round(float(r['risk'])*100)}%），最終逾時。",
            "open": {"tab": "jobs", "job_id": int(r["job_id"])}})

    # HERO 2 -- a node the model flagged BEFORE a real onset (in-scope, score >= NODE_ALERT in run-up)
    best = None
    for node in onset_nodes:
        ons = db.execute("SELECT ts FROM node_events WHERE node=? AND ts>=? AND ts<? ORDER BY ts",
                         (node, W0, W1)).fetchone()
        if not ons:
            continue
        onset_ts = ons["ts"]
        slot = db.execute(
            "SELECT node,rack,ts,score,ps_power FROM node_slots WHERE node=? AND ts<? AND ts>=? "
            "AND score>=? AND (ps_power IS NULL OR ps_power<=?) ORDER BY ts DESC LIMIT 1",
            (node, onset_ts, onset_ts - 6 * 3600, NODE_ALERT, CUT25)).fetchone()
        if slot and (best is None or slot["score"] > best[1]["score"]):
            best = (onset_ts, slot)
    if best is not None:
        onset_ts, slot = best
        jump = clampjump(slot["ts"])   # jump to the flagged run-up slot; onset is imminent
        heroes["examples"].append({
            "kind": "node_flagged_before_onset", "id": int(slot["node"]),
            "entity": f"node{int(slot['node']):04d}", "rack": int(slot["rack"]),
            "risk_pct": round(float(slot["score"]) * 100, 1),
            "onset_ts": int(onset_ts), "onset_iso": _iso(onset_ts),
            "jump_ts": jump, "jump_iso": _iso(jump),
            "title_en": f"Node flagged before failure: node{int(slot['node']):04d} scored "
                        f"{round(float(slot['score'])*100)}% ~{round((onset_ts-slot['ts'])/60)} min "
                        f"before its real onset at {_iso(onset_ts)}.",
            "title_zh": f"節點提前預警：node{int(slot['node']):04d} 於真實 onset（{_iso(onset_ts)}）前約 "
                        f"{round((onset_ts-slot['ts'])/60)} 分鐘即被評為 {round(float(slot['score'])*100)}%。",
            "open": {"tab": "nodes", "node_id": int(slot["node"])}})

    # HERO 3 -- a genuine MISS: a job that failed but scored UNDER the threshold, ending in-window
    r = db.execute(
        "SELECT * FROM jobs WHERE state<>'COMPLETED' AND risk<? AND end_ts>=? AND end_ts<? "
        "ORDER BY end_ts DESC LIMIT 1", (ALERT_THR, W0, W1)).fetchone()
    if r is not None:
        jump = clampjump(r["end_ts"] + 300)   # just after it ends -> shows as "missed by model" on the log wall
        heroes["examples"].append({
            "kind": "job_missed", "id": int(r["job_id"]), "entity": f"job {int(r['job_id'])}",
            "user": f"user_{int(r['user_id'])}", "risk_pct": round(float(r["risk"]) * 100, 1),
            "state": r["state"], "jump_ts": jump, "jump_iso": _iso(jump),
            "title_en": f"An honest miss: job {int(r['job_id'])} ended {r['state']} but P3 scored it only "
                        f"{round(float(r['risk'])*100,1)}% — below the {int(ALERT_THR*100)}% alert threshold.",
            "title_zh": f"誠實的漏報：任務 {int(r['job_id'])} 最終 {r['state']}，但 P3 僅評 "
                        f"{round(float(r['risk'])*100,1)}%，低於 {int(ALERT_THR*100)}% 告警門檻。",
            "open": {"tab": "jobs", "job_id": int(r["job_id"])}})
    db.row_factory = None
    return heroes


def _report(onset_nodes, heroes):
    db = sqlite3.connect(DST)
    nj = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ns = db.execute("SELECT COUNT(*) FROM node_slots").fetchone()[0]
    nn = db.execute("SELECT COUNT(DISTINCT node) FROM node_slots").fetchone()[0]
    ne = db.execute("SELECT COUNT(*) FROM node_events").fetchone()[0]
    db.close()
    size_mb = os.path.getsize(DST) / 1_000_000
    print("=" * 66)
    print(f"  wrote {os.path.relpath(DST, HERE)}")
    print(f"  replay window : {_iso(W0)} .. {_iso(W1)}  (3 days)")
    print(f"  jobs          : {nj:,}")
    print(f"  node_slots    : {ns:,}  across {nn} nodes  ({len(onset_nodes)} onset nodes at full cadence)")
    print(f"  node_events   : {ne}")
    print(f"  hero examples : {len(heroes['examples'])} -> {os.path.relpath(HERO, HERE)}")
    for ex in heroes["examples"]:
        print(f"      - {ex['kind']}: {ex['entity']}  jump {ex['jump_iso']}")
    print(f"  FILE SIZE     : {size_mb:.1f} MB   ({'OK <80MB' if size_mb < 80 else 'TOO BIG'})")
    print("=" * 66)


if __name__ == "__main__":
    build()
