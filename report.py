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
import os, re, json, time, random, threading, datetime, requests

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

# ---- digit-grouping normalisation -------------------------------------------------------------
# The agent faithfully renders the fact value 1501 as "1,501". _NUM has no notion of thousands
# separators, so it used to split that into "1" and "501": the "1" matched some fact by coincidence
# and the "501" matched nothing, producing a bogus rejection
# (observed: unverified ["066","275","374","501"] from "1,066"/"1,275"/"1,374"/"1,501").
#
# _GROUPED_* matches only a COMPLETE canonical grouped number -- 1-3 digits, then one or more groups
# of exactly 3 digits, optionally a decimal tail -- and only when it is not glued to another digit
# or to a decimal point. That shape requirement is what keeps the following untouched:
#   "38.8"            decimal, no separator
#   "node0697"        4 undelimited digits
#   "12 jobs, 3 fail" sentence comma (not between digits)
#   "1234 567"        two separate numbers ("1234" is 4 digits, so no valid group start)
# NOT handled, deliberately: European-style "1.501" meaning 1501. It is genuinely ambiguous with
# the decimal 1.501, and silently reinterpreting it could mask a real hallucination.
# Separators are split by how ambiguous they are:
#   HARD -- a comma or a narrow/thin/no-break space between digits is only ever digit grouping in
#           the locales this console runs in, so it is collapsed unconditionally.
#   SOFT -- a plain space groups digits in some locales but also just separates two numbers
#           ("rack 34 826 nodes"), so it gets the two-reading treatment in unverified_numbers.
_GSEP_HARD = ",\u00A0\u2009\u202F\u2007"
_GSEP_ALL  = _GSEP_HARD + " "

def _mk_group_res(seps):
    return (re.compile(r"(?<![\d.])(\d{1,3}(?:[" + seps + r"]\d{3})+(?:\.\d+)?)(?!\d)"),
            re.compile(r"[" + seps + r"]"))
_GROUPED_HARD, _DEG_HARD = _mk_group_res(_GSEP_HARD)
_GROUPED_ALL,  _DEG_ALL  = _mk_group_res(_GSEP_ALL)

def _degroup(text: str, soft: bool = False) -> str:
    """'1,501' -> '1501'; with soft=True also '1 501' -> '1501'.

    See _GROUPED_* for exactly what is and is not touched.
    """
    g, d = (_GROUPED_ALL, _DEG_ALL) if soft else (_GROUPED_HARD, _DEG_HARD)
    return g.sub(lambda m: d.sub("", m.group(1)), text)

def _iter_nums(o):
    if isinstance(o, bool): return
    if isinstance(o, (int, float)):
        yield float(o); return
    if isinstance(o, str):
        # both readings of a fact string; this only ever ADDS legitimate fact-derived values
        for t in (o, _degroup(o)):
            for m in _NUM.findall(t): yield float(m)
        return    # (fact strings are generated by this module, so soft grouping never applies)
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
# Boundaries are "not adjacent to a letter or digit" rather than \b, because \b does NOT match
# before an underscore ('_' is a word character) -- so "P2 detected" was exempt while
# "p2_node_alert_score" was not, and the extractor pulled a bare 2 out of the identifier.
_STRIP = re.compile(r"(?<![A-Za-z0-9])(?:P[0-9]|V100|AC922|GPU[0-9]|PSU-?[0-9]|DCGM|IPMI|MIG"
                    r"|SLURM|M100)(?![A-Za-z0-9])", re.I)

# ---- schema identifiers are field references, not numeric claims --------------------------------
# The agent sometimes names metrics by their raw JSON key ("the p2_node_alert_score sits above the
# p3_alert_threshold"). _NUM then pulls the digit out of the identifier: p2_ -> 2, p3_ -> 3. Whether
# that rejected depended on whether the digit happened to also be a fact value, which is why this
# failed intermittently. Two defences, applied in the VALIDATOR ONLY (never to the displayed text):
#   1. dynamic  -- every key anywhere in the facts tree that contains a digit. Durable by
#                  construction: add a field to assemble_facts and it is exempt automatically.
#   2. static   -- any snake_case token. The safety net for keys the agent paraphrases or
#                  mis-spells. snake_case does not occur in ordinary report prose.
# Neither weakens the gate: a digit inside an identifier is not an assertion about the data.
_SNAKE = re.compile(r"\w*_\w+")

def facts_key_tokens(facts) -> set:
    """Every key in the facts tree, at any depth, whose name contains a digit."""
    out = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(c.isdigit() for c in str(k)):
                    out.add(str(k))
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk(facts)
    return out

def schema_strip_re(facts):
    """A regex removing the facts' own digit-bearing key names, longest first. None if there are none."""
    toks = sorted(facts_key_tokens(facts), key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in toks), re.I) if toks else None

