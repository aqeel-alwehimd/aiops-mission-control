"""
report.py -- auto-generated operations report for the dashboard.

Strict split (mandatory):
  1. FACTS are computed in Python from data/demo.sqlite (assemble_facts). Only real values.
  2. The LLM only NARRATES those facts (generate_llm) -- it may invent nothing, and its output
     is numerically validated against the facts; any number not traceable to the facts causes a
     fall-back to the deterministic template.
  3. TEMPLATE fallback (render_template) produces the same report from the identical facts with no
     LLM at all, so the feature works fully offline (no ANTHROPIC_API_KEY / no SDK).

No action is ever taken on infrastructure -- the report only recommends, for a human to approve.
The API key is read from the ANTHROPIC_API_KEY environment variable, never hardcoded, and every
LLM call happens server-side.
"""
import os, re, json, time, threading, datetime

# ---------------- tunables ----------------
NODE_WATCH   = 0.30                                   # in-scope P2 score >= this = "on watch"
MAX_EX       = 4                                      # example IDs per outcome bucket
REPORT_MODEL = os.environ.get("REPORT_MODEL", "claude-haiku-4-5-20251001")
CACHE_TTL    = 120                                    # real seconds an entry stays valid
CACHE_BUCKET = 900                                    # virtual seconds per cache bucket (15 virtual min)
CACHE_MAX    = 256

def _iso(ts): return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z"
def _hm(ts):  return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%m-%d %H:%M")


