"""
verify_resilience.py -- retry, class-aware cooldown and payload trimming, against a MOCKED HTTP
layer. Exits; starts no server and makes no network call at all.

Run:  python verify_resilience.py
"""
import io, json, sys, time, contextlib

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

SECRET = "sk-do-not-leak-abcdef0123456789"

# ---------------------------------------------------------------- mocked HTTP layer
class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"message": {"content": "x"}}
    def json(self): return self._payload

class Mock:
    """Replays a scripted sequence of outcomes and records every call."""
    def __init__(self, script): self.script, self.calls, self.payloads = list(script), 0, []
    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        self.payloads.append(json)
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception): raise item
        return item

GOOD = {"message": {"content": json.dumps({
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
                         "which 1066 COMPLETED. 1374 jobs are currently active.",
    "risk_assessment":   "P2 flagged 6 onsets; node0697 in rack 34 leads at 41.2%.",
    "action_playbook":   ["Inspect node0697 in rack 34."],
}, ensure_ascii=False)}}

def run_llm(script, lang="en", length="full", facts=None):
    """Drive the REAL generate_llm with a mocked requests.post. Returns (text, reason, mock, log)."""
    report.clear_cooldowns()
    mock = Mock(script)
    real_post, real_base = report.requests.post, report.RETRY_BASE
    report.requests.post = mock
    report.RETRY_BASE = 0.02                       # keep the test fast; production uses 1.5s
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            t, r = report.generate_llm(facts or FACTS, lang, length)
    finally:
        report.requests.post, report.RETRY_BASE = real_post, real_base
    return t, r, mock, buf.getvalue()

import os
os.environ["LAPLACE_INVOKE_URL"] = "https://mocked.invalid/invoke"
os.environ["LAPLACE_BEARER_SECRET"] = SECRET

# ============================================================ 1. 504 then a successful retry
print("=" * 100); print("CASE 1 - HTTP 504, then the retry succeeds")
t, r, m, log = run_llm([FakeResp(504), FakeResp(200, GOOD)])
print(f"  attempts made : {m.calls}\n  reason        : {r}\n  cooldown      : "
      f"{report.cooldown_remaining(report.AGENT_MAIN)[0]:.0f}s")
check("report succeeded on the retry", t is not None and r is None, repr(r))
check("exactly 2 attempts made", m.calls == 2, str(m.calls))
check("no cooldown set after an eventual success", report.cooldown_remaining(report.AGENT_MAIN)[0] == 0)

# ============================================================ 2. three consecutive 504s
print("=" * 100); print("CASE 2 - three consecutive 504s (retries exhausted)")
t, r, m, log = run_llm([FakeResp(504), FakeResp(504), FakeResp(504)])
cd = report.cooldown_remaining(report.AGENT_MAIN)[0]
print(f"  attempts made : {m.calls}\n  reason        : {r}\n  cooldown      : {cd:.0f}s")
check("gave up after 3 attempts", t is None and m.calls == 3, str(m.calls))
check("reason names the status and attempt count",
      r == "HTTP 504 from endpoint after 3 attempts", repr(r))
check(f"SHORT cooldown ({report.COOLDOWN_TRANSIENT}s), not the old flat 120s",
      report.COOLDOWN_TRANSIENT - 1 <= cd <= report.COOLDOWN_TRANSIENT + 1, f"{cd:.0f}s")
# and what the user is told while it is cooling down
t2, r2 = report.generate_llm(FACTS, "en", "full")
print(f"  during cooldown: {r2}")
check("cooldown reason names the cause and remaining seconds",
      r2.startswith("endpoint cooling down after HTTP 504 (") and r2.endswith("remaining)"), repr(r2))

# ============================================================ 3. 401 -> no retry, long cooldown
print("=" * 100); print("CASE 3 - HTTP 401 (auth): must NOT retry")
t, r, m, log = run_llm([FakeResp(401), FakeResp(200, GOOD)])
cd = report.cooldown_remaining(report.AGENT_MAIN)[0]
print(f"  attempts made : {m.calls}\n  reason        : {r}\n  cooldown      : {cd:.0f}s")
check("exactly 1 attempt -- auth is not retried", m.calls == 1, str(m.calls))
check("reason is auth-specific and actionable",
      "authentication rejected" in (r or "") and "LAPLACE_BEARER_SECRET" in (r or ""), repr(r))
check(f"LONG cooldown ({report.COOLDOWN_AUTH}s)",
      report.COOLDOWN_AUTH - 2 <= cd <= report.COOLDOWN_AUTH + 1, f"{cd:.0f}s")

