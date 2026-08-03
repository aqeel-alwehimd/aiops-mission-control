"""
verify_charts.py -- the chart contract: enum-validated ids, gated captions, Python-computed data.
Exits; no server.

Run:  python verify_charts.py

The contract under test:
  * the agent returns chart entries of EXACTLY {chart_id, caption} and nothing else;
  * chart_id is checked against the ChartId enum -- unknown ids are dropped and flagged, never
    rendered, never fatal;
  * caption is ordinary model prose and passes the numeric hard gate like any other sentence;
  * every plotted value is computed in Python; a renderer with no rows or degenerate data reports
    itself unavailable rather than drawing something misleading;
  * the template fallback populates the same charts with no agent involved.
"""
import sys, json
import os
# These suites are not about the second agent: disable it so they can never make a live
# auditor call, whatever is in the environment. verify_auditor.py covers it, fully mocked.
os.environ["REPORT_AUDIT"] = "0"
import report, charts as chartreg
from charts import ChartId
from diagnose_guardrail import FACTS

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

class FakeClock:
    def now_ts(self): return 1664297512
POLICIES = {"alert_threshold": 0.30, "node_filter_pct": 25}
report.assemble_facts = lambda store, t, policies, window_h=6: FACTS

def run(reply, lang="en", length="full"):
    report._CACHE.clear(); report.clear_cooldowns()
    raw = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
    report.generate_llm = lambda facts, l, ln: (raw, None)
    return report.build_report(None, FakeClock(), POLICIES, window_h=24, lang=lang, length=length)

def show(title, d, n=900):
    print("=" * 100)
    print(title)
    print(f"  mode            : {d['mode']}")
    print(f"  fallback_reason : {d['fallback_reason']}")
    print(f"  numeric_check   : ok={d['numeric_check']['ok']} checked={d['numeric_check']['checked']} "
          f"field={d['numeric_check']['field']} unverified={d['numeric_check']['unverified']}")
    print(f"  advisories      : {[a['code'] for a in d['advisories']] or '[]'}")
    for a in d["advisories"]:
        print(f"                    - {a['code']}: {a['message']}")
    print(f"  charts          : {[c['chart_id'] for c in d['charts']]}")
    for c in d["charts"]:
        vals = (c['datasets'][0]['data'] if c.get('datasets') else [])
        print(f"                    - {c['chart_id']:<28} {c['type']:<12} n={len(vals)} "
              f"caption[{c['caption_source']}]: {c['caption'][:60]}")
    print(f"  unavailable     : {[(u['chart_id'], u['code']) for u in d['charts_unavailable']]}")
    print()

BASE = {
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
                         "which 1066 COMPLETED. 1374 jobs are currently active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34."],
}

# ============================================================ 1. a valid selection
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes", "caption": "How the flagged jobs actually turned out."},
    {"chart_id": "job_outcome_mix",     "caption": "What ended in the window, by final state."},
]})
show("FIXTURE 1 - two valid ids with clean captions", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))
check("both selected charts rendered",
      [c["chart_id"] for c in d["charts"]][:2] == ["prediction_outcomes", "job_outcome_mix"],
      str([c["chart_id"] for c in d["charts"]]))
check("the agent's captions were used verbatim",
      d["charts"][0]["caption"] == "How the flagged jobs actually turned out."
      and d["charts"][0]["caption_source"] == "agent")
check("charts carry finished data, not instructions",
      bool(d["charts"][0]["labels"]) and bool(d["charts"][0]["datasets"][0]["data"]))
check("chart specs are NOT echoed into the report prose",
      "Chart configs" not in d["text"] and "chart_id" not in d["text"])
check("no advisory raised for valid ids",
      not any(a["code"] == report.ADV_CHART_ID for a in d["advisories"]))

# ============================================================ 2. THE TEST: an unknown chart_id
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes",   "caption": "How the flagged jobs turned out."},
    {"chart_id": "gpu_thermal_timeline",  "caption": "An invented chart the registry does not have."},
]})
show("FIXTURE 2 - an unknown chart_id must be DROPPED and FLAGGED (not rendered, not fatal)", d)
check("report still accepted", d["mode"] == "llm", d["mode"])
check("the unknown id was dropped",
      "gpu_thermal_timeline" not in [c["chart_id"] for c in d["charts"]],
      str([c["chart_id"] for c in d["charts"]]))
check("the valid id survived",
      "prediction_outcomes" in [c["chart_id"] for c in d["charts"]])
check("an advisory flag was raised for the drop",
      any(a["code"] == report.ADV_CHART_ID for a in d["advisories"]),
      str([a["code"] for a in d["advisories"]]))
check("the advisory NAMES the dropped id",
      any("gpu_thermal_timeline" in a["message"]
          for a in d["advisories"] if a["code"] == report.ADV_CHART_ID))
check("one advisory per dropped id, not a blanket flag",
      len([a for a in d["advisories"] if a["code"] == report.ADV_CHART_ID]) == 1)

