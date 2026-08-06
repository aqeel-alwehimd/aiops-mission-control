"""
verify_cohort.py -- the flagged/outcome cohort identity, checked against the real store.
Exits; starts no server and makes no network call.

Run:  python verify_cohort.py

THE BUG THIS PINS DOWN
A generated report read: 612 jobs flagged, 406 correctly warned, 100 false alarms, 158 missed.
406 + 158 = 564 was consistent (both count failures), but 406 + 100 = 506, not 612.

The cause was not one leak but a cohort mismatch with TWO legs, running in opposite directions:
  * `flagged` counted jobs whose SUBMIT time fell in the window;
  * the outcome breakdown counted jobs whose END time fell in the window.
So a job submitted inside the window and still running at the report time was flagged but had no
outcome (leg 1, the smaller leg), while a long job submitted BEFORE the window and ended inside it
contributed a caught/false-alarm without ever being counted as flagged (leg 2, usually the larger).
Whichever leg dominated decided the sign of the gap, which is why it looked arbitrary.

The fix scores one cohort -- jobs submitted in the window -- and gives the flagged jobs that have
not ended their own bucket. This script asserts the resulting identities hold everywhere in the
replay window, at several lookbacks and thresholds, and measures how big the old gap really was.
"""
import sys
import os
# These suites are not about the second agent: disable it so they can never make a live
# auditor call, whatever is in the environment. verify_auditor.py covers it, fully mocked.
os.environ["REPORT_AUDIT"] = "0"
import report
from models import Store

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

store = Store()
W0, W1 = int(store.meta["window_start_ts"]), int(store.meta["window_end_ts"])
BASE_POL = dict(store.meta.get("default_policies", {}))
BASE_POL.setdefault("alert_threshold", 0.30)
BASE_POL.setdefault("node_filter_pct", 25)

GRID = [(frac, win, thr)
        for frac in (0.15, 0.35, 0.5, 0.7, 0.9, 1.0)
        for win in (1, 6, 24)
        for thr in (0.30, 0.60)]

# ============================================================ 1. the identities, everywhere
print("=" * 100)
print("1. cohort identities across the replay window")
print("=" * 100)
print(f"  {'at':>6} {'win':>4} {'thr':>5} | {'flagged':>7} {'caught':>7} {'false':>6} {'pend':>6} "
      f"{'sum':>6} | {'miss':>6} {'failres':>7} | old-gap")
print("  " + "-" * 96)

bad_flag, bad_fail, bad_sub, old_gaps = [], [], [], []
for frac, win, thr in GRID:
    t = int(W0 + frac * (W1 - W0))
    pol = {**BASE_POL, "alert_threshold": thr}
    f = report.assemble_facts(store, t, pol, win)
    po, jw = f["prediction_outcomes"], f["jobs_window"]
    flagged = po["flagged_total"]
    c  = po["correct_warnings"]["count"]
    fa = po["false_alarms"]["count"]
    pe = po["pending_outcome"]["count"]
    mi = po["misses"]["count"]

    if c + fa + pe != flagged:
        bad_flag.append((frac, win, thr, flagged, c + fa + pe))
    if c + mi != po["failures_resolved"]:
        bad_fail.append((frac, win, thr, po["failures_resolved"], c + mi))
    if jw["submitted_outcome_known"] + jw["submitted_still_running"] != jw["submitted"]:
        bad_sub.append((frac, win, thr))
    if jw["flagged_at_submission"] != flagged:
        bad_flag.append((frac, win, thr, "jobs_window/prediction_outcomes disagree", ""))

    # what the OLD code would have printed: outcomes taken from the ended-in-window cohort
    old = store._df("SELECT state,risk FROM jobs WHERE end_ts>? AND end_ts<=?",
                    (int(t - win * 3600), t))
    old_c  = int(((old.state != "COMPLETED") & (old.risk >= thr)).sum()) if len(old) else 0
    old_fa = int(((old.state == "COMPLETED") & (old.risk >= thr)).sum()) if len(old) else 0
    gap = flagged - (old_c + old_fa)
    old_gaps.append(gap)
    print(f"  {frac:>6} {win:>3}h {thr:>5} | {flagged:>7} {c:>7} {fa:>6} {pe:>6} {c+fa+pe:>6} | "
          f"{mi:>6} {po['failures_resolved']:>7} | {gap:+d}")