# ============================================================ 1. FACTS (pure Python)
def assemble_facts(store, t: int, policies: dict, window_h: float = 6) -> dict:
    import pandas as pd
    thr = float(policies["alert_threshold"]); pct = float(policies["node_filter_pct"])
    lo = int(t - window_h * 3600)
    cutoff = store.node_cutoff_watts(pct)
    summ = store.summary(t, policies)

    submitted = store._scalar("SELECT COUNT(*) FROM jobs WHERE submit_ts>? AND submit_ts<=?", (lo, t))
    flagged   = store._scalar("SELECT COUNT(*) FROM jobs WHERE submit_ts>? AND submit_ts<=? AND risk>=?", (lo, t, thr))
    ended = store._df("SELECT job_id,user_id,state,risk,pred_type,end_ts FROM jobs WHERE end_ts>? AND end_ts<=?", (lo, t))
    n = len(ended)
    ef = int((ended.state == "FAILED").sum()) if n else 0
    et = int((ended.state == "TIMEOUT").sum()) if n else 0
    eo = int((ended.state == "OUT_OF_MEMORY").sum()) if n else 0
    ecp = int((ended.state == "COMPLETED").sum()) if n else 0
    failed_all = ended[ended.state != "COMPLETED"] if n else ended

    def exlist(df):
        df = df.sort_values("risk", ascending=False).head(MAX_EX)
        return [{"job_id": int(r.job_id), "user": f"user_{int(r.user_id)}", "state": str(r.state),
                 "risk_pct": round(float(r.risk) * 100, 1)} for r in df.itertuples(index=False)]
    correct = failed_all[failed_all.risk >= thr] if len(failed_all) else failed_all
    misses  = failed_all[failed_all.risk <  thr] if len(failed_all) else failed_all
    falarms = ended[(ended.state == "COMPLETED") & (ended.risk >= thr)] if n else ended
    nfail = len(failed_all)
    catch = round(100 * len(correct) / nfail, 1) if nfail else None

    ons = store._df("SELECT node,rack,ts,kind FROM node_events WHERE ts>? AND ts<=? ORDER BY ts", (lo, t))
    onset_events = [{"node": int(r.node), "rack": int(r.rack), "time": _hm(int(r.ts)), "kind": str(r.kind)}
                    for r in ons.head(8).itertuples(index=False)]

    nd = store._classify_nodes(t, policies)
    high_nodes = []
    if len(nd):
        cand = nd[(nd.scored) & (nd.score >= NODE_WATCH)].sort_values("score", ascending=False).head(6)
        if not len(cand):
            cand = nd[nd.scored].sort_values("score", ascending=False).head(3)
        for r in cand.itertuples(index=False):
            high_nodes.append({"node": int(r.node), "rack": int(r.rack), "risk_pct": round(float(r.score) * 100, 1),
                "state": str(r.state), "onset": bool(r.label == 1),
                "temp_c": (None if pd.isna(r.temp) else round(float(r.temp), 1)),
                "power_w": (None if pd.isna(r.power) else round(float(r.power))),
                "fan_rpm": (None if pd.isna(r.fan) else round(float(r.fan))),
                "psu0_w": (None if pd.isna(r.ps_power) else round(float(r.ps_power)))})

    board = store._job_board(t)
    high_jobs = []
    if len(board):
        act = board[board.active].sort_values("risk", ascending=False).head(6)
        for r in act.itertuples(index=False):
            high_jobs.append({"job_id": int(r.job_id), "user": f"user_{int(r.user_id)}",
                "risk_pct": round(float(r.risk) * 100, 1), "pred_type": str(r.pred_type), "status": str(r.status)})

    return {
      "now_iso": _iso(t),
      "window": {"hours": (int(window_h) if float(window_h).is_integer() else window_h),
                 "start_ts": lo, "end_ts": int(t), "start_iso": _iso(lo), "end_iso": _iso(t)},
      "settings": {"p3_alert_threshold": thr, "p2_triage_pct": (int(pct) if float(pct).is_integer() else pct),
                   "p2_cutoff_watts": (None if cutoff == float("inf") else round(cutoff)),
                   "p2_node_alert_score": store.node_alert},
      "cluster_now": {"active_jobs": summ["active_jobs"], "running": summ["running"], "pending": summ["pending"],
                      "nodes_total": summ["nodes_total"], "nodes_anomalous": summ["nodes_anomalous"],
                      "nodes_at_risk": summ["nodes_at_risk"], "nodes_scored": summ["nodes_scored"],
                      "utilisation_pct": summ["utilisation_pct"]},
      "jobs_window": {"submitted": int(submitted), "flagged": int(flagged), "ended": n,
                      "ended_failed": ef, "ended_timeout": et, "ended_oom": eo, "ended_completed": ecp},
      "prediction_outcomes": {
         "failures_in_window": nfail, "catch_rate_pct": catch,
         "correct_warnings": {"count": len(correct), "examples": exlist(correct)},
         "false_alarms":     {"count": len(falarms), "examples": exlist(falarms)},
         "misses":           {"count": len(misses),  "examples": exlist(misses)},
      },
      "node_onsets": {"count": int(len(ons)), "events": onset_events},
      "high_risk_nodes": high_nodes,
      "high_risk_jobs": high_jobs,
      "watch_counts": {"nodes": len(high_nodes), "jobs": len(high_jobs)},
      "model_note": {
         "p3_threshold": thr, "p2_triage_pct": (int(pct) if float(pct).is_integer() else pct),
         "caveat_en": "OOM is under-predicted (SLURM memory-request columns are null in this dataset), so OOM is rarely the top predicted failure type; this is a replay of one Sept-2022 test slice.",
         "caveat_zh": "OOM 為低估項 (此資料集 SLURM 記憶體請求欄位為空)，故 OOM 鮮少被預測為首要故障型態；此為 2022-09 測試期單一片段的回放。",
      },
    }


# ============================================================ 2. NUMERIC VALIDATION
_NUM = re.compile(r"\d+(?:\.\d+)?")

def _iter_nums(o):
    if isinstance(o, bool): return
    if isinstance(o, (int, float)):
        yield float(o); return
    if isinstance(o, str):
        for m in _NUM.findall(o): yield float(m)
        return
    if isinstance(o, dict):
        for v in o.values(): yield from _iter_nums(v)
    elif isinstance(o, (list, tuple)):
        for v in o: yield from _iter_nums(v)