def _scan(text: str, allowed: set):
    """Every number in `text` that cannot be traced to a fact value, at 0/1/2 dp."""
    bad = []
    for m in _NUM.findall(text):
        x = float(m)
        if not ({round(x), round(x, 1), round(x, 2)} & allowed):
            bad.append(m)
    return sorted(set(bad), key=lambda s: (len(s), s))

def unverified_numbers(text: str, allowed: set, schema=None):
    """Numbers in the narration that do not trace to the facts. Empty list == narration accepted.

    `schema` is the optional schema_strip_re(facts) pattern; identifier tokens are removed before
    extraction so a digit inside a field NAME is never mistaken for a claim about the data.

    The rule is unchanged and unweakened: every number must trace to a fact value. What changed is
    that a grouped number is now recognised as the fact it is.

    Commas and narrow spaces are collapsed unconditionally, so "1,501" is read ONLY as 1501 -- if
    1501 is not a fact this still rejects, even when 1 and 501 happen to be facts (the old
    literal-only scan would have let that through, so this direction is strictly tighter).

    The plain space is the one genuinely ambiguous separator, so both of its readings are tried and
    the narration is accepted if EITHER is fully traceable:
      1. as grouping   -- "1 501 jobs" reads as the fact 1501
      2. as a separator -- "rack 34 826 nodes" reads as the two facts 34 and 826
    That way neither interpretation of a space can manufacture a false rejection, while a genuine
    hallucination -- absent from the facts under every reading -- is still caught.
    """
    scrubbed = text if schema is None else schema.sub(" ", text)   # the facts' own field names
    scrubbed = _SNAKE.sub(" ", scrubbed)             # any remaining snake_case identifier
    scrubbed = _STRIP.sub(" ", scrubbed)             # drop P3 / V100 / PSU-0 ... before any digits
    hard = _degroup(scrubbed)                        # commas/nbsp: collapsed unconditionally
    readings = (_scan(_degroup(hard, soft=True), allowed),   # plain space read as grouping
                _scan(hard, allowed))                        # plain space read as a separator
    for r in readings:
        if not r:
            return []
    return min(readings, key=len)                    # both dirty: report the tighter reading


# ====================================================== 2b. NARRATION POST-PROCESSING
# The LaplaceAI agent is configured platform-side with a JSON contract, so it returns an object like
#   {"executive_summary": "...", "risk_assessment": "...", "recommended_actions": "..."}
# rather than markdown. Feeding that straight to the frontend's mdToHtml() rendered the braces, quotes
# and key names literally. Rather than fight the agent's configuration, we parse the object here and
# compose the markdown in Python, so presentation is ours to control. Plain-markdown replies still
# work unchanged -- both shapes are supported.

# canonical order for the sections we expect; anything unrecognised is appended, never dropped
_SECTION_ORDER = ["executive_summary", "summary", "situation", "what_happened", "events",
                  "risk_assessment", "current_risks", "risks", "recommended_actions", "actions",
                  "recommendations", "model_note", "caveats", "notes"]
_SECTION_TITLES = {                      # (English, 繁體中文)
    "executive_summary":   ("Executive summary",   "摘要"),
    "summary":             ("Summary",             "摘要"),
    "situation":           ("Situation",           "現況"),
    "what_happened":       ("What happened",       "發生了什麼"),
    "events":              ("Events",              "事件"),
    "risk_assessment":     ("Risk assessment",     "風險評估"),
    "current_risks":       ("Current risks",       "當前風險"),
    "risks":               ("Risks",               "風險"),
    "recommended_actions": ("Recommended actions", "建議行動"),
    "actions":             ("Actions",             "行動"),
    "recommendations":     ("Recommendations",     "建議"),
    "model_note":          ("Model note",          "模型說明"),
    "caveats":             ("Caveats",             "注意事項"),
    "notes":               ("Notes",               "備註"),
}
_SUMMARY_KEYS = ("executive_summary", "summary", "situation", "overview", "brief")
# Backstop against a near-empty reply only. Deliberately low: a "brief" IS one short paragraph, and
# Chinese is dense, so a real report can be short. The specific signals below do the actual work --
# the length floor must never be the thing that rejects a genuine report.
MIN_REPORT_CHARS = {"brief": 40, "full": 80}

def _pretty_key(key: str, en: bool) -> str:
    """'risk_assessment' -> 'Risk assessment'. Unknown keys are title-cased, never dropped."""
    t = _SECTION_TITLES.get(key.strip().lower())
    if t:
        return t[0] if en else t[1]
    words = re.sub(r"[_\-]+", " ", str(key)).strip()
    return (words[:1].upper() + words[1:]) if words else str(key)

