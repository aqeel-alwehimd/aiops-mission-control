"""Find the payload size at which the LaplaceAI narration endpoint starts returning 504.

Realistic content, not filler: every probe is a genuine shift-report prompt built from the real
facts, with the facts payload and the instruction block scaled to hit a target size. Filler would
not test the same thing if the limit is on tokens or on generation length rather than raw bytes.

One attempt per probe, no retries, no backoff, so each probe measures the endpoint and not the
retry wrapper. The ladder is run twice so a sharp boundary can be told from a probabilistic one.
"""
import json, os, sys, time
sys.path.insert(0, "D:/M100/P2/dashboard")
os.chdir("D:/M100/P2/dashboard")
from dotenv import load_dotenv
load_dotenv("D:/M100/P2/dashboard/.env")

import report
from models import Store

RUNS = 2
TS = 1664303261
POL = {"alert_threshold": 0.30, "node_filter_pct": 25}

store = Store()
facts = report.assemble_facts(store, TS, POL, 6)
full = report.trim_facts_for_prompt(facts, "en")

# one attempt, generous per-attempt timeout, so a hang is measured rather than retried away
report.AGENT_PROFILE[report.AGENT_MAIN] = {"attempts": 1, "deadline": 200.0,
                                           "timeout": 200.0, "min_room": 5.0}


def facts_at(scale):
    """A REAL facts payload, pruned or padded to scale. Never invented values -- padding repeats
    genuine example rows, so token mix and JSON shape stay representative."""
    f = json.loads(json.dumps(full, ensure_ascii=False))
    po = f["prediction_outcomes"]
    if scale <= 0.15:                      # smallest: headline counts only
        return {"now_iso": f["now_iso"], "window": f["window"],
                "jobs_window": {k: v for k, v in f["jobs_window"].items()
                                if not k.startswith("cohort_note")},
                "prediction_outcomes": {k: (v["count"] if isinstance(v, dict) and "count" in v else v)
                                        for k, v in po.items() if not k.startswith("cohort_")}}
    if scale <= 0.4:
        for b in ("correct_warnings", "false_alarms", "pending_outcome", "misses"):
            po[b]["examples"] = po[b]["examples"][:1]
        f["high_risk_jobs"] = f["high_risk_jobs"][:1]
        f["high_risk_nodes"] = f["high_risk_nodes"][:1]
        f["node_onsets"]["events"] = f["node_onsets"]["events"][:1]
        return f
    if scale <= 1.0:
        return f
    reps = int(scale)                      # pad with genuine rows to exceed the current size
    for b in ("correct_warnings", "false_alarms", "pending_outcome", "misses"):
        po[b]["examples"] = (po[b]["examples"] * reps)[:4 * reps]
    f["high_risk_jobs"] = (f["high_risk_jobs"] * reps)[:4 * reps]
    f["node_onsets"]["events"] = (f["node_onsets"]["events"] * reps)[:4 * reps]
    return f


def prompt_at(target):
    """A real prompt whose size is near `target` characters."""
    for scale in (0.1, 0.15, 0.4, 1.0, 2, 3, 5, 8, 12):
        p = report._build_prompt(facts_at(scale), "en", "full")
        if len(p) >= target:
            return p
    return p


TARGETS = [400, 800, 1600, 3200, 6400, 9090, 13000, 18000]

print(f"{'target':>7} {'actual':>7} {'run':>4} {'status':>8} {'latency':>9}  note", flush=True)
print("-" * 64, flush=True)
results = []
for run in range(1, RUNS + 1):
    for tgt in TARGETS:
        p = prompt_at(tgt)
        report.clear_cooldowns()
        t0 = time.time()
        text, reason, err = report.invoke_agent(report.AGENT_MAIN, "LAPLACE_INVOKE_URL",
                                                "LAPLACE_BEARER_SECRET", p)
        dt = time.time() - t0
        ok = text is not None
        note = f"{len(text)} chars back" if ok else (reason or "")[:44]
        results.append({"run": run, "target": tgt, "size": len(p), "ok": ok,
                        "latency_s": round(dt, 1), "note": note})
        print(f"{tgt:>7} {len(p):>7} {run:>4} {'200' if ok else 'FAIL':>8} {dt:>8.1f}s  {note}",
              flush=True)
        time.sleep(3)

json.dump(results, open("C:/Users/aqeel/AppData/Local/Temp/claude/D--M100/"
                        "3bcd6330-380e-4c5d-8e24-20d5f07c5a66/scratchpad/ladder.json", "w"),
          indent=2)
print("\nsummary by size:", flush=True)
sizes = sorted({r["size"] for r in results})
for s in sizes:
    rs = [r for r in results if r["size"] == s]
    print(f"  {s:>6} chars: {sum(1 for r in rs if r['ok'])}/{len(rs)} ok  "
          f"latencies {[r['latency_s'] for r in rs]}", flush=True)
