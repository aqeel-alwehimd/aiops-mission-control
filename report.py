"""
report.py -- auto-generated operations report for the dashboard.

Strict split (mandatory):
  1. FACTS are computed in Python from data/demo.sqlite (assemble_facts). Only real values.
  2. The LLM only NARRATES those facts (generate_llm) -- it may invent nothing, and its output
     is numerically validated against the facts; any number not traceable to the facts causes a
     fall-back to the deterministic template.
  3. TEMPLATE fallback (render_template) produces the same report from the identical facts with no
     LLM at all, so the feature works fully offline (no ANTHROPIC_API_KEY / no SDK).

No action is ever taken on infrastructure -- the report only recommends, for a human to approve.
The API key is read from the ANTHROPIC_API_KEY environment variable, never hardcoded, and every
LLM call happens server-side.
"""
import os, re, json, time, random, threading, datetime, requests

import charts as chartreg
from charts import ChartId

# ---------------- tunables ----------------
NODE_WATCH   = 0.30                                   # in-scope P2 score >= this = "on watch"
MAX_EX       = 4                                      # example IDs per outcome bucket
MAX_CHARTS   = 4                                      # cap on how many charts the AGENT may select

# Total wall-clock a single /api/report may spend on outbound agent calls. The narrator owns most of
# it (its own deadline is 150s); whatever is left is what the advisory auditor may use, and if that
# is too little the audit is skipped with a flag rather than extending a request the user is waiting
# on. Measured on this endpoint: a successful narration takes 58-61s, a successful audit 19-35s.
REPORT_TIME_BUDGET = 210.0

def audit_enabled() -> bool:
    """The auditor can be switched off entirely without unsetting its credentials.
    Defaults ON; set REPORT_AUDIT=0 to disable (used by the verification scripts)."""
    return str(os.environ.get("REPORT_AUDIT", "1")).strip().lower() not in ("0", "false", "no", "off")
REPORT_MODEL = os.environ.get("REPORT_MODEL", "claude-haiku-4-5-20251001")
CACHE_TTL    = 120                                    # real seconds an entry stays valid
CACHE_BUCKET = 900                                    # virtual seconds per cache bucket (15 virtual min)
CACHE_MAX    = 256

def _iso(ts): return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z"
def _hm(ts):  return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%m-%d %H:%M")

def _job_concentration(jobs: list) -> dict:
    """How many distinct users own the high-risk job list, and the largest single share.

    A ranking of six jobs that all belong to one user, all at the same risk, is not a ranking -- it
    is one user's batch. That is the fact worth narrating, so it is computed here rather than left
    for a reader to infer from six near-identical bars.
    """
    users = [str(j.get("user")) for j in (jobs or []) if isinstance(j, dict) and j.get("user")]
    if not users:
        return {"jobs": 0, "distinct_users": 0, "top_user": None,
                "top_user_jobs": 0, "top_user_share_pct": None}
    counts = {}
    for u in users:
        counts[u] = counts.get(u, 0) + 1
    top_user, top_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return {"jobs": len(users), "distinct_users": len(counts), "top_user": top_user,
            "top_user_jobs": top_n, "top_user_share_pct": round(100.0 * top_n / len(users), 1)}


# ============================================================ 1. FACTS (pure Python)
#
# COHORTS -- read this before touching any count below.
#
# Two different questions live in this window, and they have different answers because they select
# different jobs. Conflating them is what produced the inconsistency this section now prevents:
# a report that said 612 jobs flagged / 406 correctly warned / 100 false alarms, where 406+100=506.
#
#   cohort S ("submitted")  jobs whose SUBMIT time falls in the window. This is the cohort P3 was
#                           asked to judge during this shift, and it is the ONLY cohort the
#                           prediction-outcome scoring uses. Some of its jobs have not ended yet at
#                           the virtual time t, so they have no outcome: they are pending, and they
#                           are counted explicitly rather than silently dropped.
#   cohort E ("ended")      jobs whose END time falls in the window, whenever they were submitted.
#                           This answers "what finished on my shift" and drives the volume counts
#                           and the job-outcome mix.
#
# Neither cohort contains the other. A long job submitted before the window and ended inside it is
# in E but not S; a job submitted inside the window and still running at t is in S but not E.
# Key names carry the cohort ("flagged_at_submission", "ended_in_window_*") so a reader can never
# mistake one for the other.
def assemble_facts(store, t: int, policies: dict, window_h: float = 6) -> dict:
    import pandas as pd
    thr = float(policies["alert_threshold"]); pct = float(policies["node_filter_pct"])
    lo = int(t - window_h * 3600)
    cutoff = store.node_cutoff_watts(pct)
    summ = store.summary(t, policies)

    def exlist(df):
        df = df.sort_values("risk", ascending=False).head(MAX_EX)
        return [{"job_id": int(r.job_id), "user": f"user_{int(r.user_id)}", "state": str(r.state),
                 "risk_pct": round(float(r.risk) * 100, 1)} for r in df.itertuples(index=False)]

    def exlist_inflight(df):
        """Examples for jobs that have NOT ended at t. Their `state` column holds the eventual SLURM
        outcome, which is a FUTURE fact at the virtual time -- publishing it would let the narration
        announce how a still-running job turns out. Only the observable status is emitted."""
        df = df.sort_values("risk", ascending=False).head(MAX_EX)
        return [{"job_id": int(r.job_id), "user": f"user_{int(r.user_id)}",
                 "status": ("RUNNING" if int(r.start_ts) <= t else "PENDING"),
                 "risk_pct": round(float(r.risk) * 100, 1)} for r in df.itertuples(index=False)]

    # ---- cohort S: submitted in the window. Everything scored below comes from this frame. -------
    sub = store._df("SELECT job_id,user_id,state,risk,pred_type,submit_ts,start_ts,end_ts "
                    "FROM jobs WHERE submit_ts>? AND submit_ts<=?", (lo, t))
    submitted = len(sub)
    resolved_mask = (sub.end_ts <= t) if submitted else sub          # outcome known at the virtual time
    sub_done  = sub[resolved_mask] if submitted else sub
    flagged_df = sub[sub.risk >= thr] if submitted else sub
    flagged = len(flagged_df)

    # the flagged cohort partitioned by outcome -- these three are mutually exclusive and, by
    # construction, exhaustive: correct + false_alarm + pending == flagged, always.
    fl_done = flagged_df[flagged_df.end_ts <= t] if flagged else flagged_df
    correct = fl_done[fl_done.state != "COMPLETED"] if len(fl_done) else fl_done
    falarms = fl_done[fl_done.state == "COMPLETED"] if len(fl_done) else fl_done
    pending = flagged_df[flagged_df.end_ts > t] if flagged else flagged_df
    # misses live in the same cohort but outside the flagged set: it failed and P3 did not warn.
    misses  = sub_done[(sub_done.state != "COMPLETED") & (sub_done.risk < thr)] if len(sub_done) else sub_done

    nfail = len(correct) + len(misses)               # failures from cohort S that have ENDED by t
    catch = round(100 * len(correct) / nfail, 1) if nfail else None

    # ---- cohort E: ended in the window, whenever submitted. Volume only -- never scored. ---------
    ended = store._df("SELECT job_id,user_id,state,risk,pred_type,end_ts FROM jobs WHERE end_ts>? AND end_ts<=?", (lo, t))
    n = len(ended)
    ef = int((ended.state == "FAILED").sum()) if n else 0
    et = int((ended.state == "TIMEOUT").sum()) if n else 0
    eo = int((ended.state == "OUT_OF_MEMORY").sum()) if n else 0
    ecp = int((ended.state == "COMPLETED").sum()) if n else 0

    ons = store._df("SELECT node,rack,ts,kind FROM node_events WHERE ts>? AND ts<=? ORDER BY ts", (lo, t))
    onset_events = [{"node": int(r.node), "rack": int(r.rack), "time": _hm(int(r.ts)), "kind": str(r.kind)}
                    for r in ons.head(8).itertuples(index=False)]

    nd = store._classify_nodes(t, policies)
    high_nodes = []
    if len(nd):
        cand = nd[(nd.scored) & (nd.score >= NODE_WATCH)].sort_values("score", ascending=False).head(6)
        if not len(cand):
            cand = nd[nd.scored].sort_values("score", ascending=False).head(3)
        for r in cand.itertuples(index=False):
            high_nodes.append({"node": int(r.node), "rack": int(r.rack), "risk_pct": round(float(r.score) * 100, 1),
                "state": str(r.state), "onset": bool(r.label == 1),
                "temp_c": (None if pd.isna(r.temp) else round(float(r.temp), 1)),
                "power_w": (None if pd.isna(r.power) else round(float(r.power))),
                "fan_rpm": (None if pd.isna(r.fan) else round(float(r.fan))),
                "psu0_w": (None if pd.isna(r.ps_power) else round(float(r.ps_power)))})

    board = store._job_board(t)
    high_jobs = []
    if len(board):
        act = board[board.active].sort_values("risk", ascending=False).head(6)
        for r in act.itertuples(index=False):
            high_jobs.append({"job_id": int(r.job_id), "user": f"user_{int(r.user_id)}",
                "risk_pct": round(float(r.risk) * 100, 1), "pred_type": str(r.pred_type), "status": str(r.status)})

    return {
      "now_iso": _iso(t),
      "window": {"hours": (int(window_h) if float(window_h).is_integer() else window_h),
                 "start_ts": lo, "end_ts": int(t), "start_iso": _iso(lo), "end_iso": _iso(t)},
      "settings": {"p3_alert_threshold": thr, "p2_triage_pct": (int(pct) if float(pct).is_integer() else pct),
                   "p2_cutoff_watts": (None if cutoff == float("inf") else round(cutoff)),
                   "p2_node_alert_score": store.node_alert},
      "cluster_now": {"active_jobs": summ["active_jobs"], "running": summ["running"], "pending": summ["pending"],
                      "nodes_total": summ["nodes_total"], "nodes_anomalous": summ["nodes_anomalous"],
                      "nodes_at_risk": summ["nodes_at_risk"], "nodes_scored": summ["nodes_scored"],
                      "utilisation_pct": summ["utilisation_pct"]},
      "jobs_window": {
         # cohort S -- submitted inside the window (the cohort P3 was asked to judge)
         "submitted": int(submitted),
         "flagged_at_submission": int(flagged),
         "submitted_outcome_known": int(len(sub_done)),
         "submitted_still_running": int(submitted - len(sub_done)),
         # cohort E -- ended inside the window, whenever they were submitted (volume only)
         "ended_in_window": n,
         "ended_in_window_failed": ef, "ended_in_window_timeout": et,
         "ended_in_window_oom": eo, "ended_in_window_completed": ecp,
         "cohort_note_en": "Submitted counts and ended counts describe different job sets: a job "
                           "submitted before the window can end inside it, and a job submitted "
                           "inside it can still be running.",
         "cohort_note_zh": "「提交」與「結束」統計的是不同的任務集合：視窗前提交的任務可能在視窗內結束，"
                           "而視窗內提交的任務也可能仍在執行。",
      },
      "prediction_outcomes": {
         # ONE cohort throughout: jobs submitted in the window. The identity below always holds, so
         # the three flagged buckets can be read as a partition of flagged_total.
         #   correct_warnings + false_alarms + pending_outcome == flagged_total
         #   correct_warnings + misses                        == failures_resolved
         "cohort_en": "Jobs submitted in this window. Jobs that had not ended at the report time are "
                      "counted as pending, not as errors.",
         "cohort_zh": "統計對象為本視窗內提交的任務。報告時間尚未結束的任務計為「待定」，不計為誤判。",
         "flagged_total": int(flagged),
         "failures_resolved": nfail, "catch_rate_pct": catch,
         "correct_warnings": {"count": len(correct), "examples": exlist(correct)},
         "false_alarms":     {"count": len(falarms), "examples": exlist(falarms)},
         "pending_outcome":  {"count": len(pending), "examples": exlist_inflight(pending)},
         "misses":           {"count": len(misses),  "examples": exlist(misses)},
      },
      "node_onsets": {"count": int(len(ons)), "events": onset_events},
      "high_risk_nodes": high_nodes,
      "high_risk_jobs": high_jobs,
      # Who owns the watch list. The top in-flight jobs routinely come out at the same risk to one
      # decimal place, so the interesting fact is not their ranking but their CONCENTRATION -- in
      # practice one user's batch often fills the entire list. Recording it as a fact lets the agent
      # narrate it and lets the template caption state it, both inside the numeric gate.
      "high_risk_jobs_concentration": _job_concentration(high_jobs),
      "watch_counts": {"nodes": len(high_nodes), "jobs": len(high_jobs)},
      "model_note": {
         "p3_threshold": thr, "p2_triage_pct": (int(pct) if float(pct).is_integer() else pct),
         "caveat_en": "OOM is under-predicted (SLURM memory-request columns are null in this dataset), so OOM is rarely the top predicted failure type; this is a replay of one Sept-2022 test slice.",
         "caveat_zh": "OOM 為低估項 (此資料集 SLURM 記憶體請求欄位為空)，故 OOM 鮮少被預測為首要故障型態；此為 2022-09 測試期單一片段的回放。",
      },
    }


# ============================================================ 1b. THE COHORT MODEL
#
# ONE definition of which quantity counts members of which set, and of the identities and
# non-containments that hold between them. It has three consumers, and that is the whole point:
#
#   * cohort_prose()            renders it as English prose for the AUDITOR PROMPT, so the second
#                               agent is told the relationships instead of being handed a bare JSON
#                               dump and expected to infer them;
#   * cohort_containment_review() resolves a number in the narration back to its SET, so a false
#                               containment can be caught in deterministic Python;
#   * verify_cohort.py          asserts every identity below against the real store, at every
#                               sample point.
#
# Sourcing all three from here is what stops the prompt's claims and the test's assertions drifting
# apart. Add a bucket to assemble_facts and it must be registered here or verify_cohort.py fails.
#
# The failure class this exists for: `misses` and `flagged_total` are both real facts, so the numeric
# hard gate passes a sentence that says one is inside the other -- and that sentence is false, because
# a missed failure is by definition one that was never flagged.
SET_SUBMITTED = "submitted"           # every job whose SUBMIT time falls in the window (cohort S)
SET_FLAGGED   = "flagged"             # the subset of S that P3 warned about at submission
SET_FAILED    = "failures_resolved"   # the subset of S that has ENDED and did not COMPLETE
SET_ENDED     = "ended_in_window"     # jobs whose END time falls in the window (cohort E)

SET_LABEL = {
    SET_SUBMITTED: "the jobs submitted in this window",
    SET_FLAGGED:   "the jobs P3 flagged at submission",
    SET_FAILED:    "the submitted jobs that have ended and failed",
    SET_ENDED:     "the jobs that ended in this window (whenever they were submitted)",
}