print()
check(f"caught + false alarms + pending == flagged, at all {len(GRID)} sample points",
      not bad_flag, str(bad_flag[:3]))
check("caught + missed == failures_resolved, at all sample points", not bad_fail, str(bad_fail[:3]))
check("outcome-known + still-running == submitted, at all sample points", not bad_sub, str(bad_sub[:3]))
check("the old code's gap was real and BOTH-SIGNED (so it was never just 'pending')",
      any(g > 0 for g in old_gaps) and any(g < 0 for g in old_gaps),
      f"max +{max(old_gaps)}, min {min(old_gaps)}")

# ============================================================ 2. key names name their cohort
print()
print("=" * 100)
print("1b. the SHARED cohort model -- the same definitions the auditor prompt is generated from")
print("=" * 100)
# The identities above are asserted by hand because that is what pins the arithmetic. These assert
# that report.COHORT_IDENTITIES says the SAME thing, because that structure is what
# report.cohort_prose() renders into the auditor's prompt. If the two ever disagree, the auditor is
# being told something this suite does not check, which is exactly the drift this section prevents.
bad_ident, bad_member, bad_noncont = [], [], []
for frac, win, thr in GRID:
    t = int(W0 + frac * (W1 - W0))
    f = report.assemble_facts(store, t, {**BASE_POL, "alert_threshold": thr}, win)
    for parts, whole in report.COHORT_IDENTITIES:
        lhs = sum(report._fact_at(f, p) for p in parts)
        rhs = report._fact_at(f, whole)
        if lhs != rhs:
            bad_ident.append((frac, win, thr, whole, lhs, rhs))

f = report.assemble_facts(store, int(W0 + 0.7 * (W1 - W0)), BASE_POL, 24)
for key in report.SET_OF_KEY:
    if not isinstance(report._fact_at(f, key), (int, float)):
        bad_member.append(key)
for parts, whole in report.COHORT_IDENTITIES:
    for k in tuple(parts) + (whole,):
        if k not in report.SET_OF_KEY:
            bad_ident.append(("unregistered key in an identity", k))
for inner, outer, _why in report.COHORT_NON_CONTAINMENT:
    for k in (inner, outer):
        if k not in report.SET_OF_KEY:
            bad_noncont.append(k)

check(f"every key in report.SET_OF_KEY resolves to a number in the facts", not bad_member,
      str(bad_member[:4]))
check(f"report.COHORT_IDENTITIES holds at all {len(GRID)} sample points", not bad_ident,
      str(bad_ident[:3]))
check("every key named in an identity or a non-containment is registered in SET_OF_KEY",
      not bad_noncont, str(bad_noncont[:4]))
check("correct_warnings is the ONLY key belonging to two sets (it IS the overlap)",
      [k for k, v in report.SET_OF_KEY.items() if len(v) > 1]
      == ["prediction_outcomes.correct_warnings.count"],
      str([k for k, v in report.SET_OF_KEY.items() if len(v) > 1]))
prose = report.cohort_prose(f)
# cohort_prose() names keys by their last segment now -- the full dotted paths cost ~500 characters
# of an auditor prompt that has a measured size ceiling, and the section is already given by the
# heading. The CONTENT is unchanged, which is what this asserts.
def _short(pth):
    return pth.rsplit(".", 1)[0].split(".")[-1] if pth.endswith(".count") else pth.split(".")[-1]
check("cohort_prose() states every non-containment the model declares",
      all(f"{_short(i)} is NOT part of {_short(o)}" in prose
          for i, o, _ in report.COHORT_NON_CONTAINMENT), prose[-400:])
check("cohort_prose() states every identity the model declares",
      all(f"= {_short(w)}=" in prose for _p, w in report.COHORT_IDENTITIES))
check("cohort_prose() carries THIS report's live values, not a static template",
      str(report._fact_at(f, "prediction_outcomes.flagged_total")) in prose)
print()
print("  --- the prose block the auditor is given ---")
for line in prose.split("\n"):
    print("  " + line)

