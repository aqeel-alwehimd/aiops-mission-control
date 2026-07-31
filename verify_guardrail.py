"""
verify_guardrail.py -- standalone check for the report's numeric guardrail. Exits; starts no server.

Run:  python verify_guardrail.py

Demonstrates:
  1. the comma-grouped false positive is gone (the four real strings from the observed rejection),
  2. a genuine hallucination is still rejected,
  3. decimals and zero-padded identifiers still validate,
  4. the "before" behaviour, by re-running each case against the OLD extraction logic.
"""
import re, sys

from report import allowed_numbers, unverified_numbers, _STRIP, _NUM

# ---------------------------------------------------------------- the OLD (buggy) validator
def unverified_numbers_OLD(text, allowed):
    """Exactly what report.py did before the fix: _NUM straight onto the stripped text."""
    bad = []
    for m in _NUM.findall(_STRIP.sub(" ", text)):
        x = float(m)
        if not ({round(x), round(x, 1), round(x, 2)} & allowed):
            bad.append(m)
    return sorted(set(bad), key=lambda s: (len(s), s))

# ---------------------------------------------------------------- facts (shape mirrors assemble_facts)
FACTS = {
    "now_iso": "2022-09-27 19:52Z",
    "window": {"hours": 24, "start_iso": "2022-09-26 19:52Z", "end_iso": "2022-09-27 19:52Z"},
    "settings": {"p3_alert_threshold": 0.30, "p2_triage_pct": 25, "p2_cutoff_watts": 286},
    "cluster_now": {"active_jobs": 1374, "running": 984, "pending": 608,
                    "nodes_total": 826, "nodes_anomalous": 0, "utilisation_pct": 68.6},
    "jobs_window": {"submitted": 1501, "flagged": 212, "ended": 1275,
                    "ended_failed": 44, "ended_timeout": 31, "ended_oom": 2, "ended_completed": 1066},
    "prediction_outcomes": {"failures_in_window": 77, "catch_rate_pct": 38.8,
                            "correct_warnings": {"count": 30, "examples": [{"job_id": 4288317}]},
                            "false_alarms": {"count": 182, "examples": [{"job_id": 4290155}]},
                            "misses": {"count": 47, "examples": [{"job_id": 4291002}]}},
    "node_onsets": {"count": 6, "events": [{"node": 697, "rack": 34, "time": "09-26 20:00",
                                            "kind": "isolated"}]},
    "high_risk_nodes": [{"node": 697, "rack": 34, "risk_pct": 41.2, "temp_c": 38.8,
                         "power_w": 1501, "fan_rpm": 8200, "psu0_w": 274, "state": "WARN",
                         "onset": False}],
    "high_risk_jobs": [],
    "watch_counts": {"nodes": 1, "jobs": 0},
    "model_note": {"p3_threshold": 0.30, "p2_triage_pct": 25,
                   "caveat_en": "OOM is under-predicted in this dataset.",
                   "caveat_zh": "OOM 為低估項。"},
}
ALLOWED = allowed_numbers(FACTS)

