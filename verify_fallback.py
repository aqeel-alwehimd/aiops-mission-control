"""
verify_fallback.py -- standalone check of fallback_reason, the caching policy and the circuit
breaker. Exits; starts no server and makes no outbound call except one refused local connect.

Run:  python verify_fallback.py

assemble_facts is stubbed so this exercises exactly the build_report plumbing that changed, with no
sqlite store and no dashboard process.
"""
import os, sys, time, importlib

import os
# These suites are not about the second agent: disable it so they can never make a live
# auditor call, whatever is in the environment. verify_auditor.py covers it, fully mocked.
os.environ["REPORT_AUDIT"] = "0"
import report
from verify_guardrail import FACTS

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

# ---------------------------------------------------------------- stubs
class FakeClock:
    def __init__(self, t=1664297512): self.t = t
    def now_ts(self): return self.t

POLICIES = {"alert_threshold": 0.30, "node_filter_pct": 25}
report.assemble_facts = lambda store, t, policies, window_h=6: FACTS     # bypass the sqlite store

def with_llm(text, reason=None):
    report.generate_llm = lambda facts, lang, length: (text, reason)

def build(**kw):
    return report.build_report(None, FakeClock(), POLICIES, window_h=6, lang="en", length="brief", **kw)

def reset_state():
    report._CACHE.clear()
    report.clear_cooldowns()

# ---------------------------------------------------------------- A. clean LLM -> cached
print("\nA. successful narration is accepted and IS cached")
reset_state(); with_llm('Last 24 h: 1501 jobs submitted, 1275 ended, of which 1066 COMPLETED. No node anomaly onsets in the window. Watching 1 node.')
a1 = build(); a2 = build()
check("mode == llm", a1["mode"] == "llm", a1["mode"])
check("fallback_reason is None", a1["fallback_reason"] is None, repr(a1["fallback_reason"]))
check("second call served from cache", a2["cached"] is True)

# ---------------------------------------------------------------- B. the actual bug
print("\nB. THE BUG: a comma-grouped fact is accepted (was rejected before the fix)")
reset_state(); with_llm("Over the last 24 h the cluster submitted 1,501 jobs and 1,275 ended, "
                        "of which 1,066 COMPLETED; 1,374 jobs are currently active.")
b = build()
check("mode == llm (no false rejection)", b["mode"] == "llm", b["mode"])
check("numeric_check.ok", b["numeric_check"]["ok"] is True)
check("no unverified numbers", b["numeric_check"]["unverified"] == [],
      str(b["numeric_check"]["unverified"]))
check("fallback_reason is None", b["fallback_reason"] is None)

# ---------------------------------------------------------------- C. genuine hallucination
print("\nC. a real hallucination is still rejected, is NOT cached, and does NOT trip the breaker")
reset_state(); with_llm('Last 24 h: 9999 jobs submitted, 1275 ended, of which 1066 COMPLETED. No node anomaly onsets in the window. Watching 1 node.')
cool_before = report.cooldown_remaining(report.AGENT_MAIN)[0]
c1 = build(); c2 = build()
check("mode == template_llm_rejected", c1["mode"] == "template_llm_rejected", c1["mode"])
check("unverified lists the number", c1["numeric_check"]["unverified"] == ["9999"],
      str(c1["numeric_check"]["unverified"]))
check("fallback_reason names the cause",
      c1["fallback_reason"] == "numeric check failed: 1 unmatched number (9999)",
      repr(c1["fallback_reason"]))
check("rejection did NOT trip the circuit breaker",
      report.cooldown_remaining(report.AGENT_MAIN)[0] == cool_before,
      f"cooldown {report.cooldown_remaining(report.AGENT_MAIN)[0]}")
check("rejected result was NOT cached (retried next request)", c2["cached"] is False)

# multi-number rejection wording
reset_state(); with_llm('Last 24 h: 9999 jobs submitted and 8888 ended, of which 1066 COMPLETED. No node anomaly onsets in the window. Watching 1 node.')
c3 = build()
check("plural wording for >1 unmatched",
      c3["fallback_reason"] == "numeric check failed: 2 unmatched numbers (8888, 9999)",
      repr(c3["fallback_reason"]))