# fact key path -> the set whose members it counts. A key may belong to two sets: correct_warnings
# is exactly the INTERSECTION of flagged and failed, which is why it is the only quantity that may
# legitimately be described as being in both.
SET_OF_KEY = {
    "jobs_window.submitted":                        (SET_SUBMITTED,),
    "jobs_window.submitted_outcome_known":          (SET_SUBMITTED,),
    "jobs_window.submitted_still_running":          (SET_SUBMITTED,),
    "jobs_window.flagged_at_submission":            (SET_FLAGGED,),
    "prediction_outcomes.flagged_total":            (SET_FLAGGED,),
    "prediction_outcomes.correct_warnings.count":   (SET_FLAGGED, SET_FAILED),
    "prediction_outcomes.false_alarms.count":       (SET_FLAGGED,),
    "prediction_outcomes.pending_outcome.count":    (SET_FLAGGED,),
    "prediction_outcomes.failures_resolved":        (SET_FAILED,),
    "prediction_outcomes.misses.count":             (SET_FAILED,),
    "jobs_window.ended_in_window":                  (SET_ENDED,),
    "jobs_window.ended_in_window_failed":           (SET_ENDED,),
    "jobs_window.ended_in_window_timeout":          (SET_ENDED,),
    "jobs_window.ended_in_window_oom":              (SET_ENDED,),
    "jobs_window.ended_in_window_completed":        (SET_ENDED,),
}

# (parts, whole) -- the parts sum EXACTLY to the whole, always, at every virtual time.
COHORT_IDENTITIES = [
    (("prediction_outcomes.correct_warnings.count",
      "prediction_outcomes.false_alarms.count",
      "prediction_outcomes.pending_outcome.count"), "prediction_outcomes.flagged_total"),
    (("prediction_outcomes.correct_warnings.count",
      "prediction_outcomes.misses.count"),          "prediction_outcomes.failures_resolved"),
    (("jobs_window.submitted_outcome_known",
      "jobs_window.submitted_still_running"),       "jobs_window.submitted"),
]

# (inner, outer, why) -- `inner` is NOT a subset of `outer`, so no sentence may place it inside.
COHORT_NON_CONTAINMENT = [
    ("prediction_outcomes.misses.count", "prediction_outcomes.flagged_total",
     "a missed failure is by definition one P3 did NOT flag, so a miss is never among the flagged "
     "jobs"),
    ("prediction_outcomes.failures_resolved", "prediction_outcomes.flagged_total",
     "failures_resolved counts caught AND missed failures together, so it is not a slice of the "
     "flagged jobs"),
    ("jobs_window.ended_in_window", "jobs_window.submitted",
     "a job that ended in this window may have been submitted long before it, so the ended count is "
     "not part of the submitted count"),
    ("jobs_window.ended_in_window_failed", "prediction_outcomes.flagged_total",
     "the ended-in-window failures are a different cohort from the jobs flagged at submission; "
     "neither contains the other"),
]


def _fact_at(facts, path):
    """facts['a']['b']['c'] for path 'a.b.c'. None if any hop is missing."""
    cur = facts
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def cohort_prose(facts) -> str:
    """The cohort model as prose, with THIS report's values substituted in.

    The auditor is given this alongside the JSON so the relationships are stated rather than left to
    be inferred from key names. Every line is generated from the structures above, so the prompt can
    never claim an identity verify_cohort.py does not assert.
    """
    def v(path):
        x = _fact_at(facts, path)
        return "n/a" if x is None else x

    L = ["The counts below are memberships of four DIFFERENT sets of jobs. Which set a quantity "
         "belongs to is carried by its key name:"]
    by_set = {}
    for key, sets in SET_OF_KEY.items():
        for s in sets:
            by_set.setdefault(s, []).append(key)
    for s in (SET_SUBMITTED, SET_FLAGGED, SET_FAILED, SET_ENDED):
        L.append(f"  - {SET_LABEL[s]}: {', '.join(sorted(by_set.get(s, [])))}")
    L.append("")
    L.append("These sums are exact at every report time, and this report is no exception:")
    for parts, whole in COHORT_IDENTITIES:
        lhs = " + ".join(f"{p.rsplit('.', 1)[0].split('.')[-1] if p.endswith('.count') else p.split('.')[-1]}={v(p)}"
                         for p in parts)
        L.append(f"  - {lhs}  =  {whole.split('.')[-1]}={v(whole)}")
    L.append("")
    L.append("These containments DO NOT hold. A sentence placing the first quantity inside the "
             "second is false even though both numbers are real:")
    for inner, outer, why in COHORT_NON_CONTAINMENT:
        L.append(f"  - {inner} is NOT part of {outer} — {why}.")
    L.append("")
    L.append("correct_warnings is the ONE quantity that legitimately belongs to two sets: it is "
             "exactly the overlap between the flagged jobs and the failures, i.e. the failures P3 "
             "warned about. Every other count belongs to one set only, and quantities from "
             "different sets can never be described as subsets of one another.")
    return "\n".join(L)


# ============================================================ 2. NUMERIC VALIDATION
_NUM = re.compile(r"\d+(?:\.\d+)?")

# ---- digit-grouping normalisation -------------------------------------------------------------
# The agent faithfully renders the fact value 1501 as "1,501". _NUM has no notion of thousands
# separators, so it used to split that into "1" and "501": the "1" matched some fact by coincidence
# and the "501" matched nothing, producing a bogus rejection
# (observed: unverified ["066","275","374","501"] from "1,066"/"1,275"/"1,374"/"1,501").
#
# _GROUPED_* matches only a COMPLETE canonical grouped number -- 1-3 digits, then one or more groups
# of exactly 3 digits, optionally a decimal tail -- and only when it is not glued to another digit
# or to a decimal point. That shape requirement is what keeps the following untouched:
#   "38.8"            decimal, no separator
#   "node0697"        4 undelimited digits
#   "12 jobs, 3 fail" sentence comma (not between digits)
#   "1234 567"        two separate numbers ("1234" is 4 digits, so no valid group start)
# NOT handled, deliberately: European-style "1.501" meaning 1501. It is genuinely ambiguous with
# the decimal 1.501, and silently reinterpreting it could mask a real hallucination.
# Separators are split by how ambiguous they are:
#   HARD -- a comma or a narrow/thin/no-break space between digits is only ever digit grouping in
#           the locales this console runs in, so it is collapsed unconditionally.
#   SOFT -- a plain space groups digits in some locales but also just separates two numbers
#           ("rack 34 826 nodes"), so it gets the two-reading treatment in unverified_numbers.
_GSEP_HARD = ",\u00A0\u2009\u202F\u2007"
_GSEP_ALL  = _GSEP_HARD + " "

def _mk_group_res(seps):
    return (re.compile(r"(?<![\d.])(\d{1,3}(?:[" + seps + r"]\d{3})+(?:\.\d+)?)(?!\d)"),
            re.compile(r"[" + seps + r"]"))
_GROUPED_HARD, _DEG_HARD = _mk_group_res(_GSEP_HARD)
_GROUPED_ALL,  _DEG_ALL  = _mk_group_res(_GSEP_ALL)

def _degroup(text: str, soft: bool = False) -> str:
    """'1,501' -> '1501'; with soft=True also '1 501' -> '1501'.

    See _GROUPED_* for exactly what is and is not touched.
    """
    g, d = (_GROUPED_ALL, _DEG_ALL) if soft else (_GROUPED_HARD, _DEG_HARD)
    return g.sub(lambda m: d.sub("", m.group(1)), text)

def _iter_nums(o):
    if isinstance(o, bool): return
    if isinstance(o, (int, float)):
        yield float(o); return
    if isinstance(o, str):
        # both readings of a fact string; this only ever ADDS legitimate fact-derived values
        for t in (o, _degroup(o)):
            for m in _NUM.findall(t): yield float(m)
        return    # (fact strings are generated by this module, so soft grouping never applies)
    if isinstance(o, dict):
        for v in o.values(): yield from _iter_nums(v)
    elif isinstance(o, (list, tuple)):
        for v in o: yield from _iter_nums(v)

def allowed_numbers(facts) -> set:
    """Every number a faithful narration may legitimately use: each fact value at 0/1/2 dp, its
    absolute value, and percentage forms for probabilities in [0,1]."""
    S = set()
    for n in _iter_nums(facts):
        for x in (n, abs(n)):
            S.add(round(x)); S.add(round(x, 1)); S.add(round(x, 2))
            if 0.0 <= x <= 1.0:
                S.add(round(x * 100)); S.add(round(x * 100, 1))
    return S

# tech/model tokens that legitimately contain digits but are NOT data (so the numeric check must
# ignore the embedded digit, e.g. "P3", "P2", "V100", "PSU-0", "GPU0", "AC922").
# Boundaries are "not adjacent to a letter or digit" rather than \b, because \b does NOT match
# before an underscore ('_' is a word character) -- so "P2 detected" was exempt while
# "p2_node_alert_score" was not, and the extractor pulled a bare 2 out of the identifier.
_STRIP = re.compile(r"(?<![A-Za-z0-9])(?:P[0-9]|V100|AC922|GPU[0-9]|PSU-?[0-9]|DCGM|IPMI|MIG"
                    r"|SLURM|M100)(?![A-Za-z0-9])", re.I)

# ---- schema identifiers are field references, not numeric claims --------------------------------
# The agent sometimes names metrics by their raw JSON key ("the p2_node_alert_score sits above the
# p3_alert_threshold"). _NUM then pulls the digit out of the identifier: p2_ -> 2, p3_ -> 3. Whether
# that rejected depended on whether the digit happened to also be a fact value, which is why this
# failed intermittently. Two defences, applied in the VALIDATOR ONLY (never to the displayed text):
#   1. dynamic  -- every key anywhere in the facts tree that contains a digit. Durable by
#                  construction: add a field to assemble_facts and it is exempt automatically.
#   2. static   -- any snake_case token. The safety net for keys the agent paraphrases or
#                  mis-spells. snake_case does not occur in ordinary report prose.
# Neither weakens the gate: a digit inside an identifier is not an assertion about the data.
_SNAKE = re.compile(r"\w*_\w+")

def facts_key_tokens(facts) -> set:
    """Every key in the facts tree, at any depth, whose name contains a digit."""
    out = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(c.isdigit() for c in str(k)):
                    out.add(str(k))
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk(facts)
    return out

def schema_strip_re(facts):
    """A regex removing the facts' own digit-bearing key names, longest first. None if there are none."""
    toks = sorted(facts_key_tokens(facts), key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in toks), re.I) if toks else None

def _scan(text: str, allowed: set):
    """Every number in `text` that cannot be traced to a fact value, at 0/1/2 dp."""
    bad = []
    for m in _NUM.findall(text):
        x = float(m)
        if not ({round(x), round(x, 1), round(x, 2)} & allowed):
            bad.append(m)
    return sorted(set(bad), key=lambda s: (len(s), s))

def unverified_numbers(text: str, allowed: set, schema=None):
    """Numbers in the narration that do not trace to the facts. Empty list == narration accepted.

    `schema` is the optional schema_strip_re(facts) pattern; identifier tokens are removed before
    extraction so a digit inside a field NAME is never mistaken for a claim about the data.

    The rule is unchanged and unweakened: every number must trace to a fact value. What changed is
    that a grouped number is now recognised as the fact it is.

    Commas and narrow spaces are collapsed unconditionally, so "1,501" is read ONLY as 1501 -- if
    1501 is not a fact this still rejects, even when 1 and 501 happen to be facts (the old
    literal-only scan would have let that through, so this direction is strictly tighter).

    The plain space is the one genuinely ambiguous separator, so both of its readings are tried and
    the narration is accepted if EITHER is fully traceable:
      1. as grouping   -- "1 501 jobs" reads as the fact 1501
      2. as a separator -- "rack 34 826 nodes" reads as the two facts 34 and 826
    That way neither interpretation of a space can manufacture a false rejection, while a genuine
    hallucination -- absent from the facts under every reading -- is still caught.
    """
    scrubbed = text if schema is None else schema.sub(" ", text)   # the facts' own field names
    scrubbed = _SNAKE.sub(" ", scrubbed)             # any remaining snake_case identifier
    scrubbed = _STRIP.sub(" ", scrubbed)             # drop P3 / V100 / PSU-0 ... before any digits
    hard = _degroup(scrubbed)                        # commas/nbsp: collapsed unconditionally
    readings = (_scan(_degroup(hard, soft=True), allowed),   # plain space read as grouping
                _scan(hard, allowed))                        # plain space read as a separator
    for r in readings:
        if not r:
            return []
    return min(readings, key=len)                    # both dirty: report the tighter reading


# ====================================================== 2b. NARRATION POST-PROCESSING
# The LaplaceAI agent is configured platform-side with a JSON contract, so it returns an object like
#   {"executive_summary": "...", "risk_assessment": "...", "recommended_actions": "..."}
# rather than markdown. Feeding that straight to the frontend's mdToHtml() rendered the braces, quotes
# and key names literally. Rather than fight the agent's configuration, we parse the object here and
# compose the markdown in Python, so presentation is ours to control. Plain-markdown replies still
# work unchanged -- both shapes are supported.

# canonical order for the sections we expect; anything unrecognised is appended, never dropped
_SECTION_ORDER = ["executive_summary", "summary", "situation", "what_happened", "events",
                  "risk_assessment", "current_risks", "risks", "recommended_actions", "actions",
                  "recommendations", "model_note", "caveats", "notes"]
_SECTION_TITLES = {                      # (English, 繁體中文)
    "executive_summary":   ("Executive summary",   "摘要"),
    "summary":             ("Summary",             "摘要"),
    "situation":           ("Situation",           "現況"),
    "what_happened":       ("What happened",       "發生了什麼"),
    "events":              ("Events",              "事件"),
    "risk_assessment":     ("Risk assessment",     "風險評估"),
    "current_risks":       ("Current risks",       "當前風險"),
    "risks":               ("Risks",               "風險"),
    "recommended_actions": ("Recommended actions", "建議行動"),
    "actions":             ("Actions",             "行動"),
    "recommendations":     ("Recommendations",     "建議"),
    "model_note":          ("Model note",          "模型說明"),
    "caveats":             ("Caveats",             "注意事項"),
    "notes":               ("Notes",               "備註"),
}
_SUMMARY_KEYS = ("executive_summary", "summary", "situation", "overview", "brief")
# Backstop against a near-empty reply only. Deliberately low: a "brief" IS one short paragraph, and
# Chinese is dense, so a real report can be short. The specific signals below do the actual work --
# the length floor must never be the thing that rejects a genuine report.
MIN_REPORT_CHARS = {"brief": 40, "full": 80}

def _pretty_key(key: str, en: bool) -> str:
    """'risk_assessment' -> 'Risk assessment'. Unknown keys are title-cased, never dropped."""
    t = _SECTION_TITLES.get(key.strip().lower())
    if t:
        return t[0] if en else t[1]
    words = re.sub(r"[_\-]+", " ", str(key)).strip()
    return (words[:1].upper() + words[1:]) if words else str(key)

def _spec_to_line(d: dict) -> str:
    """One dict entry as a readable line. Without this a list of dicts went through str(x) and a raw
    Python repr -- {'title': ..., 'type': ...} -- was emitted into the report body."""
    title = str(d.get("title") or d.get("name") or "").strip()
    typ   = str(d.get("type") or "").strip()
    desc  = str(d.get("description") or d.get("desc") or "").strip()
    head  = " ".join(p for p in (f"**{title}**" if title else "", f"({typ})" if typ else "") if p)
    line  = (head + (f" — {desc}" if desc else "")).strip()
    return line or ", ".join(f"{_pretty_key(k, True)}: {str(x).strip()}" for k, x in d.items())