# ============================================================ 3. THE TEST: a caption with a
#                                                                 fabricated number
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes",
     "caption": "All 8888 flagged jobs resolved before the end of the shift."},
]})
show("FIXTURE 3 - a caption containing a non-fact number must REJECT the whole report", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("the fabricated number is named", d["numeric_check"]["unverified"] == ["8888"],
      str(d["numeric_check"]["unverified"]))
check("fallback_reason points at the caption field",
      "chart_configs[0].caption" in (d["fallback_reason"] or ""), repr(d["fallback_reason"]))
check("the rejected draft is preserved for inspection",
      bool(d["rejected_draft"]) and d["rejected_draft"]["field"] == "chart_configs[0].caption")
check("the template's own charts are still shown (the fallback is not chart-less)",
      len(d["charts"]) > 0, str([c["chart_id"] for c in d["charts"]]))
check("those captions are Python-written, not the model's",
      all(c["caption_source"] == "python" for c in d["charts"]))

# ============================================================ 3b. the id itself is still exempt
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes", "caption": "How the flagged jobs turned out."},
    {"chart_id": "job_outcome_mix",     "caption": "The final states of jobs that ended."},
    {"chart_id": "node_risk_watch",     "caption": "Nodes worth watching this shift."},
]})
show("FIXTURE 3b - ids like 'p2'/'p3'-shaped identifiers are exempt; clean captions pass", d)
check("mode == llm (chart_id is enum-checked, not numerically checked)", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [])

# ============================================================ 4. captions may quote FACTS
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes",
     "caption": "Of 212 flagged jobs, 30 failed and 182 completed."},
]})
show("FIXTURE 4 - a caption quoting real fact values is accepted", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))

# ============================================================ 5. the cap, and the auto-append
d = run({**BASE, "chart_configs": [
    {"chart_id": "prediction_outcomes", "caption": "A."},
    {"chart_id": "job_outcome_mix",     "caption": "B."},
    {"chart_id": "top_flagged_jobs",    "caption": "C."},
    {"chart_id": "node_risk_watch",     "caption": "D."},
    {"chart_id": "node_feature_contributions", "caption": "E."},
    {"chart_id": "prediction_outcomes", "caption": "F -- a duplicate."},
]})
show("FIXTURE 5 - more charts than the cap allows", d)
sel, adv = report.select_charts({"chart_configs": [
    {"chart_id": c, "caption": "x"} for c in
    ["prediction_outcomes", "job_outcome_mix", "top_flagged_jobs", "node_risk_watch",
     "node_feature_contributions"]]})
check(f"agent selection capped at MAX_CHARTS ({report.MAX_CHARTS})",
      len(sel) == report.MAX_CHARTS, str(len(sel)))
check("duplicates collapse rather than doubling up",
      len(report.select_charts({"chart_configs": [
          {"chart_id": "job_outcome_mix", "caption": "a"},
          {"chart_id": "job_outcome_mix", "caption": "b"}]})[0]) == 1)

# ============================================================ 6. the auto-appended "why" chart
print("=" * 100)
print("FIXTURE 6 - node_feature_contributions is appended when the agent did not ask for it")
class TinyStore:
    """Just enough store to serve the drill-down contribution path."""
    def node_detail(self, node_id, t, policies):
        return {"node_label": f"node{node_id:04d}", "why_kind": "perprediction", "why": [
            {"feature": "cur_totP", "label_en": "Total power", "label_zh": "總功耗",
             "value": 274.0, "contribution": 0.81, "direction": "increases"},
            {"feature": "cur_g0_cT", "label_en": "GPU0 temp", "label_zh": "GPU0 溫度",
             "value": 38.8, "contribution": -0.22, "direction": "decreases"},
        ]}
sel, _ = report.select_charts({"chart_configs": [{"chart_id": "job_outcome_mix", "caption": "x"}]})
rendered, unavail = report.assemble_charts(sel, FACTS, TinyStore(), 1664297512, POLICIES, "en")
ids = [c["chart_id"] for c in rendered]
print(f"  agent selected  : {[c.value for c, _ in sel]}")
print(f"  rendered        : {ids}")
check("the contributions chart was appended", "node_feature_contributions" in ids, str(ids))
check("it was appended LAST, after the agent's own picks", ids[-1] == "node_feature_contributions")
nfc = next(c for c in rendered if c["chart_id"] == "node_feature_contributions")
check("its caption is Python-generated", nfc["caption_source"] == "python" and bool(nfc["caption"]))
check("it is a signed horizontal bar", nfc["type"] == "signed_hbar")
check("contributions keep their sign", any(v < 0 for v in nfc["datasets"][0]["data"])
      and any(v > 0 for v in nfc["datasets"][0]["data"]), str(nfc["datasets"][0]["data"]))
check("feature labels are bilingual, from the store",
      all("en" in l and "zh" in l for l in nfc["labels"]), str(nfc["labels"][:1]))
check("it is NOT appended twice when the agent did ask for it",
      [c["chart_id"] for c in report.assemble_charts(
          [(ChartId.NODE_FEATURE_CONTRIBUTIONS, "mine")], FACTS, TinyStore(), 1664297512,
          POLICIES, "en")[0]].count("node_feature_contributions") == 1)