def _spec_to_line(d: dict) -> str:
    """One dict entry as a readable line. Without this a list of dicts went through str(x) and a raw
    Python repr -- {'title': ..., 'type': ...} -- was emitted into the report body."""
    title = str(d.get("title") or d.get("name") or "").strip()
    typ   = str(d.get("type") or "").strip()
    desc  = str(d.get("description") or d.get("desc") or "").strip()
    head  = " ".join(p for p in (f"**{title}**" if title else "", f"({typ})" if typ else "") if p)
    line  = (head + (f" — {desc}" if desc else "")).strip()
    return line or ", ".join(f"{_pretty_key(k, True)}: {str(x).strip()}" for k, x in d.items())

def _value_to_md(v) -> str:
    """A section body: string as prose, list as bullets, anything else stringified."""
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {_spec_to_line(x) if isinstance(x, dict) else str(x).strip()}"
                         for x in v if (x if isinstance(x, dict) else str(x).strip()))
    if isinstance(v, dict):
        return "\n".join(f"- **{_pretty_key(k, True)}:** {str(x).strip()}" for k, x in v.items())
    return str(v).strip()

# ---- which parts of the reply are CLAIMS ABOUT THE DATA (and so must be numerically validated) ---
# chart_configs and friends are rendering instructions, not assertions: a title, a chart type and a
# mapping description legitimately carry numbers that are not fact values -- axis counts, top-N,
# window sizes ("Plot the top 2 highest-risk nodes", "the 4 SLURM end states"). The frontend draws
# charts from the database and never from anything the model says, so a number inside a chart spec
# cannot become a false claim on screen. Validating them is a false positive by construction, and it
# would trip on every future chart spec. This is a narrow, named exemption -- NOT an exemption for
# prose: any other unrecognised key is still validated in full.
_UNVALIDATED_KEYS = {"chart_configs", "chart_config", "charts", "chart_spec", "chart_specs",
                     "visualisations", "visualizations", "figures"}

def _claim_texts(obj: dict):
    """[(field_path, text)] the agent ASSERTED about the data. Field paths name the offender so a
    rejection can be diagnosed at a glance instead of by inference."""
    out = []
    for k, v in obj.items():
        if str(k).strip().lower() in _UNVALIDATED_KEYS:
            continue
        if isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    for kk, vv in x.items():
                        out.append((f"{k}[{i}].{kk}", str(vv)))
                else:
                    out.append((f"{k}[{i}]", str(x)))
        elif isinstance(v, dict):
            for kk, vv in v.items():
                out.append((f"{k}.{kk}", str(vv)))
        else:
            out.append((str(k), str(v)))
    return out

def _flatten_to_paragraph(s: str) -> str:
    """One plain-text paragraph: no markdown syntax, no newlines. The sidebar box uses textContent,
    so any markup there would be shown literally."""
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", " ", s, flags=re.M)      # headings
    s = re.sub(r"^\s*[-*+]\s+", " ", s, flags=re.M)           # bullets
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)                    # bold
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", s)   # italics
    s = s.replace("`", "")
    return re.sub(r"\s+", " ", s).strip()

