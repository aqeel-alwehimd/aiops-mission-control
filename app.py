"""
app.py -- FastAPI backend for the AIOps Mission Control demo.

Serves real model predictions (precomputed by precompute.py) through a virtual
replay clock. No model inference happens in a request handler; every endpoint just
reads the precomputed store for "the state of the system at the current virtual time".

Run from this directory:
    uvicorn app:app --reload
then open http://127.0.0.1:8000
"""
import os, json
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import Store
from replay import VirtualClock
from report import build_report

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATIC = os.path.join(HERE, "static")
POLICIES_PATH = os.path.join(DATA, "policies.json")
HEROES_PATH = os.path.join(DATA, "hero_examples.json")

store = Store()
clock = VirtualClock(store.meta["window_start_ts"], store.meta["window_end_ts"])
# No manual state at startup: the clock follows the shared, wall-clock-derived "live" position,
# so every worker (and any cold-started free-tier instance) agrees and the first glance is already
# populated mid-window. Play / pause / speed / jump layer an in-memory override on top.

# ---- policies: held in MEMORY, seeded from policies.json at startup; disk is never written ----
def _seed_policies() -> dict:
    p = dict(store.meta.get("default_policies", {}))
    if os.path.exists(POLICIES_PATH):
        try:
            p.update(json.load(open(POLICIES_PATH, encoding="utf-8")))
        except Exception:
            pass
    return p
_DEFAULT_POLICIES = _seed_policies()      # the defaults the /reset endpoint restores
policies = dict(_DEFAULT_POLICIES)        # the live, mutable, in-memory policy state

app = FastAPI(title="AIOps Mission Control -- Marconi100 demo", version="1.0",
              description="Real failure-prediction models (P2 node-anomaly, P3 job-outcome) "
                          "replayed over the Sept-2022 test period through a virtual clock.")


# ============================================================ DATA ENDPOINTS
@app.get("/api/summary")
def api_summary():
    """Dashboard KPIs and current counts at the virtual time."""
    return store.summary(clock.now_ts(), policies)

@app.get("/api/nodes")
def api_nodes(limit: int = Query(80, ge=1, le=1000), sort: str = "risk"):
    """Node table rows at the current virtual time (sort by risk/temp/power/node)."""
    return store.nodes(clock.now_ts(), policies, limit=limit, sort=sort)

@app.get("/api/jobs")
def api_jobs(limit: int = Query(200, ge=1, le=2000),
            state: Optional[str] = None, sort: str = "risk"):
    """Job table rows at the current virtual time (filter ?state=, sort by risk/elapsed/submit)."""
    return store.jobs(clock.now_ts(), policies, limit=limit, state=state, sort=sort)

@app.get("/api/logs")
def api_logs(limit: int = Query(40, ge=1, le=200)):
    """Recent auto-healing / event log lines derived from real events at the virtual time."""
    return store.logs(clock.now_ts(), policies, limit=limit)

@app.get("/api/model_info")
def api_model_info():
    """Model names, metrics, training windows, feature counts, and the explicit list of
    dashboard fields this dataset cannot fill. This is what keeps the demo honest."""
    mi = dict(store.model_info)
    mi["replay_window"] = {"start": store.meta["window_start_iso"], "end": store.meta["window_end_iso"]}
    mi["p2_risk_filter"] = {"indicator": store.meta["p2_power_indicator"],
                            "cutoffs_watts": store.meta["p2_power_cutoffs_watts"],
                            "note": store.meta["p2_filter_note"]}
    return mi


# ============================================================ OPERATIONS REPORT
@app.get("/api/report")
def api_report(window: float = Query(6, ge=0.25, le=48),
               lang: str = "zh", length: str = "brief"):
    """Auto-generated operations report at the current virtual time.

    Facts are computed in Python from the store; the LLM only narrates them (constrained to those
    values and numerically validated); if no ANTHROPIC_API_KEY / SDK, a deterministic template is
    used instead. Returns both the generated `text` and the underlying `facts` JSON.
    Params: window (lookback hours), lang (en|zh), length (brief|full)."""
    lang = "en" if lang == "en" else "zh"
    length = "full" if length == "full" else "brief"
    return build_report(store, clock, policies, window_h=window, lang=lang, length=length)