def _value_to_md(v) -> str:
    """A section body: string as prose, list as bullets, anything else stringified."""
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {_spec_to_line(x) if isinstance(x, dict) else str(x).strip()}"
                         for x in v if (x if isinstance(x, dict) else str(x).strip()))
    if isinstance(v, dict):
        return "\n".join(f"- **{_pretty_key(k, True)}:** {str(x).strip()}" for k, x in v.items())
    return str(v).strip()

# ---- which parts of the reply are CLAIMS ABOUT THE DATA (and so must be numerically validated) ---
# HISTORY, because this exemption used to be wider than it is now. Under the old contract a chart
# spec was {title, type, description} -- free text describing how to draw something ("the top 2
# highest-risk nodes", "the 4 SLURM end states"). Those numbers were rendering instructions, not
# assertions about the data, so validating them was a false positive by construction and the whole
# chart_configs key was skipped.
#
# The contract changed: a chart entry is now exactly {chart_id, caption}, and the two halves have
# genuinely different natures, so the exemption is split to match.
#
#   chart_id  -- NOT prose. It is checked by membership of the ChartId enum, which is a stricter
#                test than any numeric scan: an id either names a registered renderer or it is
#                dropped. It carries no claim, so it stays exempt from the numeric gate.
#   caption   -- ordinary prose the model wrote, displayed under a chart exactly like a sentence in
#                the body. It gets the numeric hard gate like every other sentence. A caption
#                reading "the three worst nodes exceeded 90%" is a claim, and if 90 is not a fact
#                the report is rejected.
#
# Anything else the agent puts in a chart entry is validated too -- the exemption is for the id
# field alone, not for "whatever appears inside chart_configs".
_CHART_KEYS = {"chart_configs", "chart_config", "charts", "chart_spec", "chart_specs",
               "visualisations", "visualizations", "figures"}
_CHART_ID_FIELDS = {"chart_id", "chartid", "id", "chart"}

def _chart_entries(obj: dict):
    """(key, [entries]) for the agent's chart selection, or (None, []) if it made none."""
    for k, v in (obj or {}).items():
        if str(k).strip().lower() in _CHART_KEYS and isinstance(v, (list, tuple)):
            return str(k), list(v)
    return None, []

def _claim_texts(obj: dict):
    """[(field_path, text)] the agent ASSERTED about the data. Field paths name the offender so a
    rejection can be diagnosed at a glance instead of by inference."""
    out = []
    for k, v in obj.items():
        if str(k).strip().lower() in _CHART_KEYS and isinstance(v, (list, tuple)):
            # captions are claims; the chart_id is not (see the note above)
            for i, entry in enumerate(v):
                if isinstance(entry, dict):
                    for kk, vv in entry.items():
                        if str(kk).strip().lower() in _CHART_ID_FIELDS:
                            continue
                        out.append((f"{k}[{i}].{kk}", str(vv)))
                # a bare string entry is an id on its own -- nothing asserted, nothing to check
            continue
        if isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    for kk, vv in x.items():
                        out.append((f"{k}[{i}].{kk}", str(vv)))
                else:
                    out.append((f"{k}[{i}]", str(x)))
        elif isinstance(v, dict):
            for kk, vv in v.items():
                out.append((f"{k}.{kk}", str(vv)))
        else:
            out.append((str(k), str(v)))
    return out

def _flatten_to_paragraph(s: str) -> str:
    """One plain-text paragraph: no markdown syntax, no newlines. The sidebar box uses textContent,
    so any markup there would be shown literally."""
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", " ", s, flags=re.M)      # headings
    s = re.sub(r"^\s*[-*+]\s+", " ", s, flags=re.M)           # bullets
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)                    # bold
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", s)   # italics
    s = s.replace("`", "")
    return re.sub(r"\s+", " ", s).strip()

