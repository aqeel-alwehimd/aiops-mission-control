"""
verify_demo_path.py -- the prepared demo path works end to end, and makes NO outbound call.

Run:  python verify_demo_path.py

WHAT THIS PROVES, and why each part is needed.

A demo report is only useful if it can actually be reached and is actually served from disk. Three
things have to hold together, and each has failed independently during development:

  1. THE CLOCK CAN BE PINNED. The live clock crosses one 900-virtual-second cache bucket in 0.25
     REAL seconds at the default 3600x, so a prepared moment is unreachable by scrubbing and `jump`
     alone slides straight back out of the bucket. `goto` must land exactly and hold.
  2. THE KEY MATCHES. prewarm writes under report.cache_key(...); the server computes its own from
     the pinned clock. If those disagree by so much as a rounded window the entry is dead weight.
  3. NOTHING GOES OUT. The whole point is that the demo does not depend on the endpoint. This runs
     with the agent credentials REMOVED from the environment: if any code path tried to call out it
     would fall back to a template, so `mode == "llm"` is itself the proof that the answer came from
     disk. The outbound call log is asserted empty as well, belt and braces.

MOCK MODE (`--mock`). The clock/cache/no-outbound-call machinery is independent of whether any real
report has been generated, and the LaplaceAI endpoint is not always up. `--mock` therefore builds a
throwaway cache AT A SCRATCH PATH from a stubbed narrator, exercises the identical serving path, and
deletes it. It proves the plumbing and proves nothing about narrative quality -- a mock entry carries
mode == "llm" because it went through the real gate, so it must NEVER be written to the committed
data/report_cache.json, and it is not.

Exits non-zero on failure. Starts no server -- the app is driven in-process through TestClient.
"""
import argparse, io, json, os, statistics, sys, tempfile, time, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

_ap = argparse.ArgumentParser()
_ap.add_argument("--mock", action="store_true",
                 help="build a throwaway cache at a scratch path and verify the serving path "
                      "without the agent; proves the plumbing, not the prose")
ARGS = _ap.parse_args()

# credentials are stripped BEFORE app import, so nothing can make an outbound call in this process
for k in ("LAPLACE_INVOKE_URL", "LAPLACE_BEARER_SECRET",
          "LAPLACE_AUDITOR_INVOKE_URL", "LAPLACE_AUDITOR_BEARER_SECRET"):
    os.environ.pop(k, None)
os.environ["LAPLACE_DEBUG"] = "1"          # record any outbound call that somehow happens
os.environ["REPORT_AUDIT"] = "0"

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

import report                                    # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402
import app as appmod                             # noqa: E402

# app.py calls load_dotenv() at import, which puts the credentials back. Strip them again and prove
# it, otherwise "no outbound call" would only be true by accident of configuration.
for k in ("LAPLACE_INVOKE_URL", "LAPLACE_BEARER_SECRET",
          "LAPLACE_AUDITOR_INVOKE_URL", "LAPLACE_AUDITOR_BEARER_SECRET"):
    os.environ.pop(k, None)

client = TestClient(appmod.app)
MOMENTS = os.path.join(HERE, "data", "demo_moments.json")
DEMO_TS = [1664303261, 1664296615, 1664223507, 1664216861]