# ---------------------------------------------------------------- D. endpoint unavailable
print("\nD. endpoint-unavailable reasons are surfaced verbatim and never cached")
for reason in ("LAPLACE_INVOKE_URL not set", "HTTP 401 from endpoint", "read timeout",
               "unexpected response shape (no text in reply)"):
    reset_state(); with_llm(None, reason)
    d1 = build(); d2 = build()
    check(f"reason surfaced: {reason!r}", d1["fallback_reason"] == reason and d1["mode"] == "template")
    check(f"   not cached ({reason[:22]}...)", d2["cached"] is False)

# ---------------------------------------------------------------- E. nocache bypasses a warm cache
print("\nE. nocache=True bypasses a warm cache")
reset_state(); with_llm('Last 24 h: 1501 jobs submitted, 1275 ended, of which 1066 COMPLETED. No node anomaly onsets in the window. Watching 1 node.')
build()
e_cached = build()
e_fresh  = build(nocache=True)
check("warm cache serves cached=True", e_cached["cached"] is True)
check("nocache=True forces regeneration", e_fresh["cached"] is False)

# ---------------------------------------------------------------- F. the REAL generate_llm branches
print("\nF. real generate_llm: env + cooldown branches (no network), secret never leaked")
importlib.reload(report)                          # drop the stubs, restore the genuine functions
report.assemble_facts = lambda store, t, policies, window_h=6: FACTS   # ...except the sqlite store

SECRET = "sk-super-secret-value-do-not-leak-1234567890"
old_env = {k: os.environ.get(k) for k in ("LAPLACE_INVOKE_URL", "LAPLACE_BEARER_SECRET")}
try:
    os.environ.pop("LAPLACE_INVOKE_URL", None)
    os.environ["LAPLACE_BEARER_SECRET"] = SECRET
    t, r = report.generate_llm(FACTS, "en", "brief")
    check("missing URL reported", (t, r) == (None, "LAPLACE_INVOKE_URL not set"), repr(r))

    os.environ["LAPLACE_INVOKE_URL"] = "http://127.0.0.1:9/invoke"   # nothing listens on discard
    os.environ.pop("LAPLACE_BEARER_SECRET", None)
    t, r = report.generate_llm(FACTS, "en", "brief")
    check("missing secret reported", (t, r) == (None, "LAPLACE_BEARER_SECRET not set"), repr(r))

    # connection refused -> ConnectionError branch, trips the breaker
    os.environ["LAPLACE_BEARER_SECRET"] = SECRET
    report.clear_cooldowns()
    t, r = report.generate_llm(FACTS, "en", "brief")
    # CONTRACT CHANGE: transient failures are now RETRIED, so the reason names the attempt count.
    check("refused connection reported, with retry count",
          t is None and r.startswith("could not connect to endpoint after") and "attempt" in r, repr(r))
    check("connection failure DID trip the breaker",
          report.cooldown_remaining(report.AGENT_MAIN)[0] > 0)

    # ...and while it is tripped the reason says COOLDOWN, not "missing key"
    t, r = report.generate_llm(FACTS, "en", "brief")
    check("cooldown reason is explicit, names the cause and the remaining time",
          t is None and r.startswith("endpoint cooling down after")
          and "remaining)" in r, repr(r))

    # no reason string anywhere may contain the secret
    reset_state()
    reasons = []
    for _ in range(3):
        reasons.append(report.build_report(None, FakeClock(), POLICIES, lang="en", length="brief")
                       ["fallback_reason"])
    leaked = [x for x in reasons if x and (SECRET in x or SECRET[:12] in x)]
    check("bearer secret never appears in fallback_reason", not leaked, str(reasons[:1]))
finally:
    report.clear_cooldowns()
    for k, v in old_env.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v

# ---------------------------------------------------------------- summary
print("\n" + "=" * 78)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
