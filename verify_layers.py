"""
verify_layers.py -- hard gate vs advisory layer, schema-identifier exemption, preserved draft.
Exits; starts no server.

Run:  python verify_layers.py
"""
import sys, json
import os
# These suites are not about the second agent: disable it so they can never make a live
# auditor call, whatever is in the environment. verify_auditor.py covers it, fully mocked.
os.environ["REPORT_AUDIT"] = "0"
import report
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

def show(title, d, n=700):
    print("=" * 100)
    print(title)
    print(f"  mode            : {d['mode']}")
    print(f"  fallback_reason : {d['fallback_reason']}")
    print(f"  advisories      : {[a['code'] for a in d['advisories']] or '[]'}")
    for a in d["advisories"]:
        print(f"                    - {a['code']}: {a['message']}")
    print(f"  numeric_check   : ok={d['numeric_check']['ok']} checked={d['numeric_check']['checked']} "
          f"field={d['numeric_check']['field']} unverified={d['numeric_check']['unverified']}")
    rd = d["rejected_draft"]
    print(f"  rejected_draft  : {'PRESERVED (' + str(len(rd['text'])) + ' chars)' if rd else 'None'}")
    if rd:
        print(f"                    field={rd['field']} unverified={rd['unverified']}")
        print(f"                    draft[:160]: {rd['text'][:160]!r}")
    print("  --- displayed text (first lines) ---")
    for line in d["text"][:n].split("\n")[:8]:
        print("  | " + line)
    print()

BASE = {
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
                         "which 1066 COMPLETED. 1374 jobs are currently active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34."],
}
# the CURRENT chart contract: an enum id plus a caption, nothing else. The id is exempt from the
# numeric gate (it is validated by enum membership instead); the caption is model prose and is
# gated like any sentence -- see verify_charts.py for the dedicated coverage.
CHARTS = [{"chart_id": "node_risk_watch",
           "caption": "The nodes worth watching, against the alert threshold."},
          {"chart_id": "job_outcome_mix",
           "caption": "How the jobs that ended in this window finished."}]

# ============================================================ 1. THE BUG: schema identifiers
d = run({**BASE,
         "risk_assessment": "The p2_node_alert_score sits above the configured p3_alert_threshold, "
                            "and the p2_triage_pct setting governs which node-slots are scored. The "
                            "risk_pct column for node0697 reads 41.2% in rack 34.",
         "chart_configs": CHARTS})
show("FIXTURE 1 - prose quoting p2_node_alert_score / p3_alert_threshold / p2_triage_pct / risk_pct", d)
check("mode == llm (no false positive)", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))
check("identifiers survive in the DISPLAYED text (stripping is validator-only)",
      "p2_node_alert_score" in d["text"])

# ============================================================ 2. fabricated figure in exec summary
d = run({**BASE,
         "executive_summary": "Over the last 24 h the cluster submitted 4200 jobs and 1244 ended, "
                              "of which 1066 COMPLETED and 1374 remain active on the floor.",
         "chart_configs": CHARTS})
