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
check("the prompt tells the auditor not to flag what is out of scope",
      "out of scope" in prompt)
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

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
report.clear_cooldowns()
sys.exit(1 if FAIL else 0)
