"""
precompute.py  --  score the historical test period ONCE, offline.

The live API never runs a model. It only reads the SQLite store this script writes.
Everything here is a faithful re-implementation of the two shipped notebooks:

  P3 (jobs)  : P3_02 / P3_04  -- multi-class HistGBT, score = 1 - P(COMPLETED),
               23 features, chronological 70/30 split, EXCL user 393.
               Reproduction target: ROC 0.871 / PR 0.726 @ threshold 0.30.
  P2 (nodes) : P2_09 full-scale LightGBM score (already saved in p09_scores.parquet)
               + P2_10 low-power risk triage (score only the riskiest slots;
               lowest-25% by 6h-mean power -> ROC 0.848 -> 0.926).

Output:  dashboard/data/demo.sqlite   (tables: jobs, node_slots, node_events)
         dashboard/data/model_info.json
         dashboard/data/meta.json

Run:     python precompute.py
"""
import os, json, time, sqlite3, warnings
import duckdb, numpy as np, pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_fscore_support, confusion_matrix

warnings.filterwarnings("ignore")
T0 = time.time()

# ---------------------------------------------------------------- paths / constants
DATA   = "D:/M100/data"                                   # READ-ONLY
P2DIR  = "D:/M100/P2"
HERE   = os.path.dirname(os.path.abspath(__file__))
OUT    = os.path.join(HERE, "data")
os.makedirs(OUT, exist_ok=True)
DBPATH = os.path.join(OUT, "demo.sqlite")

RAW    = DATA + "/raw_2209_extracted/year_month=22-09"
JT     = RAW + "/plugin=job_table/metric=job_info_marconi100/a_0.parquet"
P09_SCORES = P2DIR + "/p09_scores.parquet"
P09_TEST   = P2DIR + "/p09_test.parquet"
P09_TRAIN  = P2DIR + "/p09_train.parquet"
P09_ONSETS = P2DIR + "/p09_onsets.parquet"
P10_FILTER = P2DIR + "/p10_filtercols.parquet"
P09_MODELS = P2DIR + "/p09_models.joblib"        # (models_dict, results); STRICT LightGBM reproduces the saved score
P09_META   = P2DIR + "/p09_meta.json"            # feature order ("all") + feats/neigh split

# curated INPUT features shown in the node drill-down (real p09 feature names -> friendly bilingual label).
# Stored as real columns in node_slots so the panel shows the actual numbers, never a placeholder.
CURATED_NODE = [
    ("cur_g0_cT",       "GPU0 core temp (°C)",        "GPU0 核心溫度 (°C)"),
    ("cur_g1_cT",       "GPU1 core temp (°C)",        "GPU1 核心溫度 (°C)"),
    ("cur_totP",        "Total node power (W)",       "節點總功耗 (W)"),
    ("cur_ps0_inputP",  "PSU-0 input power (W)",      "PSU-0 輸入功率 (W)"),
    ("cur_fan0_0",      "Fan 0 speed (rpm)",          "風扇 0 轉速 (rpm)"),
    ("cur_amb",         "Ambient temp (°C)",          "環境溫度 (°C)"),
    ("m30m_totP",       "Power, 30-min mean (W)",     "功耗 30 分鐘均值 (W)"),
    ("m6h_totP",        "Power, 6-h mean (W)",        "功耗 6 小時均值 (W)"),
    ("slope30m_totP",   "Power, 30-min slope",        "功耗 30 分鐘斜率"),
    ("m30m_g0_cT",      "GPU0 temp, 30-min mean (°C)","GPU0 溫度 30 分鐘均值 (°C)"),
    ("slope30m_g0_cT",  "GPU0 temp, 30-min slope",    "GPU0 溫度 30 分鐘斜率"),
    ("z7d_totP",        "Power z-score vs 7-day",     "功耗 z 分數 (對 7 日基線)"),
    ("z30d_totP",       "Power z-score vs 30-day",    "功耗 z 分數 (對 30 日基線)"),
    ("z7d_g0_cT",       "GPU0 temp z-score vs 7-day", "GPU0 溫度 z 分數 (對 7 日基線)"),
    ("nb_mean_nz_totP", "Rack-neighbour mean power",  "同機櫃鄰居平均功耗"),
    ("nb_mean_nz_g0_cT","Rack-neighbour mean temp",   "同機櫃鄰居平均溫度"),
    ("fleet_anom_frac", "Fleet anomaly fraction",     "全機群異常比例"),
    ("rk_anom_o",       "Rack anomaly rank",          "機櫃異常排名"),
    ("hour",            "Hour of day (UTC)",          "當日時段 (UTC)"),
    ("dow",             "Day of week",                "星期"),
]
CURATED_NODE_FEATS = [f for f, _, _ in CURATED_NODE]

