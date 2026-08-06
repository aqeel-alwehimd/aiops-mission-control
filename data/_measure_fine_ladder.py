"""Fine ladder at the low end, with the TRIMMED prompt builder, to find where success becomes
reliable rather than just where it stops.

The coarse ladder could not go below 5,986 characters because the untrimmed builder had that as its
floor. Now that the scaffolding is smaller the same real prompt can be produced at ~3,200 upwards, so
the interesting region is finally reachable. REPS per size, one attempt each, no retries.
"""
import json, os, sys, time
sys.path.insert(0, "D:/M100/P2/dashboard")
os.chdir("D:/M100/P2/dashboard")
from dotenv import load_dotenv
load_dotenv("D:/M100/P2/dashboard/.env")

import report
from models import Store

REPS = 4
TS, POL = 1664216861, {"alert_threshold": 0.30, "node_filter_pct": 25}
store = Store()
facts = report.assemble_facts(store, TS, POL, 6)
full = report.trim_facts_for_prompt(facts, "en")

report.AGENT_PROFILE[report.AGENT_MAIN] = {"attempts": 1, "deadline": 200.0,
                                           "timeout": 200.0, "min_room": 5.0}


def shrink(level):
    """Real facts, progressively pruned. Never invented values."""
    f = json.loads(json.dumps(full, ensure_ascii=False))
    po = f["prediction_outcomes"]
    if level >= 1:
        for b in ("correct_warnings", "false_alarms", "pending_outcome", "misses"):
            po[b]["examples"] = po[b]["examples"][:1]
    if level >= 2:
        f["high_risk_jobs"] = f["high_risk_jobs"][:2]
        f["node_onsets"]["events"] = f["node_onsets"]["events"][:2]
    if level >= 3:
        for b in ("correct_warnings", "false_alarms", "pending_outcome", "misses"):
            po[b]["examples"] = []
        f["high_risk_jobs"] = f["high_risk_jobs"][:1]
        f["high_risk_nodes"] = f["high_risk_nodes"][:1]
    if level >= 4:
        f.pop("high_risk_jobs_concentration", None)
        f["node_onsets"]["events"] = []
        f.get("model_note", {}).pop("caveat_en", None)
    return f


variants = []
for lvl in (4, 3, 2, 1, 0):
    p = report._build_prompt(shrink(lvl), "en", "full")
    variants.append((len(p), p))
variants.sort()

print(f"{'size':>7} {'rep':>4} {'status':>7} {'latency':>9}  out", flush=True)
print("-" * 52, flush=True)
rows = []
for size, p in variants:
    for rep in range(1, REPS + 1):
        report.clear_cooldowns()
        t0 = time.time()
        text, reason, err = report.invoke_agent(report.AGENT_MAIN, "LAPLACE_INVOKE_URL",
                                                "LAPLACE_BEARER_SECRET", p)
        dt = time.time() - t0
        ok = text is not None
        rows.append({"size": size, "rep": rep, "ok": ok, "latency_s": round(dt, 1),
                     "out": len(text) if ok else 0})
        print(f"{size:>7} {rep:>4} {'200' if ok else 'FAIL':>7} {dt:>8.1f}s  "
              f"{len(text) if ok else reason[:30]}", flush=True)
        time.sleep(2)

print("\nsuccess rate by prompt size:", flush=True)
for size in sorted({r["size"] for r in rows}):
    rs = [r for r in rows if r["size"] == size]
    ok = [r for r in rs if r["ok"]]
    lat_ok = [r["latency_s"] for r in ok]
    print(f"  {size:>6} chars: {len(ok)}/{len(rs)}  "
          f"ok-latency {sorted(lat_ok) if lat_ok else '-'}  "
          f"fail-latency {sorted(r['latency_s'] for r in rs if not r['ok'])}", flush=True)
json.dump(rows, open("C:/Users/aqeel/AppData/Local/Temp/claude/D--M100/"
                     "3bcd6330-380e-4c5d-8e24-20d5f07c5a66/scratchpad/fine_ladder.json", "w"),
          indent=2)