if ARGS.mock:
    # Redirect the cache to a scratch file BEFORE anything reads it, so the committed artefact is
    # untouchable for the rest of this process, and build entries from a stubbed narrator.
    import prewarm_reports as pw
    _scratch = os.path.join(tempfile.mkdtemp(prefix="demopath_"), "report_cache.json")
    report.PREWARM_PATH = _scratch
    report._DISK_CACHE["sig"] = None
    MOMENTS = os.path.join(os.path.dirname(_scratch), "demo_moments.json")
    pw.MOMENTS = MOMENTS
    print(f"MOCK: scratch cache at {_scratch}\n")

    def _stub(facts, lang, length):
        """A faithful narration built from the facts, so it passes the real hard gate and the real
        content gate exactly as a live reply does -- including being written in the language that
        was asked for, and selecting charts, since both are things the gate checks."""
        po, jw = facts["prediction_outcomes"], facts["jobs_window"]
        charts = [{"chart_id": "prediction_outcomes", "caption": ""},
                  {"chart_id": "job_outcome_mix", "caption": ""},
                  {"chart_id": "top_flagged_jobs", "caption": ""},
                  {"chart_id": "node_risk_watch", "caption": ""}]
        if lang == "zh":
            return json.dumps({
                "executive_summary":
                    f"本視窗共提交 {jw['submitted']} 個任務，結束 {jw['ended_in_window']} 個，"
                    f"其中 {jw['ended_in_window_completed']} 個順利完成。",
                "risk_assessment":
                    f"提交時被標記的任務共 {po['flagged_total']} 個："
                    f"{po['correct_warnings']['count']} 個確認失敗、"
                    f"{po['false_alarms']['count']} 個為誤判、"
                    f"{po['pending_outcome']['count']} 個尚未結束。另有 "
                    f"{po['misses']['count']} 個失敗從未被標記。",
                "recommended_actions": ["交班前請複核觀察名單。"],
                "chart_configs": charts,
            }, ensure_ascii=False), None
        return json.dumps({
            "executive_summary":
                f"{jw['submitted']} jobs were submitted in this window and {jw['ended_in_window']} "
                f"ended, of which {jw['ended_in_window_completed']} completed.",
            "risk_assessment":
                f"P3 flagged {po['flagged_total']} jobs at submission: "
                f"{po['correct_warnings']['count']} were confirmed failures, "
                f"{po['false_alarms']['count']} were false alarms and "
                f"{po['pending_outcome']['count']} have not finished. Separately, "
                f"{po['misses']['count']} failures were never flagged.",
            "recommended_actions": ["Review the watch list before handover."],
            "chart_configs": charts,
        }, ensure_ascii=False), None
    report.generate_llm = _stub
    with contextlib.redirect_stdout(io.StringIO()):
        pw.prepare_demo(appmod.store, appmod.policies, window_h=6, stamps=DEMO_TS, attempts=2)
    # prewarm_reports calls load_dotenv() at import, which puts the credentials back; strip again so
    # the "nothing goes out" assertion below is real rather than incidental
    for k in ("LAPLACE_INVOKE_URL", "LAPLACE_BEARER_SECRET",
              "LAPLACE_AUDITOR_INVOKE_URL", "LAPLACE_AUDITOR_BEARER_SECRET"):
        os.environ.pop(k, None)

print("=" * 100)
print("1. the committed artefacts" if not ARGS.mock else "1. the scratch artefacts (MOCK)")
print("=" * 100)
disk = report._disk_load()
size_kb = (os.path.getsize(report.PREWARM_PATH) / 1000
           if os.path.exists(report.PREWARM_PATH) else 0)
print(f"  {report.PREWARM_PATH}")
print(f"  {len(disk)} entries, {size_kb:.0f} KB")
if not disk and not ARGS.mock:
    print("\n  NOTHING IS PREPARED. No demo report has been cached, so sections 2-3 have nothing to")
    print("  serve. This is the honest state of a deployment that has not had reports generated for")
    print("  it -- run `python prewarm_reports.py --demo`, which needs the agent endpoint. To verify")
    print("  the clock/cache machinery itself without the endpoint, run this script with --mock.\n")
for k in sorted(disk):
    e = disk[k]
    print(f"    {k:<34} mode={e['mode']:<4} charts={len(e.get('charts') or []):<2} "
          f"chars={len(e['text'])}")
check("the prewarm cache exists on disk", bool(disk))
check("every entry on disk is an LLM report, never a template",
      all(e["mode"] == "llm" for e in disk.values()),
      str([e["mode"] for e in disk.values()]))
check("the demo-moments manifest exists", os.path.exists(MOMENTS))
moments = json.load(open(MOMENTS, encoding="utf-8"))["moments"] if os.path.exists(MOMENTS) else []
check("it names four moments", len(moments) == 4, str(len(moments)))
check("each moment has BOTH languages prepared",
      all(sorted(m["languages"]) == ["en", "zh"] for m in moments),
      str([(m["virtual_ts"], m["languages"]) for m in moments]))