check("an agent caption on it wins over the Python one",
      report.assemble_charts([(ChartId.NODE_FEATURE_CONTRIBUTIONS, "mine")], FACTS, TinyStore(),
                             1664297512, POLICIES, "en")[0][0]["caption"] == "mine")
print()

# ============================================================ 7. renderers refuse to mislead
print("=" * 100)
print("FIXTURE 7 - a renderer reports itself UNAVAILABLE rather than drawing nothing/nonsense")

EMPTY = {**FACTS,
         "prediction_outcomes": {"flagged_total": 0, "failures_resolved": 0, "catch_rate_pct": None,
                                 "correct_warnings": {"count": 0, "examples": []},
                                 "false_alarms": {"count": 0, "examples": []},
                                 "pending_outcome": {"count": 0, "examples": []},
                                 "misses": {"count": 0, "examples": []}},
         "jobs_window": {**FACTS["jobs_window"], "ended_in_window": 0,
                         "ended_in_window_failed": 0, "ended_in_window_timeout": 0,
                         "ended_in_window_oom": 0, "ended_in_window_completed": 0},
         "high_risk_jobs": [], "high_risk_nodes": []}
for cid in chartreg.DEFAULT_ORDER:
    r = chartreg.render(cid, EMPTY, None, 1664297512, POLICIES)
    print(f"  {cid.value:<28} available={r.available}  [{r.code}] {(r.reason or '')[:64]}")
    check(f"{cid.value}: unavailable on empty data", not r.available)
    check(f"{cid.value}: gives a reason", bool(r.reason))

# the degeneracy rule the brief called out by name
STUBS = {**FACTS,
         "settings": {**FACTS["settings"], "p2_node_alert_score": 0.50},
         "high_risk_nodes": [{"node": 697, "rack": 34, "risk_pct": 0.6, "state": "HEALTHY",
                              "onset": False, "temp_c": 38.8, "power_w": 1501, "fan_rpm": 8200,
                              "psu0_w": 274},
                             {"node": 512, "rack": 21, "risk_pct": 0.4, "state": "HEALTHY",
                              "onset": False, "temp_c": 37.1, "power_w": 1490, "fan_rpm": 8100,
                              "psu0_w": 271}]}
r = chartreg.render(ChartId.NODE_RISK_WATCH, STUBS, None, 1664297512, POLICIES)
print(f"  node_risk_watch (0.6% and 0.4% vs a 50% threshold) -> available={r.available} [{r.code}]")
print(f"    {r.reason}")
check("stub bars beside a far taller reference line are refused", not r.available)
check("...and the refusal is classified as degenerate, not as 'no rows'", r.code == "degenerate")
check("...the rule is a named constant, not a literal in the branch",
      isinstance(chartreg.NODE_RISK_MIN_FRACTION_OF_ALERT, float))
# ...and the same chart IS drawn once a bar is meaningful against the threshold
REAL = {**STUBS, "high_risk_nodes": [{**STUBS["high_risk_nodes"][0], "risk_pct": 61.5}]}
r2 = chartreg.render(ChartId.NODE_RISK_WATCH, REAL, None, 1664297512, POLICIES)
check("a genuinely elevated node IS drawn", r2.available, str(r2.reason))
check("the alert threshold is sent as a reference line",
      r2.chart["reference_line"]["value"] == 50.0, str(r2.chart.get("reference_line")))
check("the value axis is stretched to keep the reference line visible",
      r2.chart["axis_max"] >= 50.0, str(r2.chart.get("axis_max")))
check("no data point was invented to fill the chart",
      len(r2.chart["datasets"][0]["data"]) == 1, str(r2.chart["datasets"][0]["data"]))
print()

# ============================================================ 8. the template fallback has charts
print("=" * 100)
print("FIXTURE 8 - with NO agent at all, the template report still carries the same charts")
report._CACHE.clear(); report.clear_cooldowns()
report.generate_llm = lambda facts, l, ln: (None, "LAPLACE_INVOKE_URL not set")
d = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="en", length="full")
show("  template mode, no external call", d)
check("mode == template", d["mode"] == "template", d["mode"])
check("charts are present anyway", len(d["charts"]) > 0, str([c["chart_id"] for c in d["charts"]]))
check("every caption is Python-written",
      all(c["caption_source"] == "python" for c in d["charts"]))
check("the same registry produced them",
      set(c["chart_id"] for c in d["charts"]) <= {c.value for c in ChartId})

# a Chinese template report gets Chinese captions from the same registry
report._CACHE.clear()
dz = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="zh", length="full")
check("zh fallback captions are in Chinese",
      any(any("一" <= ch <= "鿿" for ch in c["caption"]) for c in dz["charts"]),
      str([c["caption"][:30] for c in dz["charts"]][:1]))

# ============================================================ 9. brief reports carry no charts
report._CACHE.clear()
db = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="en", length="brief")
check("a brief report carries no charts (the sidebar has nowhere to draw them)",
      db["charts"] == [] and db["charts_unavailable"] == [])

