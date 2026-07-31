"""
verify_charts.py -- field-scoped numeric validation + the chart_configs exemption. Exits; no server.

Run:  python verify_charts.py

Four fixtures through the real build_report pipeline:
  1. action_playbook with three entries        -> accepted, no ordinal flagged
  2. chart_configs carrying non-fact numbers   -> accepted (rendering instructions, not claims)
  3. a fabricated figure in executive_summary  -> rejected, with the FIELD named
  4. a comma-grouped number (1,244)            -> accepted (regression check on the earlier fix)
"""
import sys, json
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
    report._CACHE.clear(); report._LLM_COOLDOWN = 0.0
    raw = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
    report.generate_llm = lambda facts, l, ln: (raw, None)
    return report.build_report(None, FakeClock(), POLICIES, window_h=24, lang=lang, length=length)

def show(title, d, n=1200):
    print("=" * 100)
    print(title)
    print(f"  mode            : {d['mode']}")
    print(f"  fallback_reason : {d['fallback_reason']}")
    print(f"  numeric_check   : ok={d['numeric_check']['ok']} checked={d['numeric_check']['checked']} "
          f"unverified={d['numeric_check']['unverified']}")
    print("  --- composed text ---")
    for line in d["text"][:n].split("\n"):
        print("  | " + line)
    if len(d["text"]) > n: print(f"  | ... ({len(d['text'])} chars total)")
    print()

CHARTS = [
    {"title": "Node risk over time", "type": "line",
     "description": "Plot the top 2 highest-risk nodes across the window."},
    {"title": "Job outcome mix", "type": "bar",
     "description": "Stacked bar of the 4 SLURM end states, 3 series wide."},
]

# ============================================================ 1. playbook of three entries
d = run({
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
                         "which 1066 COMPLETED. 1374 jobs are active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34.",
                          "Review the 47 missed failures against the 0.3 threshold.",
                          "Hold the triage at 25% of node-slots."],
})
show("FIXTURE 1 - action_playbook with three entries (ordinals must not be invented or flagged)", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))
check("playbook rendered as bullets, not '1.' '2.' '3.'",
      d["text"].count("\n- ") >= 2 and not any(f"\n{i}." in d["text"] for i in (1, 2, 3)))
check("no ordinal digit leaked into the check", "1" not in d["numeric_check"]["unverified"])

# ============================================================ 2. chart_configs with non-fact numbers
d = run({
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34."],
    "chart_configs":     CHARTS,
})
show("FIXTURE 2 - chart_configs contain 2, 4 and 3 -- none of them fact values", d)
check("mode == llm (chart specs exempt)", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))
check("chart section still rendered", "## Chart configs" in d["text"])
check("rendered readably, NOT as a Python dict repr",
      "{'title'" not in d["text"] and "**Node risk over time** (line)" in d["text"])
check("the exempted numbers really are absent from the facts",
      all(report.unverified_numbers(s["description"], report.allowed_numbers(FACTS))
          for s in CHARTS))

# ============================================================ 3. fabricated figure in a prose field
d = run({
    "executive_summary": "Over the last 24 h the cluster submitted 4200 jobs and 1244 ended, of "
                         "which 1066 COMPLETED and 1374 remain active on the floor.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34."],
    "chart_configs":     CHARTS,
})
show("FIXTURE 3 - executive_summary claims 4200 jobs, which is not a fact", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("the fabricated number is named", d["numeric_check"]["unverified"] == ["4200"],
      str(d["numeric_check"]["unverified"]))
check("fallback_reason NAMES THE FIELD",
      d["fallback_reason"] == "numeric check failed in executive_summary: 1 unmatched number (4200)",
      repr(d["fallback_reason"]))
check("chart numbers did not muddy the reason",
      "2" not in d["numeric_check"]["unverified"] and "4" not in d["numeric_check"]["unverified"])

# ============================================================ 4. comma-grouped regression
d = run({
    "executive_summary": "Over the last 24 h the cluster submitted 1,501 jobs and 1,244 ended, of "
                         "which 1,066 COMPLETED. 1,374 jobs are currently active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Review the 47 missed failures against the 0.3 threshold."],
    "chart_configs":     CHARTS,
})
show("FIXTURE 4 - comma-grouped 1,244 etc. (regression check on the earlier separator fix)", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no unmatched numbers", d["numeric_check"]["unverified"] == [],
      str(d["numeric_check"]["unverified"]))

# ============================================================ guardrail is still armed elsewhere
print("=" * 100)
print("GUARDRAIL STILL ARMED (the exemption is narrow, not a blanket pass)")
d = run({"executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, "
                              "of which 1066 COMPLETED and 1374 are active right now.",
         "action_playbook": ["Escalate the 8888 stalled jobs to the on-call engineer immediately."]})
check("fabrication inside action_playbook is caught",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["8888"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
check("   and the field is named", "action_playbook[0]" in (d["fallback_reason"] or ""),
      repr(d["fallback_reason"]))
d = run({"executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, "
                              "of which 1066 COMPLETED and 1374 are active right now.",
         "unexpected_extra_key": "A stray 7777 that is not a fact and is not a chart spec."})
check("fabrication in an UNKNOWN key is still caught (no blanket prose exemption)",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["7777"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
d = run("# Shift report\n\nThe cluster submitted 1501 jobs and 9999 ended in the window, of which "
        "1066 COMPLETED and 1374 remain active on the floor right now.")
check("plain-markdown replies are still validated wholesale",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["9999"],
      f"{d['mode']} {d['numeric_check']['unverified']}")

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
