"""
verify_auditor.py -- the Data Auditor (agent 2) is ADVISORY and can never break a report.
Exits; starts no server and makes NO live call -- the HTTP layer is mocked throughout.

Run:  python verify_auditor.py

The contract under test, in one sentence: whatever the auditor does -- time out, 504, 429, return
garbage, disagree, or not exist at all -- the report is still served, `mode` is unchanged, and the
operator can see from the response whether the auditor actually ran.

Also pins the three placement rules that came out of the diagnosis:
  * the auditor does not run for length="brief" (polled every ~12s, cache key changes 48x between polls)
  * the auditor does not run when the hard gate rejected the draft (nobody will read that text)
  * an auditor failure never puts the MAIN agent into cooldown (cooldowns are namespaced per agent)
"""
import io, os, sys, json, time, contextlib

os.environ["REPORT_AUDIT"] = "1"          # this suite is about the auditor; it must be on
os.environ.pop("LAPLACE_DEBUG", None)     # instrumentation off, as in production
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

GOOD_DRAFT = {
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
                         "which 1066 COMPLETED. 1374 jobs are currently active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
}
BAD_DRAFT = {"executive_summary": "The cluster submitted 4200 jobs, a figure found nowhere."}

# ---------------------------------------------------------------- mocked HTTP layer
class FakeResp:
    def __init__(self, status, payload=None, raw=None):
        self.status_code = status
        self._payload = payload
        self.content = (raw if raw is not None else json.dumps(payload or {}).encode())
    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

def verdict(is_valid, reason=None):
    return {"message": {"content": json.dumps({"is_valid": is_valid, "reason": reason})}}

MAIN_URL = "https://mocked.invalid/main"
AUD_URL  = "https://mocked.invalid/auditor"