SEED   = 0
TOP    = 393                        # broken auto-resubmit user, excluded from the shipped cohort
OPT    = 0.30                       # P3 recommended WARN operating threshold
FAILY  = "('FAILED','TIMEOUT','OUT_OF_MEMORY','NODE_FAIL')"
CLASSES = ["COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY"]
P3_REF = {"roc": 0.871, "pr": 0.726, "base": 0.222}
P2_REF = {"roc_full": 0.848, "roc_filt25": 0.926}

con = duckdb.connect()
con.execute("PRAGMA threads=6"); con.execute("SET TimeZone='UTC'")
con.execute("SET memory_limit='6GB'"); con.execute("SET preserve_insertion_order=false")
print("duckdb %s | pandas %s | numpy %s" % (duckdb.__version__, pd.__version__, np.__version__))


# ================================================================ PART A -- P3 JOBS
def build_p3():
    print("\n" + "="*70 + "\n[P3] building job feature table (verbatim P3_02/P3_04 SQL)\n" + "="*70)
    con.execute(f'''CREATE OR REPLACE TABLE jobs AS
    SELECT job_id, user_id, group_id, partition, qos, num_nodes, num_cpus, nice,
      CAST(num_cpus AS DOUBLE)/NULLIF(num_nodes,0) cpus_per_node,
      CASE WHEN time_limit_str LIKE '%-%' THEN
            CAST(split_part(time_limit_str,'-',1) AS DOUBLE)*1440
          + CAST(split_part(split_part(time_limit_str,'-',2),':',1) AS DOUBLE)*60
          + CAST(split_part(split_part(time_limit_str,'-',2),':',2) AS DOUBLE)
        WHEN length(time_limit_str)-length(replace(time_limit_str,':',''))=2 THEN
            CAST(split_part(time_limit_str,':',1) AS DOUBLE)*60+CAST(split_part(time_limit_str,':',2) AS DOUBLE)
        ELSE CAST(split_part(time_limit_str,':',1) AS DOUBLE) END req_walltime_min,
      TRY_CAST(regexp_extract(tres_per_node,'gres:gpu:(\\d+)',1) AS DOUBLE) gpus_per_node,
      CASE WHEN dependency    IS NOT NULL THEN 1 ELSE 0 END has_dependency,
      CASE WHEN array_task_id IS NOT NULL THEN 1 ELSE 0 END is_array_task,
      nodes, submit_time AT TIME ZONE 'UTC' submit_utc, start_time AT TIME ZONE 'UTC' start_utc,
      end_time AT TIME ZONE 'UTC' end_utc, date_diff('second',start_time,end_time)/60.0 rt_min,
      job_state, CASE WHEN job_state IN {FAILY} THEN 1 WHEN job_state='COMPLETED' THEN 0 END y_broad
    FROM read_parquet('{JT}')
    WHERE job_state IN ('FAILED','TIMEOUT','OUT_OF_MEMORY','NODE_FAIL','COMPLETED')''')
    con.execute(f'''CREATE OR REPLACE TABLE ev AS
    SELECT user_id, partition, job_id, end_utc,
      CAST(CASE WHEN job_state IN {FAILY} THEN 1 ELSE 0 END AS DOUBLE) f,
      CAST(CASE WHEN job_state='TIMEOUT' THEN 1 ELSE 0 END AS DOUBLE) is_to,
      CASE WHEN req_walltime_min>0 THEN rt_min/req_walltime_min END wt_use,
      row_number() OVER (PARTITION BY user_id ORDER BY end_utc, job_id) rn FROM jobs''')
    con.execute('''CREATE OR REPLACE TABLE evc AS
    SELECT *, sum(f) OVER w cum_f, sum(is_to) OVER w cum_to, sum(wt_use) OVER w cum_wt,
      count(wt_use) OVER w cum_wtn, rn - max(CASE WHEN f=0 THEN rn END) OVER w streak,
      max(CASE WHEN f=1 THEN end_utc END) OVER w last_fail_t
    FROM ev WINDOW w AS (PARTITION BY user_id ORDER BY rn ROWS UNBOUNDED PRECEDING)''')
    con.execute('''CREATE OR REPLACE TABLE h AS
    SELECT j.job_id, e.rn, e.cum_f, e.cum_to, e.cum_wt, e.cum_wtn, e.streak, e.last_fail_t, e.end_utc AS last_job_t
    FROM jobs j ASOF LEFT JOIN evc e ON j.user_id=e.user_id AND j.submit_utc > e.end_utc''')
    for Nw in (10, 50):
        con.execute(f'''CREATE OR REPLACE TABLE lag{Nw} AS SELECT h.job_id, h.cum_f-COALESCE(p.cum_f,0) df,
          h.rn-COALESCE(p.rn,0) dn FROM h JOIN jobs j USING(job_id)
          LEFT JOIN evc p ON p.user_id=j.user_id AND p.rn=h.rn-{Nw}''')
    con.execute('''CREATE OR REPLACE TABLE up AS SELECT j.job_id, e.cf, e.cn FROM jobs j ASOF LEFT JOIN (
      SELECT user_id, partition, end_utc, sum(f) OVER w cf, count(*) OVER w cn FROM ev
      WINDOW w AS (PARTITION BY user_id,partition ORDER BY end_utc,job_id ROWS UNBOUNDED PRECEDING)) e
      ON j.user_id=e.user_id AND j.partition=e.partition AND j.submit_utc>e.end_utc''')
    con.execute('''CREATE OR REPLACE TABLE pr AS SELECT j.job_id, CASE WHEN e.cn>0 THEN e.cf/e.cn END part_prior_fail_rate
      FROM jobs j ASOF LEFT JOIN (SELECT partition, end_utc, sum(f) OVER w cf, count(*) OVER w cn FROM ev
      WINDOW w AS (PARTITION BY partition ORDER BY end_utc,job_id ROWS UNBOUNDED PRECEDING)) e
      ON j.partition=e.partition AND j.submit_utc>e.end_utc''')
    con.execute('''CREATE OR REPLACE TABLE bu AS SELECT a.job_id, (SELECT count(*) FROM jobs b
      WHERE b.user_id=a.user_id AND b.submit_utc<a.submit_utc AND b.submit_utc>=a.submit_utc-INTERVAL 1 HOUR) user_burst_1h
      FROM jobs a''')
    con.execute('''CREATE OR REPLACE TABLE F AS SELECT j.*, COALESCE(h.rn,0) user_prior_n,
      ln(1+COALESCE(h.rn,0)) log_user_prior_n, CASE WHEN h.rn>0 THEN h.cum_f/h.rn END user_prior_fail_rate,
      CASE WHEN h.rn>0 THEN h.cum_to/h.rn END user_prior_timeout_rate,
      CASE WHEN h.cum_wtn>0 THEN h.cum_wt/h.cum_wtn END user_prior_wt_usage,
      COALESCE(h.streak,0) user_fail_streak, date_diff('minute',h.last_fail_t,j.submit_utc) user_min_since_fail,
      date_diff('minute',h.last_job_t,j.submit_utc) user_min_since_job,
      CASE WHEN l10.dn>0 THEN l10.df/l10.dn END user_fail_rate_last10,
      CASE WHEN l50.dn>0 THEN l50.df/l50.dn END user_fail_rate_last50,
      CASE WHEN up.cn>0 THEN up.cf/up.cn END user_part_fail_rate, pr.part_prior_fail_rate, bu.user_burst_1h,
      hour(j.submit_utc) submit_hour, dayofweek(j.submit_utc) submit_dow
      FROM jobs j LEFT JOIN h USING(job_id) LEFT JOIN lag10 l10 USING(job_id) LEFT JOIN lag50 l50 USING(job_id)
      LEFT JOIN up USING(job_id) LEFT JOIN pr USING(job_id) LEFT JOIN bu USING(job_id)''')
    M = con.execute("SELECT * FROM F ORDER BY job_id").df()      # ORDER BY = reproducibility (see notebooks)
    print("  feature table F: %s rows x %d cols" % (f"{len(M):,}", M.shape[1]))

    # cohort / chronological split / clock-check -> feature lists (verbatim)
    E = M[(M.y_broad.notna()) & (M.user_id != TOP)].copy(); E["y"] = E.y_broad.astype(int)
    E["partition"] = E["partition"].astype(str); E["qos"] = E["qos"].astype(str)
    tsort = np.sort(E.submit_utc.values); CUT = tsort[int(len(tsort)*0.70)]
    tr0, te0 = E[E.submit_utc < CUT], E[E.submit_utc >= CUT]
    tnum = pd.to_datetime(E.submit_utc).astype("int64").values.astype(float)
    P301_HIST = ["user_prior_fail_rate","log_user_prior_n","part_prior_fail_rate","user_burst_1h"]
    CAND = ["user_prior_timeout_rate","user_prior_wt_usage","user_fail_streak","user_min_since_fail",
            "user_min_since_job","user_fail_rate_last10","user_fail_rate_last50","user_part_fail_rate"]
    drop = []
    for f in P301_HIST + CAND:
        v = E[f].values.astype(float); ok = ~np.isnan(v)
        if (abs(np.corrcoef(v[ok],tnum[ok])[0,1])>0.5) or (100*(te0[f]>tr0[f].max()).mean()>50): drop.append(f)
    BASE_NUM = ["num_nodes","num_cpus","cpus_per_node","req_walltime_min","gpus_per_node","nice",
                "has_dependency","is_array_task","submit_hour","submit_dow"]
    BASE_CAT = ["partition","qos"]
    NEW_HIST  = [f for f in CAND if f not in drop]
    PROD_HIST = [f for f in ["user_prior_fail_rate","log_user_prior_n","user_burst_1h"] + NEW_HIST if f not in drop]
    FEATS23_NUM = BASE_NUM + PROD_HIST
    FEATS23     = FEATS23_NUM + BASE_CAT
    print("  clock-dropped: %s | Tier-2 features: %d (%d num + %d cat)"
          % (drop or "none", len(FEATS23), len(FEATS23_NUM), len(BASE_CAT)))

    # train multi-class on the training split, score the test split
    def make_pre(num, cat):
        steps = [("n", Pipeline([("i",SimpleImputer(strategy="median")),("s",StandardScaler())]), num)]
        steps.append(("c", Pipeline([("i",SimpleImputer(strategy="most_frequent")),
            ("o",OneHotEncoder(handle_unknown="ignore",min_frequency=20,sparse_output=False))]), cat))
        return ColumnTransformer(steps)
    MC = E[E.job_state.isin(CLASSES)].copy()
    mtr, mte = MC[MC.submit_utc<CUT].copy(), MC[MC.submit_utc>=CUT].copy()
    print("  train %s / test %s multi-class jobs -- fitting HistGBT..." % (f"{len(mtr):,}", f"{len(mte):,}"))
    pipe = Pipeline([("p",make_pre(FEATS23_NUM,BASE_CAT)),
                     ("m",HistGradientBoostingClassifier(random_state=SEED))]).fit(mtr[FEATS23], mtr.job_state)
    cls = list(pipe.named_steps["m"].classes_)
    proba = pipe.predict_proba(mte[FEATS23])
    pidx = {c: cls.index(c) for c in cls}
    s2 = 1.0 - proba[:, pidx["COMPLETED"]]
    mte = mte.assign(
        risk=s2,
        p_completed=proba[:, pidx["COMPLETED"]],
        p_failed=proba[:, pidx.get("FAILED", 0)] if "FAILED" in pidx else 0.0,
        p_timeout=proba[:, pidx.get("TIMEOUT", 0)] if "TIMEOUT" in pidx else 0.0,
        p_oom=proba[:, pidx.get("OUT_OF_MEMORY", 0)] if "OUT_OF_MEMORY" in pidx else 0.0,
    )
    # reproduction assertions
    roc = roc_auc_score(mte.y.values, s2); pr = average_precision_score(mte.y.values, s2)
    yh = (s2 >= OPT).astype(int)
    prec, rec, f1, _ = precision_recall_fscore_support(mte.y.values, yh, average="binary", zero_division=0)
    print("  REPRODUCTION: ROC %.4f (target %.3f) | PR %.4f (target %.3f) | base %.4f"
          % (roc, P3_REF["roc"], pr, P3_REF["pr"], mte.y.mean()))
    print("    @0.30 -> precision %.3f recall %.3f F1 %.3f" % (prec, rec, f1))
    assert abs(roc-P3_REF["roc"])<0.012 and abs(pr-P3_REF["pr"])<0.012, "P3 reproduction drifted -- STOP"
    print("  --> P3 reproduction holds.")

    # predicted failure TYPE = argmax over the three failure classes (even when overall argmax is COMPLETED)
    fail_cols = [("FAILED","p_failed"),("TIMEOUT","p_timeout"),("OUT_OF_MEMORY","p_oom")]
    fp = mte[[c for _,c in fail_cols]].values
    mte["pred_type"] = [fail_cols[i][0] for i in fp.argmax(axis=1)]

    def to_ts(s):  # unit-proof -> unix seconds UTC (duckdb hands back datetime64[us], not [ns])
        return ((pd.to_datetime(s, utc=True) - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta("1s")).astype("int64")
    out = pd.DataFrame({
        "job_id": mte.job_id.astype("int64"),
        "user_id": mte.user_id.astype("int64"),
        "partition": mte.partition.astype(str),
        "qos": mte.qos.astype(str),
        "num_nodes": mte.num_nodes.astype("int64"),
        "num_cpus": mte.num_cpus.astype("int64"),
        "gpus_per_node": mte.gpus_per_node.astype(float),
        "req_walltime_min": mte.req_walltime_min.astype(float),
        "submit_ts": to_ts(mte.submit_utc),
        "start_ts": to_ts(mte.start_utc),
        "end_ts": to_ts(mte.end_utc),
        "rt_min": mte.rt_min.astype(float),
        "state": mte.job_state.astype(str),
        "y": mte.y.astype("int64"),
        "risk": mte.risk.astype(float),
        "p_completed": mte.p_completed.astype(float),
        "p_failed": mte.p_failed.astype(float),
        "p_timeout": mte.p_timeout.astype(float),
        "p_oom": mte.p_oom.astype(float),
        "pred_type": mte.pred_type.astype(str),
    })
    # --- store the REAL input feature values for the drill-down (null preserved -> "imputed" in UI) ---
    P3_INPUT_FEATS = FEATS23_NUM + ["user_prior_n"]      # 21 numeric model inputs + raw prior-job count
    for f in P3_INPUT_FEATS:
        out[f] = pd.to_numeric(mte[f], errors="coerce").astype(float).values

    # --- P3 global feature importance (permutation on the multi-class pipe; labelled GLOBAL, not per-row) ---
    print("  computing P3 global permutation importance (drill-down WHY)...")
    ci = cls.index("COMPLETED")
    Xte = mte[FEATS23]
    base_auc = roc_auc_score(mte.y.values, 1.0 - pipe.predict_proba(Xte)[:, ci])
    rng = np.random.default_rng(SEED); imp = {}
    for f in FEATS23:
        drops = []
        for _ in range(3):
            Xs = Xte.copy(); Xs[f] = rng.permutation(Xs[f].values)
            drops.append(base_auc - roc_auc_score(mte.y.values, 1.0 - pipe.predict_proba(Xs)[:, ci]))
        imp[f] = float(np.mean(drops))
    p3_importance = [{"feature": k, "importance": round(v, 4)}
                     for k, v in sorted(imp.items(), key=lambda kv: -kv[1])[:15]]
    print("    top P3 drivers:", ", ".join(d["feature"] for d in p3_importance[:6]))

    meta = dict(cut_ts=int(pd.Timestamp(CUT).timestamp()),
                p3_roc=float(roc), p3_pr=float(pr), p3_base=float(mte.y.mean()),
                p3_prec=float(prec), p3_rec=float(rec), p3_f1=float(f1),
                p3_n_train=int(len(mtr)), p3_n_test=int(len(mte)),
                p3_features=FEATS23, p3_n_features=len(FEATS23),
                p3_input_feats=P3_INPUT_FEATS, p3_global_importance=p3_importance,
                p3_classes=list(cls),
                submit_min=int(out.submit_ts.min()), submit_max=int(out.submit_ts.max()),
                start_min=int(out.start_ts.min()), end_max=int(out.end_ts.max()))
    return out, meta


# ================================================================ PART B -- P2 NODES
# The low-power risk triage (P2_10 condition D2): the single strongest pre-t indicator is
# cur_ps0_inputP (PSU-0 input power; oriented AUC 0.752, "low = risk"). The percentile cutoffs
# are derived on the TRAINING period and applied to test -- the honest protocol -- which is why
# the shipped 25% row keeps ~13% of test rows and lands at ROC 0.926.
FILTER_LEVELS = [1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100]

def build_p2(window_start_ts):
    print("\n" + "="*70 + "\n[P2] node scores + real IPMI sensors + low-power risk triage\n" + "="*70)
    SCORE = "s_combined_30_STRICT_LightGBM"; LAB = "y_combined_30"; IND = "cur_ps0_inputP"
    # -- cutoffs from TRAIN (thresholds never see the test period) --
    print("  deriving PSU-0 power cutoffs on the training period...")
    ps = [l/100.0 for l in FILTER_LEVELS]
    trq = con.execute(f"SELECT quantile_cont({IND}, {ps}) q FROM read_parquet('{P09_TRAIN}') "
                      f"WHERE {IND} IS NOT NULL").df().q[0]
    cutoffs = {str(l): float(q) for l, q in zip(FILTER_LEVELS, trq)}
    # -- full-pop sanity: reproduce ROC 0.848 (full) and ~0.926 (lowest-25% by PSU-0 power) --
    print("  full-population reproduction (scans p09_scores + p10_filtercols)...")
    fp = con.execute(f"""
        SELECT s.{LAB} AS y, s.{SCORE} AS sc, f.{IND} AS pw
        FROM read_parquet('{P09_SCORES}') s
        JOIN read_parquet('{P10_FILTER}') f ON f.node=s.node AND f.ep=epoch(s.ts)
        WHERE s.{SCORE} IS NOT NULL AND s.{LAB} IS NOT NULL
    """).df()
    roc_full = roc_auc_score(fp.y, fp.sc)
    m = (fp.pw.values <= cutoffs["25"]) & (~np.isnan(fp.pw.values))
    roc_25 = roc_auc_score(fp.y.values[m], fp.sc.values[m])
    print("  P2 ROC full-pop      : %.4f  (target %.3f)" % (roc_full, P2_REF["roc_full"]))
    print("  P2 ROC riskiest-25%%  : %.4f  (target %.3f)  keep PSU-0<=%.0fW  (%.1f%% of test rows)"
          % (roc_25, P2_REF["roc_filt25"], cutoffs["25"], 100*m.mean()))
    assert abs(roc_full-P2_REF["roc_full"])<0.02, "P2 full-pop ROC drifted"
    assert abs(roc_25-P2_REF["roc_filt25"])<0.02, "P2 risk-filtered ROC drifted"
    del fp; util_ref = None                                  # util reference computed from window slots below

    # -- windowed slots: pull the FULL 363-feature matrix so we can (a) show real inputs and
    #    (b) compute honest per-prediction contributions from the exact model that made the score --
    import joblib
    ALLF = json.load(open(P09_META))["all"]                 # positional feature order (model has generic names)
    mdl = joblib.load(P09_MODELS)[0][("combined", 30, "STRICT", "LightGBM")]
    assert mdl.n_features_in_ == len(ALLF) == 363
    fcols = ", ".join(f't."{c}"' for c in ALLF)
    print("  extracting windowed node-slots + 363 features from p09_test (one scan)...")
    W = int(window_start_ts)
    df = con.execute(f"""
        SELECT t.node::INTEGER AS node, s.rack::INTEGER AS rack, epoch(t.ts)::BIGINT AS ts,
               s.{SCORE} AS score, s.{LAB} AS label, {fcols}
        FROM read_parquet('{P09_TEST}') t
        JOIN read_parquet('{P09_SCORES}') s ON s.node=t.node AND s.ts=t.ts
        WHERE epoch(t.ts) >= {W} AND s.{SCORE} IS NOT NULL
    """).df()
    n = len(df)
    print("  node-slots in window: %s rows | %d nodes | ts %s..%s"
          % (f"{n:,}", df.node.nunique(),
             pd.to_datetime(df.ts.min(), unit="s"), pd.to_datetime(df.ts.max(), unit="s")))

    X = df[ALLF].to_numpy(np.float32)
    # honesty gate: the loaded model must reproduce the stored score on these rows
    smp = np.random.default_rng(0).choice(n, min(5000, n), replace=False)
    rep = np.abs(mdl.predict_proba(X[smp])[:, 1] - df.score.values[smp]).max()
    print("  model<->stored score check: max|diff|=%.2e  -> %s" % (rep, "MATCH" if rep < 1e-5 else "MISMATCH"))
    assert rep < 1e-5, "P2 model does not reproduce the saved score -- contributions would be dishonest"

    # per-prediction contributions (LightGBM pred_contrib; log-odds space, last col = bias)
    print("  computing per-slot feature contributions (pred_contrib)...")
    contrib = mdl.booster_.predict(X, pred_contrib=True).astype(np.float32)   # n x 364
    C = contrib[:, :-1]
    K = 8
    absC = np.abs(C)
    part = np.argpartition(-absC, K, axis=1)[:, :K]                 # top-K unordered
    ri = np.arange(n)[:, None]
    part = np.take_along_axis(part, np.argsort(-absC[ri, part], axis=1), axis=1)  # sort within
    tv = np.round(X[ri, part], 4); tc = np.round(C[ri, part], 4); ti = part.astype(int)
    contrib_json = [json.dumps([[int(ti[r, k]), float(tv[r, k]), float(tc[r, k])] for k in range(K)])
                    for r in range(n)]

    # display columns + curated INPUT feature columns (real values, stored as columns)
    temp = np.maximum(df.cur_g0_cT.fillna(0).values, df.cur_g1_cT.fillna(0).values)
    temp[temp == 0] = np.nan
    slots = pd.DataFrame({
        "node": df.node.values, "rack": df.rack.values, "ts": df.ts.values,
        "temp": temp, "power": df.cur_totP.values, "fan": df.cur_fan0_0.values,
        "ambient": df.cur_amb.values, "ps_power": df.cur_ps0_inputP.values,
        "m6h_power": df.m6h_totP.values, "score": df.score.values, "label": df.label.values,
        "contrib_json": contrib_json,
    })
    for f in CURATED_NODE_FEATS:                          # real input values for the drill-down
        slots[f] = df[f].astype(float).values
    util_ref = float(np.nanpercentile(slots["power"].values, 99))   # proxy 100%-load reference
    del df, X, contrib, C, absC
    return slots, cutoffs, float(roc_full), float(roc_25), util_ref


# ================================================================ PART C -- EVENTS
def build_events(window_start_ts):
    print("\n[events] real anomaly onsets in the window (for the log wall)")
    W = int(window_start_ts)
    ev = con.execute(f"""
        SELECT rack::INTEGER rack, node::INTEGER node, epoch(ts)::BIGINT ts, kind
        FROM read_parquet('{P09_ONSETS}') WHERE epoch(ts) >= {W} ORDER BY ts
    """).df()
    print("  onset events in window: %s" % f"{len(ev):,}")
    return ev


# ================================================================ WRITE SQLITE
def write_db(jobs, slots, events):
    print("\n[write] -> %s" % DBPATH)
    if os.path.exists(DBPATH): os.remove(DBPATH)
    cx = sqlite3.connect(DBPATH)
    jobs.to_sql("jobs", cx, index=False)
    slots.to_sql("node_slots", cx, index=False)
    events.to_sql("node_events", cx, index=False)
    cur = cx.cursor()
    cur.executescript("""
      CREATE INDEX ix_jobs_submit ON jobs(submit_ts);
      CREATE INDEX ix_jobs_end    ON jobs(end_ts);
      CREATE INDEX ix_slots_ts    ON node_slots(ts);
      CREATE INDEX ix_slots_node  ON node_slots(node, ts);
      CREATE INDEX ix_ev_ts       ON node_events(ts);
    """)
    cx.commit(); cx.close()
    print("  jobs=%s  node_slots=%s  node_events=%s" %
          (f"{len(jobs):,}", f"{len(slots):,}", f"{len(events):,}"))


# ================================================================ MAIN
if __name__ == "__main__":
    jobs, jmeta = build_p3()
    window_start = jmeta["start_min"]                       # replay window opens at first test-job start
    node_from = window_start - 3600                          # 1h of node context before the first job
    slots, cutoffs, roc_full, roc_25, util_ref = build_p2(node_from)
    events = build_events(node_from)
    write_db(jobs, slots, events)

    window_end = max(jmeta["end_max"], int(slots.ts.max()))
    model_info = {
      "models": [
        {"id": "P3", "name": "Job-outcome model (SLURM)",
         "algorithm": "HistGradientBoosting, multi-class -> binary (1 - P(COMPLETED))",
         "task": "Predict at submission whether a job ends FAILED / TIMEOUT / OUT_OF_MEMORY vs COMPLETED",
         "n_features": jmeta["p3_n_features"], "operating_threshold": OPT,
         "metrics": {"ROC_AUC": round(jmeta["p3_roc"],3), "PR_AUC": round(jmeta["p3_pr"],3),
                     "precision@0.30": round(jmeta["p3_prec"],3), "recall@0.30": round(jmeta["p3_rec"],3),
                     "F1@0.30": round(jmeta["p3_f1"],3), "base_rate": round(jmeta["p3_base"],3)},
         "training_window": "job_table 22-09; chronological 70/30 split; train ends 2022-09-19",
         "n_train": jmeta["p3_n_train"], "n_test": jmeta["p3_n_test"],
         "prediction_time": "at job submission -- uses only information available at submit_time",
         "attribution": "global permutation importance (per-prediction attribution not cheaply available for HistGBT)",
         "notes": "Excludes user 393 (broken auto-resubmit script, 45.8% of raw jobs). OOM is under-predicted: "
                  "SLURM memory-request columns are null in this dump, so the model rarely fires the OOM class."},
        {"id": "P2", "name": "Node-anomaly model (IPMI)",
         "algorithm": "LightGBM, 363 features, 30-minute horizon",
         "task": "Predict a node monitoring-anomaly onset within 30 minutes from IPMI telemetry",
         "n_features": 363, "operating_mode": "low-power risk triage (score only the riskiest ~25% of slots)",
         "metrics": {"ROC_AUC_full_population": round(roc_full,3),
                     "ROC_AUC_risk_filtered_25pct": round(roc_25,3)},
         "training_window": "aggregated IPMI, all 831 scored nodes; test period after 2022-04-06",
         "prediction_time": "at each 15-minute telemetry slot -- uses only data up to that slot",
         "attribution": "per-prediction LightGBM feature contributions (pred_contrib, log-odds space)",
         "notes": "Low power = risk here: a node about to trip a monitoring anomaly is going idle/dropping out, "
                  "not overheating. The triage keeps the lowest-power slots; recall is essentially unchanged."}
      ],
      "methodology": "Models were trained offline in the analysis notebooks. precompute.py scored the historical "
                     "test period ONCE and stored the results (with each row's real input features and, for the "
                     "node model, its per-prediction feature contributions). The dashboard replays those stored "
                     "predictions against a virtual clock -- no training or inference runs while the site is live, "
                     "and every prediction shown used only data available at or before its own prediction time.",
      "fields_not_in_dataset": [
        {"field": "VRAM usage",     "reason": "requires NVIDIA DCGM per-GPU memory telemetry; ExaData carries only IPMI + Ganglia"},
        {"field": "PCIe error rate","reason": "requires DCGM/NVML XID + PCIe replay counters; not collected in ExaData"},
        {"field": "MIG status",     "reason": "M100 GPUs are V100 (no MIG); MIG is an A100/H100 feature"},
      ],
      "proxied_fields": [
        {"field": "Utilisation", "basis": "derived from node total power draw vs a p99 6h-mean reference (%.0f W)" % util_ref,
         "reason": "no direct GPU-utilisation channel joined into the node model; power is a monotone proxy"}
      ],
      "hardware_note": "Marconi100 nodes are IBM Power9 AC922 with 4x NVIDIA V100 (SXM2), NOT NVIDIA DGX. "
                       "'DCGM/DGX/MIG' labels in the mock UI are kept for layout but marked N/A where the data cannot fill them.",
      "dataset": "CINECA Marconi100 ExaData (public HPC telemetry, ~980 nodes)"
    }
    p2_feats = json.load(open(P09_META))["all"]             # 363-feature order (to resolve contrib indices -> names)
    meta = {
      "window_start_ts": int(window_start), "window_end_ts": int(window_end),
      "window_start_iso": pd.to_datetime(window_start, unit="s").isoformat()+"Z",
      "window_end_iso": pd.to_datetime(window_end, unit="s").isoformat()+"Z",
      "p2_power_indicator": "cur_ps0_inputP (PSU-0 input power at t, watts); low power = risk",
      "p2_power_cutoffs_watts": cutoffs, "util_ref_watts": util_ref,
      "p2_filter_note": "cutoffs derived on the training period and applied to test (D2 protocol); "
                        "riskiest 25% keeps PSU-0 <= ~286W and lifts ROC 0.848 -> ~0.926 at ~unchanged onset recall",
      # ---- drill-down (input/output/why) support ----
      "p2_feats": p2_feats,                                 # index -> feature name for stored contributions
      "p2_curated": CURATED_NODE,                           # [feat, en_label, zh_label] shown in the INPUT block
      "p2_node_alert_score": 0.50,                          # score at/above which an in-scope node is flagged
      "p3_input_feats": jmeta["p3_input_feats"],
      "p3_global_importance": jmeta["p3_global_importance"],
      "p3_classes": jmeta["p3_classes"],
      "default_policies": {
        "alert_threshold": OPT,          # P3 job risk threshold (0..1) -> which jobs are flagged
        "node_filter_pct": 25,           # P2 low-power triage strength: score the riskiest N% of slots
        "temp_isolate_c": 92,            # node auto-DRAIN temperature limit (policy display)
        "checkpoint_min": 15,            # global checkpoint interval
        "migrate_strategy": "auto_healthy_node",
        "oom_guard_enabled": True, "node_fail_guard_enabled": True
      }
    }
    json.dump(model_info, open(os.path.join(OUT,"model_info.json"),"w"), indent=2)
    json.dump(meta, open(os.path.join(OUT,"meta.json"),"w"), indent=2)
    print("\nwrote model_info.json + meta.json")
    print("replay window: %s -> %s" % (meta["window_start_iso"], meta["window_end_iso"]))
    print("\nDONE in %.0fs" % (time.time()-T0))
