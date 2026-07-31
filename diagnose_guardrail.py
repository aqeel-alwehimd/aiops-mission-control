"""
diagnose_guardrail.py -- WHERE does the unmatched number come from? Exits; no server.

Reproduces a realistic four-key agent response (executive_summary, risk_assessment,
action_playbook, chart_configs), composes it exactly as report.py does, then attributes every
number the checker extracts back to the field that produced it -- or to composition scaffolding.
"""
import json, re
import report
from report import (compose_markdown, allowed_numbers, unverified_numbers,
                    _value_to_md, _pretty_key, _STRIP, _NUM, _degroup)

# ---- facts: the shape assemble_facts returns, deliberately WITHOUT a bare 2 anywhere -------------
FACTS = {
    "now_iso": "2022-09-27 19:52Z",
    "window": {"hours": 24, "start_iso": "2022-09-26 19:52Z", "end_iso": "2022-09-27 19:52Z"},
    "settings": {"p3_alert_threshold": 0.30, "p2_triage_pct": 25, "p2_cutoff_watts": 286},
    "cluster_now": {"active_jobs": 1374, "running": 984, "pending": 390,
                    "nodes_total": 826, "nodes_anomalous": 0, "utilisation_pct": 68.6},
    "jobs_window": {"submitted": 1501, "flagged": 212, "ended": 1244,
                    "ended_failed": 44, "ended_timeout": 31, "ended_oom": 5, "ended_completed": 1066},
    "prediction_outcomes": {"failures_in_window": 77, "catch_rate_pct": 38.8,
                            "correct_warnings": {"count": 30, "examples": [{"job_id": 4288317}]},
                            "false_alarms": {"count": 182, "examples": [{"job_id": 4290155}]},
                            "misses": {"count": 47, "examples": [{"job_id": 4291002}]}},
    "node_onsets": {"count": 6, "events": [{"node": 697, "rack": 34, "time": "09-26 20:00",
                                            "kind": "isolated"}]},
    "high_risk_nodes": [{"node": 697, "rack": 34, "risk_pct": 41.2, "temp_c": 38.8,
                         "power_w": 1501, "fan_rpm": 8200, "psu0_w": 274,
                         "state": "WARN", "onset": False}],
    "high_risk_jobs": [],
    "watch_counts": {"nodes": 1, "jobs": 0},
    "model_note": {"p3_threshold": 0.30, "p2_triage_pct": 25,
                   "caveat_en": "OOM is under-predicted in this dataset.", "caveat_zh": "OOM 為低估項。"},
}
ALLOWED = allowed_numbers(FACTS)

# ---- a realistic agent reply, every prose number taken from the facts ---------------------------
REPLY = {
    "executive_summary":
        "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of which 1066 COMPLETED. "
        "1374 jobs are currently active with utilisation around 68.6%.",
    "risk_assessment":
        "P2 detected 6 node anomaly onsets. node0697 in rack 34 carries the highest score at 41.2% "
        "while running 38.8 C. P3 caught 30 of 77 failures and missed 47.",
    "action_playbook": [
        "Inspect node0697 in rack 34 before the next maintenance window.",
        "Review the 47 missed failures against the 0.3 alert threshold.",
        "Leave the triage at 25% of node-slots for now.",
    ],
    "chart_configs": [
        {"title": "Node risk over time", "type": "line",
         "description": "Plot the top 2 highest-risk nodes across the window."},
        {"title": "Job outcome mix", "type": "bar",
         "description": "Stacked bar of the 4 SLURM end states."},
    ],
}

def nums(text):
    """Exactly what the checker sees: model tokens stripped, digit grouping normalised."""
    return _NUM.findall(_degroup(_STRIP.sub(" ", text)))

def bad(text):
    return unverified_numbers(text, ALLOWED)