def _parse_json_object(raw: str):
    """The reply as a dict, or None if it is not a JSON object. Fences are already stripped."""
    t = (raw or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        o = json.loads(t)
    except ValueError:
        return None
    return o if isinstance(o, dict) and o else None

# ---- the section set is CLOSED ------------------------------------------------------------------
# Observed: the agent returned a key `executive_summary_part2`, and because composition title-cased
# any unrecognised key into its own heading, the report grew a literal "## Executive summary part2"
# and one section appeared as two. The section vocabulary is _SECTION_TITLES and nothing else.
#
# An off-vocabulary key is NOT dropped on sight -- it holds real prose the model wrote, and throwing
# it away would lose report content. It is CANONICALISED where the name obviously points at a known
# section (a `_part2` / `_2` / `_continued` suffix, or a known section as a prefix), and otherwise
# merged into the section above it. Either way its heading disappears and an advisory records it.
# This is deliberately not a hard-gate failure: the prose is fine, only the structure was wrong.
_SECTION_SUFFIX = re.compile(r"[_\s-]*(?:part|section|cont(?:inued)?|pt)?[_\s-]*\d+$", re.I)

def canonical_section(key: str):
    """An off-vocabulary key -> the section it belongs to, or None if it names nothing known."""
    k = str(key).strip().lower()
    if k in _SECTION_TITLES:
        return k
    stripped = _SECTION_SUFFIX.sub("", k)          # executive_summary_part2 -> executive_summary
    if stripped in _SECTION_TITLES:
        return stripped
    # longest known section that the key starts with, so `risk_assessment_details` -> risk_assessment
    hits = [s for s in _SECTION_TITLES if k.startswith(s)]
    return max(hits, key=len) if hits else None


def compose_markdown(obj: dict, lang: str, off_vocabulary: list = None) -> str:
    """Dict -> markdown sections, in _SECTION_ORDER, with a CLOSED heading vocabulary.

    The chart selection is skipped here on purpose. Under the current contract those entries are
    {chart_id, caption} pairs that become real rendered charts further down the pipeline; echoing
    them into the prose as a "Chart configs" section would print the plumbing next to the picture.

    `off_vocabulary`, if given, is appended with (key, resolution) for every key that was not a
    recognised section, so the caller can raise an advisory.
    """
    en = lang != "zh"
    skip = {k for k in obj if str(k).strip().lower() in _CHART_KEYS
            and isinstance(obj[k], (list, tuple))}

    # collect bodies per canonical section, preserving first-seen order for anything unknown
    sections, order, trailing = {}, [], []
    for k in obj:
        if k in skip:
            continue
        body = _value_to_md(obj[k])
        if not body:
            continue
        canon = canonical_section(k)
        if canon is None:
            # nothing in the vocabulary matches: fold it into the section above rather than
            # inventing a heading for it
            if off_vocabulary is not None:
                off_vocabulary.append((str(k), "merged into the preceding section"))
            if order:
                sections[order[-1]].append(body)
            else:
                trailing.append(body)
            continue
        if canon != str(k).strip().lower() and off_vocabulary is not None:
            off_vocabulary.append((str(k), f"merged into '{canon}'"))
        if canon not in sections:
            sections[canon] = []
            order.append(canon)
        sections[canon].append(body)

    # anything that arrived before any recognised section keeps the report's content
    if trailing:
        first = order[0] if order else "summary"
        if first not in sections:
            sections[first] = []
            order.insert(0, first)
        sections[first] = trailing + sections[first]

    ordered = [s for s in _SECTION_ORDER if s in sections] + \
              [s for s in order if s not in _SECTION_ORDER]
    out = []
    for s in ordered:
        out.append(f"## {_pretty_key(s, en)}")
        out.append("\n\n".join(sections[s]))
        out.append("")
    return "\n".join(out).strip()

# ---- meta-commentary / wrong-language rejection --------------------------------------------------
# Deliberately a small set of conservative signals, not a classifier. One observed reply was entirely
# Chinese meta-commentary for a lang="en" request, saying it had produced shift_report.json and
# offered it as a download card -- no report content at all. That must never reach the user.
_META_PAT = re.compile(
    r"(i\s+have\s+(?:generated|created|produced|attached|prepared)|i've\s+(?:generated|created|produced)"
    r"|i\s+am\s+an?\s+|as\s+an\s+ai|as\s+requested,?\s+i"
    r"|download\s+(?:card|link|button|it)|you\s+can\s+download|attached\s+(?:file|report)|attachment"
    r"|\.json\s*(?:file|檔)|shift_report\.json"
    # 身為 / 作為 ("in my capacity as ...") is the formal Chinese self-introduction and is more common
    # in practice than 我是: measured, 3 of 8 generated demo reports opened with "身為 Data Analyst".
    # Matching only 我是 let the same failure through under a different phrasing.
    r"|我(?:已|是)|(?:身|作)為\s*(?:a\s+)?[A-Za-z一-鿿 ]{0,20}(?:Analyst|analyst|分析師|助理|AI)"
    r"|已(?:為您)?(?:產生|生成|建立|完成)|檔案已|下載(?:卡|連結|按鈕)|附(?:件|上))",
    re.I)
_CJK = re.compile(r"[一-鿿㐀-䶿]")

def _cjk_ratio(text: str) -> float:
    letters = [c for c in text if not c.isspace()]
    return (sum(1 for c in letters if _CJK.match(c)) / len(letters)) if letters else 0.0


# ================================================================================================
# HARD GATE -- deterministic, blocking, stays in Python permanently.
#
# Only checks with an exact answer live here. They are the credibility spine of the feature and
# must never be delegated to a model:
#   (a) the response contains usable report content at all   -> no_report_content()
#   (b) every number the agent asserts traces to a fact      -> check_claims()
# Anything requiring judgment belongs in advisory_review() below, NOT here.
# ================================================================================================
def no_report_content(text: str, length: str = "full"):
    """The ONE content hard-failure: there is nothing to display. Reason string, or None.

    Deliberately narrow. A merely short, oddly-toned or wrong-language report still has a body and
    is shown to the user with an advisory attached; only an empty reply, or pure meta-commentary
    with no substantive body, fails here.
    """
    t = (text or "").strip()
    if not t:
        return "agent returned an empty response"
    if len(t) < 20:
        return f"agent returned only {len(t)} characters, nothing to display"
    # "no substantive body" is decided by whether the reply cites ANY figure, not by its length.
    # A genuine shift report always quotes at least one number; pure meta-commentary about having
    # produced a file quotes none. Length is the wrong proxy -- an 84-character reply that is
    # entirely meta-commentary has no body, while a terse but real report does.
    if _META_PAT.search(t[:400]) and not _NUM.findall(_SNAKE.sub(" ", _STRIP.sub(" ", t))):
        return "agent returned meta-commentary with no report body"
    return None


# ================================================================================================
# ADVISORY LAYER -- heuristic, NON-BLOCKING, to be superseded by the reviewer agent.
#
# >>> REVIEWER-AGENT INTEGRATION POINT <<<
# A second LLM agent will later perform semantic review (tone, completeness, whether the narrative
# actually matches the facts). When it lands, it should be called from HERE and its findings
# appended to the same list, in the same shape, so nothing downstream changes. The contract:
#
#   input :  text    -- the composed report as the user would read it
#            lang    -- the requested language ("en" | "zh")
#            length  -- "brief" | "full"
#            obj     -- the parsed agent reply (dict) when the JSON contract was used, else None
#   output:  list of {"code": str, "message": str}; [] means nothing to flag
#
# A second advisory runs at the same seam but LATER in the pipeline, because it needs something
# advisory_review() cannot see: caption_consistency_review(charts) compares each agent-written
# caption against the chart it labels, and the charts do not exist until after the gate has run. It
# returns the same shape and its findings join the same list. When the reviewer agent lands it
# should be called alongside both.
#
# Invariant: these functions NEVER block. They cannot reject a report, they cannot raise, and their
# results must not feed back into mode selection. Judgment-based checks used to reject outright,
# which threw away reports that were perfectly good; they now surface as flags the operator can see
# and ignore.
# ================================================================================================
ADV_LANGUAGE = "language_mismatch"
ADV_META     = "meta_commentary"
ADV_SHORT    = "short_content"
ADV_CHART_ID = "unknown_chart_id"       # raised by select_charts, one per dropped id
ADV_SECTION  = "off_vocabulary_section" # the agent invented a heading outside the closed set

def advisory_review(text: str, lang: str, length: str = "full", obj=None) -> list:
    """Non-blocking quality flags for a report that has already passed the hard gate."""
    out = []
    t = (text or "").strip()
    if _META_PAT.search(t[:400]):
        out.append({"code": ADV_META,
                    "message": "Reply opens with commentary about the assistant or a file rather "
                               "than with report content."})
    cjk = _cjk_ratio(t)
    if lang == "en" and cjk > 0.15:
        out.append({"code": ADV_LANGUAGE,
                    "message": f"English was requested but {cjk:.0%} of the characters are Chinese."})
    elif lang == "zh" and cjk < 0.05:
        out.append({"code": ADV_LANGUAGE,
                    "message": f"Chinese was requested but only {cjk:.0%} of the characters are Chinese."})
    floor = MIN_REPORT_CHARS.get(length, MIN_REPORT_CHARS["full"])
    if len(t) < floor:
        out.append({"code": ADV_SHORT,
                    "message": f"Report is only {len(t)} characters, shorter than the {floor} "
                               f"expected for a '{length}' report."})
    return out


# ---- caption vs the chart it labels --------------------------------------------------------------
# THE GAP THIS FILLS, and why it can only be advisory.
#
# The hard gate asks one question: does every number the agent wrote appear somewhere in the facts?
# That is a whole-report test, and it is the right test for the body, where a sentence may
# legitimately cite any fact. It is too weak for a CAPTION, because a caption is scoped to ONE chart
# and inherits that chart's claim of relevance. An observed failure: a caption read "the 460 flagged
# jobs, including the 34 missed failures" under a chart whose ring is the flagged cohort. 460 and 34
# are both real facts, so the gate passed -- but the 34 are not among the 460, and the sentence is
# false. No whole-report numeric test can catch that by construction: both numbers are present.
#
# So this compares each agent-written caption against ITS OWN chart's numbers. A number in a caption
# that appears nowhere in the chart it labels is not necessarily wrong -- an operator may reasonably
# mention the alert threshold, or the window length, under a chart that does not plot it -- which is
# exactly why this NEVER blocks. It is a flag that says "check this sentence against this picture".
#
# WHAT THIS DELIBERATELY CANNOT CATCH, stated so nobody assumes otherwise.
# The scope test is about membership, not about grammar. Both 212 and 47 genuinely appear on the
# outcomes panel -- 212 as the ring's total, 47 in the footnote beside it -- so the sentence
# "of the 212 flagged jobs, 47 were missed failures" passes this check even though the relation it
# asserts is false. Tightening the set to exclude the footnote would not fix it either: it would
# start flagging the CORRECT sentence ("separately, 47 failures were never flagged"), and an
# advisory that fires on good text stops being read. A false relational claim between two numbers
# that both belong to the panel is a judgment call, and judgment belongs to the reviewer agent, not
# to a deterministic layer. What guards it today is structural rather than textual: misses are no
# longer a slice of the ring, the footnote carries a note saying they are a different cohort, and
# the prompt tells the agent not to describe them as flagged.
ADV_CAPTION = "caption_chart_mismatch"

def chart_numbers(chart: dict) -> set:
    """Every number a caption on THIS chart can legitimately cite, at 0/1/2 dp.

    Plotted values, anything numeric embedded in its own labels, its reference line, its axis
    bound, its footnote, its stated segment sum, and the bar/bucket count -- since "six jobs" or
    "24 buckets" is a fair thing to say about a chart with six bars or 24 buckets.
    """
    if not isinstance(chart, dict):
        return set()
    seed = {k: chart.get(k) for k in
            ("labels", "datasets", "reference_line", "axis_max", "footnote", "segment_sum",
             "bucket_count", "point_notes", "color_legend", "subject_node")}
    S = allowed_numbers(seed)
    for ds in (chart.get("datasets") or []):
        data = ds.get("data") or []
        S.add(len(data))                      # "four of the six", "across 24 buckets"
        tot = sum(x for x in data if isinstance(x, (int, float)))
        for x in (tot, round(float(tot), 1)):  # a caption may legitimately total a plotted series
            S.add(round(x)); S.add(round(x, 1)); S.add(round(x, 2))
    S.add(len(chart.get("labels") or []))
    return S

def caption_consistency_review(charts: list) -> list:
    """Non-blocking flags where an agent-written caption cites a number its chart does not carry."""
    out = []
    for ch in (charts or []):
        if not isinstance(ch, dict) or ch.get("caption_source") != "agent":
            continue                          # Python-written captions are built from the chart
        caption = str(ch.get("caption") or "")
        if not caption.strip():
            continue
        stray = unverified_numbers(caption, chart_numbers(ch))
        if stray:
            out.append({"code": ADV_CAPTION,
                        "message": f"Caption on '{ch.get('chart_id')}' cites "
                                   f"{', '.join(stray)} — {'a number' if len(stray) == 1 else 'numbers'} "
                                   f"that this chart does not plot. The figure may be real elsewhere "
                                   f"in the facts, but it is not in this picture; check the sentence "
                                   f"against the chart."})
    return out


# ---- false CONTAINMENT between two cohorts, in deterministic Python -------------------------------
# WHAT THIS IS, AND WHY IT CAN EXIST AT ALL.
#
# caption_consistency_review() proved that the deterministic layer catches things the model does not,
# and it works because it tests MEMBERSHIP against a set the code owns. The same trick applies to the
# relational class, because the facts' own KEY NAMES carry the cohort (SET_OF_KEY above): if a
# sentence says quantity A sits inside quantity B, and A's key and B's key belong to sets the cohort
# model declares non-nesting, the sentence is false and Python can say so without any judgment.
#
# THREE CONDITIONS, ALL REQUIRED, and each one is there to buy precision:
#   1. both numbers resolve UNAMBIGUOUSLY to a set. A value is only resolvable if every SET_OF_KEY
#      key carrying it maps to the same single set AND the value appears nowhere else in the facts
#      tree. correct_warnings is deliberately never resolvable -- it genuinely belongs to two sets.
#   2. the pair appears in COHORT_NON_CONTAINMENT. Two numbers from sets that legitimately nest
#      (flagged inside submitted) are never flagged.
#   3. a containment cue sits BETWEEN them, in the direction the cue implies, close enough to be one
#      claim, with no separating marker in the gap.
#
# Condition 3 is what stops the documented false positive. "Of the 331 flagged jobs, 108 were caught;
# separately, 47 failures were never flagged" contains both numbers and a cue, and is TRUE -- the
# semicolon and "separately" are separators, and "never flagged" is a negation, so the gap guard
# drops it. An advisory that fires on correct text stops being read, which is the whole reason this
# is narrow rather than clever.
#
# WHAT IT CANNOT DO, stated so nobody assumes otherwise. It needs two numbers. A sentence asserting
# a false containment in words alone -- "most of the flagged jobs have already been caught" when 17%
# have -- carries nothing to resolve and is invisible here. That class is the auditor's, not Python's.
ADV_COHORT = "cohort_containment"

# a number, optionally with thousands grouping, with its position preserved
_NUM_POS = re.compile(r"\d{1,3}(?:[" + _GSEP_HARD + r"]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_DEG_POS = re.compile(r"[" + _GSEP_HARD + r"]")
_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+|[\n\r]+|(?<=[。！？；：])")

# FOUR PHRASINGS, because the cue does not reliably sit between the two numbers. Measured on the
# fixtures: restricting to "inner CUE outer" caught 1 of 7 documented false claims, because English
# puts the cue before the pair ("Of the 288 flagged, 151 failed") or after it, with the container
# referred to by a pronoun ("...the 231 resolved failures sit within that cohort"), at least as often.
#
#   A  inner CUE outer      "47 of the 331 flagged jobs"
#   B  outer CUE inner      "331 flagged jobs, including 47 missed failures"
#   C  outer ... inner CUE <anaphor>   "flagged 331 jobs, and the 231 failures sit within that cohort"
#   D  CUE outer ... inner  "Of the 288 jobs flagged, 151 have resolved as failures"
#
# C and D are the loose ones, so they use a NARROWER cue set: "of the" and "in the" are dropped
# because outside an explicit between-position they have too many innocent readings ("in the last
# 6 h"). C additionally requires an anaphor naming the container, and both require the pair to be
# ADJACENT with nothing between them -- see the adjacency note in cohort_containment_review.
_CUE_INNER_FIRST = (r"within", r"inside", r"among", r"amongst", r"out of", r"of the", r"of these",
                    r"of those", r"part of", r"belong(?:s|ed)? to", r"included in", r"in the",
                    r"sit(?:s)? in", r"fall(?:s)? (?:in|within)",
                    r"之中", r"之內", r"當中", r"屬於", r"納入")
_CUE_OUTER_FIRST = (r"including", r"includes", r"include", r"of which", r"comprising",
                    r"made up of", r"consist(?:s|ing)? of", r"broken down into", r"其中", r"包括",
                    r"包含", r"內含")
# the subset strong enough to be trusted when it is NOT between the two numbers
_CUE_STRONG = (r"within", r"inside", r"among", r"amongst", r"part of", r"belong(?:s|ed)? to",
               r"included in", r"sit(?:s)? (?:in|within)", r"fall(?:s)? (?:in|within)",
               r"of the", r"of these", r"of those", r"之中", r"之內", r"當中", r"屬於")
_CUE_INNER_RE  = re.compile("|".join(_CUE_INNER_FIRST), re.I)
_CUE_OUTER_RE  = re.compile("|".join(_CUE_OUTER_FIRST), re.I)
# rule D: a strong cue introducing the container's number. The gap between cue and number allows
# words but NO DIGITS -- live text writes "Within the flagged cohort of 288 jobs, 151 have resolved
# as failures", where five words separate the cue from the number it introduces. Requiring adjacency
# missed that; allowing digits in the gap would let the cue reach past an intervening quantity.
_CUE_BEFORE_RE = re.compile(r"(?:" + "|".join(_CUE_STRONG) + r")[^\d]{0,40}$", re.I)
# rule C: a strong cue that starts just after the contained number and points back at a named group.
# The window is cut at the next digit before matching (see _after_window), so this can never reach
# across a third number and pair two quantities that are not adjacent.
_CUE_AFTER_RE  = re.compile(r"^.{0,45}?(?:" + "|".join(_CUE_STRONG) + r")\s+"
                            r"(?:that|those|these|this|the|it|them|該|此|這些|那些)?\s*"
                            r"(?:cohort|set|group|total|jobs|figure|count|them|those|these|it"
                            r"|群|集合|批|總數)", re.I | re.S)

def _after_window(sent: str, start: int, width: int = 90) -> str:
    """The text just after a number, truncated at the next digit. Rule C looks for a cue pointing
    BACK at the container, so anything past the next number belongs to a different claim."""
    w = sent[start:start + width]
    d = re.search(r"\d", w)
    return w[:d.start()] if d else w

# anything in the gap between the two numbers that says they are being contrasted, not nested
_SEPARATOR_RE = re.compile(
    r"[;；]|\bseparately\b|\bwhereas\b|\bwhile\b|\bbut\b|\bhowever\b|\bby contrast\b|\bin contrast\b"
    r"|\bnever\b|\bnot\b|\bno\b|\boutside\b|\bdistinct\b|\bdifferent\b|\bunlike\b|\brather than\b"
    r"|另外|另有|separately|而|但|卻|未|沒有|非|不同|以外|之外", re.I)

CONTAINMENT_MAX_GAP = 140     # chars between the two numbers; beyond this it is not one claim

def _blank(pattern, text: str) -> str:
    """Replace every match with the SAME NUMBER of spaces, so string offsets survive scrubbing."""
    return pattern.sub(lambda m: " " * (m.end() - m.start()), text)

def _num_value(token: str) -> float:
    return float(_DEG_POS.sub("", token))

def resolve_sets(facts) -> dict:
    """value -> the single set it unambiguously counts members of. Ambiguous values are absent.

    A value earns an entry only if every SET_OF_KEY key holding it maps to the same one set, and the
    value does not occur anywhere else in the facts tree. That second condition is what stops a node
    count that happens to equal a job count from being read as a cohort quantity.
    """
    by_value, tracked_paths = {}, set(SET_OF_KEY)
    for key, sets in SET_OF_KEY.items():
        val = _fact_at(facts, key)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        by_value.setdefault(round(float(val), 2), set()).update(sets)

    # every numeric value living anywhere OUTSIDE the tracked keys
    elsewhere = set()
    def walk(node, path):
        if path in tracked_paths:
            return
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            elsewhere.add(round(float(node), 2)); return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, path)
        elif isinstance(node, str):
            for m in _NUM.findall(node):
                elsewhere.add(round(float(m), 2))
    walk(facts, "")

    return {v: next(iter(s)) for v, s in by_value.items()
            if len(s) == 1 and v not in elsewhere}

def _non_containment_pairs():
    """{(inner_set, outer_set): (inner_key, outer_key, why)} for every declared non-containment."""
    out = {}
    for inner, outer, why in COHORT_NON_CONTAINMENT:
        si, so = SET_OF_KEY.get(inner), SET_OF_KEY.get(outer)
        if not si or not so or len(si) != 1 or len(so) != 1:
            continue                       # a two-set key cannot anchor a containment claim
        out[(si[0], so[0])] = (inner, outer, why)
    return out

def cohort_containment_review(text: str, facts: dict, extra_texts=None) -> list:
    """Non-blocking flags where a sentence puts one cohort's quantity inside another's.

    `extra_texts` is [(where, text)] for anything outside the narrative body that the agent also
    wrote -- chart captions, in practice -- so this covers the same ground the auditor does.
    """
    resolved = resolve_sets(facts or {})
    pairs = _non_containment_pairs()
    if not resolved or not pairs:
        return []

    out, seen = [], set()
    for where, body in [("narrative", text or "")] + list(extra_texts or []):
        scrubbed = _blank(_SNAKE, _blank(_STRIP, body))       # P3 / p2_node_alert_score carry no claim
        for sent in _SENT_SPLIT.split(scrubbed):
            if not sent or not sent.strip():
                continue
            # EVERY number in the sentence, resolvable or not. The pair examined must be ADJACENT in
            # this list: an unresolvable number sitting between two resolvable ones means they are
            # not the two halves of one containment claim. Without this the check read
            # "submitted 2245 jobs and 2029 ended, of which 1669 completed" as putting 1669 inside
            # 2245 -- it paired the first and third numbers across an intervening one and fired on a
            # correct sentence. Adjacency is the single cheapest precision win here.
            nums = [(m.start(), m.end(), round(_num_value(m.group(0)), 2))
                    for m in _NUM_POS.finditer(sent)]
            for (s1, e1, v1), (s2, e2, v2) in zip(nums, nums[1:]):
                set1, set2 = resolved.get(v1), resolved.get(v2)
                if set1 is None or set2 is None:
                    continue
                gap = sent[e1:s2]
                if len(gap) > CONTAINMENT_MAX_GAP or _SEPARATOR_RE.search(gap):
                    continue
                if _CUE_INNER_RE.search(gap):                   # A: inner CUE outer
                    cand, rule = (set1, set2), "A"
                elif _CUE_OUTER_RE.search(gap):                 # B: outer CUE inner
                    cand, rule = (set2, set1), "B"
                elif _CUE_AFTER_RE.match(_after_window(sent, e2)):   # C: outer .. inner CUE <anaphor>
                    cand, rule = (set2, set1), "C"
                elif _CUE_BEFORE_RE.search(sent[max(0, s1 - 30):s1]):   # D: CUE outer .. inner
                    cand, rule = (set2, set1), "D"
                else:
                    continue
                info = pairs.get(cand)
                if info is None:
                    continue
                inner_key, outer_key, why = info
                quote = sent.strip()[:220]
                fingerprint = (where, quote, cand)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                out.append({"code": ADV_COHORT, "rule": rule,
                            "message": f"In the {where}, \"{quote}\" describes "
                                       f"{SET_LABEL[cand[0]]} as being inside "
                                       f"{SET_LABEL[cand[1]]}. {inner_key} is not part of "
                                       f"{outer_key} — {why}. Both numbers are real facts, so the "
                                       f"numeric gate cannot see this."})
    return out


def compose_narration(raw: str, lang: str, length: str):
    """Raw agent reply -> (text, hard_reason, claims, obj).

    `text` is ALWAYS the composed text, even when hard_reason is set, so a rejected draft can be
    preserved and returned to the caller for inspection instead of being discarded.
    `hard_reason` is set only by the hard gate's content check (nothing to display).

    Handles both shapes: a JSON object (composed into markdown here) and plain markdown prose.
    For length="brief" the result is always ONE plain-text paragraph, because the sidebar element
    renders it with textContent and would otherwise show markdown syntax literally.

    `claims` is [(field_path, text)] to run the numeric guardrail over, or None meaning "validate
    the whole text" (the plain-markdown case, where the model wrote every character). Scoping the
    check to the agent's own field values keeps composition scaffolding -- headings, bullet markers,
    separators this module generates -- out of validation, since the model did not write it.
    """
    raw = (raw or "").strip()
    obj = _parse_json_object(raw)
    off_vocab = []              # keys that were not recognised sections; drives ADV_SECTION

    if obj is not None:
        if length == "brief":
            pick_k = next((k for k in _SUMMARY_KEYS if k in obj and str(obj[k]).strip()), None)
            if pick_k is not None:
                text, claims = _flatten_to_paragraph(str(obj[pick_k])), [(pick_k, str(obj[pick_k]))]
            else:
                # same exclusion as compose_markdown: chart entries are plumbing, not prose
                text = _flatten_to_paragraph(" ".join(
                    _value_to_md(v) for k, v in obj.items()
                    if not (str(k).strip().lower() in _CHART_KEYS and isinstance(v, (list, tuple)))))
                claims = _claim_texts(obj)
        else:
            text, claims = compose_markdown(obj, lang, off_vocab), _claim_texts(obj)
    else:
        text, claims = (_flatten_to_paragraph(raw) if length == "brief" else raw), None

    return text, no_report_content(text, length), claims, obj, off_vocab


def section_advisories(off_vocab) -> list:
    """One advisory naming every heading the agent invented outside the closed section set."""
    if not off_vocab:
        return []
    named = ", ".join(f"'{k}' ({how})" for k, how in off_vocab[:6])
    return [{"code": ADV_SECTION,
             "message": f"Agent used {len(off_vocab)} heading"
                        f"{'' if len(off_vocab) == 1 else 's'} outside the defined section set: "
                        f"{named}. The prose was kept; the invented heading was not."}]

def check_claims(claims, text, allowed, schema=None):
    """-> (unverified, field). `claims` None means validate `text` wholesale (plain-markdown reply).

    Returns the FIRST offending field so the fallback reason can name it.
    """
    if claims is None:
        return unverified_numbers(text, allowed, schema), None
    for field, value in claims:
        bad = unverified_numbers(value, allowed, schema)
        if bad:
            return bad, field
    return [], None


# ============================================================ 3. LLM NARRATION (server-side only)
# ============================================================ 3. LLM NARRATION (LaplaceAI Endpoint)
# ---------------------------------------------------------------- resilience settings
# A LaplaceAI 504 is a gateway timeout on THEIR side: the agent exceeded their internal limit, so a
# larger client timeout cannot help. What helps is retrying (gateway timeouts are usually transient)
# and not punishing the whole process for one bad minute.
REQUEST_TIMEOUT   = 300.0   # per attempt; the agent legitimately takes ~60s, so this stays generous
RETRY_ATTEMPTS    = 3       # 1 initial + 2 retries
RETRY_BASE        = 1.5     # seconds; exponential 1.5, 3.0 with jitter
RETRY_DEADLINE    = 150.0   # HARD CEILING on total elapsed across all attempts
MIN_ATTEMPT_ROOM  = 15.0    # do not start another attempt with less than this left in the budget

COOLDOWN_TRANSIENT = 25     # 5xx / timeout after retries exhausted: short, the endpoint may recover
COOLDOWN_AUTH      = 900    # 401/403: credentials will not fix themselves
COOLDOWN_CLIENT    = 600    # other 4xx: the request is malformed, retrying is pointless

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
AUTH_STATUS      = {401, 403}

# ---- agents -------------------------------------------------------------------------------------
AGENT_MAIN    = "main"       # narrates the facts; its output IS the report
AGENT_AUDITOR = "auditor"    # advisory second opinion; never blocks, never changes `mode`

# Per-agent resilience profile. The auditor is advisory, so it gets a tighter budget: fewer
# attempts and a shorter deadline than the narrator, because a slow audit is worth less than a
# fast report. Its per-attempt timeout is nevertheless GENEROUS, and deliberately so -- see the
# note on AUDIT_TIMEOUT below.
AGENT_PROFILE = {
    AGENT_MAIN:    {"attempts": RETRY_ATTEMPTS, "deadline": RETRY_DEADLINE,
                    "timeout": REQUEST_TIMEOUT, "min_room": MIN_ATTEMPT_ROOM},
    AGENT_AUDITOR: {"attempts": 2, "deadline": 75.0, "timeout": 60.0, "min_room": 10.0},
}

# ---- cooldown, namespaced PER AGENT ---------------------------------------------------------------
# This was a single module-level scalar. It is now keyed by agent, and that separation is not
# cosmetic: the fix routes the auditor through the same wrapper as the narrator, and that wrapper
# TRIPS THE BREAKER on failure. With one shared scalar, an auditor that fails -- which, measured, it
# did on every call for a while -- would have put the narrator into cooldown and turned every
# subsequent report into a template. Each agent now cools down only itself.
_COOLDOWN = {}               # agent -> (until_epoch, why)

def cooldown_remaining(agent: str):
    """-> (seconds_remaining, why). (0.0, "") when the agent is not cooling down."""
    until, why = _COOLDOWN.get(agent, (0.0, ""))
    return max(0.0, until - time.time()), why

def clear_cooldowns():
    """Drop every agent's cooldown. Used by the verification scripts between fixtures."""
    _COOLDOWN.clear()

def _log(msg: str):
    """Attempt-level logging. Never receives the bearer secret -- callers pass status/class only."""
    print(f"[report.llm] {msg}", flush=True)


# ---------------------------------------------------------------- outbound-call instrumentation
# OFF unless LAPLACE_DEBUG is set to something other than 0/false. When on, every outbound LaplaceAI
# call records agent, host, path, payload size, wall-clock latency, status and exception class, both
# to stdout and to an in-memory ring the /api/debug/llm_calls route can serve.
# The bearer secret is NEVER recorded: only the URL host and path are kept, and any query string is
# replaced with a marker rather than stored.
CALL_LOG_MAX = 200
_CALL_LOG = []
_CALL_LOG_LOCK = threading.Lock()

def debug_on() -> bool:
    return str(os.environ.get("LAPLACE_DEBUG", "")).strip().lower() not in ("", "0", "false", "no")

def _redact_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        u = urlparse(url or "")
        return f"{u.netloc}{u.path}" + ("?<redacted>" if u.query else "")
    except Exception:
        return "<unparseable-url>"

def _resp_note(r) -> str:
    """A cheap, TOTALLY SAFE description of a response body for the call log.

    Instrumentation must never be able to break the thing it observes: this returns "" when
    debugging is off (so nothing is computed at all) and swallows anything odd about the object it
    is handed, because that object may be a test double rather than a real requests.Response.
    """
    if not debug_on():
        return ""
    try:
        return f"resp={len(getattr(r, 'content', b'') or b'')}b"
    except Exception:
        return ""

def record_call(agent, url, payload_bytes, latency_s, status, exc=None, note=""):
    """One outbound call, recorded. Cheap no-op when debugging is off, and never raises."""
    if not debug_on():
        return
    row = {"ts": round(time.time(), 3), "agent": agent, "endpoint": _redact_url(url),
           "payload_bytes": int(payload_bytes or 0), "latency_s": round(float(latency_s), 2),
           "status": status, "exception": (type(exc).__name__ if exc is not None else None),
           "note": note}
    try:
        with _CALL_LOG_LOCK:
            _CALL_LOG.append(row)
            if len(_CALL_LOG) > CALL_LOG_MAX:
                del _CALL_LOG[: len(_CALL_LOG) - CALL_LOG_MAX]
        print(f"[llm.call] agent={row['agent']:<7} {row['endpoint']:<58} "
              f"bytes={row['payload_bytes']:>7} {row['latency_s']:>7.2f}s "
              f"status={row['status']} exc={row['exception']} {row['note']}", flush=True)
    except Exception:
        pass          # a broken log line must never fail a report

def call_log(limit: int = 100) -> list:
    with _CALL_LOG_LOCK:
        return list(_CALL_LOG[-int(limit):])

def clear_call_log():
    with _CALL_LOG_LOCK:
        _CALL_LOG.clear()

def _cooldown(seconds: int, why: str, agent: str = AGENT_MAIN):
    _COOLDOWN[agent] = (time.time() + seconds, why)
    _log(f"[{agent}] cooldown {seconds}s after {why}")

def _extract_text(res_data):
    """Pull the narration out of the LaplaceAI payload shape."""
    text = ""
    if isinstance(res_data, dict):
        msg = res_data.get("message")
        if isinstance(msg, dict):
            text = msg.get("content", "")
        elif isinstance(msg, str):
            text = msg
        else:
            text = res_data.get("response") or res_data.get("output") or res_data.get("text") or ""
    else:
        text = str(res_data)
    text = (text or "").strip()
    if text.startswith("```json"): text = text[7:]
    if text.startswith("```"):     text = text[3:]
    if text.endswith("```"):       text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------- prompt payload (trimmed)
# The guardrail and the raw-facts panel always use the FULL facts object. Only the prompt payload is
# trimmed, so the agent has less to chew on (a smaller payload is one fewer reason for the agent to
# exceed its gateway limit). Trimming can never cause a guardrail failure: the allowed-number set is
# built from the full facts, so anything the agent quotes from the trimmed subset is a subset of what
# validates.
PROMPT_EXAMPLES_CAP = 2      # was MAX_EX (4) per outcome bucket
PROMPT_LIST_CAP     = 4      # onset events / high-risk nodes / high-risk jobs

def trim_facts_for_prompt(facts: dict, lang: str = "en") -> dict:
    """A narration-sized view of the facts. Drops fields the report never references."""
    f = json.loads(json.dumps(facts, ensure_ascii=False))     # deep copy, never mutate the original

    w = f.get("window", {})
    for k in ("start_ts", "end_ts"):                          # epoch ints; the prose uses the ISO forms
        w.pop(k, None)
    f.get("cluster_now", {}).pop("nodes_scored", None)        # not referenced by any report section

    po = f.get("prediction_outcomes", {})
    for bucket in ("correct_warnings", "false_alarms", "pending_outcome", "misses"):
        d = po.get(bucket)
        if isinstance(d, dict) and isinstance(d.get("examples"), list):
            d["examples"] = d["examples"][:PROMPT_EXAMPLES_CAP]

    ons = f.get("node_onsets", {})
    if isinstance(ons.get("events"), list):
        ons["events"] = ons["events"][:PROMPT_LIST_CAP]
    for key in ("high_risk_nodes", "high_risk_jobs"):
        if isinstance(f.get(key), list):
            f[key] = f[key][:PROMPT_LIST_CAP]

    # only the caveat for the requested language. This also removes the CJK string from an English
    # payload, which was the one realistic way an English report picked up a language advisory.
    mn = f.get("model_note", {})
    mn.pop("caveat_zh" if lang != "zh" else "caveat_en", None)
    return f


def _chart_menu_block(lang: str) -> str:
    """The chart contract, spelled out for the agent.

    The agent SELECTS and CAPTIONS; it never describes chart data. Every number on every chart is
    computed in Python from the database, so there is nothing for the model to specify beyond which
    question is worth showing and what to say underneath it.
    """
    langname = "Traditional Chinese (繁體中文)" if lang == "zh" else "English"
    ids = "\n".join(f'  - "{cid.value}": {desc}' for cid, desc in chartreg.CHART_MENU.items())
    return (
        "CHARTS:\n"
        "You may also select charts to accompany the report. The dashboard computes every chart's "
        "data itself from the database -- you do not describe, specify or supply any chart data.\n"
        "Available chart ids, and what each one answers:\n"
        f"{ids}\n"
        "Rules for the chart selection:\n"
        f'  - Return them under the key "chart_configs", as a list of objects with EXACTLY two '
        f'fields: "chart_id" and "caption".\n'
        '  - "chart_id" must be copied verbatim from the list above. Do not invent ids.\n'
        f'  - "caption" is one short sentence of prose, written in {langname}, exactly like the '
        f'rest of the report, saying what the operator should take from that chart.\n'
        "  - A caption is held to the same standard as the report body: any number in it must come "
        "from FACTS DATA. Writing no number at all is always safe.\n"
        "  - A caption must describe the chart it sits under, and only that chart. Do not put a "
        "number in a caption unless that number is actually shown in THAT chart -- a figure that is "
        "real elsewhere in the facts is still wrong under a chart that does not plot it. In "
        "particular, missed failures are NOT part of the flagged cohort, so never describe them as "
        "being among the flagged jobs.\n"
        f"  - Select only the charts that matter for THIS shift -- at most {MAX_CHARTS}, fewer is "
        "fine, and none is a valid answer on a quiet shift.\n"
        "  - Do NOT include titles, chart types, axes, series, colours, or any data values.\n"
    )


def _build_prompt(payload: dict, lang: str, length: str) -> str:
    """The instruction block sent to the agent. `payload` is the TRIMMED facts view."""
    langname = "Traditional Chinese (繁體中文)" if lang == "zh" else "English"
    shape = ("OUTPUT: one tight paragraph, 2-4 sentences, no headings"
             if length == "brief" else
             "OUTPUT: cover situation, what happened, current risks, recommended actions "
             "and a model note")
    rules = [
        "Use ONLY the exact numbers, counts, and percentages provided in the FACTS DATA below.",
        "DO NOT perform your own math, round numbers, or calculate new percentages.",
        "If a statistic is not directly in FACTS DATA, do not include it.",
        "Write every number EXACTLY as it appears in the JSON: no thousands separators, so write "
        "1501 and not 1,501 or 1 501. Keep decimals exactly as given.",
        "Do NOT number list items with digits (no '1.', '2.', 'Step 3'). Every list entry must be "
        "plain text; the dashboard renders them as bullets itself.",
        'Refer to metrics in PLAIN LANGUAGE, never by their raw JSON field name. Write "the node '
        'alert score", not "p2_node_alert_score"; "the alert threshold", not "p3_alert_threshold".',
        'Do NOT introduce counts or quantities of your own. If you must express a small quantity '
        'that is not in the facts, write it as a word ("both", "all three"), never as a digit.',
        "Report the model's MISSES honestly; never hide false alarms or missed failures.",
        "Recommend actions for a human to approve. Never state or imply that an action was taken, "
        "and never simulate touching infrastructure.",
        f"{shape}. Prioritise what matters operationally: incidents, misses, high-risk items.",
        "Return ONLY the report content itself. No preamble, no self-introduction, no explanation "
        "of what you did.",
        "NEVER mention producing, saving, attaching or downloading a file, and never refer to a "
        "download card, link or attachment. Put the report text in your reply directly.",
        "Do not describe yourself or your process ('I have generated...', 'As an AI...').",
        "If you answer with a JSON object, every value must be a plain STRING of report prose (or "
        "a list of strings), with the single exception of the chart selection described below; "
        "no other nested objects, no file references.",
        # the closed section vocabulary. Without this the agent split one section across
        # `executive_summary` and `executive_summary_part2`, and the report grew a heading reading
        # "Executive summary part2".
        "The JSON keys are a CLOSED SET. Use only these, at most once each: "
        + ", ".join(sorted(_SECTION_TITLES)) + ". "
        "Never invent a key, never suffix one with part2/_2/_continued, and never split one "
        "section across two keys -- if a section is long, keep it in a single string.",
    ]
    body = "\n".join(f"- {r}" for r in rules)
    # Charts belong to the full report panel; a brief report is one paragraph in a sidebar box with
    # nowhere to draw them, so the menu is not sent at all for length="brief".
    menu = f"{_chart_menu_block(lang)}\n" if length == "full" else ""
    return (
        f"WRITE THE ENTIRE RESPONSE IN {langname.upper()}.\n\n"
        f"Generate an operational shift report in {langname} with style/length '{length}'.\n\n"
        f"CRITICAL INSTRUCTIONS:\n{body}\n\n"
        f"{menu}"
        f"FACTS DATA:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"REMINDER: the entire response must be written in {langname}, and must contain the "
        f"report content only."
    )


def _attempt(invoke_url, bearer_secret, prompt, timeout, agent="main"):
    """One HTTP attempt. -> (text, err_class, detail, status)

    err_class is None on success, else one of 'transient' | 'auth' | 'client' | 'shape'.
    `agent` is a label for the call log only; it never changes behaviour.
    """
    headers = {"Authorization": f"Bearer {bearer_secret}", "Content-Type": "application/json"}
    body = {"message": prompt}
    nbytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    t0 = time.time()
    try:
        r = requests.post(invoke_url, headers=headers, json=body, timeout=timeout)
    except requests.exceptions.Timeout as e:
        record_call(agent, invoke_url, nbytes, time.time() - t0, None, e, f"timeout={timeout:.0f}s")
        return None, "transient", "read timeout", None
    except requests.exceptions.ConnectionError as e:
        record_call(agent, invoke_url, nbytes, time.time() - t0, None, e)
        return None, "transient", "could not connect to endpoint", None
    except Exception as e:
        record_call(agent, invoke_url, nbytes, time.time() - t0, None, e)
        return None, "transient", f"request failed ({type(e).__name__})", None

    sc = r.status_code
    record_call(agent, invoke_url, nbytes, time.time() - t0, sc, None, _resp_note(r))
    if sc == 200:
        try:
            data = r.json()
        except ValueError:
            return None, "shape", "response body was not valid JSON", sc
        text = _extract_text(data)
        if not text:
            return None, "shape", "unexpected response shape (no text in reply)", sc
        return text, None, None, sc
    if sc in AUTH_STATUS:
        return None, "auth", f"HTTP {sc} from endpoint (authentication rejected)", sc
    if sc in TRANSIENT_STATUS:
        return None, "transient", f"HTTP {sc} from endpoint", sc
    return None, "client", f"HTTP {sc} from endpoint", sc


def generate_llm(facts: dict, lang: str, length: str):
    """Narrate the facts via the LaplaceAI agent endpoint, retrying transient failures.

    Returns (text, reason). `text` is None when the LLM path could not be used, and `reason` is then
    a short plain-words explanation for the UI's fallback_reason. The bearer secret is never included
    in a reason string, a log line, or an exception message.

    Retries: up to RETRY_ATTEMPTS attempts on 5xx / timeout / connection error, exponential backoff
    with jitter, bounded by RETRY_DEADLINE seconds of total elapsed time. Auth and malformed-request
    failures are NOT retried -- they are configuration errors and retrying only wastes the budget.
    """
    prompt = _build_prompt(trim_facts_for_prompt(facts, lang), lang, length)
    text, reason, _err = invoke_agent(AGENT_MAIN, "LAPLACE_INVOKE_URL", "LAPLACE_BEARER_SECRET",
                                      prompt)
    return text, reason


def invoke_agent(agent, url_env, secret_env, prompt, budget_left=None):
    """The ONE resilient outbound path. Both agents go through it. -> (text, reason, err_class).

    Retries transient failures with exponential backoff and jitter, bounded by the agent's deadline;
    does NOT retry auth or malformed-request failures, which are configuration errors that retrying
    only wastes budget on. Trips that AGENT'S cooldown -- never another's.

    `budget_left`, when given, further caps the deadline: the caller may have already spent most of
    the request's total time budget, and an advisory call must not extend a request the user is
    waiting on.
    """
    p = AGENT_PROFILE.get(agent, AGENT_PROFILE[AGENT_MAIN])
    attempts, deadline = p["attempts"], p["deadline"]
    per_timeout, min_room = p["timeout"], p["min_room"]
    if budget_left is not None:
        deadline = min(deadline, max(0.0, float(budget_left)))

    invoke_url = os.environ.get(url_env)
    bearer_secret = os.environ.get(secret_env)

    # env first, cooldown second, so an active cooldown is never mistaken for a missing key
    if not invoke_url:
        return None, f"{url_env} not set", "no_credentials"
    if not bearer_secret:
        return None, f"{secret_env} not set", "no_credentials"
    remain, why = cooldown_remaining(agent)
    if remain > 0:
        return None, (f"endpoint cooling down after {why or 'an earlier failure'} "
                      f"({int(remain) + 1}s remaining)"), "cooldown"
    if deadline < min_room:
        return None, (f"only {deadline:.0f}s of the request budget left, less than the "
                      f"{min_room:.0f}s needed for one attempt"), "no_budget"

    def cause_label(detail, sc):
        """Short noun phrase for the cooldown message: 'HTTP 504', 'a read timeout', ..."""
        if sc: return f"HTTP {sc}"
        return "a read timeout" if "timeout" in (detail or "") else "a connection failure"

    started = time.time()
    last_detail, last_sc, last_err = "endpoint unavailable", None, "transient"
    for i in range(1, attempts + 1):
        left = deadline - (time.time() - started)
        if i > 1 and left < min_room:
            _log(f"[{agent}] attempt {i} skipped: only {left:.0f}s of the {deadline:.0f}s budget left")
            break
        text, err, detail, sc = _attempt(invoke_url, bearer_secret, prompt,
                                         min(per_timeout, max(left, min_room)), agent=agent)
        if err is None:
            _log(f"[{agent}] attempt {i}/{attempts} ok (HTTP 200, {time.time()-started:.1f}s)")
            return text, None, None
        last_detail, last_sc, last_err = detail, sc, err
        _log(f"[{agent}] attempt {i}/{attempts} failed: {detail} [class={err}]")

        if err == "auth":
            _cooldown(COOLDOWN_AUTH, f"HTTP {sc}", agent)
            return None, (f"{detail} -- check {secret_env}; not retried and paused for "
                          f"{COOLDOWN_AUTH // 60} minutes"), "auth"
        if err == "client":
            _cooldown(COOLDOWN_CLIENT, f"HTTP {sc}", agent)
            return None, f"{detail} -- request rejected as malformed; not retried", "client"
        if err == "shape":
            # the endpoint is up and answered 200: do not trip the breaker, do not retry
            return None, detail, "shape"

        if i < attempts:                             # transient -> back off and try again
            delay = RETRY_BASE * (2 ** (i - 1))
            delay *= 0.75 + random.random() * 0.5    # +/-25% jitter
            if (time.time() - started) + delay + min_room > deadline:
                _log(f"[{agent}] backoff would exceed the retry deadline; giving up")
                break
            _log(f"[{agent}] backing off {delay:.1f}s before attempt {i + 1}")
            time.sleep(delay)

    tried = min(i, attempts)
    _cooldown(COOLDOWN_TRANSIENT, cause_label(last_detail, last_sc), agent)
    is_timeout = last_sc is None and "timeout" in (last_detail or "")
    return (None, f"{last_detail} after {tried} attempt{'' if tried == 1 else 's'}",
            "timeout" if is_timeout else last_err)


# ================================================================================================
# 3.5  DATA AUDITOR (agent 2) -- ADVISORY ONLY
#
# Contract, and every word of it is load-bearing: the auditor never blocks, never changes `mode`,
# and never decides whether a report is served. A dead, slow or cooling-down auditor degrades to a
# normal report carrying an advisory flag. It is a second opinion, not a gate.
#
# WHY THE PAYLOAD IS THE SAME TRIMMED VIEW THE NARRATOR SAW.
# The auditor is asked "does this narrative match the facts?". If it is shown LESS than the narrator
# was, it can flag a true statement as unsupported simply because the supporting field was withheld
# -- a false audit flag, which is worse than no audit. So the payload is literally
# trim_facts_for_prompt(facts, lang): the identical bytes the narrator was given. That makes the
# false-flag case impossible by construction rather than by careful field-picking, because every
# claim the narrator could possibly have made traces to something in this payload.
# What that excludes, relative to the full facts: window.start_ts/end_ts (epoch ints; the prose uses
# the ISO forms), cluster_now.nodes_scored (referenced by no report section), outcome-example arrays
# capped at 2 and list sections at 4, and the other language's caveat string. None of those can
# appear in the narrative, because the narrator never saw them either.
#
# WHY THE TIMEOUT IS 60s AND NOT 15s.
# Measured on this endpoint: the auditor answered 0/5 calls at a 15s timeout and 2/2 at 120s, taking
# 19.0s and 35.1s. The narrator's SUCCESSFUL calls on the same host take 58-61s. A 15s budget could
# never have succeeded; it bought nothing and cost a full 15s of user-visible latency on every
# report. See AGENT_PROFILE[AGENT_AUDITOR].
# ================================================================================================
ADV_AUDIT_FLAG        = "auditor_flag"           # the auditor ran and DISAGREED with the narrative
ADV_AUDIT_NOCREDS     = "auditor_no_credentials"
ADV_AUDIT_FAILED      = "auditor_failed"
ADV_AUDIT_TIMEOUT     = "auditor_timeout"
ADV_AUDIT_COOLDOWN    = "auditor_cooldown"
ADV_AUDIT_UNPARSEABLE = "auditor_unparseable"
ADV_AUDIT_BUDGET      = "auditor_skipped_budget"

# state -> (advisory code or None, human sentence). "ok" and "flagged" mean the auditor really ran.
_AUDIT_ADVISORY = {
    "ok":           (None,                  "Auditor agreed with the narrative."),
    "flagged":      (ADV_AUDIT_FLAG,        "Auditor flagged the narrative."),
    "no_credentials": (ADV_AUDIT_NOCREDS,   "Auditor is not configured, so this report was not reviewed."),
    "cooldown":     (ADV_AUDIT_COOLDOWN,    "Auditor is in cooldown after an earlier failure, so this report was not reviewed."),
    "timeout":      (ADV_AUDIT_TIMEOUT,     "Auditor timed out, so this report was not reviewed."),
    "failed":       (ADV_AUDIT_FAILED,      "Auditor call failed, so this report was not reviewed."),
    "unparseable":  (ADV_AUDIT_UNPARSEABLE, "Auditor replied with something that is not the expected verdict, so its answer was discarded."),
    "skipped_budget": (ADV_AUDIT_BUDGET,    "Auditor was skipped to stay inside the request time budget."),
    "skipped":      (None,                  "Auditor does not run for this report."),
}

# ---- the contract ---------------------------------------------------------------------------------
# WHY THIS PROMPT LOOKS NOTHING LIKE THE ONE IT REPLACES.
#
# Measured, live, across four virtual timestamps: five narratives containing four confirmed false
# claims, and the auditor returned is_valid=true on all five. The architecture was not the problem --
# the contract was, in five specific ways, each of which is answered below:
#
#   1. It returned ONE BOOLEAN over ~600 words, and offered the `true` variant first. "Valid" was the
#      cheap answer and nothing forced per-claim engagement.
#      -> the output is now a LIST OF FINDINGS, each quoting the span it objects to, and the auditor
#         must also enumerate the relational claims it checked. An empty findings list is still the
#         pass condition, but it now has to be paid for with a list of what was examined.
#
#   2. It was asked to "verify that numbers and facts match" -- which is exactly what the
#      deterministic hard gate already does, perfectly, BEFORE the auditor is ever called. It was
#      being asked to re-derive a solved problem, and the class that actually gets through is a
#      different one.
#      -> the prompt now states outright that every number is already verified and that re-checking
#         existence is not its job.
#
#   3. The facts arrived as a bare JSON dump. The identities that hold between them live in Python
#      (see the cohort model above) and the auditor was never told they exist, so it had no basis on
#      which to detect a violation.
#      -> cohort_prose(facts) is injected alongside the JSON, generated from the SAME structures
#         verify_cohort.py asserts, so the prompt and the tests cannot drift apart.
#
#   4. It contained no example of the target failure class, though documented instances existed.
#      -> three worked examples of real false claims, each with the reason it is false, plus a
#         CORRECT narrative as a counter-example so the auditor is not simply taught to object.
#
#   5. It audited the narrative only. Three of the four live failures were in the body, but one was
#      in a caption, and captions were structurally invisible to it.
#      -> captions are passed in and audited under the same rules.
#
# Everything about HOW it is called is unchanged: invoke_agent, the auditor's own cooldown namespace
# and retry profile, the 60s timeout (a 15s one answered 0/5), mode=="llm" and length=="full" only,
# inside REPORT_TIME_BUDGET, seven distinct advisory codes, fail-open throughout.
_AUDIT_EXAMPLES = """\
WORKED EXAMPLES. These are real false claims that this auditor previously passed. Each one uses only
real numbers, so the numeric checker had nothing to say about any of them.

  FALSE: "P3 flagged 331 jobs at submission, and the 145 resolved failures sit within that cohort."
    Why: failures_resolved counts caught AND missed failures. The missed ones were never flagged, so
    the 145 cannot be inside the 331. Finding severity: high.

  FALSE: "the 460 flagged jobs, including the 34 missed failures"
    Why: same error in a caption. A missed failure is one that was NOT flagged; it cannot be part of
    the flagged set. Finding severity: high.

  FALSE: "Most of the flagged jobs have already been caught."  (facts: 17% caught, 71% still pending)
    Why: contains NO number, so nothing can be checked numerically -- and it still contradicts the
    proportions. "Most" asserts a majority; the majority are pending, not caught. Finding severity:
    high. A qualitative word is a claim about the data and you must check it against the numbers.

  FALSE: "four TIMEOUT-predicted jobs appear on the watch list"  (facts: six such jobs)
    Why: a quantity written as a WORD is still a quantity. Spelled-out numbers are not exempt from
    matching the facts. Finding severity: medium.

  CORRECT, DO NOT FLAG: "P3 flagged 331 jobs at submission; 108 have been confirmed as real
  failures, 73 were false alarms and 150 have not finished yet. Separately, 37 failures were never
  flagged at all."
    Why it is fine: 108 + 73 + 150 = 331 is the flagged partition, and the 37 misses are stated as a
    SEPARATE quantity rather than as a slice of the 331. Reporting misses honestly alongside the
    flagged cohort is correct and required. Flagging this would be a false positive.
"""

def _audit_prompt(payload: dict, draft: str, identities: str = "", captions=None) -> str:
    cap_block = ""
    if captions:
        lines = "\n".join(f'  - chart "{cid}": {txt}' for cid, txt in captions if str(txt).strip())
        if lines:
            cap_block = ("CHART CAPTIONS THE SAME WRITER PRODUCED (audit these under the same rules "
                         "as the narrative -- a caption is a claim):\n" + lines + "\n\n")
    return (
        "You are a Data Auditor reviewing an operational shift report. You are the SECOND check, "
        "not the first.\n\n"

        "WHAT HAS ALREADY BEEN DONE FOR YOU, so you do not waste effort repeating it:\n"
        "Deterministic code has already verified that EVERY number in this report traces to a value "
        "in the FACTS DATA below. There are no invented figures. Checking whether a number exists in "
        "the facts is NOT your job and finding that it does is not a result.\n\n"

        "WHAT IS ACTUALLY YOUR JOB -- the RELATIONSHIPS between numbers, which no numeric check can "
        "see because both numbers are real:\n"
        "  (a) CONTAINMENT: a quantity described as being inside, among, part of or a subset of a "
        "group it does not belong to.\n"
        "  (b) DENOMINATOR: a rate, share, proportion or catch rate computed or described over the "
        "wrong base set.\n"
        "  (c) CAUSATION: a cause-and-effect or explanatory claim the facts do not support. The "
        "facts are counts and measurements; they rarely license 'because'.\n"
        "  (d) QUALITATIVE CONTRADICTION: a word like most, few, nearly all, the majority, largely, "
        "rarely, dominated by -- with NO number attached -- that contradicts the underlying "
        "proportions. These are invisible to every numeric check and you are the only thing that can "
        "catch them.\n"
        "  (e) MATERIAL OMISSION: bad news that is present in the facts and absent from the report -- "
        "missed failures, a low catch rate, a node onset. Only flag an omission when the fact is in "
        "the FACTS DATA below.\n\n"

        "HOW THE COUNTS RELATE TO EACH OTHER. Read this before judging any containment claim:\n"
        f"{identities}\n\n"

        f"{_AUDIT_EXAMPLES}\n"

        f"FACTS DATA:\n{json.dumps(payload, ensure_ascii=False)}\n\n"

        f"{cap_block}"

        f"REPORT NARRATIVE TO AUDIT:\n{draft}\n\n"

        "Judge ONLY against the FACTS DATA above. It is the same data the writer was given, so a "
        "fact that is absent from it was absent for the writer too and must never be flagged as "
        "missing or unsupported.\n\n"

        "Return ONLY a JSON object, with exactly these two keys:\n"
        '{\n'
        '  "relational_claims_checked": ["a short description of each relationship you examined, '
        'whether or not it was wrong -- at least three entries"],\n'
        '  "findings": [\n'
        '    {"quote": "the exact span of text you object to, copied verbatim",\n'
        '     "contradicts": "the fact key or identity it violates",\n'
        '     "why": "one sentence on why it is false",\n'
        '     "severity": "high | medium | low",\n'
        '     "location": "narrative | caption"}\n'
        '  ]\n'
        '}\n'
        "An EMPTY findings list is the correct answer for a sound report, and sound reports are "
        "common -- do not manufacture a finding to look diligent. But an empty list is only credible "
        "alongside a populated relational_claims_checked list showing what you actually examined."
    )


MAX_AUDIT_FINDINGS = 8       # what is carried into the advisories; the rest are counted, not listed

def _coerce_findings(parsed: dict) -> list:
    """The auditor's findings list, normalised. Tolerates a partly-malformed entry rather than
    discarding a whole audit for one bad field."""
    raw = parsed.get("findings")
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for f in raw:
        if isinstance(f, str):
            f = {"why": f}
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "medium").strip().lower()
        if sev not in ("high", "medium", "low"):
            sev = "medium"
        loc = str(f.get("location") or "narrative").strip().lower()
        entry = {"quote": str(f.get("quote") or "").strip()[:300],
                 "contradicts": str(f.get("contradicts") or "").strip()[:200],
                 "why": str(f.get("why") or f.get("reason") or "").strip()[:400],
                 "severity": sev,
                 "location": ("caption" if "caption" in loc else "narrative")}
        if entry["quote"] or entry["why"]:
            out.append(entry)
    return out