def allowed_numbers(facts) -> set:
    """Every number a faithful narration may legitimately use: each fact value at 0/1/2 dp, its
    absolute value, and percentage forms for probabilities in [0,1]."""
    S = set()
    for n in _iter_nums(facts):
        for x in (n, abs(n)):
            S.add(round(x)); S.add(round(x, 1)); S.add(round(x, 2))
            if 0.0 <= x <= 1.0:
                S.add(round(x * 100)); S.add(round(x * 100, 1))
    return S

# tech/model tokens that legitimately contain digits but are NOT data (so the numeric check must
# ignore the embedded digit, e.g. "P3", "P2", "V100", "PSU-0", "GPU0", "AC922").
_STRIP = re.compile(r"\b(?:P[0-9]|V100|AC922|GPU[0-9]|PSU-?[0-9]|DCGM|IPMI|MIG|SLURM|M100)\b", re.I)

def unverified_numbers(text: str, allowed: set):
    bad = []
    for m in _NUM.findall(_STRIP.sub(" ", text)):
        x = float(m)
        if not ({round(x), round(x, 1), round(x, 2)} & allowed):
            bad.append(m)
    return sorted(set(bad), key=lambda s: (len(s), s))


# ============================================================ 3. LLM NARRATION (server-side only)
def _llm_system(lang: str, length: str) -> str:
    langname = "Traditional Chinese (繁體中文)" if lang == "zh" else "English"
    shape = ("a single tight paragraph, 2-4 sentences, no headings"
             if length == "brief" else
             "Markdown with these sections in order: Situation, What happened, Current risks, "
             "Recommended actions, Model note")
    return (
        "You are an HPC operations assistant writing a shift report for a supercomputer's AIOps "
        "console. You are given a JSON 'situation report' of FACTS.\n"
        "HARD RULES:\n"
        "- Use ONLY values present in the JSON. Never invent or infer any number, ID, user, node, "
        "rack, score, timestamp, or fact that is not in the JSON.\n"
        "- Do NOT compute new ratios or percentages; only cite numbers that already appear in the JSON.\n"
        "- Do NOT number list items with digits (use bullet points).\n"
        "- Report the model's MISSES honestly; never hide false alarms or missed failures.\n"
        "- Recommend actions for a human to approve; never state or imply an action was taken, and "
        "never simulate touching infrastructure.\n"
        f"- Write in {langname}.\n"
        f"OUTPUT: {shape}. Prioritise what matters operationally (incidents, misses, high-risk items)."
    )

_LLM_COOLDOWN = 0.0                                            # circuit breaker: skip LLM until this real time