check("the credentials really are absent from this process",
      not os.environ.get("LAPLACE_INVOKE_URL") and not os.environ.get("LAPLACE_BEARER_SECRET"))

print()
print("=" * 100)
print("2. goto pins the clock on the exact virtual second, and holds")
print("=" * 100)
for m in moments:
    ts = m["virtual_ts"]
    st = client.post("/api/clock", json={"action": "goto", "value": ts}).json()
    time.sleep(0.35)                       # >1 bucket of live time at 3600x (0.25s per bucket)
    again = client.get("/api/clock").json()
    print(f"  goto {ts} -> virtual_ts={st['virtual_ts']} paused={st['paused']} "
          f"live={st['live']}; after 0.35s still {again['virtual_ts']}")
    check(f"{ts}: lands on the exact second", st["virtual_ts"] == ts, str(st["virtual_ts"]))
    check(f"{ts}: and is paused, so it does not slide out of the cache bucket", st["paused"] is True)
    check(f"{ts}: still there a moment later", again["virtual_ts"] == ts, str(again["virtual_ts"]))
    check(f"{ts}: the key the server computes matches the one prewarm wrote",
          report._key_str(report.cache_key(ts, 6.0, "en", "full", appmod.policies)) in disk,
          report._key_str(report.cache_key(ts, 6.0, "en", "full", appmod.policies)))

print()
print("=" * 100)
print("3. a request at each demo moment is served from cache, with no outbound call")
print("=" * 100)
report.clear_call_log()
report._CACHE.clear()                      # force the DISK path, not a warm in-process hit
lat = []
for m in moments:
    ts = m["virtual_ts"]
    client.post("/api/clock", json={"action": "goto", "value": ts})
    for lang in ("en", "zh"):
        t0 = time.perf_counter()
        r = client.get(f"/api/report?length=full&window=6&lang={lang}")
        dt = (time.perf_counter() - t0) * 1000
        d = r.json()
        lat.append(dt)
        print(f"  {ts} {lang}  mode={d['mode']:<4} cached={d.get('cached')} "
              f"prewarmed={d.get('prewarmed')} charts={len(d['charts'])} {dt:7.1f} ms")
        check(f"{ts}/{lang}: served as an LLM report, not a template", d["mode"] == "llm", d["mode"])
        check(f"{ts}/{lang}: flagged as cached", d.get("cached") is True)
        check(f"{ts}/{lang}: and specifically as a prewarmed DISK entry",
              d.get("prewarmed") is True)
        check(f"{ts}/{lang}: the full chart set rendered", len(d["charts"]) >= 7,
              str(len(d["charts"])))
        check(f"{ts}/{lang}: no numeric-gate failure travelled with it",
              not d["numeric_check"]["unverified"], str(d["numeric_check"]["unverified"]))

calls = report.call_log(200)
print()
print(f"  outbound agent calls during the whole of section 3: {len(calls)}")
for c in calls:
    print(f"    {c}")
check("ZERO outbound agent calls were made", len(calls) == 0, str(len(calls)))
if lat:
    print(f"  served latency: median {statistics.median(lat):.1f} ms, "
          f"min {min(lat):.1f} ms, max {max(lat):.1f} ms")
    check("served in well under a second", max(lat) < 1000, f"max {max(lat):.0f} ms")
else:
    print("  no latency to report: nothing was prepared to serve")

print()
print("=" * 100)
print("4. the clock still behaves normally afterwards")
print("=" * 100)
st = client.post("/api/clock", json={"action": "reset"}).json()
check("reset returns the clock to the shared wall-clock position", st["live"] is True)
check("   and it is running again", st["paused"] is False)
a = client.get("/api/clock").json()["virtual_ts"]
time.sleep(0.6)
b = client.get("/api/clock").json()["virtual_ts"]
print(f"  live clock advanced {b - a} virtual seconds in 0.6 real seconds")
check("the live clock advances again once the pin is dropped", b != a, f"{a} -> {b}")
bad = client.post("/api/clock", json={"action": "goto"})
check("goto without a timestamp is rejected rather than silently doing something",
      bad.status_code == 400, str(bad.status_code))

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