show("FIXTURE 2 - fabricated 4200 in executive_summary", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("field named in fallback_reason",
      d["fallback_reason"] == "numeric check failed in executive_summary: 1 unmatched number (4200)",
      repr(d["fallback_reason"]))
check("DRAFT PRESERVED", bool(d["rejected_draft"]) and "4200" in d["rejected_draft"]["text"])
check("draft carries the offending field + values",
      d["rejected_draft"]["field"] == "executive_summary"
      and d["rejected_draft"]["unverified"] == ["4200"])
check("template is what is displayed, not the draft", "# Shift report" in d["text"])

# ============================================================ 3. fabricated figure in playbook
d = run({**BASE,
         "action_playbook": ["Inspect node0697 in rack 34.",
                             "Escalate the 8888 stalled jobs to the on-call engineer."],
         "chart_configs": CHARTS})
show("FIXTURE 3 - fabricated 8888 in action_playbook", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("field named", "action_playbook[1]" in (d["fallback_reason"] or ""), repr(d["fallback_reason"]))
check("draft preserved", bool(d["rejected_draft"]) and "8888" in d["rejected_draft"]["text"])

# ============================================================ 4. comma-grouped regression
d = run({**BASE,
         "executive_summary": "Over the last 24 h the cluster submitted 1,501 jobs and 1,244 ended, "
                              "of which 1,066 COMPLETED. 1,374 jobs are currently active.",
         "chart_configs": CHARTS})
show("FIXTURE 4 - comma-grouped 1,244 (regression)", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [])

# ============================================================ 5. chart contract (regression)
d = run({**BASE, "chart_configs": CHARTS})
show("FIXTURE 5 - chart entries: enum-checked ids, clean captions", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [])
check("chart plumbing is not echoed into the prose", "chart_id" not in d["text"])
check("the ids became real, server-computed charts",
      {"node_risk_watch", "job_outcome_mix"} & {c["chart_id"] for c in d["charts"]}
      or any(u["chart_id"] in ("node_risk_watch", "job_outcome_mix") for u in d["charts_unavailable"]),
      str([c["chart_id"] for c in d["charts"]]))
# ...and a caption is NOT exempt: only the id is
d = run({**BASE, "chart_configs": [
    {"chart_id": "job_outcome_mix", "caption": "All 4242 of them completed cleanly."}]})
check("a fabricated number in a CAPTION is rejected",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["4242"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
check("   and the caption field is named",
      "chart_configs[0].caption" in (d["fallback_reason"] or ""), repr(d["fallback_reason"]))
# ...and an unknown id is dropped with a flag rather than rejecting the report
d = run({**BASE, "chart_configs": [{"chart_id": "made_up_chart", "caption": "Clean caption."}]})
check("an unknown chart_id drops with an advisory, it does not reject",
      d["mode"] == "llm" and any(a["code"] == report.ADV_CHART_ID for a in d["advisories"]),
      f"{d['mode']} {[a['code'] for a in d['advisories']]}")
check("   the dropped id never reaches the rendered list",
      "made_up_chart" not in [c["chart_id"] for c in d["charts"]])

# ============================================================ 6. Chinese reply to an English request
d = run({"executive_summary": "過去 24 小時叢集共提交 1501 個任務，結束 1244 個，其中 1066 個順利完成。"
                              "目前有 1374 個任務在執行中。",
         "risk_assessment":   "P2 偵測到 6 次節點異常，node0697（機櫃 34）分數最高，達 41.2%。",
         "action_playbook":   ["檢查 node0697（機櫃 34）。"]}, lang="en")
show("FIXTURE 6 - Chinese reply to an English request (was a hard rejection, now advisory)", d)
check("report is STILL RETURNED, not discarded", d["mode"] == "llm", d["mode"])
check("language advisory raised", any(a["code"] == "language_mismatch" for a in d["advisories"]),
      str([a["code"] for a in d["advisories"]]))
check("the agent's own text is what is displayed", "1501" in d["text"] and "# Shift report" not in d["text"])
check("no fallback_reason on an accepted report", d["fallback_reason"] is None)

# ============================================================ 7. empty / pure meta-commentary
for name, reply in [("empty string", ""),
                    ("whitespace only", "   \n  "),
                    ("pure meta-commentary, no body",
                     "我已為您產生完整的交接報告，並已輸出為 shift_report.json 檔案。請由下方下載卡片取得。")]:
    d = run(reply)
    show(f"FIXTURE 7 - {name} (must HARD-fail: nothing to display)", d)
    check(f"{name}: hard-failed", d["mode"] == "template_llm_rejected", d["mode"])
    check(f"{name}: reason names the content failure",
          "empty" in (d["fallback_reason"] or "") or "no report body" in (d["fallback_reason"] or "")
          or "nothing to display" in (d["fallback_reason"] or ""), repr(d["fallback_reason"]))
    check(f"{name}: numeric check never ran", d["numeric_check"]["checked"] is False)

# ============================================================ hard gate still armed
print("=" * 100)
print("HARD GATE STILL ARMED")
d = run("# Shift report\n\nThe cluster submitted 1501 jobs and 9999 ended in the window, of which "
        "1066 COMPLETED and 1374 remain active on the floor right now.")
check("plain-markdown fabrication still caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["9999"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run({**BASE, "unexpected_extra_key": "A stray 7777 that is neither a fact nor a chart spec."})
check("unknown-key fabrication still caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["7777"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run({**BASE, "risk_assessment": "The p2_node_alert_score is fine but 5150 nodes are down."})
check("a real fabrication ALONGSIDE an identifier is still caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["5150"],
      f"{d['mode']} {d['numeric_check']['unverified']}")

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