def generate_llm(facts: dict, lang: str, length: str):
    """Return narrated text, or None to signal 'use the template' (no key / no SDK / error).
    Fast-fails: no retries, a short timeout, and a cooldown so a broken/absent endpoint never
    hangs the polled report endpoint."""
    global _LLM_COOLDOWN
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if time.time() < _LLM_COOLDOWN:
        return None
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic(max_retries=0, timeout=15.0)   # reads ANTHROPIC_API_KEY from env
        msg = client.messages.create(
            model=REPORT_MODEL, max_tokens=1200, temperature=0.2,
            system=_llm_system(lang, length),
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        return text or None
    except Exception:
        _LLM_COOLDOWN = time.time() + 120                     # stop hammering a broken/slow endpoint for 2 min
        return None                                           # any failure -> template path


# ============================================================ 4. TEMPLATE FALLBACK (deterministic)
def _plural(n, en): return "" if (en and n == 1) else ("" if not en else "s")

def _ids(examples):
    return ", ".join(str(e["job_id"]) for e in examples)

def render_template(facts: dict, lang: str, length: str) -> str:
    en = lang != "zh"
    w = facts["window"]; s = facts["settings"]; jw = facts["jobs_window"]
    po = facts["prediction_outcomes"]; on = facts["node_onsets"]
    nfail = po["failures_in_window"]; corr = po["correct_warnings"]["count"]
    miss = po["misses"]["count"]; fa = po["false_alarms"]["count"]
    hn = facts["high_risk_nodes"]; hj = facts["high_risk_jobs"]; cn = facts["cluster_now"]
    thr = s["p3_alert_threshold"]; tri = s["p2_triage_pct"]
    incident = (on["count"] > 0) or (nfail > 0)

    if length == "brief":
        if en:
            head = f"Last {w['hours']} h: {jw['submitted']} jobs submitted ({jw['flagged']} flagged by P3), {jw['ended']} ended"
            head += f" — {jw['ended_failed']} FAILED, {jw['ended_timeout']} TIMEOUT, {jw['ended_oom']} OOM."
            if nfail:
                head += f" Of {nfail} failure{_plural(nfail,en)}, P3 caught {corr} and missed {miss} ({fa} false alarm{_plural(fa,en)})."
            else:
                head += " No job failures in the window."
            head += f" {on['count']} node anomaly onset{_plural(on['count'],en)}."
            head += f" Watching {len(hn)} node{_plural(len(hn),en)} and {len(hj)} high-risk job{_plural(len(hj),en)}."
            return head
        else:
            head = f"近 {w['hours']} 小時：提交 {jw['submitted']} 個任務 (P3 標記 {jw['flagged']})，結束 {jw['ended']} 個"
            head += f" — {jw['ended_failed']} 失敗、{jw['ended_timeout']} 逾時、{jw['ended_oom']} 記憶體不足。"
            if nfail:
                head += f" {nfail} 次失敗中 P3 命中 {corr}、漏報 {miss}（誤報 {fa}）。"
            else:
                head += " 視窗內無任務失敗。"
            head += f" 節點異常 onset {on['count']} 次。目前關注 {len(hn)} 個節點、{len(hj)} 個高風險任務。"
            return head

    # ---- full markdown ----
    L = []
    if en:
        L.append(f"# Shift report — {w['start_iso']} → {w['end_iso']}\n")
        state = "elevated" if incident else "nominal"
        L.append(f"**Situation.** Over the last {w['hours']} h the cluster state is **{state}**: "
                 f"{cn['active_jobs']} active jobs ({cn['running']} running, {cn['pending']} pending), "
                 f"{cn['nodes_anomalous']} node(s) in a live anomaly window, utilisation ~{cn['utilisation_pct']}% (power proxy).\n")
        L.append("## What happened")
        if on["count"]:
            L.append(f"- **{on['count']} node anomaly onset(s)** detected by P2:")
            for e in on["events"]:
                L.append(f"    - node{e['node']:04d} (rack {e['rack']}) at {e['time']} [{e['kind']}]")
        else:
            L.append("- No node anomaly onsets in the window.")
        L.append(f"- **{jw['ended']} jobs ended**: {jw['ended_failed']} FAILED, {jw['ended_timeout']} TIMEOUT, "
                 f"{jw['ended_oom']} OOM, {jw['ended_completed']} COMPLETED.")
        if nfail:
            L.append(f"- **Warnings:** P3 raised **{corr} correct warning(s)** and had **{fa} false alarm(s)**; "
                     f"it **missed {miss}** failure(s)"
                     + (f" (catch rate {po['catch_rate_pct']}%)." if po['catch_rate_pct'] is not None else "."))
            if corr and po["correct_warnings"]["examples"]:
                L.append(f"    - correctly warned: jobs {_ids(po['correct_warnings']['examples'])}")
            if miss and po["misses"]["examples"]:
                L.append(f"    - missed (failed but under threshold): jobs {_ids(po['misses']['examples'])}")
            if fa and po["false_alarms"]["examples"]:
                L.append(f"    - false alarms (flagged but completed): jobs {_ids(po['false_alarms']['examples'])}")
        else:
            L.append("- **Warnings:** no failures ended in the window to score.")
        L.append("\n## Current risks")
        if hn:
            L.append("- Nodes to watch:")
            for x in hn:
                sens = f"{x['temp_c']}°C / {x['power_w']}W / {x['fan_rpm']}rpm" if x['temp_c'] is not None else f"PSU-0 {x['psu0_w']}W (idle)"
                L.append(f"    - node{x['node']:04d} (rack {x['rack']}) — P2 {x['risk_pct']}% [{x['state']}{', onset' if x['onset'] else ''}] — {sens}")
        else:
            L.append("- No in-scope nodes above the watch level right now.")
        if hj:
            L.append("- Jobs to watch (running/queued):")
            for x in hj:
                L.append(f"    - job {x['job_id']} ({x['user']}) — P3 {x['risk_pct']}% predicted {x['pred_type']} [{x['status']}]")
        L.append("\n## Recommended actions")
        acts = []
        for x in hn:
            if x["onset"] or x["risk_pct"] >= 50:
                acts.append(f"Inspect node{x['node']:04d} (rack {x['rack']}, P2 {x['risk_pct']}%) — suggest checking its power/health.")
        pend = [x for x in hj if x["status"] == "PENDING"]
        for x in pend[:3]:
            acts.append(f"Consider notifying {x['user']} about queued job {x['job_id']} (predicted {x['pred_type']}, {x['risk_pct']}%) before it starts.")
        if miss:
            acts.append(f"Review the {miss} missed failure(s); if this recurs, consider lowering the P3 alert threshold below {thr}.")
        if fa > corr and (fa + corr) > 0:
            acts.append(f"False alarms ({fa}) exceed correct warnings ({corr}); consider raising the threshold above {thr}.")
        if not acts:
            acts.append("No action required beyond routine monitoring.")
        for a in acts:
            L.append(f"- {a} *(recommendation — requires human approval; no action has been taken)*")
        L.append("\n## Model note")
        L.append(f"- Settings in force: P3 alert threshold **{thr}**, P2 triage scoring the riskiest **{tri}%** of node-slots"
                 + (f" (PSU-0 ≤ {s['p2_cutoff_watts']} W)." if s["p2_cutoff_watts"] else "."))
        L.append(f"- Caveat: {facts['model_note']['caveat_en']}")
        return "\n".join(L)
    else:
        L.append(f"# 交接報告 — {w['start_iso']} → {w['end_iso']}\n")
        state = "偏高" if incident else "正常"
        L.append(f"**現況。** 近 {w['hours']} 小時叢集狀態為 **{state}**："
                 f"{cn['active_jobs']} 個活躍任務（{cn['running']} 執行中、{cn['pending']} 等待中），"
                 f"{cn['nodes_anomalous']} 個節點處於即時異常視窗，使用率約 {cn['utilisation_pct']}%（功耗 proxy）。\n")
        L.append("## 發生了什麼")
        if on["count"]:
            L.append(f"- P2 偵測到 **{on['count']} 次節點異常 onset**：")
            for e in on["events"]:
                L.append(f"    - node{e['node']:04d}（機櫃 {e['rack']}）於 {e['time']} [{e['kind']}]")
        else:
            L.append("- 視窗內無節點異常 onset。")
        L.append(f"- **{jw['ended']} 個任務結束**：{jw['ended_failed']} 失敗、{jw['ended_timeout']} 逾時、"
                 f"{jw['ended_oom']} 記憶體不足、{jw['ended_completed']} 完成。")
        if nfail:
            L.append(f"- **告警成效：** P3 發出 **{corr} 次正確告警**、**{fa} 次誤報**，並 **漏報 {miss}** 次失敗"
                     + (f"（命中率 {po['catch_rate_pct']}%）。" if po['catch_rate_pct'] is not None else "。"))
            if corr and po["correct_warnings"]["examples"]:
                L.append(f"    - 正確告警：任務 {_ids(po['correct_warnings']['examples'])}")
            if miss and po["misses"]["examples"]:
                L.append(f"    - 漏報（失敗但低於門檻）：任務 {_ids(po['misses']['examples'])}")
            if fa and po["false_alarms"]["examples"]:
                L.append(f"    - 誤報（標記但完成）：任務 {_ids(po['false_alarms']['examples'])}")
        else:
            L.append("- **告警成效：** 視窗內無結束的失敗任務可評分。")
        L.append("\n## 當前風險")
        if hn:
            L.append("- 需關注的節點：")
            for x in hn:
                sens = f"{x['temp_c']}°C / {x['power_w']}W / {x['fan_rpm']}rpm" if x['temp_c'] is not None else f"PSU-0 {x['psu0_w']}W（閒置）"
                L.append(f"    - node{x['node']:04d}（機櫃 {x['rack']}）— P2 {x['risk_pct']}% [{x['state']}{'，onset' if x['onset'] else ''}] — {sens}")
        else:
            L.append("- 目前無在分流範圍內且超過關注水準的節點。")
        if hj:
            L.append("- 需關注的任務（執行/排隊中）：")
            for x in hj:
                L.append(f"    - 任務 {x['job_id']}（{x['user']}）— P3 {x['risk_pct']}% 預測 {x['pred_type']} [{x['status']}]")
        L.append("\n## 建議行動")
        acts = []
        for x in hn:
            if x["onset"] or x["risk_pct"] >= 50:
                acts.append(f"檢查 node{x['node']:04d}（機櫃 {x['rack']}，P2 {x['risk_pct']}%）— 建議查看其功耗/健康狀態。")
        pend = [x for x in hj if x["status"] == "PENDING"]
        for x in pend[:3]:
            acts.append(f"考慮通知 {x['user']}：排隊中任務 {x['job_id']}（預測 {x['pred_type']}，{x['risk_pct']}%）於啟動前先行處理。")
        if miss:
            acts.append(f"檢視 {miss} 次漏報失敗；若持續發生，可考慮將 P3 告警門檻調降至 {thr} 以下。")
        if fa > corr and (fa + corr) > 0:
            acts.append(f"誤報（{fa}）多於正確告警（{corr}）；可考慮將門檻調高於 {thr}。")
        if not acts:
            acts.append("除例行監控外，暫無需採取行動。")
        for a in acts:
            L.append(f"- {a} *(建議事項 — 需人工核准；尚未執行任何動作)*")
        L.append("\n## 模型說明")
        L.append(f"- 生效設定：P3 告警門檻 **{thr}**，P2 分流只評分最可疑的 **{tri}%** 節點槽位"
                 + (f"（PSU-0 ≤ {s['p2_cutoff_watts']} W）。" if s["p2_cutoff_watts"] else "。"))
        L.append(f"- 注意：{facts['model_note']['caveat_zh']}")
        return "\n".join(L)


# ============================================================ 5. BUILD (facts + narrate + cache)
_CACHE = {}
_LOCK = threading.Lock()

def _cache_get(key):
    with _LOCK:
        v = _CACHE.get(key)
        if v and (time.time() - v[1]) < CACHE_TTL:
            return v[0]
        if v:
            _CACHE.pop(key, None)
    return None

def _cache_put(key, out):
    with _LOCK:
        _CACHE[key] = (out, time.time())
        if len(_CACHE) > CACHE_MAX:
            for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][1])[: len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(k, None)

def build_report(store, clock, policies, window_h=6, lang="zh", length="brief") -> dict:
    t = clock.now_ts()
    key = (t // CACHE_BUCKET, round(float(window_h), 2), lang, length,
           round(float(policies["alert_threshold"]), 3), int(float(policies["node_filter_pct"])))
    cached = _cache_get(key)
    if cached:
        return {**cached, "cached": True}

    facts = assemble_facts(store, t, policies, window_h)
    allowed = allowed_numbers(facts)
    llm = generate_llm(facts, lang, length)
    unver = []
    if llm is not None:
        unver = unverified_numbers(llm, allowed)
        if not unver:
            text, mode = llm, "llm"
        else:
            text, mode = render_template(facts, lang, length), "template_llm_rejected"
    else:
        text, mode = render_template(facts, lang, length), "template"

    out = {"text": text, "mode": mode, "length": length, "lang": lang, "window_h": window_h,
           "generated_iso": facts["now_iso"], "virtual_ts": t,
           "numeric_check": {"ok": (mode != "template_llm_rejected"), "unverified": unver},
           "facts": facts, "cached": False}
    _cache_put(key, out)
    return out