# ============================================================ DRILL-DOWN (input / output / why)
@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: int):
    """Input feature values, model output, and (global) attribution for one job."""
    d = store.job_detail(job_id, policies)
    if d is None:
        return JSONResponse({"error": "job not found in the test cohort"}, status_code=404)
    return d

@app.get("/api/nodes/{node_id}")
def api_node_detail(node_id: int):
    """Input IPMI features, model output, and per-prediction contributions for a node's
    most-recent 15-minute slot at the current virtual time."""
    d = store.node_detail(node_id, clock.now_ts(), policies)
    if d is None:
        return JSONResponse({"error": "no telemetry slot for this node at the current virtual time"}, status_code=404)
    return d


# ============================================================ CLOCK CONTROL
class ClockCmd(BaseModel):
    action: str                       # play | pause | reset | step | speed | jump | jump_frac
    value: Optional[float] = None

@app.get("/api/clock")
def api_clock_get():
    return clock.state()

@app.post("/api/clock")
def api_clock_post(cmd: ClockCmd):
    a = cmd.action.lower(); v = cmd.value
    if   a == "play":      clock.play()
    elif a == "pause":     clock.pause()
    elif a == "reset":     clock.reset()
    elif a == "step":      clock.step(v if v is not None else 900)     # default nudge = 15 virtual min
    elif a == "speed":     clock.set_speed(v if v is not None else 3600)
    elif a == "jump":      clock.jump(v)
    elif a == "jump_frac": clock.jump_frac(v if v is not None else 0)
    else:                  return JSONResponse({"error": f"unknown action '{a}'"}, status_code=400)
    return clock.state()


# ============================================================ POLICIES
class PolicyUpdate(BaseModel):
    alert_threshold: Optional[float] = None       # P3 job risk threshold (0..1)
    node_filter_pct: Optional[float] = None       # P2 low-power triage strength (1..100)
    temp_isolate_c: Optional[float] = None
    checkpoint_min: Optional[int] = None
    migrate_strategy: Optional[str] = None
    oom_guard_enabled: Optional[bool] = None
    node_fail_guard_enabled: Optional[bool] = None

@app.get("/api/policies")
def api_policies_get():
    return {"policies": policies, "defaults": _DEFAULT_POLICIES,
            "node_filter_cutoffs_watts": store.meta["p2_power_cutoffs_watts"]}

@app.post("/api/policies")
def api_policies_post(upd: PolicyUpdate):
    """Update policy state IN MEMORY only. The read-only deployment never writes to disk;
    changes live for the process lifetime and reset on restart (or via /api/policies/reset)."""
    global policies
    changes = {k: v for k, v in upd.dict().items() if v is not None}
    if "alert_threshold" in changes: changes["alert_threshold"] = max(0.0, min(1.0, float(changes["alert_threshold"])))
    if "node_filter_pct" in changes: changes["node_filter_pct"] = max(1.0, min(100.0, float(changes["node_filter_pct"])))
    policies.update(changes)                   # in-memory only -- no disk write
    return {"policies": policies, "applied": changes}

@app.post("/api/policies/reset")
def api_policies_reset():
    """Restore the seeded defaults (in memory)."""
    global policies
    policies = dict(_DEFAULT_POLICIES)
    return {"policies": policies, "reset": True}


# ============================================================ HEALTH / HEROES
@app.get("/health")
def health():
    """Liveness probe for the platform."""
    return {"status": "ok"}

@app.get("/api/heroes")
def api_heroes():
    """Guided-tour anchors (IDs + virtual timestamps to jump to). Empty list if the file is absent."""
    if os.path.exists(HEROES_PATH):
        try:
            return json.load(open(HEROES_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"examples": []}


# ============================================================ STATIC / ROOT
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC, "index.html"))


# ============================================================ ENTRYPOINT
if __name__ == "__main__":
    # Local / platform run: bind 0.0.0.0 on $PORT (default 8000). Nothing hardcodes 8000 elsewhere.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
