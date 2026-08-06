"""
verify_prompts.py -- the outbound prompts stay under the size the endpoint can actually serve.
Exits non-zero on failure. Starts no server and makes NO network call.

Run:  python verify_prompts.py

WHY THIS SUITE EXISTS
Measured against the LaplaceAI narration endpoint: a report-sized prompt fails with HTTP 504 while a
trivial one succeeds, and the boundary is probabilistic rather than sharp -- the SAME prompt failed
and succeeded on consecutive attempts, every failure landing at ~60.7 s and every success under 60 s.
What is being hit is the agent's own generation budget, and prompt bulk spends that budget before the
report is written. A prompt that is merely verbose is therefore a production outage, not untidiness.

The prompt got there by accretion, not by decision:
    5,881 chars  before the chart menu and the closed section vocabulary existed
    8,685        after eight richly-described chart ids were added
    9,090        after the closed section vocabulary was added
Each step was individually reasonable. Nobody chose the total.

So this suite pins BOTH directions:
  * neither prompt may exceed PROMPT_CEILING_CHARS -- adding a chart or a section breaks a test;
  * every chart id and every section name must still appear in the prompt, so the ceiling can never
    be satisfied by quietly deleting capability instead of verbosity. That is the failure mode a
    size-only check would invite.
"""
import os, sys

os.environ["REPORT_AUDIT"] = "0"
import report
import charts as chartreg
from models import Store

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

store = Store()
POL = {"alert_threshold": 0.30, "node_filter_pct": 25}
# The busiest point in the replay window: most watch-list rows, most onset events, so the largest
# facts payload a real request can produce. Measuring at a quiet moment would understate it.
BUSY_TS = 1664216861
FACTS = report.assemble_facts(store, BUSY_TS, POL, 6)

print("=" * 100)
print("1. prompt sizes at the busiest virtual moment")
print("=" * 100)
sizes = report.prompt_sizes(FACTS)
for k in ("narrator_en", "narrator_zh", "auditor_en", "auditor_zh"):
    ceil = report.PROMPT_CEILING["narrator" if k.startswith("narrator") else "auditor"]
    print(f"  {k:<14} {sizes[k]:>6} chars   ceiling {ceil:>6}   "
          f"margin {ceil - sizes[k]:>6}")

# Two ceilings because the binding constraint is the agent's ~60 s GENERATION budget, not input
# size, and the two agents produce very different amounts of output. See report.PROMPT_CEILING.
check("no prompt exceeds its agent's ceiling", not sizes["over_ceiling"],
      f"over: {sizes['over_ceiling']} -- trim verbosity, do not remove capability")
for k in ("narrator_en", "narrator_zh", "auditor_en", "auditor_zh"):
    ceil = report.PROMPT_CEILING["narrator" if k.startswith("narrator") else "auditor"]
    check(f"  {k} fits its {ceil}-char ceiling", sizes[k] <= ceil, f"{sizes[k]} chars")

print()
print("=" * 100)
print("2. the ceiling was NOT met by dropping capability")
print("=" * 100)
for lang in ("en", "zh"):
    payload = report.trim_facts_for_prompt(FACTS, lang)
    narr = report._build_prompt(payload, lang, "full")
    missing_ids = [cid.value for cid in chartreg.CHART_MENU if f'"{cid.value}"' not in narr]
    check(f"{lang}: every chart id is still offered to the agent", not missing_ids, str(missing_ids))
    missing_secs = [k for k in report._SECTION_TITLES if k not in narr]
    check(f"{lang}: every section is still in the closed vocabulary sent",
          not missing_secs, str(missing_secs))
    for needle, why in (("FACTS DATA", "the facts block is still labelled"),
                        ("chart_configs", "the chart contract is still stated"),
                        ("CLOSED SET", "the vocabulary is still declared closed")):
        check(f"{lang}: {why}", needle in narr)

print()
print("=" * 100)
print("3. the auditor kept its identities and one example per failure class")
print("=" * 100)
payload = report.trim_facts_for_prompt(FACTS, "en")
aud = report._audit_prompt(payload, "draft", identities=report.cohort_prose(FACTS),
                           captions=[("prediction_outcomes", "a caption")])
for inner, outer, _why in report.COHORT_NON_CONTAINMENT:
    short = lambda p: p.rsplit(".", 1)[0].split(".")[-1] if p.endswith(".count") else p.split(".")[-1]
    check(f"non-containment {short(inner)} vs {short(outer)} is still stated",
          f"{short(inner)} is NOT part of {short(outer)}" in aud)
for parts, whole in report.COHORT_IDENTITIES:
    short = lambda p: p.rsplit(".", 1)[0].split(".")[-1] if p.endswith(".count") else p.split(".")[-1]
    check(f"identity summing to {short(whole)} is still stated", f"= {short(whole)}=" in aud)
for label, needle in (("containment", "FALSE (containment"),
                      ("qualitative / no number", "FALSE (qualitative"),
                      ("quantity written as a word", "FALSE (quantity as a word"),
                      ("the CORRECT counter-example", "CORRECT -- DO NOT FLAG")):
    check(f"an example of {label} survives the trim", needle in aud)
for cls in ("CONTAINMENT", "DENOMINATOR", "CAUSATION", "QUALITATIVE CONTRADICTION",
            "MATERIAL OMISSION"):
    check(f"the {cls} class is still named", cls in aud)

print()
print("=" * 100)
print("4. the payload carries no field the narration cannot use")
print("=" * 100)
import json
for lang, other in (("en", "zh"), ("zh", "en")):
    p = report.trim_facts_for_prompt(FACTS, lang)
    blob = json.dumps(p, ensure_ascii=False)
    check(f"{lang}: the other language's cohort/caveat prose is dropped",
          f"cohort_note_{other}" not in blob and f"cohort_{other}" not in blob
          and f"caveat_{other}" not in blob)
    check(f"{lang}: watch_counts is dropped (it restates two list lengths already present)",
          "watch_counts" not in blob)
    check(f"{lang}: model_note no longer duplicates settings",
          '"p3_threshold"' not in blob)
    # The two long cohort notes are merged into one short line for the prompt only; the full
    # bilingual notes stay in `facts` for the template and the raw-facts panel. What must survive is
    # the STATEMENT, in the requested language -- losing it is what let a narrative merge the cohorts.
    need = "不同的任務集合" if lang == "zh" else "different job sets"
    check(f"{lang}: the merged cohort statement survives, in the right language",
          "cohort_note" in blob and need in blob)
    check(f"{lang}: the full bilingual notes are untouched in `facts` itself",
          bool(FACTS["jobs_window"].get("cohort_note_en"))
          and bool(FACTS["jobs_window"].get("cohort_note_zh"))
          and bool(FACTS["prediction_outcomes"].get("cohort_en")))
    check(f"{lang}: the outcome buckets all survive",
          all(b in blob for b in ("correct_warnings", "false_alarms", "pending_outcome", "misses")))

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