def audit_llm(facts: dict, draft_narrative: str, lang: str = "en", budget_left=None,
              captions=None) -> dict:
    """Second-opinion review of a narrative. NEVER raises, NEVER blocks. -> audit state dict:

        {"ran": bool, "state": str, "is_valid": bool|None, "reason": str|None, "findings": [...],
         "checked": [...], "latency_s": float, "advisory_code": str|None, "message": str}

    `state` is one of the keys of _AUDIT_ADVISORY. `ran` is True only when the auditor actually
    answered -- which is the point: previously every failure path returned is_valid=True, so a
    100%-dead auditor was indistinguishable from one that had read the report and approved it.

    `is_valid` is retained and means exactly "the findings list came back empty", so every existing
    consumer keeps working while the informative payload is `findings`.

    `captions` is [(chart_id, caption)] for the AGENT'S OWN captions. Python-written captions are
    excluded by the caller: they are built from the chart and there is nothing to audit.
    """
    t0 = time.time()

    def out(state, is_valid=None, reason=None, findings=None, checked=None):
        code, sentence = _AUDIT_ADVISORY.get(state, (ADV_AUDIT_FAILED, "Auditor state unknown."))
        return {"ran": state in ("ok", "flagged"), "state": state, "is_valid": is_valid,
                "reason": reason, "findings": list(findings or []), "checked": list(checked or []),
                "latency_s": round(time.time() - t0, 2),
                "advisory_code": code, "message": sentence}

    prompt = _audit_prompt(trim_facts_for_prompt(facts, lang), draft_narrative,
                           identities=cohort_prose(facts or {}), captions=captions)
    text, reason, err = invoke_agent(AGENT_AUDITOR, "LAPLACE_AUDITOR_INVOKE_URL",
                                     "LAPLACE_AUDITOR_BEARER_SECRET", prompt,
                                     budget_left=budget_left)
    if text is None:
        state = {"no_credentials": "no_credentials", "cooldown": "cooldown",
                 "timeout": "timeout", "no_budget": "skipped_budget"}.get(err, "failed")
        return out(state, reason=reason)

    parsed = _parse_json_object(text)
    # Both shapes are accepted. The findings contract is what is asked for; the older single-boolean
    # verdict is still understood rather than being thrown away as unparseable, because a reply that
    # says something is worth more than one discarded for using last month's schema.
    if isinstance(parsed, dict) and "findings" in parsed:
        findings = _coerce_findings(parsed)
        checked = [str(x)[:200] for x in (parsed.get("relational_claims_checked") or [])
                   if str(x).strip()][:12]
        if findings:
            top = findings[0]
            reason = f"{top['why'] or top['quote']}" + (f" (contradicts {top['contradicts']})"
                                                        if top["contradicts"] else "")
            return out("flagged", is_valid=False, reason=reason, findings=findings, checked=checked)
        return out("ok", is_valid=True, findings=[], checked=checked)

    if isinstance(parsed, dict) and "is_valid" in parsed:
        valid = bool(parsed.get("is_valid"))
        legacy = ([] if valid else [{"quote": "", "contradicts": "", "severity": "medium",
                                     "location": "narrative",
                                     "why": str(parsed.get("reason") or "discrepancy detected")}])
        return out("ok" if valid else "flagged", is_valid=valid,
                   reason=parsed.get("reason"), findings=legacy)

    return out("unparseable", reason=f"auditor reply was not the expected verdict: {text[:120]}")