print()
print("=" * 100)
print("2. key names are unambiguous about which cohort they count")
print("=" * 100)
f = report.assemble_facts(store, int(W0 + 0.7 * (W1 - W0)), BASE_POL, 24)
jw, po = f["jobs_window"], f["prediction_outcomes"]
for k in sorted(jw):
    print(f"  jobs_window.{k:<28} = {jw[k] if not isinstance(jw[k], str) else jw[k][:44] + '...'}")
print()
check("the bare 'flagged' key is gone (it did not say WHEN a job was flagged)", "flagged" not in jw)
check("the bare 'ended' key is gone (it did not say WHICH jobs ended)", "ended" not in jw)
check("submission-cohort counts say so", "flagged_at_submission" in jw)
check("ended-cohort counts say so",
      all(k in jw for k in ("ended_in_window", "ended_in_window_failed",
                            "ended_in_window_timeout", "ended_in_window_oom",
                            "ended_in_window_completed")))
check("the pending bucket exists and is named for what it is", "pending_outcome" in po)
check("'failures_in_window' renamed -- it never meant 'in the window'", "failures_in_window" not in po)
check("the facts carry the cohort caveat in both languages",
      bool(jw.get("cohort_note_en")) and bool(jw.get("cohort_note_zh"))
      and bool(po.get("cohort_en")) and bool(po.get("cohort_zh")))

# ============================================================ 3. no future leakage in the pending bucket
print()
print("=" * 100)
print("3. the pending bucket must not leak how a still-running job turns out")
print("=" * 100)
leaky = []
for frac in (0.3, 0.5, 0.7, 0.9):
    t = int(W0 + frac * (W1 - W0))
    f = report.assemble_facts(store, t, BASE_POL, 24)
    for ex in f["prediction_outcomes"]["pending_outcome"]["examples"]:
        if "state" in ex:
            leaky.append((frac, ex))
ex = f["prediction_outcomes"]["pending_outcome"]["examples"][:2]
print(f"  sample pending examples: {ex}")
check("a pending example carries no eventual SLURM state", not leaky, str(leaky[:2]))
check("it carries the observable status instead",
      all("status" in e and e["status"] in ("RUNNING", "PENDING") for e in ex), str(ex))

# ============================================================ 4. the template narrates it correctly
print()
print("=" * 100)
print("4. the rendered template reports the pending bucket rather than hiding it")
print("=" * 100)
t = int(W0 + 0.5 * (W1 - W0))
f = report.assemble_facts(store, t, BASE_POL, 6)
po = f["prediction_outcomes"]
for lang in ("en", "zh"):
    txt = report.render_template(f, lang, "full")
    line = next((l for l in txt.split("\n") if str(po["flagged_total"]) in l and "P3" in l), "")
    print(f"  [{lang}] {line.strip()[:150]}")
    check(f"{lang}: the full template states the flagged total", str(po["flagged_total"]) in txt)
    check(f"{lang}: ...and the pending count alongside it",
          str(po["pending_outcome"]["count"]) in txt)
    brief = report.render_template(f, lang, "brief")
    check(f"{lang}: the brief report mentions the pending count too",
          str(po["pending_outcome"]["count"]) in brief, brief[:110])

# ============================================================ 5. every fact stays a real number
print()
print("=" * 100)
print("5. the numeric guardrail still derives its allowed set from these facts")
print("=" * 100)
allowed = report.allowed_numbers(f)
for name, val in (("flagged_total", po["flagged_total"]),
                  ("pending_outcome.count", po["pending_outcome"]["count"]),
                  ("correct_warnings.count", po["correct_warnings"]["count"]),
                  ("misses.count", po["misses"]["count"])):
    q = f"The report cites {val} for {name.split('.')[0]}."
    u = report.unverified_numbers(q, allowed, report.schema_strip_re(f))
    check(f"quoting {name} ({val}) validates", u == [], str(u))
u = report.unverified_numbers("A total of 987654 jobs were flagged.", allowed)
check("a number absent from the new facts is still rejected", u == ["987654"], str(u))

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for x in FAIL: print("  FAILED:", x)
sys.exit(1 if FAIL else 0)