def report_composition_finding():
    """STEP 1-4: compose, validate, attribute. Called from __main__ only."""
    print("=" * 100)
    print("STEP 1 - compose the reply exactly as report.py does today")
    print("=" * 100)
    composed = compose_markdown(REPLY, "en")
    for line in composed.split("\n"):
        print("  | " + line)

    print()
    print("=" * 100)
    print("STEP 2 - what the guardrail says about the COMPOSED document (current behaviour)")
    print("=" * 100)
    overall = bad(composed)
    print(f"  unverified on composed markdown : {overall}")
    print(f"  -> mode would be               : {'llm' if not overall else 'template_llm_rejected'}")

    print()
    print("=" * 100)
    print("STEP 3 - attribute every extracted number to its SOURCE")
    print("=" * 100)
    print(f"  {'source':<34} {'numbers extracted':<40} unmatched")
    print("  " + "-" * 96)

    # (a) each field the agent actually wrote, checked on the RAW value
    for key in ("executive_summary", "risk_assessment"):
        v = REPLY[key]
        print(f"  {key:<34} {str(nums(v)):<40} {bad(v)}")
    for i, entry in enumerate(REPLY["action_playbook"]):
        print(f"  {'action_playbook[' + str(i) + ']':<34} {str(nums(entry)):<40} {bad(entry)}")
    for i, spec in enumerate(REPLY["chart_configs"]):
        blob = " ".join(str(x) for x in spec.values())
        print(f"  {'chart_configs[' + str(i) + ']':<34} {str(nums(blob)):<40} {bad(blob)}")

    # (b) scaffolding: what the composition step itself adds, with all agent values removed
    scaffold_lines = []
    for k in REPLY:
        scaffold_lines.append(f"## {_pretty_key(k, True)}")
    scaffold = "\n".join(scaffold_lines)
    print(f"  {'[scaffolding: headings]':<34} {str(nums(scaffold)):<40} {bad(scaffold)}")

    # (c) what _value_to_md does to a list of DICTS -- the chart_configs rendering path
    cc_rendered = _value_to_md(REPLY["chart_configs"])
    print()
    print("  chart_configs rendered by _value_to_md():")
    for line in cc_rendered.split("\n"):
        print("    | " + line)
    print(f"  numbers it contributes: {nums(cc_rendered)}   unmatched: {bad(cc_rendered)}")

    print()
    print("=" * 100)
    print("STEP 4 - FINDING")
    print("=" * 100)
    field_bad = {}
    for key in ("executive_summary", "risk_assessment"):
        if bad(REPLY[key]): field_bad[key] = bad(REPLY[key])
    pb = sorted({n for e in REPLY["action_playbook"] for n in bad(e)})
    if pb: field_bad["action_playbook"] = pb
    cc = sorted({n for s in REPLY["chart_configs"] for n in bad(" ".join(str(x) for x in s.values()))})
    if cc: field_bad["chart_configs"] = cc
    if bad(scaffold): field_bad["scaffolding"] = bad(scaffold)

    print(f"  unmatched numbers by source: {json.dumps(field_bad, ensure_ascii=False)}")
    print()
    if field_bad.get("chart_configs") and not any(k in field_bad for k in
                                                  ("executive_summary", "risk_assessment", "action_playbook")):
        print("  => The agent's PROSE is clean. Every unmatched number comes from chart_configs,")
        print("     which are rendering instructions (axis counts, top-N, window sizes), not claims")
        print("     about the data. Validating them is a false positive by construction.")
    elif field_bad.get("scaffolding"):
        print("  => Composition scaffolding is contributing digits. Fix the scaffolding, not the model.")
    else:
        print("  => Unmatched numbers come from the agent's prose; that is a genuine guardrail hit.")
    print()
    print("  NOTE on rendering: _value_to_md() sends a list of dicts through str(x), so chart_configs")
    print("  is emitted as a raw Python dict repr into the report body (see STEP 3c above).")



# ==================================================================================================
# ADDENDUM -- the THIRD false positive: schema identifiers quoted in prose
# ==================================================================================================
def _attribute(text):
    """For each number the checker extracts, name the source token it was pulled out of."""
    scrubbed = _STRIP.sub(" ", text)
    rows = []
    for tok in re.findall(r"\S+", text):
        got = _NUM.findall(_degroup(_STRIP.sub(" ", tok)))
        for g in got:
            rows.append((tok, g, "OK" if not bad(g) else "UNMATCHED"))
    return rows, _NUM.findall(_degroup(scrubbed)), bad(text)

if __name__ == "__main__":
    report_composition_finding()
    print()
    print("=" * 100)
    print("ADDENDUM - prose that quotes the facts' RAW FIELD NAMES")
    print("=" * 100)
    PROSE = ("The p2_node_alert_score sits above the configured p3_alert_threshold, and the "
             "p2_triage_pct setting still governs which node-slots are scored. The risk_pct "
             "column for node0697 reads 41.2%.")
    print(f"  prose: {PROSE}")
    print()
    rows, extracted, unmatched = _attribute(PROSE)
    print(f"  {'source token':<26} {'number pulled out':<20} status")
    print("  " + "-" * 70)
    for tok, g, st in rows:
        print(f"  {tok:<26} {g:<20} {st}")
    print()
    print(f"  all extracted : {extracted}")
    print(f"  UNMATCHED     : {unmatched}")
    print()
    print("  _STRIP on each identifier (trailing \b cannot match before '_', a word character):")
    for t in ("p2_node_alert_score", "p3_alert_threshold", "p2_triage_pct", "risk_pct", "P2 detected"):
        print(f"    {t!r:24s} -> {_STRIP.sub(' ', t)!r}")
    print()
    print("  is 3 (from p3_) a fact value here?", 3 in ALLOWED, " | is 2 (from p2_)?", 2 in ALLOWED)
    print("  => that asymmetry is exactly why this failed INTERMITTENTLY.")