# ============================================================ 3b. 400 -> no retry, long cooldown
print("=" * 100); print("CASE 3b - HTTP 400 (malformed): must NOT retry")
t, r, m, log = run_llm([FakeResp(400), FakeResp(200, GOOD)])
cd = report.cooldown_remaining(report.AGENT_MAIN)[0]
print(f"  attempts made : {m.calls}\n  reason        : {r}\n  cooldown      : {cd:.0f}s")
check("exactly 1 attempt -- malformed request is not retried", m.calls == 1, str(m.calls))
check("reason says the request was rejected as malformed", "malformed" in (r or ""), repr(r))
check(f"LONG cooldown ({report.COOLDOWN_CLIENT}s)",
      report.COOLDOWN_CLIENT - 2 <= cd <= report.COOLDOWN_CLIENT + 1, f"{cd:.0f}s")

# ============================================================ 4. connection timeout
print("=" * 100); print("CASE 4 - read timeout (transient): retried, then short cooldown")
Timeout = report.requests.exceptions.Timeout
t, r, m, log = run_llm([Timeout("t1"), Timeout("t2"), Timeout("t3")])
cd = report.cooldown_remaining(report.AGENT_MAIN)[0]
print(f"  attempts made : {m.calls}\n  reason        : {r}\n  cooldown      : {cd:.0f}s")
check("timeout retried up to 3 attempts", m.calls == 3, str(m.calls))
check("reason names the timeout and attempt count",
      r == "read timeout after 3 attempts", repr(r))
check("SHORT cooldown for a timeout",
      report.COOLDOWN_TRANSIENT - 1 <= cd <= report.COOLDOWN_TRANSIENT + 1, f"{cd:.0f}s")
t2, r2 = report.generate_llm(FACTS, "en", "full")
check("cooldown reason distinguishes a timeout",
      "cooling down after a read timeout" in (r2 or ""), repr(r2))

# ============================================================ 5. a normal 200 -- no change
print("=" * 100); print("CASE 5 - plain HTTP 200 (no behaviour change)")
t, r, m, log = run_llm([FakeResp(200, GOOD)])
print(f"  attempts made : {m.calls}\n  reason        : {r}")
check("single attempt, success", m.calls == 1 and t is not None and r is None, str(m.calls))
check("no cooldown", report.cooldown_remaining(report.AGENT_MAIN)[0] == 0)

# ============================================================ secret never logged
print("=" * 100); print("SECRET HYGIENE")
_, _, _, log504 = run_llm([FakeResp(504), FakeResp(504), FakeResp(504)])
_, _, _, log401 = run_llm([FakeResp(401)])
alllog = log504 + log401
print("  sample log lines:")
for line in [l for l in alllog.split("\n") if l.strip()][:4]:
    print("   ", line)
check("bearer secret never appears in any log line", SECRET not in alllog)
check("attempt outcomes ARE logged", "attempt 1/3 failed" in alllog and "cooldown" in alllog)

# ============================================================ 6. payload trimming
print("=" * 100); print("CASE 6 - trimmed prompt payload (measured on REAL facts from demo.sqlite)")
from models import Store
from replay import VirtualClock
_store = Store()
_clock = VirtualClock(_store.meta["window_start_ts"], _store.meta["window_end_ts"])
_pol = dict(_store.meta.get("default_policies", {}))
_pol.setdefault("alert_threshold", 0.30); _pol.setdefault("node_filter_pct", 25)
# a mid-window moment with real activity, and a 24 h lookback so the example arrays are populated
REAL = report.assemble_facts(_store, int(_store.meta["window_start_ts"] + 0.6 *
                             (_store.meta["window_end_ts"] - _store.meta["window_start_ts"])),
                             _pol, window_h=24)

full_json = json.dumps(REAL, ensure_ascii=False)
trim_en = report.trim_facts_for_prompt(REAL, "en")
trim_zh = report.trim_facts_for_prompt(REAL, "zh")
tj = json.dumps(trim_en, ensure_ascii=False)
full_prompt = report._build_prompt(REAL, "en", "full")
trim_prompt = report._build_prompt(trim_en, "en", "full")
print(f"  full facts JSON        : {len(full_json):>6} chars")
print(f"  trimmed (en) payload   : {len(tj):>6} chars   ({100*(1-len(tj)/len(full_json)):.0f}% smaller)")
print(f"  trimmed (zh) payload   : {len(json.dumps(trim_zh, ensure_ascii=False)):>6} chars")
print(f"  whole prompt, untrimmed: {len(full_prompt):>6} chars")
print(f"  whole prompt, trimmed  : {len(trim_prompt):>6} chars   "
      f"({100*(1-len(trim_prompt)/len(full_prompt)):.0f}% smaller)")