# ============================================================ guardrail still armed elsewhere
print()
print("=" * 100)
print("GUARDRAIL STILL ARMED (the chart_id exemption is narrow, not a blanket pass)")
report.generate_llm = None      # restored by run()
d = run({**BASE, "action_playbook": ["Escalate the 8888 stalled jobs to the on-call engineer."],
         "chart_configs": [{"chart_id": "job_outcome_mix", "caption": "Clean caption."}]})
check("fabrication inside action_playbook is caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["8888"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run({**BASE, "unexpected_extra_key": "A stray 7777 that is not a fact and is not a chart."})
check("fabrication in an UNKNOWN key is still caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["7777"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run("# Shift report\n\nThe cluster submitted 1501 jobs and 9999 ended in the window, of which "
        "1066 COMPLETED and 1374 remain active on the floor right now.")
check("plain-markdown replies are still validated wholesale",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["9999"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run({**BASE, "chart_configs": [
    {"chart_id": "job_outcome_mix", "caption": "Clean.", "description": "A stray 5150 nodes."}]})
check("a THIRD field smuggled into a chart entry is validated too (only the id is exempt)",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["5150"],
      f"{d['mode']} {d['numeric_check']['unverified']}")

# ==================================================================================================
# SECOND PASS -- time-series pair, layout hints, the revised outcomes chart, the caption advisory,
# and the model-info panel that must stay OUT of the agent-visible enum.
# ==================================================================================================
print()
print("=" * 100)
print("FIXTURE 10 - the failures-over-time pair: ONE query, ONE aggregation, two views")
print("=" * 100)

WIN_LO, WIN_HI = FACTS["window"]["start_ts"], FACTS["window"]["end_ts"]

class FotStore:
    """A store whose only job is to answer the failures query -- and to COUNT how often it is asked.

    `db_path` is distinct per instance so the aggregation memo cannot leak between fixtures.
    """
    _seq = 0
    def __init__(self, rows):
        FotStore._seq += 1
        self.db_path = f"fake://fot/{FotStore._seq}"
        self.rows, self.queries = rows, []
    def _df(self, q, p=()):
        import pandas as pd
        self.queries.append((q, p))
        lo, hi, _ = p
        return pd.DataFrame([{"end_ts": ts, "state": st} for ts, st in self.rows
                             if lo < ts <= hi], columns=["end_ts", "state"])

# Three failures sharing one bucket, then two ISOLATED single failures far apart -- the sparse
# pattern this chart has to survive. Offsets are from the window start so every row really falls
# inside (lo, hi]; anchoring them to the bucket grid instead would put some before lo.
ROWS = [(WIN_LO + 600, "FAILED"), (WIN_LO + 700, "FAILED"), (WIN_LO + 800, "TIMEOUT"),
        (WIN_LO + 900 * 20 + 60, "TIMEOUT"),
        (WIN_LO + 900 * 50 + 30, "OUT_OF_MEMORY")]
st = FotStore(ROWS)
chartreg._FOT_MEMO.clear()
rb = chartreg.render(ChartId.FAILURES_OVER_TIME_BARS, FACTS, st, 1664297512, POLICIES)
rl = chartreg.render(ChartId.FAILURES_OVER_TIME_LINES, FACTS, st, 1664297512, POLICIES)
print(f"  store queries issued for BOTH renderers: {len(st.queries)}")
print(f"  buckets: {rb.chart['bucket_count']}   datasets bars={len(rb.chart['datasets'])} "
      f"lines={len(rl.chart['datasets'])}")
check("both renderers available", rb.available and rl.available)
check("EXACTLY ONE query served both charts", len(st.queries) == 1, str(len(st.queries)))
check("the query is the only aggregation source (no second SELECT)",
      all("SELECT end_ts, state FROM jobs" in q for q, _ in st.queries))

bars_stack = [sum(ds["data"][i] for ds in rb.chart["datasets"])
              for i in range(len(rb.chart["labels"]))]
lines_total = rl.chart["datasets"][0]["data"]
check("bar stack heights == the line chart's total series, bucket for bucket",
      bars_stack == lines_total, f"{bars_stack[:4]} vs {lines_total[:4]}")
check("the two charts carry identical bucket labels",
      [l["text"] for l in rb.chart["labels"]] == [l["text"] for l in rl.chart["labels"]])
check("per-type series are identical between the two charts",
      {ds["legend"]["key"]: ds["data"] for ds in rb.chart["datasets"]}
      == {ds["legend"]["key"]: ds["data"] for ds in rl.chart["datasets"][1:]})
check("total series == sum of the per-type series",
      lines_total == [sum(ds["data"][i] for ds in rl.chart["datasets"][1:])
                      for i in range(len(lines_total))])
check("all five seeded failures are accounted for", sum(lines_total) == 5, str(sum(lines_total)))

print()
print("  line-chart drawing rules (counts in discrete bins):")
tot_ds, type_ds = rl.chart["datasets"][0], rl.chart["datasets"][1:]
print(f"    total : width={tot_ds['style']['line_width']} point={tot_ds['style']['point_radius']} "
      f"tension={tot_ds['style']['tension']}")
print(f"    types : width={type_ds[0]['style']['line_width']} "
      f"point={type_ds[0]['style']['point_radius']} tension={type_ds[0]['style']['tension']}")
check("NO curve smoothing on any line (tension 0 everywhere)",
      all(ds["style"]["tension"] == 0 for ds in rl.chart["datasets"]))
check("the total line is drawn more prominently than the per-type lines",
      tot_ds["style"]["line_width"] > type_ds[0]["style"]["line_width"]
      and tot_ds["style"]["point_radius"] > type_ds[0]["style"]["point_radius"])
check("point markers are large enough for an isolated single failure to read",
      min(ds["style"]["point_radius"] for ds in rl.chart["datasets"]) >= 2.5)
check("an isolated single failure survives as its own bucket (no decimation)",
      lines_total.count(1) >= 2, str([i for i, v in enumerate(lines_total) if v == 1]))
check("every bucket is plotted, zeros included",
      len(lines_total) == rl.chart["bucket_count"] == len(rb.chart["labels"]))
check("both charts are full width", rb.chart["width"] == rl.chart["width"] == chartreg.WIDTH_FULL)
check("both carry the ended-in-window cohort note",
      rb.chart["cohort_note"]["key"] == rl.chart["cohort_note"]["key"]
      == "chart.cohort.endedInWindow")
check("the plotted resolution is stated on the chart",
      rb.chart["subtitle"]["key"] == "chart.failuresOverTime.sub")

# bucket count scales without touching the renderer
WIDE = {**FACTS, "window": {**FACTS["window"], "start_ts": WIN_HI - 96 * 3600}}
chartreg._FOT_MEMO.clear()
wide = chartreg.render(ChartId.FAILURES_OVER_TIME_LINES, WIDE, FotStore(ROWS), 1, POLICIES)
print(f"\n  96-hour window -> {wide.chart['bucket_count']} buckets, all plotted")
check("a much longer window just yields more buckets, none dropped",
      wide.chart["bucket_count"] == 385
      and len(wide.chart["datasets"][0]["data"]) == 385, str(wide.chart["bucket_count"]))

# ---- availability -------------------------------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 11 - failures-over-time availability rules")
print("=" * 100)
chartreg._FOT_MEMO.clear()
empty = FotStore([])
for cid in (ChartId.FAILURES_OVER_TIME_BARS, ChartId.FAILURES_OVER_TIME_LINES):
    r = chartreg.render(cid, FACTS, empty, 1, POLICIES)
    print(f"  {cid.value:<28} all-zero buckets -> available={r.available} [{r.code}]")
    check(f"{cid.value}: every bucket zero -> unavailable", not r.available)
    check(f"{cid.value}: ...classified no_rows", r.code == "no_rows")

chartreg._FOT_MEMO.clear()
one = FotStore([(WIN_LO + 900 * 7 + 5, "FAILED")])
r = chartreg.render(ChartId.FAILURES_OVER_TIME_LINES, FACTS, one, 1, POLICIES)
print(f"  ONE non-zero bucket -> available={r.available} (must be True: a real event)")
check("a SINGLE non-zero bucket is NOT degenerate -- it is drawn", r.available, str(r.reason))
check("...and it is the only non-zero point", sum(r.chart["datasets"][0]["data"]) == 1)

chartreg._FOT_MEMO.clear()
r = chartreg.render(ChartId.FAILURES_OVER_TIME_BARS, FACTS, None, 1, POLICIES)
check("no store -> unavailable, not a crash", not r.available and r.code == "no_store")
NOWIN = {**FACTS, "window": {"hours": 24}}
chartreg._FOT_MEMO.clear()
r = chartreg.render(ChartId.FAILURES_OVER_TIME_BARS, NOWIN, FotStore(ROWS), 1, POLICIES)
check("facts with no window bounds -> unavailable with the right reason",
      not r.available and "window start/end" in (r.reason or ""), str(r.reason))

# ---- the pair-append rule, all three cases --------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 12 - the mandatory-pair rule fires in all three cases")
print("=" * 100)
FOTB, FOTL = ChartId.FAILURES_OVER_TIME_BARS, ChartId.FAILURES_OVER_TIME_LINES

def pair_case(name, picked):
    chartreg._FOT_MEMO.clear()
    sel = [(c, "agent caption") for c in picked]
    rendered, _un = report.assemble_charts(sel, FACTS, FotStore(ROWS), 1664297512, POLICIES, "en")
    ids = [c["chart_id"] for c in rendered]
    what = ", ".join(c.value for c in picked) or "(nothing)"
    print(f"  agent picked {what:<52} -> {ids}")
    return rendered, ids

rendered, ids = pair_case("bars only", [FOTB])
check("picked BARS -> lines appended", FOTL.value in ids, str(ids))
check("   the appended partner is adjacent to it",
      abs(ids.index(FOTB.value) - ids.index(FOTL.value)) == 1, str(ids))
check("   the appended partner has a Python caption",
      next(c for c in rendered if c["chart_id"] == FOTL.value)["caption_source"] == "python")
check("   the agent's own caption is kept on the one it picked",
      next(c for c in rendered if c["chart_id"] == FOTB.value)["caption_source"] == "agent")

rendered, ids = pair_case("lines only", [FOTL])
check("picked LINES -> bars appended", FOTB.value in ids, str(ids))
check("   adjacent", abs(ids.index(FOTB.value) - ids.index(FOTL.value)) == 1, str(ids))

rendered, ids = pair_case("neither", [ChartId.JOB_OUTCOME_MIX])
check("picked NEITHER -> both appended", FOTB.value in ids and FOTL.value in ids, str(ids))
check("   adjacent to each other",
      abs(ids.index(FOTB.value) - ids.index(FOTL.value)) == 1, str(ids))

rendered, ids = pair_case("both", [FOTB, FOTL])
check("picked BOTH -> neither duplicated",
      ids.count(FOTB.value) == 1 and ids.count(FOTL.value) == 1, str(ids))
check("   both keep the agent's captions",
      all(c["caption_source"] == "agent" for c in rendered
          if c["chart_id"] in (FOTB.value, FOTL.value)))

# an unavailable partner is not forced into the output
chartreg._FOT_MEMO.clear()
rendered, unavail = report.assemble_charts([(FOTB, "x")], FACTS, FotStore([]), 1, POLICIES, "en")
check("an appended partner that is unavailable is NOT forced in",
      FOTL.value not in [c["chart_id"] for c in rendered]
      and FOTL.value in [u["chart_id"] for u in unavail])

# ---- layout hints ---------------------------------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 13 - every registry entry carries a layout width hint")
print("=" * 100)
for cid in ChartId:
    print(f"  {cid.value:<28} {chartreg.LAYOUT.get(cid)}")
check("LAYOUT covers every ChartId", set(chartreg.LAYOUT) == set(ChartId),
      str(set(ChartId) - set(chartreg.LAYOUT)))
check("every hint is half or full",
      all(v in (chartreg.WIDTH_HALF, chartreg.WIDTH_FULL) for v in chartreg.LAYOUT.values()))
check("both time-series charts are full width",
      chartreg.LAYOUT[FOTB] == chartreg.LAYOUT[FOTL] == chartreg.WIDTH_FULL)
check("the doughnut, job bar, node bar and contribution bar stay half",
      all(chartreg.LAYOUT[c] == chartreg.WIDTH_HALF for c in
          (ChartId.PREDICTION_OUTCOMES, ChartId.JOB_OUTCOME_MIX, ChartId.NODE_RISK_WATCH,
           ChartId.NODE_FEATURE_CONTRIBUTIONS)))
chartreg._FOT_MEMO.clear()
_r, _u = report.assemble_charts([], FACTS, FotStore(ROWS), 1664297512, POLICIES, "en")
check("every RENDERED chart carries its width in the payload",
      all(c.get("width") in (chartreg.WIDTH_HALF, chartreg.WIDTH_FULL) for c in _r),
      str([(c["chart_id"], c.get("width")) for c in _r]))
check("the default order puts the two time-series charts adjacent",
      abs(chartreg.DEFAULT_ORDER.index(FOTB) - chartreg.DEFAULT_ORDER.index(FOTL)) == 1)

# ---- the revised outcomes chart --------------------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 14 - prediction_outcomes: the ring is ONE cohort and sums to a stated total")
print("=" * 100)
po = FACTS["prediction_outcomes"]
r = chartreg.render(ChartId.PREDICTION_OUTCOMES, FACTS, None, 1, POLICIES)
ch = r.chart
seg = ch["datasets"][0]["data"]
print(f"  segments {[l['key'].split('.')[-1] for l in ch['labels']]} = {seg}")
print(f"  segment_sum={ch['segment_sum']}  flagged_total={po['flagged_total']}  "
      f"footnote(misses)={ch['footnote']['value']}")
check("the ring has exactly three segments (misses removed)", len(seg) == 3, str(len(seg)))
check("SUMMATION IDENTITY: segments sum to flagged_total",
      ch["segment_sum"] == sum(seg) == po["flagged_total"],
      f"{sum(seg)} vs {po['flagged_total']}")
check("misses are NOT a segment",
      not any(l.get("key") == "chart.cat.missed" for l in ch["labels"]))
check("misses are still surfaced, as a separate labelled figure",
      ch["footnote"]["value"] == po["misses"]["count"]
      and ch["footnote"]["label"]["key"] == "chart.cat.missed")
check("the footnote explains why misses are not in the ring", bool(ch["footnote"].get("note")))
check("the subtitle states what the segments sum to",
      ch["subtitle"]["key"] == "chart.predictionOutcomes.sub")
check("the chart declares its cohort", ch["cohort_note"]["key"] == "chart.cohort.flagged")
check("the agent menu names the cohort of each segment",
      "flagged cohort" in chartreg.CHART_MENU[ChartId.PREDICTION_OUTCOMES]
      and "NOT in that cohort" in chartreg.CHART_MENU[ChartId.PREDICTION_OUTCOMES])
cap = chartreg.default_caption(ChartId.PREDICTION_OUTCOMES, FACTS, "en", ch)
print(f"  python caption: {cap}")
check("the Python caption separates the two cohorts rather than merging them",
      "Separately" in cap and "never flagged" in cap, cap)
NOFLAG = {**FACTS, "prediction_outcomes": {**po, "flagged_total": 0,
          "correct_warnings": {"count": 0, "examples": []},
          "false_alarms": {"count": 0, "examples": []},
          "pending_outcome": {"count": 0, "examples": []},
          "misses": {"count": 12, "examples": []}}}
r0 = chartreg.render(ChartId.PREDICTION_OUTCOMES, NOFLAG, None, 1, POLICIES)
check("an empty flagged cohort -> unavailable even when misses exist",
      not r0.available and r0.code == "no_rows", str(r0.reason))

# ---- top_flagged_jobs colouring + concentration ------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 15 - top_flagged_jobs carries information beyond a near-constant bar length")
print("=" * 100)
SAME = {**FACTS,
        "high_risk_jobs": [
            {"job_id": 1, "user": "user_1544", "risk_pct": 99.6, "pred_type": "TIMEOUT", "status": "RUNNING"},
            {"job_id": 2, "user": "user_1544", "risk_pct": 99.6, "pred_type": "TIMEOUT", "status": "RUNNING"},
            {"job_id": 3, "user": "user_1544", "risk_pct": 99.6, "pred_type": "FAILED",  "status": "RUNNING"},
            {"job_id": 4, "user": "user_77",   "risk_pct": 99.5, "pred_type": "OUT_OF_MEMORY", "status": "PENDING"}],
        "high_risk_jobs_concentration": report._job_concentration([
            {"user": "user_1544"}, {"user": "user_1544"}, {"user": "user_1544"}, {"user": "user_77"}])}
r = chartreg.render(ChartId.TOP_FLAGGED_JOBS, SAME, None, 1, POLICIES)
ch = r.chart
print(f"  bar values : {ch['datasets'][0]['data']}   (near-identical by design)")
print(f"  bar colours: {ch['datasets'][0]['colors']}")
print(f"  legend     : {[(s['label']['key'], s['color']) for s in ch['color_legend']]}")
check("bars are coloured by predicted failure type, not by risk band",
      ch["datasets"][0]["colors"]
      == [chartreg.PRED_TYPE_COLORS["TIMEOUT"], chartreg.PRED_TYPE_COLORS["TIMEOUT"],
          chartreg.PRED_TYPE_COLORS["FAILED"], chartreg.PRED_TYPE_COLORS["OUT_OF_MEMORY"]])
check("a colour legend accompanies it", len(ch["color_legend"]) == 3)
check("the legend has one entry per distinct type, in first-seen order",
      [s["label"]["key"] for s in ch["color_legend"]]
      == ["chart.cat.timeout", "chart.cat.failed", "chart.cat.oom"])
check("NO axis trick manufactures spread between near-identical values",
      ch["axis_max"] >= max(ch["datasets"][0]["data"]) and ch["datasets"][0]["data"][0] == 99.6)
conc = SAME["high_risk_jobs_concentration"]
print(f"  concentration fact: {conc}")
check("the facts record the distinct-user count", conc["distinct_users"] == 2)
check("...and the dominant user's share",
      conc["top_user"] == "user_1544" and conc["top_user_share_pct"] == 75.0, str(conc))
cap = chartreg.default_caption(ChartId.TOP_FLAGGED_JOBS, SAME, "en", ch)
print(f"  python caption: {cap}")
check("the template caption states the concentration", "2 users" in cap and "75.0%" in cap, cap)
solo = report._job_concentration([{"user": "user_9"}, {"user": "user_9"}])
check("a single-owner list is described as such",
      "All of them belong to user_9" in chartreg.default_caption(
          ChartId.TOP_FLAGGED_JOBS, {**SAME, "high_risk_jobs_concentration": solo}, "en", ch))
check("an empty list does not crash the concentration fact",
      report._job_concentration([])["distinct_users"] == 0)

# ---- the caption-consistency advisory ----------------------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 16 - ADVISORY: a caption citing a fact its own chart does not plot")
print("=" * 100)
# 1501 (jobs submitted) is a REAL fact -- the hard gate passes it -- but job_outcome_mix plots
# [1066, 44, 31, 5] and knows nothing about 1501. This is the class of error the gate cannot catch.
d = run({**BASE, "chart_configs": [
    {"chart_id": "job_outcome_mix",
     "caption": "Across the 1501 jobs submitted this window the mix was dominated by completions."}]})
show("  a real fact, but not one this chart shows", d)
check("the report is still ACCEPTED (this never blocks)", d["mode"] == "llm", d["mode"])
check("the hard gate passed it -- 1501 is a genuine fact",
      d["numeric_check"]["ok"] and d["numeric_check"]["unverified"] == [])
adv = [a for a in d["advisories"] if a["code"] == report.ADV_CAPTION]
check("the ADVISORY fires", len(adv) == 1, str([a["code"] for a in d["advisories"]]))
check("   it names the offending number", adv and "1501" in adv[0]["message"], str(adv))
check("   it names the chart", adv and "job_outcome_mix" in adv[0]["message"])

d = run({**BASE, "chart_configs": [
    {"chart_id": "job_outcome_mix", "caption": "1066 jobs completed and 44 failed."}]})
check("a caption citing this chart's OWN numbers raises nothing",
      not any(a["code"] == report.ADV_CAPTION for a in d["advisories"]),
      str([a["code"] for a in d["advisories"]]))
d = run({**BASE, "chart_configs": [
    {"chart_id": "job_outcome_mix", "caption": "Completions dominated the window."}]})
check("a caption with no numbers at all raises nothing",
      not any(a["code"] == report.ADV_CAPTION for a in d["advisories"]))
check("Python-written captions are never flagged (they are built from the chart)",
      not any(a["code"] == report.ADV_CAPTION for a in
              report.caption_consistency_review(
                  [{"chart_id": "x", "caption_source": "python", "caption": "9999 things",
                    "datasets": [{"data": [1]}]}])))
# a number belonging to a DIFFERENT chart is caught
oc = chartreg.render(ChartId.PREDICTION_OUTCOMES, FACTS, None, 1, POLICIES).chart
foreign = {**oc, "caption_source": "agent",
           "caption": f"{FACTS['jobs_window']['ended_in_window']} jobs ended in the window."}
adv = report.caption_consistency_review([foreign])
check("a figure belonging to another chart is flagged on this one",
      len(adv) == 1 and str(FACTS["jobs_window"]["ended_in_window"]) in adv[0]["message"], str(adv))

# THE DOCUMENTED BOUNDARY. Both numbers below really are on the outcomes panel -- 212 is the ring's
# total, 47 sits in the footnote beside it -- so a membership test cannot see that "of ... were"
# joins them falsely. Asserted here so the limit is recorded rather than assumed away; the
# structural fix (misses are no longer a slice) is what actually prevents this, and judgment about
# the relation belongs to the reviewer agent.
relational = {**oc, "caption_source": "agent",
              "caption": f"Of the {po['flagged_total']} flagged jobs, "
                         f"{po['misses']['count']} were missed failures."}
adv = report.caption_consistency_review([relational])
print(f"  KNOWN LIMIT - a false RELATION between two on-panel numbers -> {len(adv)} advisory")
check("KNOWN LIMIT: a false relation between two on-panel numbers is not numerically detectable",
      adv == [], str(adv))
correct = {**oc, "caption_source": "agent",
           "caption": f"Separately, {po['misses']['count']} failures were never flagged."}
check("   ...and the correct wording of the same figures is not flagged either (no false positive)",
      report.caption_consistency_review([correct]) == [])
check("   the structural guard is what prevents it: misses are not a ring segment",
      not any(l.get("key") == "chart.cat.missed" for l in oc["labels"])
      and oc["footnote"]["value"] == po["misses"]["count"])
check("   and the prompt forbids describing misses as flagged",
      "missed failures are NOT part of the flagged cohort"
      in report._chart_menu_block("en"))

# ---- model-info panel stays out of the agent's reach -------------------------------------------------
print()
print("=" * 100)
print("FIXTURE 17 - model-level importance is a STATIC panel, not an agent-selectable chart")
print("=" * 100)
class MetaStore:
    def __init__(self, meta): self.meta = meta
imp = chartreg.model_importance_panels(MetaStore({"p3_global_importance": [
    {"feature": "log_user_prior_n", "importance": 0.0342},
    {"feature": "user_prior_timeout_rate", "importance": 0.033}]}))
print(f"  panels      : {[p['chart_id'] for p in imp['panels']]}")
print(f"  unavailable : {[(u['model_id'], u['code']) for u in imp['unavailable']]}")
check("'model_importance_p3' is NOT in the agent-visible enum",
      "model_importance_p3" not in {c.value for c in ChartId})
check("no importance panel id appears in the enum",
      all(p["chart_id"] not in {c.value for c in ChartId} for p in imp["panels"]))
check("it is absent from the agent's chart menu",
      all("importance" not in k.value for k in chartreg.CHART_MENU))
check("coerce_id refuses it, so the agent cannot select it by name",
      chartreg.coerce_id("model_importance_p3") is None)
sel, advs = report.select_charts({"chart_configs": [{"chart_id": "model_importance_p3", "caption": "x"}]})
check("asking for it is dropped and flagged like any unknown id",
      sel == [] and any(a["code"] == report.ADV_CHART_ID for a in advs))
p = imp["panels"][0]
check("the panel states the importance METRIC rather than a bare number",
      p["metric_note"]["key"] == "mi.importance.p3.metric")
check("it declares its scope as model-level across all predictions",
      p["scope_note"]["key"] == "mi.importance.scope")
check("it carries the explicit contrast with the per-prediction chart",
      p["contrast_note"]["key"] == "mi.importance.vsPerPrediction")
check("a model with no STORED global importance is reported, not improvised",
      any(u["model_id"] == "P2" and u["code"] == "not_stored" for u in imp["unavailable"]))
check("   and the reason says why a derived stand-in was refused",
      any("biased local average" in u["reason"] for u in imp["unavailable"] if u["model_id"] == "P2"))
check("a store with no stored P3 importance reports P3 unavailable too",
      any(u["model_id"] == "P3" for u in
          chartreg.model_importance_panels(MetaStore({}))["unavailable"]))

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