def audit_advisories(audit: dict) -> list:
    """The advisory list for an audit outcome. [] when the auditor ran and found nothing.

    A flagged audit now produces ONE advisory PER FINDING, each carrying the span the auditor
    objected to and the fact it says that span contradicts. A single collapsed sentence was fine for
    a boolean verdict; it is the wrong shape for a list, because an operator's next action is to look
    at the quoted text.
    """
    if not isinstance(audit, dict):
        return []
    code = audit.get("advisory_code")
    if not code:
        return []
    msg = audit.get("message") or "Auditor did not review this report."
    detail = audit.get("reason")
    if code != ADV_AUDIT_FLAG:
        return [{"code": code, "message": f"{msg} ({detail})" if detail else msg}]

    findings = audit.get("findings") or []
    if not findings:                                   # flagged with no itemised finding
        return [{"code": code,
                 "message": f"Auditor Agent flagged narrative: {detail or 'discrepancy detected'}"}]

    out = []
    for f in findings[:MAX_AUDIT_FINDINGS]:
        where = f.get("location") or "narrative"
        quote = (f.get("quote") or "").strip()
        bits = [f"Auditor Agent flagged the {where} [{f.get('severity', 'medium')}]"]
        if quote:
            bits.append(f'— "{quote}"')
        if f.get("why"):
            bits.append(f"— {f['why']}")
        if f.get("contradicts"):
            bits.append(f"(contradicts {f['contradicts']})")
        out.append({"code": code, "message": " ".join(bits)})
    extra = len(findings) - len(out)
    if extra > 0:
        out.append({"code": code, "message": f"Auditor Agent raised {extra} further finding"
                                             f"{'' if extra == 1 else 's'} not listed here."})
    return out