print(f"  examples per bucket    : {report.MAX_EX} -> {report.PROMPT_EXAMPLES_CAP}")
print(f"  onset events           : {len(REAL['node_onsets']['events'])} -> "
      f"{len(trim_en['node_onsets']['events'])}")
print(f"  high-risk nodes        : {len(REAL['high_risk_nodes'])} -> {len(trim_en['high_risk_nodes'])}")

check("trimmed payload is smaller", len(tj) < len(full_json))
check("the original facts object is NOT mutated", json.dumps(REAL, ensure_ascii=False) == full_json)
# watch_counts is deliberately NOT in this list any more. It was exactly len(high_risk_nodes) and
# len(high_risk_jobs), both of which are still in the payload, so it was a field the narration could
# only restate -- and the narrator prompt has a measured size ceiling driven by the endpoint's ~60 s
# generation budget. Every other section still has to survive the trim.
for k in ("window", "settings", "cluster_now", "jobs_window", "prediction_outcomes",
          "node_onsets", "high_risk_nodes", "high_risk_jobs", "model_note"):
    check(f"section kept: {k}", k in trim_en)
check("watch_counts is dropped, and the lists it counted are still present",
      "watch_counts" not in trim_en
      and isinstance(trim_en.get("high_risk_nodes"), list)
      and isinstance(trim_en.get("high_risk_jobs"), list))
check("now_iso is dropped, and the identical timestamp is still there as window.end_iso",
      "now_iso" not in trim_en
      and trim_en["window"]["end_iso"] == REAL["now_iso"],
      f'{trim_en["window"]["end_iso"]} vs {REAL["now_iso"]}')
check("examples capped", all(
    len(trim_en["prediction_outcomes"][b]["examples"]) <= report.PROMPT_EXAMPLES_CAP
    for b in ("correct_warnings", "false_alarms", "misses")))
check("counts kept in full (only the examples are capped)",
      all(trim_en["prediction_outcomes"][b]["count"] == REAL["prediction_outcomes"][b]["count"]
          for b in ("correct_warnings", "false_alarms", "misses")))
check("English payload drops the Chinese caveat", "caveat_zh" not in trim_en["model_note"]
      and "caveat_en" in trim_en["model_note"])
check("Chinese payload drops the English caveat", "caveat_en" not in trim_zh["model_note"]
      and "caveat_zh" in trim_zh["model_note"])
check("the prompt carries the TRIMMED payload",
      '"start_ts"' not in trim_prompt and '"start_iso"' in trim_prompt)

# THE POINT: the gate validates against the FULL facts, so a value trimmed OUT of the prompt --
# and therefore one the agent could only know from a fuller view -- still passes.
print()
print("  gate validates against the FULL facts, not the trimmed payload:")
_allowed = report.allowed_numbers(REAL)
_schema = report.schema_strip_re(REAL)
_dropped = []
_ns = REAL["cluster_now"].get("nodes_scored")
if _ns is not None: _dropped.append(("cluster_now.nodes_scored", _ns))
for _b in ("correct_warnings", "false_alarms", "misses"):
    _ex = REAL["prediction_outcomes"][_b]["examples"]
    if len(_ex) > report.PROMPT_EXAMPLES_CAP:
        _dropped.append((f"{_b}.examples[{report.PROMPT_EXAMPLES_CAP}].job_id",
                         _ex[report.PROMPT_EXAMPLES_CAP]["job_id"]))
if len(REAL["node_onsets"]["events"]) > report.PROMPT_LIST_CAP:
    _dropped.append(("node_onsets.events[last].node",
                     REAL["node_onsets"]["events"][-1]["node"]))
check("at least one real value is dropped from the prompt but kept in the facts", bool(_dropped),
      str(_dropped[:3]))
for _name, _val in _dropped[:3]:
    _q = f"The report cites {_val} from {_name.split('.')[0]}."
    _u = report.unverified_numbers(_q, _allowed, _schema)
    print(f"    {_name:<44} = {_val:<10} -> quoting it validates: {not _u}")
    check(f"value dropped from prompt still validates ({_name})", _u == [], str(_u))

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
report.clear_cooldowns()
sys.exit(1 if FAIL else 0)
