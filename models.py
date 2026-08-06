"""
models.py -- data access over the precomputed store (dashboard/data/demo.sqlite).

The live API NEVER runs a model. Every number returned here is either
  * a real model prediction computed once by precompute.py, or
  * a real IPMI / SLURM measurement, or
  * a clearly-labelled proxy (utilisation from power) or N/A (not in the dataset).

All queries answer one question: "what is the state of the system at virtual time T?"
The current policy settings are applied here, so changing them in the UI actually
changes which jobs are flagged and which nodes are scored.
"""
import os, json, sqlite3, threading, datetime
import pandas as pd, numpy as np

import feature_labels

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# Which store to serve. Defaults to the slim, deploy-safe edition; point DEMO_DB at
# data/demo.sqlite locally to serve the full test period. Relative paths resolve from here.
DB_PATH = os.environ.get("DEMO_DB", "data/demo_lite.sqlite")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(HERE, DB_PATH)

def _iso_z(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# display / operating constants
JOB_RECENT_H   = 4      # a finished job stays on the job board this many virtual hours
NODE_MAXAGE_H  = 6      # how far back to look for a node's most-recent 15-min slot
NODE_ALERT_S   = 0.50   # P2 score at/above which an IN-SCOPE node is flagged "at-risk"
LOG_WINDOW_H   = 3      # how far back the log wall reaches

# bilingual labels + grouping for the P3 job drill-down INPUT block
P3_LABELS = {
    "req_walltime_min":   ("Requested walltime (min)", "申請時限 (分鐘)", "request"),
    "num_nodes":          ("Nodes requested", "申請節點數", "request"),
    "gpus_per_node":      ("GPUs per node", "每節點 GPU 數", "request"),
    "num_cpus":           ("CPUs requested", "申請 CPU 數", "request"),
    "cpus_per_node":      ("CPUs per node", "每節點 CPU 數", "request"),
    "partition":          ("Partition", "分區 (partition)", "request"),
    "qos":                ("QoS", "服務品質 (QoS)", "request"),
    "nice":               ("Nice value", "Nice 優先值", "request"),
    "has_dependency":     ("Has dependency", "有相依性", "request"),
    "is_array_task":      ("Array task", "陣列任務", "request"),
    "submit_hour":        ("Submit hour (UTC)", "提交時段 (UTC)", "request"),
    "submit_dow":         ("Submit day-of-week", "提交星期", "request"),
    "user_prior_n":            ("User's prior jobs", "使用者歷史任務數", "history"),
    "log_user_prior_n":        ("log(1+prior jobs)", "log(1+歷史任務數)", "history"),
    "user_prior_fail_rate":    ("User prior failure rate", "使用者歷史失敗率", "history"),
    "user_prior_timeout_rate": ("User prior timeout rate", "使用者歷史逾時率", "history"),
    "user_prior_wt_usage":     ("User prior walltime usage", "使用者歷史時限使用率", "history"),
    "user_fail_streak":        ("Current failure streak", "當前連續失敗數", "history"),
    "user_min_since_fail":     ("Minutes since last failure", "距上次失敗 (分鐘)", "history"),
    "user_min_since_job":      ("Minutes since last job", "距上次任務 (分鐘)", "history"),
    "user_fail_rate_last10":   ("Failure rate, last 10", "近 10 次失敗率", "history"),
    "user_fail_rate_last50":   ("Failure rate, last 50", "近 50 次失敗率", "history"),
    "user_part_fail_rate":     ("User×partition failure rate", "使用者×分區失敗率", "history"),
    "user_burst_1h":           ("Jobs submitted in last hour", "近 1 小時提交數", "history"),
}
CLASS_LABEL = {"COMPLETED": ("Completed", "完成"), "FAILED": ("Failed", "失敗"),
               "TIMEOUT": ("Timeout", "逾時"), "OUT_OF_MEMORY": ("Out of memory", "記憶體不足")}


class Store:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        # read-only, per-request queries only -- the whole DB is NEVER loaded into memory
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.model_info = json.load(open(os.path.join(DATA, "model_info.json"), encoding="utf-8"))
        self.meta = json.load(open(os.path.join(DATA, "meta.json"), encoding="utf-8"))
        # the replay window travels with the served db: prefer the demo_meta table (slim edition),
        # otherwise fall back to the window recorded in meta.json (full store).
        try:
            dm = {r[0]: r[1] for r in self.db.execute("SELECT key, value FROM demo_meta")}
            if "window_start_ts" in dm and "window_end_ts" in dm:
                self.meta["window_start_ts"] = int(dm["window_start_ts"])
                self.meta["window_end_ts"] = int(dm["window_end_ts"])
                self.meta["window_start_iso"] = _iso_z(dm["window_start_ts"])
                self.meta["window_end_iso"] = _iso_z(dm["window_end_ts"])
        except sqlite3.OperationalError:
            pass  # no demo_meta table (full store) -> keep meta.json's window
        self._ensure_indexes()
        self.util_ref = float(self.meta["util_ref_watts"])
        self.cutoffs = {int(k): float(v) for k, v in self.meta["p2_power_cutoffs_watts"].items()}
        self.n_nodes_total = self._scalar("SELECT COUNT(DISTINCT node) FROM node_slots")
        # drill-down support
        self.p2_feats = self.meta.get("p2_feats", [])                 # contrib index -> feature name
        self.p2_label = {f: (en, zh) for f, en, zh in self.meta.get("p2_curated", [])}
        self.p2_curated = self.meta.get("p2_curated", [])
        self.p3_importance = self.meta.get("p3_global_importance", [])
        self.p3_input_feats = self.meta.get("p3_input_feats", [])
        self.node_alert = float(self.meta.get("p2_node_alert_score", NODE_ALERT_S))
        self._job_cols = set(r[1] for r in self.db.execute("PRAGMA table_info(jobs)"))

    def _ensure_indexes(self):
        """Guarantee the indexes the per-request time-window / ID lookups rely on. Uses
        IF NOT EXISTS and is wrapped in try/except so it is a harmless no-op on a read-only
        filesystem (the slim edition already ships with all of these)."""
        for s in (
            "CREATE INDEX IF NOT EXISTS ix_jobs_submit ON jobs(submit_ts)",
            "CREATE INDEX IF NOT EXISTS ix_jobs_end    ON jobs(end_ts)",
            "CREATE INDEX IF NOT EXISTS ix_jobs_id     ON jobs(job_id)",
            "CREATE INDEX IF NOT EXISTS ix_slots_ts    ON node_slots(ts)",
            "CREATE INDEX IF NOT EXISTS ix_slots_node  ON node_slots(node, ts)",
            "CREATE INDEX IF NOT EXISTS ix_ev_ts       ON node_events(ts)",
        ):
            try:
                self.db.execute(s)
            except Exception:
                pass
        try:
            self.db.commit()
        except Exception:
            pass

    # -------------------------------------------------- low level
    def _df(self, q, p=()):
        with self._lock:
            return pd.read_sql_query(q, self.db, params=p)

    def _scalar(self, q, p=()):
        with self._lock:
            return self.db.execute(q, p).fetchone()[0]

    def node_cutoff_watts(self, pct: float) -> float:
        """PSU-0 power cutoff for 'keep the riskiest pct% of slots' (train-derived, from meta)."""
        pct = float(pct)
        if pct >= 100: return float("inf")
        levels = sorted(self.cutoffs)
        if pct in self.cutoffs: return self.cutoffs[pct]
        lo = max([l for l in levels if l <= pct], default=levels[0])
        hi = min([l for l in levels if l >= pct], default=levels[-1])
        if lo == hi: return self.cutoffs[lo]
        f = (pct - lo) / (hi - lo)
        return self.cutoffs[lo] + f * (self.cutoffs[hi] - self.cutoffs[lo])

    # -------------------------------------------------- JOBS
    def _job_board(self, t: int, recent_h=JOB_RECENT_H) -> pd.DataFrame:
        """Jobs that are active (pending/running) or ended within recent_h at virtual time t."""
        lo = t - recent_h * 3600
        d = self._df("SELECT * FROM jobs WHERE submit_ts <= ? AND end_ts >= ?", (t, lo))
        if d.empty:
            d["status"] = []; return d
        pend = t < d.start_ts
        run  = (d.start_ts <= t) & (t < d.end_ts)
        d["status"] = np.where(pend, "PENDING", np.where(run, "RUNNING", d.state))
        d["elapsed_min"] = np.where(pend, 0.0, np.where(run, (t - d.start_ts) / 60.0, d.rt_min))
        req = d.req_walltime_min.replace(0, np.nan)
        d["progress"] = np.where(pend, 0.0,
                          np.where(run, np.clip(d.elapsed_min / req * 100, 0, 100), 100.0))
        d["progress"] = d["progress"].fillna(0.0)
        d["active"] = pend | run
        return d

    def jobs(self, t: int, policies: dict, limit=200, state=None, sort="risk",
             flagged_only=False) -> dict:
        thr = float(policies["alert_threshold"])
        d = self._job_board(t)
        if d.empty:
            return {"virtual_ts": t, "count": 0, "jobs": []}
        d["flagged"] = (d.risk >= thr) & d.active           # only in-flight jobs can be "flagged"
        # `flagged_only` exists so a caller can request the flagged set COMPLETE rather than hoping a
        # limit covers it. Sorting by risk and truncating does not: measured, the last flagged job
        # sits at rank 816 at the default 0.30 threshold and rank 1,821 at 0.05, so any fixed limit
        # silently drops flagged jobs as soon as the operator lowers the slider.
        if flagged_only:
            d = d[d.flagged]
        if state:
            d = d[d.status == state]
        srt = {"risk": ("risk", False), "elapsed": ("elapsed_min", False),
               "submit": ("submit_ts", False), "job": ("job_id", True)}.get(sort, ("risk", False))
        d = d.sort_values(srt[0], ascending=srt[1]).head(int(limit))
        rows = []
        for r in d.itertuples(index=False):
            failtype = r.pred_type if r.pred_type else "FAILED"
            rows.append({
                "job_id": int(r.job_id), "user": f"user_{int(r.user_id)}",
                "partition": str(r.partition), "qos": str(r.qos),
                "num_nodes": int(r.num_nodes), "gpus_per_node": (None if pd.isna(r.gpus_per_node) else int(r.gpus_per_node)),
                "status": r.status, "final_state": str(r.state),
                "elapsed_min": round(float(r.elapsed_min), 1),
                "req_walltime_min": (None if pd.isna(r.req_walltime_min) else round(float(r.req_walltime_min), 1)),
                "progress": round(float(r.progress), 1),
                "risk": round(float(r.risk), 4), "risk_pct": round(float(r.risk) * 100, 1),
                "pred_type": failtype, "flagged": bool(r.flagged),
                "active": bool(r.active),
                "band": ("high" if r.risk >= 0.6 else "medium" if r.risk >= thr else "low"),
            })
        return {"virtual_ts": t, "count": len(rows), "jobs": rows}

    # -------------------------------------------------- NODES
    def _latest_slots(self, t: int) -> pd.DataFrame:
        lo = t - NODE_MAXAGE_H * 3600
        d = self._df("SELECT * FROM node_slots WHERE ts <= ? AND ts >= ? ORDER BY ts", (t, lo))
        if d.empty: return d
        return d.sort_values("ts").groupby("node", as_index=False).tail(1)

    def _classify_nodes(self, t: int, policies: dict) -> pd.DataFrame:
        d = self._latest_slots(t)
        if d.empty: return d
        cutoff = self.node_cutoff_watts(policies["node_filter_pct"])
        # low power = risk -> in scope when PSU-0 power is at/below the cutoff (null = idle = in scope)
        d["scored"] = d.ps_power.isna() | (d.ps_power <= cutoff)
        crit = d.label == 1                                  # a real anomaly onset within 30 min (ground truth)
        warn = d.scored & (d.score >= NODE_ALERT_S) & ~crit  # model-predicted at-risk, in scope
        d["state"] = np.where(crit, "CRITICAL", np.where(warn, "WARNING", "HEALTHY"))
        guard = bool(policies.get("node_fail_guard_enabled", True))
        hot = d.temp.notna() & (d.temp >= float(policies.get("temp_isolate_c", 92)))
        d["isolated"] = ((d.state == "CRITICAL") & guard) | hot
        d["util"] = np.clip(d.power / self.util_ref * 100.0, 0, 100)
        return d

    def nodes(self, t: int, policies: dict, limit=80, sort="risk") -> dict:
        d = self._classify_nodes(t, policies)
        if d.empty:
            return {"virtual_ts": t, "count": 0, "nodes_total": self.n_nodes_total, "nodes": []}
        rank = {"CRITICAL": 0, "WARNING": 1, "HEALTHY": 2}
        d["rk"] = d.state.map(rank)
        if sort == "risk":
            d = d.sort_values(["rk", "scored", "score"], ascending=[True, False, False])
        elif sort == "temp":
            d = d.sort_values("temp", ascending=False)
        elif sort == "power":
            d = d.sort_values("power", ascending=False)
        elif sort == "node":
            d = d.sort_values("node", ascending=True)
        shown = d.head(int(limit))
        rows = []
        for r in shown.itertuples(index=False):
            rows.append({
                "node": int(r.node), "node_label": f"node{int(r.node):04d}", "rack": int(r.rack),
                "state": r.state, "isolated": bool(r.isolated),
                "temp": (None if pd.isna(r.temp) else round(float(r.temp), 1)),
                "power": (None if pd.isna(r.power) else round(float(r.power), 0)),
                "fan": (None if pd.isna(r.fan) else round(float(r.fan), 0)),
                "ambient": (None if pd.isna(r.ambient) else round(float(r.ambient), 1)),
                "util": (None if pd.isna(r.util) else round(float(r.util), 1)),
                "scored": bool(r.scored),
                "risk": (round(float(r.score), 4) if r.scored else None),
                "risk_pct": (round(float(r.score) * 100, 1) if r.scored else None),
                "onset": bool(r.label == 1),
                # fields the ExaData dataset simply does not carry:
                "vram": None, "pcie_err": None, "mig_status": None,
            })
        counts = d.state.value_counts().to_dict()
        return {"virtual_ts": t, "count": len(rows), "nodes_total": self.n_nodes_total,
                "nodes_scored": int(d.scored.sum()), "nodes_in_scope_pct": round(100 * d.scored.mean(), 1),
                "state_counts": {k: int(counts.get(k, 0)) for k in ("CRITICAL", "WARNING", "HEALTHY")},
                "nodes": rows}

    # -------------------------------------------------- SUMMARY
    def summary(self, t: int, policies: dict) -> dict:
        thr = float(policies["alert_threshold"])
        active = self._df("SELECT * FROM jobs WHERE submit_ts <= ? AND end_ts > ?", (t, t))
        running = int(((active.start_ts <= t) & (active.end_ts > t)).sum()) if not active.empty else 0
        pending = int((active.start_ts > t).sum()) if not active.empty else 0
        warn = active[active.risk >= thr] if not active.empty else active
        wtype = warn.pred_type.value_counts().to_dict() if not warn.empty else {}
        board = self._job_board(t)
        slurm = {}
        if not board.empty:
            fam = board.status.replace({"OUT_OF_MEMORY": "FAILED", "TIMEOUT": "FAILED", "NODE_FAIL": "FAILED"})
            slurm = fam.value_counts().to_dict()
        nd = self._classify_nodes(t, policies)
        crit = int((nd.state == "CRITICAL").sum()) if not nd.empty else 0
        atrisk = int((nd.state == "WARNING").sum()) if not nd.empty else 0
        isolated = int(nd.isolated.sum()) if not nd.empty else 0
        util = round(float(nd.util.mean()), 1) if not nd.empty else None
        # aggregate GPU-hours currently in-flight and at risk (real, from num_nodes x gpus x elapsed)
        gpu_at_risk = 0.0
        if not active.empty:
            g = active[active.risk >= thr]
            gpu_at_risk = float((g.num_nodes * g.gpus_per_node.fillna(0) * (t - g.start_ts).clip(lower=0) / 3600).sum())
        return {
            "virtual_ts": t,
            "active_jobs": int(len(active)), "running": running, "pending": pending,
            "warnings": {"total": int(len(warn)),
                         "OOM": int(wtype.get("OUT_OF_MEMORY", 0)),
                         "TIMEOUT": int(wtype.get("TIMEOUT", 0)),
                         "FAILED": int(wtype.get("FAILED", 0))},
            "nodes_total": self.n_nodes_total, "nodes_anomalous": crit, "nodes_at_risk": atrisk,
            "nodes_isolated": isolated, "nodes_scored": int(nd.scored.sum()) if not nd.empty else 0,
            "utilisation_pct": util, "utilisation_is_proxy": True,
            "gpu_hours_at_risk": round(gpu_at_risk, 0),
            "slurm": {k: int(slurm.get(k, 0)) for k in ("RUNNING", "PENDING", "COMPLETED", "FAILED")},
        }

    # -------------------------------------------------- LOGS (auto-healing wall)
    def logs(self, t: int, policies: dict, limit=40) -> dict:
        thr = float(policies["alert_threshold"])
        lo = t - LOG_WINDOW_H * 3600
        ev = []
        # Every event carries the entity it is about as STRUCTURED DATA -- (kind, id) -- alongside the
        # human message. The id is already in the message text, but only as formatted prose; a UI that
        # recovered it by regex would keep working right up until someone reworded a sentence, and
        # then fail silently and look like a dead button. Emitting it as a field makes "jump to this
        # entity" a lookup rather than a parse.
        # 1) real node anomaly onsets (+ policy auto-isolation)
        on = self._df("SELECT * FROM node_events WHERE ts > ? AND ts <= ? ORDER BY ts", (lo, t))
        for r in on.itertuples(index=False):
            node_ent = {"kind": "node", "id": int(r.node)}
            ev.append((int(r.ts), "ERROR", "P2 Detector",
                       f"Node node{int(r.node):04d} (rack {int(r.rack)}): monitoring-anomaly onset "
                       f"detected [{r.kind}] within 30-min horizon.", node_ent))
            if policies.get("node_fail_guard_enabled", True):
                ev.append((int(r.ts) + 1, "CRITICAL", "Auto-Isolation",
                           f"Node node{int(r.node):04d} set to DRAIN and isolated from topology (policy).",
                           node_ent))
        # 2) jobs flagged at submission (threshold-dependent -> reflects the policy live)
        fj = self._df("SELECT * FROM jobs WHERE submit_ts > ? AND submit_ts <= ? AND risk >= ?", (lo, t, thr))
        for r in fj.itertuples(index=False):
            ev.append((int(r.submit_ts), "WARNING", "P3 Predictor",
                       f"Job {int(r.job_id)} (user_{int(r.user_id)}) flagged at submission: "
                       f"predicted {r.pred_type} risk {r.risk*100:.0f}%. Checkpoint advised.",
                       {"kind": "job", "id": int(r.job_id)}))
        # 3) jobs that just ended as a failure (shows the model right or wrong, honestly)
        ej = self._df("SELECT * FROM jobs WHERE end_ts > ? AND end_ts <= ? AND state <> 'COMPLETED'", (lo, t))
        for r in ej.itertuples(index=False):
            hit = "correctly predicted" if r.risk >= thr else "missed by model"
            lvl = "ERROR" if r.risk >= thr else "INFO"
            ev.append((int(r.end_ts), lvl, "Auto-Healing",
                       f"Job {int(r.job_id)} ended {r.state} ({hit}; submission risk {r.risk*100:.0f}%).",
                       {"kind": "job", "id": int(r.job_id)}))
        ev.sort(key=lambda x: x[0], reverse=True)
        rows = [{"ts": e[0], "time": _hms(e[0]), "level": e[1], "module": e[2], "msg": e[3],
                 "entity": e[4]} for e in ev[:int(limit)]]
        return {"virtual_ts": t, "count": len(rows), "logs": rows}

    # -------------------------------------------------- DRILL-DOWN (input / output / why)
    def job_detail(self, job_id: int, policies: dict):
        thr = float(policies["alert_threshold"])
        d = self._df("SELECT * FROM jobs WHERE job_id=?", (int(job_id),))
        if d.empty:
            return None
        r = d.iloc[0]
        def val(f):
            if f not in d.columns:
                return None
            v = r[f]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            return v
        # INPUT -- the real feature values fed to the model at submission
        inp = []
        for f in list(self.p3_input_feats) + ["partition", "qos"]:
            en, zh, grp = P3_LABELS.get(f, (f, f, "request"))
            v = val(f)
            inp.append({"feature": f, "label_en": en, "label_zh": zh, "group": grp,
                        "value": (round(v, 4) if isinstance(v, float) else v),
                        "null": v is None, "imputed": bool(v is None and grp == "history")})
        # OUTPUT
        probs = {"COMPLETED": float(r.p_completed), "FAILED": float(r.p_failed),
                 "TIMEOUT": float(r.p_timeout), "OUT_OF_MEMORY": float(r.p_oom)}
        pred_class = max(probs, key=probs.get); risk = float(r.risk)
        # WHY -- GLOBAL importance (labelled as global, not per-prediction) with this job's values
        why = []
        for it in self.p3_importance:
            f = it["feature"]; en, zh, _ = P3_LABELS.get(f, (f, f, ""))
            v = val(f)
            why.append({"feature": f, "label_en": en, "label_zh": zh, "importance": it["importance"],
                        "value": (round(v, 4) if isinstance(v, float) else v), "null": v is None})
        return {
            "kind": "job", "id": int(job_id), "user": f"user_{int(r.user_id)}", "state": str(r.state),
            "when_en": "Predicted at job submission — uses only information available at submit time.",
            "when_zh": "於任務提交時預測 — 僅使用提交當下可得的資訊。",
            "input": inp,
            "output": {"risk": round(risk, 4), "risk_pct": round(risk * 100, 1),
                       "predicted_class": pred_class,
                       "predicted_class_en": CLASS_LABEL.get(pred_class, (pred_class, ""))[0],
                       "predicted_class_zh": CLASS_LABEL.get(pred_class, ("", pred_class))[1],
                       "pred_type": str(r.pred_type),
                       "class_probs": {k: round(v, 4) for k, v in probs.items()},
                       "threshold": thr, "flagged": bool(risk >= thr)},
            "why": why, "why_kind": "global",
        }

    # -------------------------------------------------- NATIVE-RESOLUTION IPMI TRACE
    # node_raw is an ADDITIVE table written by build_node_raw.py: one row per node per ~20 s, the
    # native ExaData sampling rate, versus node_slots' 15-minute means. It covers only a roster of
    # nodes (those with a recorded onset, plus a couple of baseline nodes) -- see that script for
    # why. Every method here degrades to "no coverage" rather than raising when the table is absent,
    # so a store built before this existed still serves.
    def has_node_raw(self) -> bool:
        try:
            return bool(self._scalar("SELECT COUNT(*) FROM sqlite_master "
                                     "WHERE type='table' AND name='node_raw'"))
        except Exception:
            return False

    def node_raw_nodes(self) -> set:
        """Which nodes have a native-resolution trace at all."""
        if not self.has_node_raw():
            return set()
        try:
            return {int(r[0]) for r in self.db.execute("SELECT DISTINCT node FROM node_raw")}
        except Exception:
            return set()

    def node_raw_trace(self, node_id: int, lo: int, hi: int):
        """Native-resolution rows for one node in [lo, hi]. Empty frame when there is no coverage.

        `ps0_w` is PSU-0 input power, the quantity the P2 triage actually runs on. It arrived after
        the table did, so a store built by the earlier two-metric builder has no such column: it is
        filled with NaN rather than raising, and the renderer then falls back to total power exactly
        as it did before.
        """
        cols = ["ts", "power_w", "temp_c", "ps0_w"]
        if not self.has_node_raw():
            return pd.DataFrame(columns=cols)
        have = self.node_raw_columns()
        select = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
        return self._df(f"SELECT {select} FROM node_raw "
                        "WHERE node=? AND ts>=? AND ts<=? ORDER BY ts",
                        (int(node_id), int(lo), int(hi)))

    def node_raw_columns(self) -> set:
        """Which columns this store's node_raw actually has."""
        if not self.has_node_raw():
            return set()
        try:
            return {str(r[1]) for r in self.db.execute("PRAGMA table_info(node_raw)")}
        except Exception:
            return set()

    def node_detail(self, node_id: int, t: int, policies: dict):
        lo = t - NODE_MAXAGE_H * 3600
        d = self._df("SELECT * FROM node_slots WHERE node=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                     (int(node_id), t, lo))
        if d.empty:
            return None
        r = d.iloc[0]
        cutoff = self.node_cutoff_watts(policies["node_filter_pct"])
        ps = None if pd.isna(r.ps_power) else float(r.ps_power)
        in_scope = (ps is None) or (ps <= cutoff)
        score = float(r.score); crit = int(r.label) == 1
        flagged = in_scope and (score >= self.node_alert)
        state = "CRITICAL" if crit else ("WARNING" if flagged else "HEALTHY")
        # INPUT -- curated real IPMI-derived feature values
        inp = []
        for f, _en, _zh in self.p2_curated:
            v = r[f] if f in d.columns else None
            v = None if (v is None or pd.isna(v)) else float(v)
            en, zh = feature_labels.label(f)     # one resolver for both blocks, so they agree
            inp.append({"feature": f, "label_en": en, "label_zh": zh,
                        "value": (round(v, 3) if v is not None else None), "null": v is None})
        # WHY -- per-prediction LightGBM contributions (signed, log-odds space)
        why = []
        try:
            contrib = json.loads(r.contrib_json)
        except Exception:
            contrib = []
        for idx, value, c in contrib:
            name = self.p2_feats[idx] if 0 <= idx < len(self.p2_feats) else f"f{idx}"
            # Compositional labels cover all 363 features. This used to be a 20-entry dict lookup
            # with the raw identifier as the fallback, so 343 of them rendered as `slope30m_p0P`.
            en, zh = feature_labels.label(name)
            why.append({"feature": name, "label_en": en, "label_zh": zh,
                        "value": round(float(value), 3), "contribution": round(float(c), 4),
                        "direction": "increases" if c > 0 else "decreases"})
        return {
            "kind": "node", "id": int(node_id), "node_label": f"node{int(node_id):04d}", "rack": int(r.rack),
            "ts": int(r.ts), "slot_time": _hms(int(r.ts)),
            "when_en": f"Predicted at the 15-min telemetry slot {_hms(int(r.ts))} — uses only data up to that slot.",
            "when_zh": f"於 15 分鐘遙測時槽 {_hms(int(r.ts))} 預測 — 僅使用該時槽 (含) 之前的資料。",
            "state": state,
            "input": inp,
            "output": {"risk": round(score, 4), "risk_pct": round(score * 100, 1),
                       "threshold": self.node_alert, "in_scope": bool(in_scope),
                       "filter_pct": policies["node_filter_pct"],
                       "cutoff_watts": (None if cutoff == float("inf") else round(cutoff)),
                       "flagged": bool(flagged), "onset": bool(crit)},
            "why": why, "why_kind": "perprediction",
        }


def _hms(ts) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%m-%d %H:%M:%S")