class Router:
    """Routes by URL so the two agents can be scripted independently, and counts each."""
    def __init__(self, auditor_script):
        self.auditor_script = list(auditor_script)
        self.main_calls, self.auditor_calls = 0, 0
    def __call__(self, url, headers=None, json=None, timeout=None):
        if url == AUD_URL:
            self.auditor_calls += 1
            item = self.auditor_script[min(self.auditor_calls - 1, len(self.auditor_script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        self.main_calls += 1
        raise AssertionError("the main agent must be stubbed at generate_llm, not over HTTP")

def run(auditor_script, draft=GOOD_DRAFT, length="full", creds=True, quiet=True):
    """Drive build_report with the main agent stubbed and the auditor's HTTP layer scripted."""
    report._CACHE.clear(); report.clear_cooldowns()
    report.generate_llm = lambda facts, l, ln: (json.dumps(draft, ensure_ascii=False), None)
    if creds:
        os.environ["LAPLACE_AUDITOR_INVOKE_URL"] = AUD_URL
        os.environ["LAPLACE_AUDITOR_BEARER_SECRET"] = "sk-auditor-secret-never-logged"
    else:
        os.environ.pop("LAPLACE_AUDITOR_INVOKE_URL", None)
        os.environ.pop("LAPLACE_AUDITOR_BEARER_SECRET", None)
    os.environ["LAPLACE_INVOKE_URL"] = MAIN_URL
    os.environ["LAPLACE_BEARER_SECRET"] = "sk-main-secret-never-logged"

    router = Router(auditor_script)
    real_post, real_base = report.requests.post, report.RETRY_BASE
    report.requests.post = router
    report.RETRY_BASE = 0.01                      # keep the suite fast; production uses 1.5s
    buf = io.StringIO()
    try:
        if quiet:
            with contextlib.redirect_stdout(buf):
                d = report.build_report(None, FakeClock(), POLICIES, window_h=24,
                                        lang="en", length=length)
        else:
            d = report.build_report(None, FakeClock(), POLICIES, window_h=24,
                                    lang="en", length=length)
    finally:
        report.requests.post, report.RETRY_BASE = real_post, real_base
    return d, router, buf.getvalue()

def codes(d):
    return [a["code"] for a in d["advisories"]]

def show(title, d, router):
    print("=" * 100)
    print(title)
    a = d["auditor"]
    print(f"  mode            : {d['mode']}   (must never change because of the auditor)")
    print(f"  auditor.ran     : {a['ran']}   state={a['state']}   latency={a['latency_s']}s")
    print(f"  auditor.reason  : {str(a['reason'])[:88]}")
    print(f"  advisories      : {codes(d) or '[]'}")
    print(f"  auditor HTTP calls made: {router.auditor_calls}")
    print()

Timeout = report.requests.exceptions.Timeout

# ============================================================ 1. timeout
d, r, _ = run([Timeout("t1"), Timeout("t2")])
show("CASE 1 - auditor TIMES OUT on every attempt", d, r)
check("report still served", bool(d["text"]) and d["mode"] == "llm", d["mode"])
check("mode unchanged by the auditor", d["mode"] == "llm")
check("advisory code is auditor_timeout", report.ADV_AUDIT_TIMEOUT in codes(d), str(codes(d)))
check("auditor.ran is False (a dead auditor must not look like a passing one)",
      d["auditor"]["ran"] is False)
check("it retried within its own profile", r.auditor_calls == 2, str(r.auditor_calls))

# ============================================================ 2. HTTP 504
d, r, _ = run([FakeResp(504), FakeResp(504)])
show("CASE 2 - auditor returns HTTP 504", d, r)
check("report still served", d["mode"] == "llm" and bool(d["text"]))
check("advisory code is auditor_failed", report.ADV_AUDIT_FAILED in codes(d), str(codes(d)))
check("auditor.ran is False", d["auditor"]["ran"] is False)
check("the reason names the status", "504" in str(d["auditor"]["reason"]), str(d["auditor"]["reason"]))

# ============================================================ 3. HTTP 429
d, r, _ = run([FakeResp(429), FakeResp(429)])
show("CASE 3 - auditor returns HTTP 429 (rate limited)", d, r)
check("report still served", d["mode"] == "llm")
check("advisory raised", report.ADV_AUDIT_FAILED in codes(d), str(codes(d)))
check("429 is treated as transient and retried", r.auditor_calls == 2, str(r.auditor_calls))

# ============================================================ 4. malformed JSON
d, r, _ = run([FakeResp(200, {"message": {"content": "I think the report looks fine, honestly."}})])
show("CASE 4 - auditor returns 200 with text that is not the expected verdict", d, r)
check("report still served", d["mode"] == "llm")
check("advisory code is auditor_unparseable",
      report.ADV_AUDIT_UNPARSEABLE in codes(d), str(codes(d)))
check("auditor.ran is False -- an unparseable reply is not an approval",
      d["auditor"]["ran"] is False)
check("not retried (the endpoint answered 200)", r.auditor_calls == 1, str(r.auditor_calls))

# body that is not JSON at all
d, r, _ = run([FakeResp(200, None, raw=b"<html>gateway</html>")])
check("a non-JSON body also degrades to an advisory, not an exception",
      d["mode"] == "llm" and d["auditor"]["ran"] is False, str(codes(d)))

# ============================================================ 5. valid JSON, is_valid FALSE
d, r, _ = run([FakeResp(200, verdict(False, "The narrative claims 9 onsets; the facts say 6."))])
show("CASE 5 - auditor RAN and DISAGREED", d, r)
check("report is STILL served -- the auditor is advisory, not a gate", d["mode"] == "llm")
check("mode unchanged", d["mode"] == "llm")
check("advisory code is auditor_flag", report.ADV_AUDIT_FLAG in codes(d), str(codes(d)))
check("auditor.ran is True", d["auditor"]["ran"] is True)
check("is_valid False is surfaced", d["auditor"]["is_valid"] is False)
check("the auditor's own words reach the advisory",
      any("9 onsets" in a["message"] for a in d["advisories"]))
check("the agent's text is what is displayed, not the template", "1501" in d["text"])

# ...and the passing case raises nothing
d, r, _ = run([FakeResp(200, verdict(True, None))])
show("CASE 5b - auditor RAN and AGREED", d, r)
check("no auditor advisory when it agrees",
      not any(c.startswith("auditor") for c in codes(d)), str(codes(d)))
check("auditor.ran is True and state is ok",
      d["auditor"]["ran"] is True and d["auditor"]["state"] == "ok")
check("a passing audit is DISTINGUISHABLE from a dead one",
      d["auditor"]["ran"] is not run([Timeout("x")])[0]["auditor"]["ran"])

# ============================================================ 6. credentials absent
d, r, _ = run([FakeResp(200, verdict(True))], creds=False)
show("CASE 6 - auditor credentials absent", d, r)
check("report still served", d["mode"] == "llm" and bool(d["text"]))
check("advisory code is auditor_no_credentials",
      report.ADV_AUDIT_NOCREDS in codes(d), str(codes(d)))
check("auditor.ran is False", d["auditor"]["ran"] is False)
check("NO HTTP call was attempted", r.auditor_calls == 0, str(r.auditor_calls))

# ============================================================ 7. cooldown is per agent
print("=" * 100)
print("CASE 7 - an auditor failure must NEVER gate the main agent")
print("=" * 100)
d, r, _ = run([FakeResp(504), FakeResp(504)])
main_cd, main_why = report.cooldown_remaining(report.AGENT_MAIN)
aud_cd, aud_why = report.cooldown_remaining(report.AGENT_AUDITOR)
print(f"  after an auditor 504 storm:  main cooldown={main_cd:.0f}s   auditor cooldown={aud_cd:.0f}s")
check("the AUDITOR is now cooling down", aud_cd > 0, f"{aud_cd:.0f}s")
check("the MAIN agent is NOT cooling down", main_cd == 0, f"{main_cd:.0f}s ({main_why})")
check("cooldown state is namespaced per agent, not a shared scalar",
      isinstance(report._COOLDOWN, dict) and report.AGENT_AUDITOR in report._COOLDOWN
      and report.AGENT_MAIN not in report._COOLDOWN, str(list(report._COOLDOWN)))
# ...and while the auditor is cooling down it says so, and still does not block
d2, r2, _ = run([FakeResp(200, verdict(True))], quiet=True)   # run() clears cooldowns
report._cooldown(30, "HTTP 504", report.AGENT_AUDITOR)        # re-arm it deliberately
report._CACHE.clear()
report.generate_llm = lambda facts, l, ln: (json.dumps(GOOD_DRAFT, ensure_ascii=False), None)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    d3 = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="en", length="full")
check("a cooling-down auditor yields auditor_cooldown, not a failure",
      d3["mode"] == "llm" and report.ADV_AUDIT_COOLDOWN in codes(d3), str(codes(d3)))
check("   and the main agent still produced the report", "1501" in d3["text"])
report.clear_cooldowns()
print()

# ============================================================ 8. placement rules
print("=" * 100)
print("CASE 8 - WHEN the auditor runs")
print("=" * 100)
d, r, _ = run([FakeResp(200, verdict(True))], length="brief")
print(f"  length=brief  -> auditor HTTP calls: {r.auditor_calls}, state={d['auditor']['state']}")
check("the auditor is NOT called for a brief report", r.auditor_calls == 0, str(r.auditor_calls))
check("   and the brief report is unaffected", d["mode"] == "llm" and bool(d["text"]))
check("   its state says it simply does not run here", d["auditor"]["state"] == "skipped")

d, r, _ = run([FakeResp(200, verdict(True))], draft=BAD_DRAFT)
print(f"  hard gate rejects -> auditor HTTP calls: {r.auditor_calls}, mode={d['mode']}")
check("the hard gate still rejects the fabricated draft",
      d["mode"] == "template_llm_rejected" and d["numeric_check"]["unverified"] == ["4200"],
      f"{d['mode']} {d['numeric_check']['unverified']}")
check("the auditor is NOT called on a draft the gate threw away", r.auditor_calls == 0,
      str(r.auditor_calls))

d, r, _ = run([FakeResp(200, verdict(True))])
check("the auditor IS called for a full report that passed the gate", r.auditor_calls == 1,
      str(r.auditor_calls))
print()

# ============================================================ 9. time budget
print("=" * 100)
print("CASE 9 - the auditor is skipped rather than extending a request")
print("=" * 100)
report._CACHE.clear(); report.clear_cooldowns()
os.environ["LAPLACE_AUDITOR_INVOKE_URL"] = AUD_URL
os.environ["LAPLACE_AUDITOR_BEARER_SECRET"] = "sk"
real_budget = report.REPORT_TIME_BUDGET
def slow_main(facts, l, ln):
    time.sleep(0.05)
    return json.dumps(GOOD_DRAFT, ensure_ascii=False), None
try:
    report.REPORT_TIME_BUDGET = 0.0        # pretend narration consumed the whole budget
    report.generate_llm = slow_main
    router = Router([FakeResp(200, verdict(True))])
    real_post = report.requests.post
    report.requests.post = router
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="en", length="full")
finally:
    report.requests.post = real_post
    report.REPORT_TIME_BUDGET = real_budget
print(f"  budget exhausted -> auditor HTTP calls: {router.auditor_calls}, state={d['auditor']['state']}")
check("report still served", d["mode"] == "llm" and bool(d["text"]))
check("advisory code is auditor_skipped_budget",
      report.ADV_AUDIT_BUDGET in codes(d), str(codes(d)))
check("NO call was made once the budget was gone", router.auditor_calls == 0)
print()

# ============================================================ 10. payload + secret hygiene
print("=" * 100)
print("CASE 10 - the auditor sees exactly what the narrator saw, and no secret is logged")
print("=" * 100)
trimmed = report.trim_facts_for_prompt(FACTS, "en")
prompt = report._audit_prompt(trimmed, "draft text")
full_bytes = len(json.dumps(FACTS, ensure_ascii=False).encode())
trim_bytes = len(json.dumps(trimmed, ensure_ascii=False).encode())
print(f"  full facts {full_bytes}b -> auditor payload carries the trimmed {trim_bytes}b view")
check("the auditor payload is the TRIMMED view, not the whole facts dict",
      json.dumps(trimmed, ensure_ascii=False) in prompt
      and json.dumps(FACTS, ensure_ascii=False) not in prompt)
check("...which is byte-identical to what the narrator was given, so no field the narrative "
      "could cite is withheld",
      json.dumps(report.trim_facts_for_prompt(FACTS, "en"), ensure_ascii=False) in prompt)
check("the prompt tells the auditor not to flag what the writer never saw",
      "must never be flagged as missing" in prompt, prompt[-400:])
d, r, log = run([FakeResp(504), Timeout("x")], quiet=True)
check("the bearer secret never appears in any advisory message",
      not any("sk-auditor-secret" in a["message"] for a in d["advisories"]))
check("...nor in the auditor state", "sk-auditor-secret" not in json.dumps(d["auditor"]))

# ============================================================ 11. the response is inspectable
print()
print("=" * 100)
print("CASE 11 - auditor state is visible in the API response")
print("=" * 100)
for label, script, creds in (("agreed", [FakeResp(200, verdict(True))], True),
                             ("flagged", [FakeResp(200, verdict(False, "mismatch"))], True),
                             ("timeout", [Timeout("x"), Timeout("y")], True),
                             ("no creds", [FakeResp(200, verdict(True))], False)):
    d, r, _ = run(script, creds=creds)
    a = d["auditor"]
    print(f"  {label:9} ran={str(a['ran']):5} state={a['state']:16} code={a['advisory_code']}")
    check(f"{label}: response carries an auditor block", isinstance(a, dict) and "state" in a)
    check(f"{label}: mode is llm regardless", d["mode"] == "llm")
check("every distinct failure mode has its own advisory code",
      len({report.ADV_AUDIT_FLAG, report.ADV_AUDIT_NOCREDS, report.ADV_AUDIT_FAILED,
           report.ADV_AUDIT_TIMEOUT, report.ADV_AUDIT_COOLDOWN, report.ADV_AUDIT_UNPARSEABLE,
           report.ADV_AUDIT_BUDGET}) == 7)
check("audit_llm never raises, whatever it is handed",
      report.audit_llm({}, "", lang="en", budget_left=-5)["ran"] is False)

# ============================================================ 12. the REBUILT contract
# The auditor's output shape changed from a verdict to a list of findings, its prompt changed to
# target relational claims rather than numeric ones, and its scope grew to include captions. None of
# that may weaken anything asserted above, which is why these cases come last rather than replacing
# the ones before them.
print()
print("=" * 100)
print("CASE 12 - the findings contract")
print("=" * 100)

def findings_reply(findings, checked=("checked the flagged partition",
                                      "checked the failure identity",
                                      "checked the two cohorts")):
    return {"message": {"content": json.dumps({"relational_claims_checked": list(checked),
                                               "findings": list(findings)})}}

FIND = [{"quote": "the 231 resolved failures sit within that cohort",
         "contradicts": "prediction_outcomes.failures_resolved vs flagged_total",
         "why": "failures_resolved includes misses, which were never flagged",
         "severity": "high", "location": "narrative"}]

d, r, _ = run([FakeResp(200, findings_reply(FIND))])
show("CASE 12a - auditor returns a findings LIST with one finding", d, r)
check("a non-empty findings list flags the report", d["auditor"]["state"] == "flagged")
check("the report is STILL served -- findings are advisory", d["mode"] == "llm")
check("the finding survives into the auditor block", len(d["auditor"]["findings"]) == 1)
check("the quoted span reaches the advisory",
      any("231 resolved failures" in a["message"] for a in d["advisories"]), str(codes(d)))
check("the severity reaches the advisory", any("[high]" in a["message"] for a in d["advisories"]))
check("the contradicted fact reaches the advisory",
      any("failures_resolved" in a["message"] for a in d["advisories"]))
check("is_valid is retained and means 'the findings list was empty'",
      d["auditor"]["is_valid"] is False)

d, r, _ = run([FakeResp(200, findings_reply([]))])
show("CASE 12b - empty findings list is the PASS condition", d, r)
check("an empty findings list is state ok", d["auditor"]["state"] == "ok")
check("no auditor advisory is raised", not any(c.startswith("auditor") for c in codes(d)),
      str(codes(d)))
check("the work it says it did is recorded", len(d["auditor"]["checked"]) == 3)
check("is_valid True", d["auditor"]["is_valid"] is True)

MANY = [dict(FIND[0], quote=f"claim {i}") for i in range(12)]
d, r, _ = run([FakeResp(200, findings_reply(MANY))])
n_flag = sum(1 for a in d["advisories"] if a["code"] == report.ADV_AUDIT_FLAG)
check("many findings produce one advisory each, capped, with the remainder counted",
      n_flag == report.MAX_AUDIT_FINDINGS + 1, f"{n_flag} advisories for {len(MANY)} findings")
check("   and the report is still served", d["mode"] == "llm")

d, r, _ = run([FakeResp(200, verdict(False, "the old single-boolean shape"))])
check("the PREVIOUS verdict shape is still understood, not discarded as unparseable",
      d["auditor"]["state"] == "flagged" and d["auditor"]["ran"] is True, d["auditor"]["state"])
check("   and it is normalised into a finding",
      len(d["auditor"]["findings"]) == 1
      and "old single-boolean" in d["auditor"]["findings"][0]["why"])
d, r, _ = run([FakeResp(200, {"message": {"content": json.dumps({"verdict": "looks fine"})}})])
check("a reply that is neither shape is still unparseable, not a silent pass",
      d["auditor"]["state"] == "unparseable" and d["auditor"]["ran"] is False)
print()

print("=" * 100)
print("CASE 13 - the prompt targets the RELATIONAL class and carries the cohort identities")
print("=" * 100)
p = report._audit_prompt(report.trim_facts_for_prompt(FACTS, "en"), "draft",
                         identities=report.cohort_prose(FACTS),
                         captions=[("prediction_outcomes", "a caption the agent wrote")])
for label, needle in (
        ("states that numbers are ALREADY verified", "already verified"),
        ("says re-checking a number is NOT its job", "is NOT your job"),
        ("names containment as the target class", "CONTAINMENT"),
        ("names the wrong-denominator class", "DENOMINATOR"),
        ("names unsupported causal claims", "CAUSATION"),
        ("names qualitative contradiction with no number", "QUALITATIVE CONTRADICTION"),
        ("names material omission", "MATERIAL OMISSION"),
        ("carries a worked example of a real false claim", "sit within that cohort"),
        ("carries a CORRECT counter-example so it is not taught to object to everything",
         "CORRECT, DO NOT FLAG"),
        ("asks for findings, not a verdict", '"findings"'),
        ("requires the relational claims it checked to be enumerated",
         "relational_claims_checked"),
        ("says an empty list is the right answer for a sound report", "EMPTY findings list is the correct"),
        ("includes the agent's caption in scope", "a caption the agent wrote"),
        ("tells it a caption is a claim", "a caption is a claim")):
    check(f"prompt {label}", needle in p, needle)
check("the prompt states the non-containment the live failures violated",
      "misses.count is NOT part of" in p and "failures_resolved is NOT part of" in p)
check("the identities carry THIS report's live values",
      str(FACTS["prediction_outcomes"]["flagged_total"]) in p)
check("the payload is still the TRIMMED view the narrator saw",
      json.dumps(report.trim_facts_for_prompt(FACTS, "en"), ensure_ascii=False) in p)
print(f"  prompt is {len(p)} characters (was ~{len(json.dumps(FACTS, ensure_ascii=False)) + 400})")
print()

print("=" * 100)
print("CASE 14 - captions are audited, and the resilience profile is unchanged")
print("=" * 100)
seen_prompt = {}
class Capture(Router):
    def __call__(self, url, headers=None, json=None, timeout=None):
        if url == AUD_URL:
            seen_prompt["text"] = (json or {}).get("message", "")
            seen_prompt["timeout"] = timeout
        return Router.__call__(self, url, headers=headers, json=json, timeout=timeout)

report._CACHE.clear(); report.clear_cooldowns()
DRAFT_WITH_CHART = dict(GOOD_DRAFT, chart_configs=[
    {"chart_id": "prediction_outcomes", "caption": "A caption written by the agent."},
    {"chart_id": "job_outcome_mix", "caption": ""}])
report.generate_llm = lambda facts, l, ln: (json.dumps(DRAFT_WITH_CHART, ensure_ascii=False), None)
os.environ["LAPLACE_AUDITOR_INVOKE_URL"] = AUD_URL
os.environ["LAPLACE_AUDITOR_BEARER_SECRET"] = "sk"
os.environ["LAPLACE_INVOKE_URL"] = MAIN_URL
os.environ["LAPLACE_BEARER_SECRET"] = "sk"
cap_router = Capture([FakeResp(200, findings_reply([]))])
real_post = report.requests.post
report.requests.post = cap_router
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        d = report.build_report(None, FakeClock(), POLICIES, window_h=24, lang="en", length="full")
finally:
    report.requests.post = real_post
check("the agent's caption was sent to the auditor",
      "A caption written by the agent." in seen_prompt.get("text", ""))
check("an EMPTY caption is not sent (Python writes those, there is nothing to audit)",
      "job_outcome_mix" not in seen_prompt.get("text", "").split("REPORT NARRATIVE")[0]
      .split("CHART CAPTIONS")[-1])
check("the auditor still uses its own generous per-attempt timeout, not the 15s that failed 5/5",
      seen_prompt.get("timeout", 0) >= 60.0, str(seen_prompt.get("timeout")))
check("the auditor profile is unchanged: 2 attempts, 75s deadline, 60s timeout",
      report.AGENT_PROFILE[report.AGENT_AUDITOR] ==
      {"attempts": 2, "deadline": 75.0, "timeout": 60.0, "min_room": 10.0},
      str(report.AGENT_PROFILE[report.AGENT_AUDITOR]))
check("it still routes through invoke_agent (one resilient path for both agents)",
      "invoke_agent" in report.audit_llm.__code__.co_names)
print()

# ============================================================ 15. the DETERMINISTIC cohort check
print("=" * 100)
print("CASE 15 - cohort_containment_review: deterministic, non-blocking, and quiet on good text")
print("=" * 100)
FALSE_SENT = ("P3 flagged 331 jobs at submission, and the 231 resolved failures sit within that "
              "cohort.")
TRUE_SENT  = ("P3 flagged 331 jobs at submission; separately, 78 failures were never flagged.")
FX = {"jobs_window": {"submitted": 2245, "flagged_at_submission": 331,
                      "submitted_outcome_known": 1735, "submitted_still_running": 510,
                      "ended_in_window": 2029, "ended_in_window_failed": 254,
                      "ended_in_window_timeout": 98, "ended_in_window_oom": 8,
                      "ended_in_window_completed": 1669},
      "prediction_outcomes": {"flagged_total": 331, "failures_resolved": 231,
                              "correct_warnings": {"count": 153}, "false_alarms": {"count": 86},
                              "pending_outcome": {"count": 92}, "misses": {"count": 78}}}
hit = report.cohort_containment_review(FALSE_SENT, FX)
check("a false containment between two REAL facts is caught", len(hit) == 1, str(hit))
check("   the advisory names the offending sentence",
      hit and "sit within that cohort" in hit[0]["message"])
check("   and its code is its own, not borrowed from the caption check",
      hit and hit[0]["code"] == report.ADV_COHORT)
check("the CORRECT sentence stating misses separately is NOT flagged",
      report.cohort_containment_review(TRUE_SENT, FX) == [], "this is the documented FP trap")
check("a caption is covered as well as the body",
      len(report.cohort_containment_review("", FX, [("caption on 'x'",
          "The ring covers all 331 flagged jobs, including the 78 missed failures.")])) == 1)
check("correct_warnings is never resolvable -- it genuinely belongs to two sets",
      153 not in report.resolve_sets(FX).keys() or
      report.resolve_sets(FX).get(153) is None, str(report.resolve_sets(FX).get(153)))
check("the check never raises on junk input",
      report.cohort_containment_review(None, {}) == []
      and report.cohort_containment_review("x", None) == [])
print()

print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
report.clear_cooldowns()
sys.exit(1 if FAIL else 0)
