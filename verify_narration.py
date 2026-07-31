"""
verify_narration.py -- standalone check of the agent-reply post-processing. Exits; no server.

Run:  python verify_narration.py

Feeds report.py's post-processing three real-shaped fixtures -- a JSON-object reply, a plain-markdown
reply, and the observed meta-commentary reply -- and prints, for each, the resulting mode, the
composed text and the fallback reason. Also checks a hallucinated number is still caught and that a
rejected result is never cached.
"""
import sys, json, importlib

import report
from verify_guardrail import FACTS

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

class FakeClock:
    def now_ts(self): return 1664297512
POLICIES = {"alert_threshold": 0.30, "node_filter_pct": 25}

# ---------------------------------------------------------------- fixtures
# 1. the agent's JSON contract (values are prose strings; numbers are real facts)
F_JSON = json.dumps({
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 1275 ended, "
                         "of which 1066 COMPLETED. 1374 jobs are currently active.",
    "risk_assessment":   "6 node anomaly onsets were detected by P2. node0697 in rack 34 is the "
                         "highest-scoring node at 41.2%, running 38.8 C.",
    "recommended_actions": ["Inspect node0697 in rack 34.",
                            "Review the 47 missed failures against the 0.3 threshold."],
    "unexpected_extra_key": "This key is not in the expected set and must be appended, not dropped.",
}, ensure_ascii=False)

# 2. an ordinary markdown reply (what the agent used to return)
F_MD = ("# Shift report\n\n"
        "**Situation.** Over the last 24 h the cluster submitted 1501 jobs and 1275 ended, of which "
        "1066 COMPLETED; 1374 jobs are currently active and utilisation is ~68.6%.\n\n"
        "## What happened\n"
        "- 6 node anomaly onsets detected by P2, the worst being node0697 in rack 34 at 41.2%.\n"
        "- Of 77 failures P3 caught 30 and missed 47, with 182 false alarms.\n")

# 3. the observed failure: entirely Chinese meta-commentary for a lang="en" request, describing a
#    file it produced -- no report content at all
F_META = ("我已為您產生完整的交接報告，並已將結果輸出為 shift_report.json 檔案。\n"
          "您可以透過下方的下載卡片取得該檔案。如需其他格式或不同的時間視窗，請再告訴我。")

# 4. a genuine hallucination inside an otherwise well-formed JSON reply
F_HALLUC = json.dumps({
    "executive_summary": "Over the last 24 h the cluster submitted 1501 jobs and 9999 ended, "
                         "of which 1066 COMPLETED and 1374 remain active in the queue right now.",
}, ensure_ascii=False)

# ---------------------------------------------------------------- drive build_report end-to-end
report.assemble_facts = lambda store, t, policies, window_h=6: FACTS

def run(raw, lang="en", length="full"):
    report._CACHE.clear(); report._LLM_COOLDOWN = 0.0
    report.generate_llm = lambda facts, l, ln: (raw, None)
    return report.build_report(None, FakeClock(), POLICIES, window_h=24, lang=lang, length=length)

def show(title, d, n=900):
    print("=" * 100)
    print(f"{title}\n  mode            : {d['mode']}\n  fallback_reason : {d['fallback_reason']}")
    print(f"  numeric_check   : ok={d['numeric_check']['ok']} checked={d['numeric_check']['checked']} "
          f"unverified={d['numeric_check']['unverified']}")
    print(f"  cached          : {d['cached']}")
    print("  --- composed text ---")
    for line in d["text"][:n].split("\n"):
        print("  | " + line)
    if len(d["text"]) > n: print(f"  | ... ({len(d['text'])} chars total)")
    print()