def _parse_json_object(raw: str):
    """The reply as a dict, or None if it is not a JSON object. Fences are already stripped."""
    t = (raw or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        o = json.loads(t)
    except ValueError:
        return None
    return o if isinstance(o, dict) and o else None

def compose_markdown(obj: dict, lang: str) -> str:
    """Dict -> markdown sections, expected keys first (in _SECTION_ORDER), the rest appended."""
    en = lang != "zh"
    known = [k for k in _SECTION_ORDER if k in obj]
    rest = [k for k in obj if k not in known]
    out = []
    for k in known + rest:
        body = _value_to_md(obj[k])
        if not body:
            continue
        out.append(f"## {_pretty_key(k, en)}")
        out.append(body)
        out.append("")
    return "\n".join(out).strip()

# ---- meta-commentary / wrong-language rejection --------------------------------------------------
# Deliberately a small set of conservative signals, not a classifier. One observed reply was entirely
# Chinese meta-commentary for a lang="en" request, saying it had produced shift_report.json and
# offered it as a download card -- no report content at all. That must never reach the user.
_META_PAT = re.compile(
    r"(i\s+have\s+(?:generated|created|produced|attached|prepared)|i've\s+(?:generated|created|produced)"
    r"|i\s+am\s+an?\s+|as\s+an\s+ai|as\s+requested,?\s+i"
    r"|download\s+(?:card|link|button|it)|you\s+can\s+download|attached\s+(?:file|report)|attachment"
    r"|\.json\s*(?:file|檔)|shift_report\.json"
    r"|我(?:已|是)|已(?:為您)?(?:產生|生成|建立|完成)|檔案已|下載(?:卡|連結|按鈕)|附(?:件|上))",
    re.I)
_CJK = re.compile(r"[一-鿿㐀-䶿]")

def _cjk_ratio(text: str) -> float:
    letters = [c for c in text if not c.isspace()]
    return (sum(1 for c in letters if _CJK.match(c)) / len(letters)) if letters else 0.0


# ================================================================================================
# HARD GATE -- deterministic, blocking, stays in Python permanently.
#
# Only checks with an exact answer live here. They are the credibility spine of the feature and
# must never be delegated to a model:
#   (a) the response contains usable report content at all   -> no_report_content()
#   (b) every number the agent asserts traces to a fact      -> check_claims()
# Anything requiring judgment belongs in advisory_review() below, NOT here.
# ================================================================================================
def no_report_content(text: str, length: str = "full"):
    """The ONE content hard-failure: there is nothing to display. Reason string, or None.

    Deliberately narrow. A merely short, oddly-toned or wrong-language report still has a body and
    is shown to the user with an advisory attached; only an empty reply, or pure meta-commentary
    with no substantive body, fails here.
    """
    t = (text or "").strip()
    if not t:
        return "agent returned an empty response"
    if len(t) < 20:
        return f"agent returned only {len(t)} characters, nothing to display"
    # "no substantive body" is decided by whether the reply cites ANY figure, not by its length.
    # A genuine shift report always quotes at least one number; pure meta-commentary about having
    # produced a file quotes none. Length is the wrong proxy -- an 84-character reply that is
    # entirely meta-commentary has no body, while a terse but real report does.
    if _META_PAT.search(t[:400]) and not _NUM.findall(_SNAKE.sub(" ", _STRIP.sub(" ", t))):
        return "agent returned meta-commentary with no report body"
    return None


# ================================================================================================
# ADVISORY LAYER -- heuristic, NON-BLOCKING, to be superseded by the reviewer agent.
#
# >>> REVIEWER-AGENT INTEGRATION POINT <<<
# A second LLM agent will later perform semantic review (tone, completeness, whether the narrative
# actually matches the facts). When it lands, it should be called from HERE and its findings
# appended to the same list, in the same shape, so nothing downstream changes. The contract:
#
#   input :  text    -- the composed report as the user would read it
#            lang    -- the requested language ("en" | "zh")
#            length  -- "brief" | "full"
#            obj     -- the parsed agent reply (dict) when the JSON contract was used, else None
#   output:  list of {"code": str, "message": str}; [] means nothing to flag
#
# Invariant: this function NEVER blocks. It cannot reject a report, it cannot raise, and its result
# must not feed back into mode selection. Judgment-based checks used to reject outright, which threw
# away reports that were perfectly good; they now surface as flags the operator can see and ignore.
# ================================================================================================
ADV_LANGUAGE = "language_mismatch"
ADV_META     = "meta_commentary"
ADV_SHORT    = "short_content"

def advisory_review(text: str, lang: str, length: str = "full", obj=None) -> list:
    """Non-blocking quality flags for a report that has already passed the hard gate."""
    out = []
    t = (text or "").strip()
    if _META_PAT.search(t[:400]):
        out.append({"code": ADV_META,
                    "message": "Reply opens with commentary about the assistant or a file rather "
                               "than with report content."})
    cjk = _cjk_ratio(t)
    if lang == "en" and cjk > 0.15:
        out.append({"code": ADV_LANGUAGE,
                    "message": f"English was requested but {cjk:.0%} of the characters are Chinese."})
    elif lang == "zh" and cjk < 0.05:
        out.append({"code": ADV_LANGUAGE,
                    "message": f"Chinese was requested but only {cjk:.0%} of the characters are Chinese."})
    floor = MIN_REPORT_CHARS.get(length, MIN_REPORT_CHARS["full"])
    if len(t) < floor:
        out.append({"code": ADV_SHORT,
                    "message": f"Report is only {len(t)} characters, shorter than the {floor} "
                               f"expected for a '{length}' report."})
    return out

def compose_narration(raw: str, lang: str, length: str):
    """Raw agent reply -> (text, hard_reason, claims, obj).

    `text` is ALWAYS the composed text, even when hard_reason is set, so a rejected draft can be
    preserved and returned to the caller for inspection instead of being discarded.
    `hard_reason` is set only by the hard gate's content check (nothing to display).

    Handles both shapes: a JSON object (composed into markdown here) and plain markdown prose.
    For length="brief" the result is always ONE plain-text paragraph, because the sidebar element
    renders it with textContent and would otherwise show markdown syntax literally.

    `claims` is [(field_path, text)] to run the numeric guardrail over, or None meaning "validate
    the whole text" (the plain-markdown case, where the model wrote every character). Scoping the
    check to the agent's own field values keeps composition scaffolding -- headings, bullet markers,
    separators this module generates -- out of validation, since the model did not write it.
    """
    raw = (raw or "").strip()
    obj = _parse_json_object(raw)

    if obj is not None:
        if length == "brief":
            pick_k = next((k for k in _SUMMARY_KEYS if k in obj and str(obj[k]).strip()), None)
            if pick_k is not None:
                text, claims = _flatten_to_paragraph(str(obj[pick_k])), [(pick_k, str(obj[pick_k]))]
            else:
                text = _flatten_to_paragraph(" ".join(_value_to_md(v) for v in obj.values()))
                claims = _claim_texts(obj)
        else:
            text, claims = compose_markdown(obj, lang), _claim_texts(obj)
    else:
        text, claims = (_flatten_to_paragraph(raw) if length == "brief" else raw), None

    return text, no_report_content(text, length), claims, obj

def check_claims(claims, text, allowed, schema=None):
    """-> (unverified, field). `claims` None means validate `text` wholesale (plain-markdown reply).

    Returns the FIRST offending field so the fallback reason can name it.
    """
    if claims is None:
        return unverified_numbers(text, allowed, schema), None
    for field, value in claims:
        bad = unverified_numbers(value, allowed, schema)
        if bad:
            return bad, field
    return [], None


# ============================================================ 3. LLM NARRATION (server-side only)
# ============================================================ 3. LLM NARRATION (LaplaceAI Endpoint)
# ---------------------------------------------------------------- resilience settings
# A LaplaceAI 504 is a gateway timeout on THEIR side: the agent exceeded their internal limit, so a
# larger client timeout cannot help. What helps is retrying (gateway timeouts are usually transient)
# and not punishing the whole process for one bad minute.
REQUEST_TIMEOUT   = 300.0   # per attempt; the agent legitimately takes ~60s, so this stays generous
RETRY_ATTEMPTS    = 3       # 1 initial + 2 retries
RETRY_BASE        = 1.5     # seconds; exponential 1.5, 3.0 with jitter
RETRY_DEADLINE    = 150.0   # HARD CEILING on total elapsed across all attempts
MIN_ATTEMPT_ROOM  = 15.0    # do not start another attempt with less than this left in the budget

COOLDOWN_TRANSIENT = 25     # 5xx / timeout after retries exhausted: short, the endpoint may recover
COOLDOWN_AUTH      = 900    # 401/403: credentials will not fix themselves
COOLDOWN_CLIENT    = 600    # other 4xx: the request is malformed, retrying is pointless

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
AUTH_STATUS      = {401, 403}

_LLM_COOLDOWN = 0.0          # skip the LLM until this real time
_LLM_COOLDOWN_WHY = ""       # why we are cooling down, so the UI can say so precisely

def _log(msg: str):
    """Attempt-level logging. Never receives the bearer secret -- callers pass status/class only."""
    print(f"[report.llm] {msg}", flush=True)

def _cooldown(seconds: int, why: str):
    global _LLM_COOLDOWN, _LLM_COOLDOWN_WHY
    _LLM_COOLDOWN = time.time() + seconds
    _LLM_COOLDOWN_WHY = why
    _log(f"cooldown {seconds}s after {why}")

def _extract_text(res_data):
    """Pull the narration out of the LaplaceAI payload shape."""
    text = ""
    if isinstance(res_data, dict):
        msg = res_data.get("message")
        if isinstance(msg, dict):
            text = msg.get("content", "")
        elif isinstance(msg, str):
            text = msg
        else:
            text = res_data.get("response") or res_data.get("output") or res_data.get("text") or ""
    else:
        text = str(res_data)
    text = (text or "").strip()
    if text.startswith("```json"): text = text[7:]
    if text.startswith("```"):     text = text[3:]
    if text.endswith("```"):       text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------- prompt payload (trimmed)
# The guardrail and the raw-facts panel always use the FULL facts object. Only the prompt payload is
# trimmed, so the agent has less to chew on (a smaller payload is one fewer reason for the agent to
# exceed its gateway limit). Trimming can never cause a guardrail failure: the allowed-number set is
# built from the full facts, so anything the agent quotes from the trimmed subset is a subset of what
# validates.
PROMPT_EXAMPLES_CAP = 2      # was MAX_EX (4) per outcome bucket
PROMPT_LIST_CAP     = 4      # onset events / high-risk nodes / high-risk jobs

def trim_facts_for_prompt(facts: dict, lang: str = "en") -> dict:
    """A narration-sized view of the facts. Drops fields the report never references."""
    f = json.loads(json.dumps(facts, ensure_ascii=False))     # deep copy, never mutate the original

    w = f.get("window", {})
    for k in ("start_ts", "end_ts"):                          # epoch ints; the prose uses the ISO forms
        w.pop(k, None)
    f.get("cluster_now", {}).pop("nodes_scored", None)        # not referenced by any report section

    po = f.get("prediction_outcomes", {})
    for bucket in ("correct_warnings", "false_alarms", "misses"):
        d = po.get(bucket)
        if isinstance(d, dict) and isinstance(d.get("examples"), list):
            d["examples"] = d["examples"][:PROMPT_EXAMPLES_CAP]

    ons = f.get("node_onsets", {})
    if isinstance(ons.get("events"), list):
        ons["events"] = ons["events"][:PROMPT_LIST_CAP]
    for key in ("high_risk_nodes", "high_risk_jobs"):
        if isinstance(f.get(key), list):
            f[key] = f[key][:PROMPT_LIST_CAP]

    # only the caveat for the requested language. This also removes the CJK string from an English
    # payload, which was the one realistic way an English report picked up a language advisory.
    mn = f.get("model_note", {})
    mn.pop("caveat_zh" if lang != "zh" else "caveat_en", None)
    return f


def _build_prompt(payload: dict, lang: str, length: str) -> str:
    """The instruction block sent to the agent. `payload` is the TRIMMED facts view."""
    langname = "Traditional Chinese (繁體中文)" if lang == "zh" else "English"
    shape = ("OUTPUT: one tight paragraph, 2-4 sentences, no headings"
             if length == "brief" else
             "OUTPUT: cover situation, what happened, current risks, recommended actions "
             "and a model note")
    rules = [
        "Use ONLY the exact numbers, counts, and percentages provided in the FACTS DATA below.",
        "DO NOT perform your own math, round numbers, or calculate new percentages.",
        "If a statistic is not directly in FACTS DATA, do not include it.",
        "Write every number EXACTLY as it appears in the JSON: no thousands separators, so write "
        "1501 and not 1,501 or 1 501. Keep decimals exactly as given.",
        "Do NOT number list items with digits (no '1.', '2.', 'Step 3'). Every list entry must be "
        "plain text; the dashboard renders them as bullets itself.",
        'Refer to metrics in PLAIN LANGUAGE, never by their raw JSON field name. Write "the node '
        'alert score", not "p2_node_alert_score"; "the alert threshold", not "p3_alert_threshold".',
        'Do NOT introduce counts or quantities of your own. If you must express a small quantity '
        'that is not in the facts, write it as a word ("both", "all three"), never as a digit.',
        "Report the model's MISSES honestly; never hide false alarms or missed failures.",
        "Recommend actions for a human to approve. Never state or imply that an action was taken, "
        "and never simulate touching infrastructure.",
        f"{shape}. Prioritise what matters operationally: incidents, misses, high-risk items.",
        "Return ONLY the report content itself. No preamble, no self-introduction, no explanation "
        "of what you did.",
        "NEVER mention producing, saving, attaching or downloading a file, and never refer to a "
        "download card, link or attachment. Put the report text in your reply directly.",
        "Do not describe yourself or your process ('I have generated...', 'As an AI...').",
        "If you answer with a JSON object, every value must be a plain STRING of report prose (or "
        "a list of strings); no nested objects, no file references.",
    ]
    body = "\n".join(f"- {r}" for r in rules)
    return (
        f"WRITE THE ENTIRE RESPONSE IN {langname.upper()}.\n\n"
        f"Generate an operational shift report in {langname} with style/length '{length}'.\n\n"
        f"CRITICAL INSTRUCTIONS:\n{body}\n\n"
        f"FACTS DATA:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"REMINDER: the entire response must be written in {langname}, and must contain the "
        f"report content only."
    )


def _attempt(invoke_url, bearer_secret, prompt, timeout):
    """One HTTP attempt. -> (text, err_class, detail, status)

    err_class is None on success, else one of 'transient' | 'auth' | 'client' | 'shape'.
    """
    headers = {"Authorization": f"Bearer {bearer_secret}", "Content-Type": "application/json"}
    try:
        r = requests.post(invoke_url, headers=headers, json={"message": prompt}, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, "transient", "read timeout", None
    except requests.exceptions.ConnectionError:
        return None, "transient", "could not connect to endpoint", None
    except Exception as e:
        return None, "transient", f"request failed ({type(e).__name__})", None

    sc = r.status_code
    if sc == 200:
        try:
            data = r.json()
        except ValueError:
            return None, "shape", "response body was not valid JSON", sc
        text = _extract_text(data)
        if not text:
            return None, "shape", "unexpected response shape (no text in reply)", sc
        return text, None, None, sc
    if sc in AUTH_STATUS:
        return None, "auth", f"HTTP {sc} from endpoint (authentication rejected)", sc
    if sc in TRANSIENT_STATUS:
        return None, "transient", f"HTTP {sc} from endpoint", sc
    return None, "client", f"HTTP {sc} from endpoint", sc


def generate_llm(facts: dict, lang: str, length: str):
    """Narrate the facts via the LaplaceAI agent endpoint, retrying transient failures.

    Returns (text, reason). `text` is None when the LLM path could not be used, and `reason` is then
    a short plain-words explanation for the UI's fallback_reason. The bearer secret is never included
    in a reason string, a log line, or an exception message.

    Retries: up to RETRY_ATTEMPTS attempts on 5xx / timeout / connection error, exponential backoff
    with jitter, bounded by RETRY_DEADLINE seconds of total elapsed time. Auth and malformed-request
    failures are NOT retried -- they are configuration errors and retrying only wastes the budget.
    """
    invoke_url = os.environ.get("LAPLACE_INVOKE_URL")
    bearer_secret = os.environ.get("LAPLACE_BEARER_SECRET")

    # env first, cooldown second, so an active cooldown is never mistaken for a missing key
    if not invoke_url:
        return None, "LAPLACE_INVOKE_URL not set"
    if not bearer_secret:
        return None, "LAPLACE_BEARER_SECRET not set"
    remain = _LLM_COOLDOWN - time.time()
    if remain > 0:
        why = _LLM_COOLDOWN_WHY or "an earlier failure"
        return None, f"endpoint cooling down after {why} ({int(remain) + 1}s remaining)"

    prompt = _build_prompt(trim_facts_for_prompt(facts, lang), lang, length)

    def cause_label(detail, sc):
        """Short noun phrase for the cooldown message: 'HTTP 504', 'a read timeout', ..."""
        if sc: return f"HTTP {sc}"
        return "a read timeout" if "timeout" in (detail or "") else "a connection failure"

    started = time.time()
    last_detail, last_sc = "endpoint unavailable", None
    for i in range(1, RETRY_ATTEMPTS + 1):
        left = RETRY_DEADLINE - (time.time() - started)
        if i > 1 and left < MIN_ATTEMPT_ROOM:
            _log(f"attempt {i} skipped: only {left:.0f}s of the {RETRY_DEADLINE:.0f}s budget left")
            break
        text, err, detail, sc = _attempt(invoke_url, bearer_secret, prompt,
                                         min(REQUEST_TIMEOUT, max(left, MIN_ATTEMPT_ROOM)))
        if err is None:
            _log(f"attempt {i}/{RETRY_ATTEMPTS} ok (HTTP 200, {time.time()-started:.1f}s)")
            return text, None
        last_detail, last_sc = detail, sc
        _log(f"attempt {i}/{RETRY_ATTEMPTS} failed: {detail} [class={err}]")

        if err == "auth":
            _cooldown(COOLDOWN_AUTH, f"HTTP {sc}")
            return None, (f"{detail} -- check LAPLACE_BEARER_SECRET; not retried and paused for "
                          f"{COOLDOWN_AUTH // 60} minutes")
        if err == "client":
            _cooldown(COOLDOWN_CLIENT, f"HTTP {sc}")
            return None, f"{detail} -- request rejected as malformed; not retried"
        if err == "shape":
            # the endpoint is up and answered 200: do not trip the breaker, do not retry
            return None, detail

        if i < RETRY_ATTEMPTS:                       # transient -> back off and try again
            delay = RETRY_BASE * (2 ** (i - 1))
            delay *= 0.75 + random.random() * 0.5    # +/-25% jitter
            if (time.time() - started) + delay + MIN_ATTEMPT_ROOM > RETRY_DEADLINE:
                _log("backoff would exceed the retry deadline; giving up")
                break
            _log(f"backing off {delay:.1f}s before attempt {i + 1}")
            time.sleep(delay)

    tried = min(i, RETRY_ATTEMPTS)
    _cooldown(COOLDOWN_TRANSIENT, cause_label(last_detail, last_sc))
    return None, f"{last_detail} after {tried} attempt{'' if tried == 1 else 's'}"



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

# ---- disk-backed prewarm cache -----------------------------------------------------------------
# _CACHE lives in ONE process's memory, so a prewarm script run separately would populate its own
# dict and do nothing for the server -- the prewarm would be a no-op. Successful reports are
# therefore also mirrored to a small JSON file that any process can read. Disk entries ignore
# CACHE_TTL: they exist precisely so a prepared demo never depends on a live call. Every filesystem
# operation degrades silently, so a read-only deployment behaves exactly as before.
PREWARM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "report_cache.json")

def _key_str(key) -> str:
    return "|".join(str(x) for x in key)

def _disk_load() -> dict:
    try:
        with open(PREWARM_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

def _disk_save(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(PREWARM_PATH), exist_ok=True)
        tmp = PREWARM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, PREWARM_PATH)
        return True
    except Exception:
        return False

def _cache_get(key):
    with _LOCK:
        v = _CACHE.get(key)
        if v and (time.time() - v[1]) < CACHE_TTL:
            return v[0]
        if v:
            _CACHE.pop(key, None)
    hit = _disk_load().get(_key_str(key))          # prewarmed entries never expire
    if hit:
        return {**hit, "prewarmed": True}
    return None

def _cache_put(key, out, to_disk=False):
    with _LOCK:
        _CACHE[key] = (out, time.time())
        if len(_CACHE) > CACHE_MAX:
            for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][1])[: len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(k, None)
    if to_disk:                                   # only prewarm_reports.py asks for this
        d = _disk_load()
        d[_key_str(key)] = out
        _disk_save(d)

def cache_key(clock_ts, window_h, lang, length, policies):
    """The cache key, exposed so prewarm_reports.py writes entries the server will actually find."""
    return (int(clock_ts) // CACHE_BUCKET, round(float(window_h), 2), lang, length,
            round(float(policies["alert_threshold"]), 3), int(float(policies["node_filter_pct"])))

def build_report(store, clock, policies, window_h=6, lang="zh", length="brief", nocache=False,
                 persist=False) -> dict:
    t = clock.now_ts()
    key = cache_key(t, window_h, lang, length, policies)
    if not nocache:
        cached = _cache_get(key)
        if cached:
            return {**cached, "cached": True}

    facts = assemble_facts(store, t, policies, window_h)
    allowed = allowed_numbers(facts)
    schema = schema_strip_re(facts)          # the facts' own digit-bearing key names
    raw, reason = generate_llm(facts, lang, length)
    unver, bad_field, advisories, draft = [], None, [], None

    if raw is not None:
        # shape the reply (JSON contract -> markdown, or prose as-is); `llm` is the composed text
        # even when the hard gate is about to reject it, so the draft can be preserved.
        llm, content_reason, claims, obj = compose_narration(raw, lang, length)
        # ADVISORY LAYER: never blocks, never feeds back into `mode`. Reviewer-agent seam.
        advisories = advisory_review(llm, lang, length, obj)
    else:
        llm, content_reason, claims, obj = None, None, None, None

    if llm is None:
        text, mode = render_template(facts, lang, length), "template"
    elif content_reason:
        # HARD GATE (a): nothing to display at all.
        text, mode, reason = render_template(facts, lang, length), "template_llm_rejected", content_reason
        draft = {"text": llm, "field": None, "unverified": [], "reason": content_reason}
    else:
        # HARD GATE (b): every asserted number must trace to a fact. Scoped to the agent's own
        # field values, with schema identifiers removed, so scaffolding and field NAMES are never
        # mistaken for claims. A numeric rejection is a content problem, NOT an endpoint failure,
        # so it must not trip the circuit breaker (generate_llm owns _LLM_COOLDOWN).
        unver, bad_field = check_claims(claims, llm, allowed, schema)
        if not unver:
            text, mode, reason = llm, "llm", None
        else:
            text, mode = render_template(facts, lang, length), "template_llm_rejected"
            reason = (f"numeric check failed{' in ' + bad_field if bad_field else ''}: "
                      f"{len(unver)} unmatched number"
                      f"{'' if len(unver) == 1 else 's'} ({', '.join(unver)})")
            draft = {"text": llm, "field": bad_field, "unverified": unver, "reason": reason}

    out = {"text": text, "mode": mode, "length": length, "lang": lang, "window_h": window_h,
           "generated_iso": facts["now_iso"], "virtual_ts": t,
           # `checked` distinguishes "the numeric check ran and passed" from "it never ran because
           # the reply was rejected as non-report content first"
           "numeric_check": {"ok": (not unver), "checked": bool(llm is not None and not content_reason),
                             "unverified": unver, "field": bad_field},
           # advisory flags: quality observations, NOT rejection reasons. Present on accepted
           # reports too -- a flagged report is still shown.
           "advisories": advisories,
           # the draft the hard gate threw away, kept so the next false positive is diagnosable at
           # a glance instead of by round-tripping. NOT rendered as the report; the template is.
           "rejected_draft": draft,
           "fallback_reason": (None if mode == "llm" else (reason or "template fallback")),
           "facts": facts, "cached": False}
    # Only a successful LLM narration is cached. A fallback is a transient condition (cooldown,
    # timeout, a one-off numeric rejection); pinning it for the whole TTL is what made the fixed
    # bug keep reappearing with "cached": true. Fallbacks are therefore re-attempted next request,
    # bounded by the 120s circuit breaker and the frontend's 12s brief-report throttle.
    if mode == "llm":
        _cache_put(key, out, to_disk=persist)
    return out