# ============================================================ 4. TEMPLATE FALLBACK (deterministic)
def _plural(n, en): return "" if (en and n == 1) else ("" if not en else "s")

def _ids(examples):
    return ", ".join(str(e["job_id"]) for e in examples)

def render_template(facts: dict, lang: str, length: str) -> str:
    en = lang != "zh"
    w = facts["window"]; s = facts["settings"]; jw = facts["jobs_window"]
    po = facts["prediction_outcomes"]; on = facts["node_onsets"]
    nfail = po["failures_resolved"]; corr = po["correct_warnings"]["count"]
    miss = po["misses"]["count"]; fa = po["false_alarms"]["count"]
    pend_out = po["pending_outcome"]["count"]; flagged = po["flagged_total"]
    hn = facts["high_risk_nodes"]; hj = facts["high_risk_jobs"]; cn = facts["cluster_now"]
    thr = s["p3_alert_threshold"]; tri = s["p2_triage_pct"]
    incident = (on["count"] > 0) or (nfail > 0)

    if length == "brief":
        if en:
            head = f"Last {w['hours']} h: {jw['submitted']} jobs submitted ({flagged} flagged by P3), {jw['ended_in_window']} ended"
            head += f" — {jw['ended_in_window_failed']} FAILED, {jw['ended_in_window_timeout']} TIMEOUT, {jw['ended_in_window_oom']} OOM."
            if nfail:
                head += (f" Of the flagged jobs {corr} have failed and {fa} completed, with {pend_out} "
                         f"still running; P3 missed {miss} failure{_plural(miss,en)}.")
            else:
                head += " No submitted job has failed yet in the window."
            head += f" {on['count']} node anomaly onset{_plural(on['count'],en)}."
            head += f" Watching {len(hn)} node{_plural(len(hn),en)} and {len(hj)} high-risk job{_plural(len(hj),en)}."
            return head
        else:
            head = f"近 {w['hours']} 小時：提交 {jw['submitted']} 個任務 (P3 標記 {flagged})，結束 {jw['ended_in_window']} 個"
            head += f" — {jw['ended_in_window_failed']} 失敗、{jw['ended_in_window_timeout']} 逾時、{jw['ended_in_window_oom']} 記憶體不足。"
            if nfail:
                head += f" 已標記任務中 {corr} 個失敗、{fa} 個完成、{pend_out} 個仍在執行；另漏報 {miss} 次失敗。"
            else:
                head += " 視窗內提交的任務尚無失敗。"
            head += f" 節點異常 onset {on['count']} 次。目前關注 {len(hn)} 個節點、{len(hj)} 個高風險任務。"
            return head

    # ---- full markdown ----
    L = []
    if en:
        L.append(f"# Shift report — {w['start_iso']} → {w['end_iso']}\n")
        state = "elevated" if incident else "nominal"
        L.append(f"**Situation.** Over the last {w['hours']} h the cluster state is **{state}**: "
                 f"{cn['active_jobs']} active jobs ({cn['running']} running, {cn['pending']} pending), "
                 f"{cn['nodes_anomalous']} node(s) in a live anomaly window, utilisation ~{cn['utilisation_pct']}% (power proxy).\n")
        L.append("## What happened")
        if on["count"]:
            L.append(f"- **{on['count']} node anomaly onset(s)** detected by P2:")
            for e in on["events"]:
                L.append(f"    - node{e['node']:04d} (rack {e['rack']}) at {e['time']} [{e['kind']}]")
        else:
            L.append("- No node anomaly onsets in the window.")
        L.append(f"- **{jw['ended_in_window']} jobs ended in the window** (submitted at any time): "
                 f"{jw['ended_in_window_failed']} FAILED, {jw['ended_in_window_timeout']} TIMEOUT, "
                 f"{jw['ended_in_window_oom']} OOM, {jw['ended_in_window_completed']} COMPLETED.")
        # Scoring switches cohort here, and says so: everything below counts jobs SUBMITTED in the
        # window, so the flagged buckets add up to the flagged total.
        L.append(f"- **Of the {jw['submitted']} jobs submitted in the window, P3 flagged {flagged}**: "
                 f"**{corr} have since failed** (correct warnings), **{fa} completed** (false alarms), "
                 f"and **{pend_out} are still running** (no outcome yet).")
        if nfail:
            L.append(f"- **Warnings:** against {nfail} failure(s) that have already ended, P3 caught "
                     f"**{corr}** and **missed {miss}**"
                     + (f" (catch rate {po['catch_rate_pct']}%)." if po['catch_rate_pct'] is not None else "."))
            if corr and po["correct_warnings"]["examples"]:
                L.append(f"    - correctly warned: jobs {_ids(po['correct_warnings']['examples'])}")
            if miss and po["misses"]["examples"]:
                L.append(f"    - missed (failed but under threshold): jobs {_ids(po['misses']['examples'])}")
            if fa and po["false_alarms"]["examples"]:
                L.append(f"    - false alarms (flagged but completed): jobs {_ids(po['false_alarms']['examples'])}")
        else:
            L.append("- **Warnings:** none of the jobs submitted in this window has failed yet, so "
                     "there is no catch rate to report.")
        L.append("\n## Current risks")
        if hn:
            L.append("- Nodes to watch:")
            for x in hn:
                sens = f"{x['temp_c']}°C / {x['power_w']}W / {x['fan_rpm']}rpm" if x['temp_c'] is not None else f"PSU-0 {x['psu0_w']}W (idle)"
                L.append(f"    - node{x['node']:04d} (rack {x['rack']}) — P2 {x['risk_pct']}% [{x['state']}{', onset' if x['onset'] else ''}] — {sens}")
        else:
            L.append("- No in-scope nodes above the watch level right now.")
        if hj:
            L.append("- Jobs to watch (running/queued):")
            for x in hj:
                L.append(f"    - job {x['job_id']} ({x['user']}) — P3 {x['risk_pct']}% predicted {x['pred_type']} [{x['status']}]")
        L.append("\n## Recommended actions")
        acts = []
        for x in hn:
            if x["onset"] or x["risk_pct"] >= 50:
                acts.append(f"Inspect node{x['node']:04d} (rack {x['rack']}, P2 {x['risk_pct']}%) — suggest checking its power/health.")
        pend = [x for x in hj if x["status"] == "PENDING"]
        for x in pend[:3]:
            acts.append(f"Consider notifying {x['user']} about queued job {x['job_id']} (predicted {x['pred_type']}, {x['risk_pct']}%) before it starts.")
        if miss:
            acts.append(f"Review the {miss} missed failure(s); if this recurs, consider lowering the P3 alert threshold below {thr}.")
        if fa > corr and (fa + corr) > 0:
            acts.append(f"False alarms ({fa}) exceed correct warnings ({corr}); consider raising the threshold above {thr}.")
        if not acts:
            acts.append("No action required beyond routine monitoring.")
        for a in acts:
            L.append(f"- {a} *(recommendation — requires human approval; no action has been taken)*")
        L.append("\n## Model note")
        L.append(f"- Settings in force: P3 alert threshold **{thr}**, P2 triage scoring the riskiest **{tri}%** of node-slots"
                 + (f" (PSU-0 ≤ {s['p2_cutoff_watts']} W)." if s["p2_cutoff_watts"] else "."))
        L.append(f"- Caveat: {facts['model_note']['caveat_en']}")
        return "\n".join(L)
    else:
        L.append(f"# 交接報告 — {w['start_iso']} → {w['end_iso']}\n")
        state = "偏高" if incident else "正常"
        L.append(f"**現況。** 近 {w['hours']} 小時叢集狀態為 **{state}**："
                 f"{cn['active_jobs']} 個活躍任務（{cn['running']} 執行中、{cn['pending']} 等待中），"
                 f"{cn['nodes_anomalous']} 個節點處於即時異常視窗，使用率約 {cn['utilisation_pct']}%（功耗 proxy）。\n")
        L.append("## 發生了什麼")
        if on["count"]:
            L.append(f"- P2 偵測到 **{on['count']} 次節點異常 onset**：")
            for e in on["events"]:
                L.append(f"    - node{e['node']:04d}（機櫃 {e['rack']}）於 {e['time']} [{e['kind']}]")
        else:
            L.append("- 視窗內無節點異常 onset。")
        L.append(f"- **視窗內結束 {jw['ended_in_window']} 個任務**（提交時間不限）："
                 f"{jw['ended_in_window_failed']} 失敗、{jw['ended_in_window_timeout']} 逾時、"
                 f"{jw['ended_in_window_oom']} 記憶體不足、{jw['ended_in_window_completed']} 完成。")
        # 以下改以「視窗內提交」的任務為統計對象，故各類別加總等於標記總數。
        L.append(f"- **視窗內提交的 {jw['submitted']} 個任務中，P3 標記了 {flagged} 個**："
                 f"**{corr} 個已失敗**（正確告警）、**{fa} 個已完成**（誤報）、"
                 f"**{pend_out} 個仍在執行**（尚無結果）。")
        if nfail:
            L.append(f"- **告警成效：** 已結束的 {nfail} 次失敗中，P3 命中 **{corr}**、**漏報 {miss}**"
                     + (f"（命中率 {po['catch_rate_pct']}%）。" if po['catch_rate_pct'] is not None else "。"))
            if corr and po["correct_warnings"]["examples"]:
                L.append(f"    - 正確告警：任務 {_ids(po['correct_warnings']['examples'])}")
            if miss and po["misses"]["examples"]:
                L.append(f"    - 漏報（失敗但低於門檻）：任務 {_ids(po['misses']['examples'])}")
            if fa and po["false_alarms"]["examples"]:
                L.append(f"    - 誤報（標記但完成）：任務 {_ids(po['false_alarms']['examples'])}")
        else:
            L.append("- **告警成效：** 視窗內提交的任務尚無失敗結束，故無命中率可報告。")
        L.append("\n## 當前風險")
        if hn:
            L.append("- 需關注的節點：")
            for x in hn:
                sens = f"{x['temp_c']}°C / {x['power_w']}W / {x['fan_rpm']}rpm" if x['temp_c'] is not None else f"PSU-0 {x['psu0_w']}W（閒置）"
                L.append(f"    - node{x['node']:04d}（機櫃 {x['rack']}）— P2 {x['risk_pct']}% [{x['state']}{'，onset' if x['onset'] else ''}] — {sens}")
        else:
            L.append("- 目前無在分流範圍內且超過關注水準的節點。")
        if hj:
            L.append("- 需關注的任務（執行/排隊中）：")
            for x in hj:
                L.append(f"    - 任務 {x['job_id']}（{x['user']}）— P3 {x['risk_pct']}% 預測 {x['pred_type']} [{x['status']}]")
        L.append("\n## 建議行動")
        acts = []
        for x in hn:
            if x["onset"] or x["risk_pct"] >= 50:
                acts.append(f"檢查 node{x['node']:04d}（機櫃 {x['rack']}，P2 {x['risk_pct']}%）— 建議查看其功耗/健康狀態。")
        pend = [x for x in hj if x["status"] == "PENDING"]
        for x in pend[:3]:
            acts.append(f"考慮通知 {x['user']}：排隊中任務 {x['job_id']}（預測 {x['pred_type']}，{x['risk_pct']}%）於啟動前先行處理。")
        if miss:
            acts.append(f"檢視 {miss} 次漏報失敗；若持續發生，可考慮將 P3 告警門檻調降至 {thr} 以下。")
        if fa > corr and (fa + corr) > 0:
            acts.append(f"誤報（{fa}）多於正確告警（{corr}）；可考慮將門檻調高於 {thr}。")
        if not acts:
            acts.append("除例行監控外，暫無需採取行動。")
        for a in acts:
            L.append(f"- {a} *(建議事項 — 需人工核准；尚未執行任何動作)*")
        L.append("\n## 模型說明")
        L.append(f"- 生效設定：P3 告警門檻 **{thr}**，P2 分流只評分最可疑的 **{tri}%** 節點槽位"
                 + (f"（PSU-0 ≤ {s['p2_cutoff_watts']} W）。" if s["p2_cutoff_watts"] else "。"))
        L.append(f"- 注意：{facts['model_note']['caveat_zh']}")
        return "\n".join(L)