# ============================================================ 1. JSON object -> markdown
d = run(F_JSON)
show("FIXTURE 1 - agent returns a JSON object (the platform-side JSON contract)", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("no braces/quotes/key names leaked into the text",
      "{" not in d["text"] and '"executive_summary"' not in d["text"])
check("keys became markdown headings", "## Executive summary" in d["text"], )
check("known keys are in canonical order",
      d["text"].index("## Executive summary") < d["text"].index("## Risk assessment")
      < d["text"].index("## Recommended actions"))
check("unknown key title-cased and appended, not dropped",
      "## Unexpected extra key" in d["text"]
      and d["text"].index("## Unexpected extra key") > d["text"].index("## Recommended actions"))
check("list value rendered as bullets", "- Inspect node0697 in rack 34." in d["text"])
check("guardrail ran and passed", d["numeric_check"]["checked"] and d["numeric_check"]["ok"])

# ============================================================ 2. plain markdown passes through
d = run(F_MD)
show("FIXTURE 2 - agent returns plain markdown prose", d)
check("mode == llm", d["mode"] == "llm", d["mode"])
check("markdown preserved verbatim (bar trailing whitespace)", d["text"] == F_MD.strip())
check("guardrail ran and passed", d["numeric_check"]["checked"] and d["numeric_check"]["ok"])

# ============================================================ 3. meta-commentary is rejected
d = run(F_META)
show("FIXTURE 3 - agent returns Chinese meta-commentary about a file it produced (lang=en)", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("fallback_reason explains it", bool(d["fallback_reason"]), repr(d["fallback_reason"]))
check("numeric check did NOT run (content rejected first)", d["numeric_check"]["checked"] is False)
check("user sees the deterministic template, not the meta-text",
      "shift_report.json" not in d["text"] and "Shift report" in d["text"])
check("rejected result was NOT cached", d["cached"] is False and not report._CACHE)

# ============================================================ 4. hallucination still caught
d = run(F_HALLUC)
show("FIXTURE 4 - well-formed JSON containing an invented number (9999)", d)
check("mode == template_llm_rejected", d["mode"] == "template_llm_rejected", d["mode"])
check("the invented number is named", d["numeric_check"]["unverified"] == ["9999"],
      str(d["numeric_check"]["unverified"]))
check("fallback_reason names the numeric failure",
      "numeric check failed" in (d["fallback_reason"] or ""), repr(d["fallback_reason"]))
check("rejected result was NOT cached", d["cached"] is False and not report._CACHE)

# ============================================================ 5. brief = one plain paragraph
print("=" * 100)
print("FIXTURE 5 - length='brief' must yield ONE plain-text paragraph (sidebar uses textContent)")
for name, raw in (("JSON reply", F_JSON), ("markdown reply", F_MD)):
    d = run(raw, length="brief")
    txt = d["text"]
    print(f"  {name:15s} mode={d['mode']:22s} -> {txt[:150]}{'...' if len(txt) > 150 else ''}")
    check(f"{name}: mode == llm", d["mode"] == "llm", d["mode"])
    check(f"{name}: single line, no newlines", "\n" not in txt)
    check(f"{name}: no markdown syntax", not any(m in txt for m in ("##", "**", "- ", "{", '":')))
print()

# ============================================================ 6. wrong language both ways
print("=" * 100)
print("FIXTURE 6 - language mismatch")
d = run(F_MD, lang="zh")                      # English prose for a Chinese request
# CONTRACT CHANGE: language is now an ADVISORY, not a rejection. A wrong-language report still has
# a body, so it is returned with a flag rather than discarded (see report.advisory_review).
check("English reply for lang=zh is RETURNED, not discarded", d["mode"] == "llm", d["mode"])
check("   language advisory raised", any(a["code"] == "language_mismatch" for a in d["advisories"]),
      str([a["code"] for a in d["advisories"]]))
zh_ok = ("## 摘要\n過去 24 小時叢集共提交 1501 個任務，結束 1275 個，其中 1066 個順利完成。"
         "目前有 1374 個任務在執行中，使用率約 68.6%。\n\n"
         "## 風險評估\nP2 偵測到 6 次節點異常 onset，其中 node0697（機櫃 34）分數最高，達 41.2%。")
d = run(zh_ok, lang="zh")
check("genuine Chinese report for lang=zh is accepted", d["mode"] == "llm", d["mode"])
print()

# ============================================================ summary
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