# ---------------------------------------------------------------- cases
# (label, narration, must_be_accepted)
CASES = [
    # --- 1. the real observed failure: four comma-grouped facts -------------------------------
    ("REAL FAILURE - 1,501 submitted",
     "Over the last 24 h, 1,501 jobs were submitted.", True),
    ("REAL FAILURE - 1,275 ended",
     "In the window 1,275 jobs ended.", True),
    ("REAL FAILURE - 1,374 active",
     "There are 1,374 active jobs right now.", True),
    ("REAL FAILURE - 1,066 completed",
     "Of those, 1,066 COMPLETED normally.", True),
    ("REAL FAILURE - all four together (the exact observed rejection)",
     "Over the last 24 h the cluster submitted 1,501 jobs and 1,275 ended, of which 1,066 "
     "COMPLETED; 1,374 jobs are currently active.", True),

    # --- 2. other grouping spellings ---------------------------------------------------------
    ("grouped with thin space '1 501'",  "A total of 1 501 jobs were submitted.", True),
    ("grouped with nbsp '1 501'",        "A total of 1 501 jobs were submitted.", True),
    ("grouped with plain space '1 501'",      "A total of 1 501 jobs were submitted.", True),
    ("ungrouped 1501 (control)",              "A total of 1501 jobs were submitted.", True),

    # --- 3. the guardrail must STILL catch real hallucinations --------------------------------
    ("HALLUCINATION - invented count 9,999",
     "A total of 9,999 jobs were submitted.", False),
    ("HALLUCINATION - invented grouped 2,468",
     "There were 2,468 job failures in the window.", False),
    ("HALLUCINATION - invented decimal",
     "The catch rate was 73.4% over the period.", False),
    ("HALLUCINATION - invented node id",
     "Investigate node0512, which is overheating.", False),
    ("HALLUCINATION - computed ratio alongside two real grouped facts",
     "That is 1,501 submitted against 1,275 ended, a ratio of 417.6.", False),
    ("HALLUCINATION - grouped value whose PARTS are facts but whose whole is not "
     "(the old literal-only scan let this through)",
     "A total of 1,002 jobs were submitted.", False),

    # --- 4. things that must keep validating -------------------------------------------------
    ("decimal 38.8 (a fact)",         "P3 catch rate stands at 38.8%.", True),
    ("decimal 68.6 (a fact)",         "Utilisation is ~68.6% (power proxy).", True),
    ("zero-padded id node0697",       "node0697 (rack 34) reached P2 41.2%.", True),
    ("model tokens P2/P3/V100/PSU-0", "P3 and P2 on V100 boards; PSU-0 read 274 W.", True),
    ("sentence comma, not a group",   "In total 44 FAILED, 31 TIMEOUT, 2 OOM.", True),
    ("two numbers separated by a space (must NOT merge)",
     "rack 34 826 nodes total.", True),
    ("percent form of a probability",  "The P3 alert threshold is 0.3 (30%).", True),
    ("large grouped job id 4,288,317", "Correctly warned on job 4,288,317.", True),

    # --- 5. PRE-EXISTING limitation, recorded so it is visible rather than a surprise ----------
    # _scan accepts a number when round(x) / round(x,1) / round(x,2) matches a fact. So a decimal
    # that rounds onto a small fact (here 1 = watch_counts.nodes) is accepted. This is unchanged by
    # the grouping fix -- the old validator behaved identically -- and the brief forbids touching the
    # rounding tolerance, so it is asserted as-is, not "fixed".
    ("KNOWN LIMIT - decimal that rounds onto the fact 1 is accepted (pre-existing)",
     "That works out to a ratio of 1.18.", True),
]

def main():
    w = 54
    print("=" * 100)
    print("NUMERIC GUARDRAIL VERIFICATION")
    print(f"facts contain {len(ALLOWED)} allowed numeric values "
          f"(incl. 1501, 1275, 1374, 1066, 38.8, 68.6, 697, 4288317)")
    print("=" * 100)
    print(f"{'case':{w}} {'BEFORE':>22}  {'AFTER':>22}  verdict")
    print("-" * 100)

    fails = []
    for label, text, want_ok in CASES:
        old = unverified_numbers_OLD(text, ALLOWED)
        new = unverified_numbers(text, ALLOWED)
        old_s = "accepted" if not old else "REJECTED " + ",".join(old[:4])
        new_s = "accepted" if not new else "REJECTED " + ",".join(new[:4])
        got_ok = (not new)
        ok = (got_ok == want_ok)
        if not ok:
            fails.append((label, want_ok, new))
        print(f"{label:{w}.{w}} {old_s:>22.22}  {new_s:>22.22}  {'PASS' if ok else '*** FAIL ***'}")

    print("-" * 100)
    # the headline regression, stated explicitly
    combined = CASES[4][1]
    print("The exact observed failure, before and after:")
    print(f"  narration : {combined}")
    print(f"  BEFORE    : unverified = {unverified_numbers_OLD(combined, ALLOWED)}")
    print(f"  AFTER     : unverified = {unverified_numbers(combined, ALLOWED)}")
    print()
    print("Guardrail still bites (a value absent from the facts under every reading):")
    for label, text, want in CASES:
        if want is False:
            print(f"  {label:<46} -> unverified = {unverified_numbers(text, ALLOWED)}")
    print("-" * 100)
    if fails:
        print(f"{len(fails)} CASE(S) FAILED:")
        for label, want, got in fails:
            print(f"  {label}: expected {'accept' if want else 'reject'}, unverified={got}")
        return 1
    print(f"ALL {len(CASES)} CASES PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