# ============================================================ 4b. CHARTS (selected by the agent,
#                                                                        computed in Python)
def select_charts(obj):
    """The agent's chart selection -> ([(ChartId, caption)], [advisory, ...]).

    The agent may name ids and write captions; it may do nothing else. Every id is checked against
    the ChartId enum. An id outside the enum is DROPPED SILENTLY from the output -- it never reaches
    a renderer and never becomes a broken panel -- and raises one advisory so the drop is visible
    rather than mysterious. The selection is capped at MAX_CHARTS; the overflow is dropped without
    an advisory, since selecting too many is not an error, just more than the panel can carry.
    """
    advisories, picked, seen = [], [], set()
    key, entries = _chart_entries(obj or {})
    if not entries:
        return [], advisories

    for i, entry in enumerate(entries):
        if isinstance(entry, dict):
            raw = next((entry[k] for k in entry if str(k).strip().lower() in _CHART_ID_FIELDS), None)
            caption = str(entry.get("caption") or entry.get("text") or "").strip()
        else:                                   # a bare id string, no caption
            raw, caption = entry, ""
        cid = chartreg.coerce_id(raw)
        if cid is None:
            advisories.append({"code": ADV_CHART_ID,
                               "message": f"Agent asked for chart '{raw}' at {key}[{i}], which is "
                                          f"not a known chart id; it was dropped."})
            continue
        if cid in seen:                          # a duplicate is not an error, just redundant
            continue
        seen.add(cid)
        picked.append((cid, caption))

    return picked[:MAX_CHARTS], advisories


def assemble_charts(selection, facts, store, t, policies, lang):
    """Render the selected charts. -> (rendered, unavailable).

    `rendered` entries carry everything the frontend needs to draw: id, type, title reference,
    caption, labels and datasets. Anything selected that cannot be drawn honestly is left out of
    `rendered` and reported in `unavailable` with the renderer's own reason.

    Two kinds of chart are appended regardless of what the agent chose, both with Python captions:

      * NODE_FEATURE_CONTRIBUTIONS -- the "why is this flagged" chart. It answers the question the
        drill-down panel exists for and the agent does not reliably pick it.
      * the halves of a MANDATORY PAIR -- the failures-over-time bars and lines plot identical
        numbers from one aggregation, so showing one without the other asks the reader to infer the
        view they were not given. Selecting either pulls in the other; selecting neither pulls in
        both. Nothing is forced into the output that is not independently available: an appended
        chart still goes through its renderer and can still report itself unavailable.
    """
    rendered, unavailable = [], []
    have = {cid for cid, _ in selection}
    partner = {}
    for a, b in chartreg.MANDATORY_PAIRS:
        partner[a], partner[b] = b, a

    # A missing partner is inserted IMMEDIATELY AFTER the chart that pulled it in, not at the end:
    # the whole point of the pair is a side-by-side comparison, which fails if the two halves end up
    # separated by three other charts.
    chosen = []
    for cid, caption in selection:
        chosen.append((cid, caption))
        mate = partner.get(cid)
        if mate is not None and mate not in have:
            chosen.append((mate, ""))
            have.add(mate)
    # neither half selected -> append the whole pair, still adjacent
    for a, b in chartreg.MANDATORY_PAIRS:
        if a not in have and b not in have:
            chosen.extend([(a, ""), (b, "")])
            have.update((a, b))
    if ChartId.NODE_FEATURE_CONTRIBUTIONS not in have:
        chosen.append((ChartId.NODE_FEATURE_CONTRIBUTIONS, ""))

    for cid, caption in chosen:
        res = chartreg.render(cid, facts, store, t, policies)
        if not res.available:
            unavailable.append(res.as_unavailable_entry())
            continue
        chart = dict(res.chart)
        caption = (caption or "").strip()
        chart["caption"] = caption or chartreg.default_caption(cid, facts, lang, chart)
        chart["caption_source"] = "agent" if caption else "python"
        rendered.append(chart)
    return rendered, unavailable


# ============================================================ 5. BUILD (facts + narrate + cache)
_CACHE = {}
_LOCK = threading.Lock()

# ---- disk-backed prewarm cache -----------------------------------------------------------------
# _CACHE lives in ONE process's memory, so a prewarm script run separately would populate its own
# dict and do nothing for the server -- the prewarm would be a no-op. Successful reports are
# therefore also mirrored to a small JSON file that any process can read. Disk entries ignore
# CACHE_TTL: they exist precisely so a prepared demo never depends on a live call. Every filesystem
# operation degrades silently, so a read-only deployment behaves exactly as before.
PREWARM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "report_cache.json")

def _key_str(key) -> str:
    return "|".join(str(x) for x in key)

# The prewarm file is read on every cache MISS, and the brief report is polled every ~12s with a key
# that changes constantly, so that is a hot path. Committing prepared demo reports makes the file
# permanently present and roughly a megabyte, which would mean re-parsing a megabyte of JSON several
# times a minute. It is therefore memoised on (mtime, size) -- a cheap stat instead of a parse, and
# still picks up a file rewritten by prewarm_reports.py in another process.
_DISK_CACHE = {"sig": None, "data": {}}

def _disk_load() -> dict:
    try:
        st = os.stat(PREWARM_PATH)
        sig = (st.st_mtime_ns, st.st_size)
    except Exception:
        _DISK_CACHE["sig"], _DISK_CACHE["data"] = None, {}
        return {}
    if sig == _DISK_CACHE["sig"]:
        return _DISK_CACHE["data"]
    try:
        with open(PREWARM_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    _DISK_CACHE["sig"], _DISK_CACHE["data"] = sig, data
    return data

def _disk_save(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(PREWARM_PATH), exist_ok=True)
        tmp = PREWARM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, PREWARM_PATH)
        _DISK_CACHE["sig"] = None      # force the next read to re-parse what we just wrote
        return True
    except Exception:
        return False

def _cache_get(key):
    with _LOCK:
        v = _CACHE.get(key)
        if v and (time.time() - v[1]) < CACHE_TTL:
            return v[0]
        if v:
            _CACHE.pop(key, None)
    hit = _disk_load().get(_key_str(key))          # prewarmed entries never expire
    if hit:
        return {**hit, "prewarmed": True}
    return None

def _cache_put(key, out, to_disk=False):
    with _LOCK:
        _CACHE[key] = (out, time.time())
        if len(_CACHE) > CACHE_MAX:
            for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][1])[: len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(k, None)
    if to_disk:                                   # only prewarm_reports.py asks for this
        d = _disk_load()
        d[_key_str(key)] = out
        _disk_save(d)

def cache_key(clock_ts, window_h, lang, length, policies):
    """The cache key, exposed so prewarm_reports.py writes entries the server will actually find."""
    return (int(clock_ts) // CACHE_BUCKET, round(float(window_h), 2), lang, length,
            round(float(policies["alert_threshold"]), 3), int(float(policies["node_filter_pct"])))

def build_report(store, clock, policies, window_h=6, lang="zh", length="brief", nocache=False,
                 persist=False) -> dict:
    t = clock.now_ts()
    key = cache_key(t, window_h, lang, length, policies)
    if not nocache:
        cached = _cache_get(key)
        if cached:
            return {**cached, "cached": True}

    started = time.time()
    facts = assemble_facts(store, t, policies, window_h)
    allowed = allowed_numbers(facts)
    schema = schema_strip_re(facts)          # the facts' own digit-bearing key names
    raw, reason = generate_llm(facts, lang, length)
    unver, bad_field, advisories, draft = [], None, [], None
    selection, chart_adv = [], []
    audit = {"ran": False, "state": "skipped", "is_valid": None, "reason": None,
             "findings": [], "checked": [], "latency_s": 0.0, "advisory_code": None,
             "message": _AUDIT_ADVISORY["skipped"][1]}

    if raw is not None:
        # shape the reply (JSON contract -> markdown, or prose as-is); `llm` is the composed text
        # even when the hard gate is about to reject it, so the draft can be preserved.
        llm, content_reason, claims, obj, off_vocab = compose_narration(raw, lang, length)
        # the agent's chart picks: enum-validated, unknown ids dropped with an advisory each
        selection, chart_adv = select_charts(obj)
        # ADVISORY LAYER: never blocks, never feeds back into `mode`.
        advisories = (advisory_review(llm, lang, length, obj) + chart_adv
                      + section_advisories(off_vocab))
    else:
        llm, content_reason, claims, obj = None, None, None, None

    if llm is None:
        text, mode = render_template(facts, lang, length), "template"
    elif content_reason:
        # HARD GATE (a): nothing to display at all.
        text, mode, reason = render_template(facts, lang, length), "template_llm_rejected", content_reason
        draft = {"text": llm, "field": None, "unverified": [], "reason": content_reason}
    else:
        # HARD GATE (b): every asserted number must trace to a fact.
        unver, bad_field = check_claims(claims, llm, allowed, schema)
        if not unver:
            text, mode, reason = llm, "llm", None
        else:
            text, mode = render_template(facts, lang, length), "template_llm_rejected"
            reason = (f"numeric check failed{' in ' + bad_field if bad_field else ''}: "
                      f"{len(unver)} unmatched number"
                      f"{'' if len(unver) == 1 else 's'} ({', '.join(unver)})")
            draft = {"text": llm, "field": bad_field, "unverified": unver, "reason": reason}

    # ---- AGENT 2 (Data Auditor): advisory second opinion --------------------------------------
    # Placed HERE, deliberately, and not where it was:
    #   * after the hard gate, and only for mode == "llm" -- auditing a draft the numeric gate is
    #     about to throw away spends a call reviewing text nobody will read;
    #   * only for length == "full" -- the brief report is polled every ~12s with a cache key that
    #     (measured) changes 48 times between polls, so auditing it meant an extra outbound call
    #     every 12 seconds for a paragraph in a sidebar;
    #   * inside a total time budget -- if narration already consumed the request, the audit is
    #     skipped and says so rather than making the user wait longer for an advisory note.
    #
    # Its SCOPE now includes the agent's chart captions. Three of the four confirmed live failures
    # were in the body and one was in a caption, so auditing the body alone could never have caught
    # all of them. The captions come from `selection` -- the agent's own text, already parsed at this
    # point -- rather than from the rendered charts, which do not exist until further down. Only
    # agent-written captions are sent: a Python default caption is built from the chart and there is
    # nothing in it to audit.
    agent_captions = [(cid.value, cap) for cid, cap in (selection or []) if str(cap or "").strip()]
    if mode == "llm" and length == "full" and audit_enabled():
        left = REPORT_TIME_BUDGET - (time.time() - started)
        audit = audit_llm(facts, text, lang=lang, budget_left=left, captions=agent_captions)
        advisories = list(advisories) + audit_advisories(audit)

    # DETERMINISTIC cohort check, at the same seam and independent of the auditor. It runs for the
    # LLM path only -- the template's own sentences are generated from these facts and cannot state a
    # false containment -- and it covers the body and the captions together.
    if mode == "llm":
        advisories = list(advisories) + cohort_containment_review(
            text, facts, [(f"caption on '{cid}'", cap) for cid, cap in agent_captions])

    # ---- charts: computed here, from the facts and the store, for EVERY mode ----
    charts, charts_unavailable = [], []
    if length == "full":
        picks = selection if mode == "llm" else [(cid, "") for cid in chartreg.DEFAULT_ORDER]
        charts, charts_unavailable = assemble_charts(picks, facts, store, t, policies, lang)
        advisories = list(advisories) + caption_consistency_review(charts)

    out = {"text": text, "mode": mode, "length": length, "lang": lang, "window_h": window_h,
           "generated_iso": facts["now_iso"], "virtual_ts": t,
           "numeric_check": {"ok": (not unver), "checked": bool(llm is not None and not content_reason),
                             "unverified": unver, "field": bad_field},
           # the auditor's own state, so "did the second agent actually run?" is answerable at a
           # glance instead of being inferred from the absence of a flag
           "auditor": audit,
           "advisories": advisories,
           "charts": charts,
           "charts_unavailable": charts_unavailable,
           "rejected_draft": draft,
           "fallback_reason": (None if mode == "llm" else (reason or "template fallback")),
           "facts": facts, "cached": False}
    if mode == "llm":
        _cache_put(key, out, to_disk=persist)
    return out