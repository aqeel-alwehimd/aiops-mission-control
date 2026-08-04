"""
charts.py -- the chart registry: a CLOSED set of chart ids, one renderer each.

The architectural rule this module exists to enforce: **the model never produces a number.**
Every value plotted on the dashboard is computed here, in Python, from the facts dictionary that
assemble_facts() built out of SQLite -- or, for the per-prediction contributions, from the same
store query the drill-down panel uses. The agent's only say in a chart is *which* one to show and
what to write underneath it.

Two consequences follow, and both are deliberate:

  * A renderer returns DATA, not a drawing instruction. Labels, datasets, colours, axis references
    and the reference line are all decided here; the frontend loops over what it is given and draws
    it. There is no arithmetic in JavaScript.
  * A renderer that cannot answer its question says so. `ChartResult.unavailable(...)` is the only
    honest response to an empty or degenerate slice -- a chart is never padded, back-filled, or
    given a placeholder point to make it look populated. An operator who sees no chart learns
    something true; an operator who sees a fabricated one does not.

LABELS AND LANGUAGE
Anything the user reads that is not data goes out as an i18n *reference*, not as text:
    {"key": "chart.cat.caught"}          -> the frontend resolves it through the existing I18N dict
    {"en": "...", "zh": "..."}           -> a bilingual label that comes from the database
    {"text": "job 4288317"}              -> a literal identifier, the same in both languages
so the English/Chinese toggle relabels a chart without another API call.
"""
import datetime
from enum import Enum


# ============================================================ chart ids (the closed set)
class ChartId(str, Enum):
    """The complete set of charts the report can show. The agent may select from these ids and
    nothing else; anything outside this enum is dropped and flagged (see report.select_charts).

    NOTE: the model-info importance panels are deliberately NOT here. They are static, they are not
    part of a shift narrative, and putting them in the enum would offer the agent a chart it has no
    business selecting. See model_importance_panels() at the bottom of this module.
    """
    PREDICTION_OUTCOMES        = "prediction_outcomes"
    JOB_OUTCOME_MIX            = "job_outcome_mix"
    FAILURES_OVER_TIME_BARS    = "failures_over_time_bars"
    FAILURES_OVER_TIME_LINES   = "failures_over_time_lines"
    TOP_FLAGGED_JOBS           = "top_flagged_jobs"
    NODE_RISK_WATCH            = "node_risk_watch"
    NODE_SENSOR_TRACE          = "node_sensor_trace"
    NODE_FEATURE_CONTRIBUTIONS = "node_feature_contributions"


# One line per chart, sent to the agent so it can choose. Kept next to the enum so a new chart
# cannot be added without writing the line that tells the agent what it answers.
CHART_MENU = {
    ChartId.PREDICTION_OUTCOMES:
        "how the jobs P3 FLAGGED in this window have turned out so far -- caught (flagged and since "
        "failed), false alarm (flagged and completed), pending (flagged, still running). These three "
        "are the flagged cohort and sum to the flagged total. Missed failures are NOT in that cohort "
        "-- they were never flagged -- and are shown beside the chart as a separate figure",
    ChartId.JOB_OUTCOME_MIX:
        "what the jobs that ended in this window ended as (completed / failed / timeout / OOM)",
    ChartId.FAILURES_OVER_TIME_BARS:
        "WHEN failures happened: jobs that ended in this window bucketed into 15-minute bins by end "
        "time, stacked by outcome type (failed / timeout / OOM). Good for reading composition",
    ChartId.FAILURES_OVER_TIME_LINES:
        "the same 15-minute failure buckets drawn as lines, one per outcome type plus a total. Good "
        "for reading the trend and spotting a burst",
    ChartId.TOP_FLAGGED_JOBS:
        "which in-flight jobs currently carry the highest predicted risk, coloured by the failure "
        "type predicted for each",
    ChartId.NODE_RISK_WATCH:
        "which nodes are on the watch list and how their P2 risk compares with the alert threshold",
    ChartId.NODE_SENSOR_TRACE:
        "what one node's RAW sensors actually did -- total power and GPU temperature at the native "
        "20-second IPMI sampling rate, with the anomaly onset marked when one occurred. Use it to "
        "show the physical behaviour behind a node alert rather than just its score",
    ChartId.NODE_FEATURE_CONTRIBUTIONS:
        "WHY the single highest-risk node is flagged -- the per-prediction feature contributions "
        "behind its score",
}

# ---- mandatory pairs -----------------------------------------------------------------------------
# Charts that only make sense together. The bars and the lines plot IDENTICAL numbers from one
# aggregation: the bars read composition, the lines read trend, and a reader who is shown one and
# not the other is being asked to infer the other. If the agent picks one and both are drawable, the
# partner is appended with a Python-written caption. See report.assemble_charts.
MANDATORY_PAIRS = [(ChartId.FAILURES_OVER_TIME_BARS, ChartId.FAILURES_OVER_TIME_LINES)]

# ---- layout hint ---------------------------------------------------------------------------------
# "half" sits in a two-column grid; "full" spans it. A time series with twenty-plus buckets is
# illegible at half width, so every time-series chart is full. Every registry entry must appear here
# -- there is a test that fails if one is missing, so a new chart cannot ship without a hint.
WIDTH_HALF, WIDTH_FULL = "half", "full"
LAYOUT = {
    ChartId.PREDICTION_OUTCOMES:        WIDTH_HALF,
    ChartId.JOB_OUTCOME_MIX:            WIDTH_HALF,
    ChartId.FAILURES_OVER_TIME_BARS:    WIDTH_FULL,
    ChartId.FAILURES_OVER_TIME_LINES:   WIDTH_FULL,
    ChartId.TOP_FLAGGED_JOBS:           WIDTH_HALF,
    ChartId.NODE_RISK_WATCH:            WIDTH_HALF,
    ChartId.NODE_SENSOR_TRACE:          WIDTH_FULL,   # a 1000-point time series needs the room
    ChartId.NODE_FEATURE_CONTRIBUTIONS: WIDTH_HALF,
}


# ============================================================ tunables (no magic numbers inline)
MAX_FLAGGED_JOB_BARS = 6      # a horizontal bar chart stops being readable much past this
MAX_NODE_RISK_BARS   = 6
MAX_CONTRIB_BARS     = 10     # top-|contribution| features; the tail is noise at this scale
MAX_IMPORTANCE_BARS  = 12     # model-info panel

# failures-over-time bucket width. 15 minutes matches the P2 telemetry slot grid and the job
# end_ts resolution is 1 second, so this is a real choice of granularity rather than a limit of the
# data. The bucket COUNT is deliberately unbounded: a 48 h window is 193 buckets and a longer one
# more, every one of them plotted. Thinning happens in the axis LABELS on the frontend, never in the
# data -- dropping buckets would hide exactly the isolated single failure this chart exists to show.
FOT_BIN_SECONDS = 900

# node_risk_watch degeneracy rule -- RETUNED 2026-08-03 against a 40-point sweep of the replay
# period (sweep_report.py). What the sweep measured, with the alert threshold at 50%:
#   top watch-node score   min 0.20%   median 2.90%   mean 10.57%   max 61.50%
#   score buckets          <1%: 9pts   1-5%: 17   5-12.5%: 6   12.5-25%: 3   >=25%: 5
# At the old 0.25 the chart rendered at 8/40 points (20%). The fleet really is quiet most of the
# time, so silence is often correct -- but 20% is too rare for a chart that is the only view of
# node risk.
#
# The original problem was never the low scores per se, it was the PICTURE: an in-plot reference
# line at 50% forces the axis to 50%, so a 3% bar becomes a stub and the chart says nothing. That is
# fixed properly below -- when the top bar is under the threshold the reference LINE is dropped, the
# axis scales to the data so the bars are legible, and the subtitle states in words exactly how far
# below the threshold the fleet is sitting. With the line gone there is no longer a reason to hide
# a real ranking, so the bar is lowered to 0.10 (renders at 14/40 = 35%).
#
# It still refuses below that: under 10% of the threshold -- 5% absolute here -- the ordering of
# watch-list nodes is noise-floor jitter, and ranking noise is worse than showing nothing.
NODE_RISK_MIN_FRACTION_OF_ALERT = 0.10

# palette -- the dashboard's existing accent colours, so a chart looks native to the panel
C_GOOD    = "#34d399"   # emerald  -- correct / completed
C_BAD     = "#f43f5e"   # rose     -- failure / raises risk
C_WARN    = "#fbbf24"   # amber    -- false alarm / timeout
C_INFO    = "#22d3ee"   # cyan     -- neutral emphasis
C_MUTED   = "#64748b"   # slate    -- pending / no outcome yet
C_VIOLET  = "#a78bfa"   # violet   -- OOM

# one colour per predicted failure type, shared by every chart that encodes type, so a colour means
# the same thing wherever it appears in the report
PRED_TYPE_COLORS = {"FAILED": C_BAD, "TIMEOUT": C_WARN, "OUT_OF_MEMORY": C_VIOLET}
PRED_TYPE_KEYS   = {"FAILED": "chart.cat.failed", "TIMEOUT": "chart.cat.timeout",
                    "OUT_OF_MEMORY": "chart.cat.oom"}


# ============================================================ result type
class ChartResult:
    """Either a fully computed chart, or a stated reason there is nothing honest to draw."""

    def __init__(self, chart_id, chart=None, code=None, reason=None):
        self.chart_id = str(getattr(chart_id, "value", chart_id))
        self.chart = chart
        self.code = code
        self.reason = reason

    @property
    def available(self):
        return self.chart is not None

    @classmethod
    def ok(cls, chart_id, chart):
        return cls(chart_id, chart=chart)

    @classmethod
    def unavailable(cls, chart_id, code, reason):
        """code is machine-readable ('no_rows' | 'degenerate' | 'no_store'); reason is for a human."""
        return cls(chart_id, code=code, reason=reason)

    def as_unavailable_entry(self):
        return {"chart_id": self.chart_id, "code": self.code, "reason": self.reason}


def _lbl_key(key):            return {"key": key}
def _lbl_text(text):          return {"text": str(text)}
def _lbl_bi(en, zh):          return {"en": str(en), "zh": str(zh)}


# ============================================================ shared aggregation (ONE query)
# failures_over_time_bars and failures_over_time_lines plot the same numbers. They must therefore
# read them from the same place: if each renderer ran its own query the two charts could disagree
# after a schema change, a threshold change, or a clock tick between calls, and a reader comparing
# them would have no way to tell which was right. So there is exactly one query and exactly one
# aggregation, memoised for the (store, window) it was computed for, and both renderers consume it.
_FOT_MEMO = {}
_FOT_MEMO_MAX = 8

# the outcome types this chart counts, in stacking order, with their palette entry
FOT_OUTCOMES = (("failed",  "FAILED",        "chart.cat.failed",  C_BAD),
                ("timeout", "TIMEOUT",       "chart.cat.timeout", C_WARN),
                ("oom",     "OUT_OF_MEMORY", "chart.cat.oom",     C_VIOLET))


def _hm(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%m-%d %H:%M")


def _hms(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%H:%M:%S")


def failures_over_time(facts, store):
    """THE single query and THE single aggregation behind both failures-over-time charts.

    Buckets the ENDED-IN-WINDOW cohort -- jobs whose end_ts falls in the window, whenever they were
    submitted, exactly the cohort behind jobs_window.ended_in_window_* -- into fixed 15-minute bins
    by end timestamp, counted by outcome type.

    Returns None when there is no store to ask. Otherwise a dict:
        bin_seconds, bins        -- bin start epochs, every one of them, gaps included as zeros
        labels                   -- "MM-DD HH:MM" per bin
        series                   -- {"failed": [...], "timeout": [...], "oom": [...]}
        total                    -- per-bin sum of the three
        grand_total              -- sum over the window
    Bins are aligned to the epoch 15-minute grid so they line up with the P2 telemetry slots; the
    first and last bin may therefore be partial, which is why the resolution is stated on the chart.
    """
    w = facts.get("window") or {}
    lo, hi = w.get("start_ts"), w.get("end_ts")
    if store is None or lo is None or hi is None:
        return None
    lo, hi = int(lo), int(hi)

    key = (getattr(store, "db_path", id(store)), lo, hi)
    if key in _FOT_MEMO:
        return _FOT_MEMO[key]

    # --- the one query -------------------------------------------------------------------------
    rows = store._df("SELECT end_ts, state FROM jobs WHERE end_ts>? AND end_ts<=? AND state<>?",
                     (lo, hi, "COMPLETED"))

    # --- the one aggregation -------------------------------------------------------------------
    b0 = (lo // FOT_BIN_SECONDS) * FOT_BIN_SECONDS
    b1 = (hi // FOT_BIN_SECONDS) * FOT_BIN_SECONDS
    bins = list(range(b0, b1 + FOT_BIN_SECONDS, FOT_BIN_SECONDS))
    index = {b: i for i, b in enumerate(bins)}
    series = {name: [0] * len(bins) for name, _, _, _ in FOT_OUTCOMES}
    state_to_name = {state: name for name, state, _, _ in FOT_OUTCOMES}

    for r in rows.itertuples(index=False):
        name = state_to_name.get(str(r.state))
        if name is None:                       # an outcome type this chart does not track
            continue
        i = index.get((int(r.end_ts) // FOT_BIN_SECONDS) * FOT_BIN_SECONDS)
        if i is not None:
            series[name][i] += 1

    total = [sum(series[n][i] for n, _, _, _ in FOT_OUTCOMES) for i in range(len(bins))]
    out = {"bin_seconds": FOT_BIN_SECONDS, "bins": bins, "labels": [_hm(b) for b in bins],
           "series": series, "total": total, "grand_total": sum(total)}

    if len(_FOT_MEMO) >= _FOT_MEMO_MAX:
        _FOT_MEMO.clear()
    _FOT_MEMO[key] = out
    return out


def _fot_guard(cid, agg, facts, store):
    """The availability rule both failures-over-time charts share, so they cannot diverge.

    Every bucket zero -> nothing happened, nothing to draw. A SINGLE non-zero bucket is NOT
    degenerate: one failure at one moment is a real event and is exactly what an operator wants to
    see, so it is drawn.
    """
    if agg is None:
        if store is None:
            return ChartResult.unavailable(
                cid, "no_store", "bucketing ended jobs by time needs the store; none was given")
        return ChartResult.unavailable(
            cid, "no_rows", "the facts carry no window start/end timestamps to bucket over")
    if not agg["bins"]:
        return ChartResult.unavailable(cid, "no_rows", "the window spans no complete time bucket")
    if agg["grand_total"] == 0:
        return ChartResult.unavailable(
            cid, "no_rows",
            f"no job ended as a failure, timeout or OOM in any of the {len(agg['bins'])} "
            f"15-minute buckets across this window")
    return None


def _pct_axis_max(values, reference):
    """Upper bound for a percentage value axis, computed HERE so the frontend does no arithmetic.

    A reference line is drawn by the chart canvas, not by a dataset, so the axis will not stretch to
    include it on its own: a 50% threshold above a 9% tallest bar would simply fall outside the plot.
    Headroom is added above whichever is larger, clamped to 100 because these are percentages.
    """
    top = max(list(values) + [reference])
    return round(min(100.0, top * 1.1), 1)


# ============================================================ renderers
# Every renderer has the signature (facts, store, t, policies) -> ChartResult, even when it does not
# need all four, so the registry can call them uniformly. `store` may be None (the guardrail test
# harness drives build_report with no sqlite store); a renderer that needs it must say no, not raise.

def _r_prediction_outcomes(facts, store, t, policies):
    """The FLAGGED cohort split by outcome. Three segments, and they sum to the flagged total.

    WHY ONLY THREE. A doughnut encodes parts of a whole, so every segment must belong to the same
    whole. Caught, false alarm and pending do: each is a job P3 flagged, and by the cohort identity
    established in assemble_facts they sum exactly to flagged_total. A MISSED failure is a job P3
    did not flag -- it is in the submitted cohort but outside the flagged set, so adding it as a
    fourth segment made the ring sum to flagged_total + misses, a quantity that means nothing. That
    is not a cosmetic problem: it licensed a caption reading "the 460 flagged jobs, including the 34
    missed failures", which is false while every number in it is a real fact, so the numeric gate
    could not catch it.

    Misses are still reported -- they are the honest part of the story and must not be dropped --
    but as a separate labelled figure beside the ring, not as a slice of it.
    """
    po = facts.get("prediction_outcomes") or {}
    def cnt(bucket):
        b = po.get(bucket)
        return int(b.get("count", 0)) if isinstance(b, dict) else 0
    caught, fa, pending = cnt("correct_warnings"), cnt("false_alarms"), cnt("pending_outcome")
    missed = cnt("misses")

    data = [caught, fa, pending]
    segment_sum = sum(data)

    if segment_sum == 0:
        return ChartResult.unavailable(
            ChartId.PREDICTION_OUTCOMES, "no_rows",
            "no job submitted in this window was flagged, so the flagged cohort is empty and there "
            "is nothing to split")

    return ChartResult.ok(ChartId.PREDICTION_OUTCOMES, {
        "chart_id": ChartId.PREDICTION_OUTCOMES.value,
        "type": "doughnut",
        "width": LAYOUT[ChartId.PREDICTION_OUTCOMES],
        "title": _lbl_key("chart.predictionOutcomes.title"),
        "subtitle": _lbl_key("chart.predictionOutcomes.sub"),
        "labels": [_lbl_key("chart.cat.caught"), _lbl_key("chart.cat.falseAlarm"),
                   _lbl_key("chart.cat.pending")],
        "datasets": [{"legend": _lbl_key("chart.legend.jobs"), "data": data,
                      "colors": [C_GOOD, C_WARN, C_MUTED]}],
        "value_suffix": "",
        # what the ring adds up to, asserted here and stated in the subtitle
        "segment_sum": segment_sum,
        # the other cohort, kept visible but kept OUT of the ring
        "footnote": {"label": _lbl_key("chart.cat.missed"), "value": missed,
                     "note": _lbl_key("chart.predictionOutcomes.missNote")},
        "cohort_note": _lbl_key("chart.cohort.flagged"),
    })


def _r_job_outcome_mix(facts, store, t, policies):
    jw = facts.get("jobs_window") or {}
    data = [int(jw.get("ended_in_window_completed", 0)), int(jw.get("ended_in_window_failed", 0)),
            int(jw.get("ended_in_window_timeout", 0)), int(jw.get("ended_in_window_oom", 0))]
    if sum(data) == 0:
        return ChartResult.unavailable(ChartId.JOB_OUTCOME_MIX, "no_rows",
                                       "no job ended inside this window")
    return ChartResult.ok(ChartId.JOB_OUTCOME_MIX, {
        "chart_id": ChartId.JOB_OUTCOME_MIX.value,
        "type": "bar",
        "width": LAYOUT[ChartId.JOB_OUTCOME_MIX],
        "title": _lbl_key("chart.jobOutcomeMix.title"),
        "subtitle": _lbl_key("chart.jobOutcomeMix.sub"),
        "labels": [_lbl_key("chart.cat.completed"), _lbl_key("chart.cat.failed"),
                   _lbl_key("chart.cat.timeout"), _lbl_key("chart.cat.oom")],
        "datasets": [{"legend": _lbl_key("chart.legend.jobs"), "data": data,
                      "colors": [C_GOOD, C_BAD, C_WARN, C_VIOLET]}],
        "axis_x": _lbl_key("chart.axis.finalState"),
        "axis_y": _lbl_key("chart.legend.jobs"),
        "value_suffix": "",
    })


def _r_failures_over_time_bars(facts, store, t, policies):
    """Stacked bars: one stack segment per outcome type, per 15-minute bin."""
    agg = failures_over_time(facts, store)
    bad = _fot_guard(ChartId.FAILURES_OVER_TIME_BARS, agg, facts, store)
    if bad:
        return bad
    return ChartResult.ok(ChartId.FAILURES_OVER_TIME_BARS, {
        "chart_id": ChartId.FAILURES_OVER_TIME_BARS.value,
        "type": "stacked_bar",
        "width": LAYOUT[ChartId.FAILURES_OVER_TIME_BARS],
        "title": _lbl_key("chart.failuresOverTime.title"),
        "subtitle": _lbl_key("chart.failuresOverTime.sub"),
        "labels": [_lbl_text(x) for x in agg["labels"]],
        "datasets": [{"legend": _lbl_key(key), "data": agg["series"][name], "color": col}
                     for name, _state, key, col in FOT_OUTCOMES],
        "axis_x": _lbl_key("chart.axis.binEnd"),
        "axis_y": _lbl_key("chart.axis.failuresPerBin"),
        "value_suffix": "",
        "cohort_note": _lbl_key("chart.cohort.endedInWindow"),
        "bucket_count": len(agg["bins"]),
    })


def _r_failures_over_time_lines(facts, store, t, policies):
    """The identical numbers as lines: one per outcome type, plus a prominent total.

    Straight segments, not curves. These are counts in discrete bins; curve tension would draw a
    value at every pixel between two buckets, and none of those values exist. Point markers stay
    large enough that a single isolated failure in one bucket is legible, because most buckets are
    empty and an unmarked 1 in a sea of zeros reads as noise on the axis.
    """
    agg = failures_over_time(facts, store)
    bad = _fot_guard(ChartId.FAILURES_OVER_TIME_LINES, agg, facts, store)
    if bad:
        return bad

    per_type = [{"legend": _lbl_key(key), "data": agg["series"][name], "color": col,
                 "style": {"line_width": 1.5, "point_radius": 2.5, "tension": 0, "order": 2}}
                for name, _state, key, col in FOT_OUTCOMES]
    # the total is the line the eye should land on first: thicker, bigger markers, drawn on top
    total = {"legend": _lbl_key("chart.legend.totalFailures"), "data": agg["total"], "color": C_INFO,
             "style": {"line_width": 3, "point_radius": 4, "tension": 0, "order": 1}}

    return ChartResult.ok(ChartId.FAILURES_OVER_TIME_LINES, {
        "chart_id": ChartId.FAILURES_OVER_TIME_LINES.value,
        "type": "line",
        "width": LAYOUT[ChartId.FAILURES_OVER_TIME_LINES],
        "title": _lbl_key("chart.failuresOverTimeLines.title"),
        "subtitle": _lbl_key("chart.failuresOverTime.sub"),
        "labels": [_lbl_text(x) for x in agg["labels"]],
        "datasets": [total] + per_type,
        "axis_x": _lbl_key("chart.axis.binEnd"),
        "axis_y": _lbl_key("chart.axis.failuresPerBin"),
        "value_suffix": "",
        "cohort_note": _lbl_key("chart.cohort.endedInWindow"),
        "bucket_count": len(agg["bins"]),
    })


def _r_top_flagged_jobs(facts, store, t, policies):
    """The riskiest jobs currently in flight that are at or above the P3 alert threshold.

    'Flagged' is the same test the dashboard's job table applies: risk >= the alert threshold in
    force. Only in-flight jobs are shown, because a finished job is not something a shift can act on.
    """
    thr_pct = float((facts.get("settings") or {}).get("p3_alert_threshold", 0)) * 100.0
    rows = [j for j in (facts.get("high_risk_jobs") or [])
            if isinstance(j, dict) and j.get("risk_pct") is not None
            and float(j["risk_pct"]) >= thr_pct]
    rows = sorted(rows, key=lambda j: float(j["risk_pct"]), reverse=True)[:MAX_FLAGGED_JOB_BARS]
    if not rows:
        return ChartResult.unavailable(ChartId.TOP_FLAGGED_JOBS, "no_rows",
                                       "no in-flight job is at or above the P3 alert threshold")
    vals = [round(float(j["risk_pct"]), 1) for j in rows]

    # COLOUR CARRIES THE INFORMATION THE HEIGHT DOES NOT.
    # In practice these bars come out near-identical -- the top in-flight jobs sit at 99.6% to one
    # decimal, so a length encoding shows a ranking with nothing to rank. Colouring by the predicted
    # failure TYPE gives the chart a second, genuinely varying dimension, and makes a caption like
    # "four of the six are timeout risks" checkable against the picture. The risk axis is left
    # honest: no zero-suppression or axis trick to manufacture spread that the data does not have.
    types = [str(j.get("pred_type") or "FAILED") for j in rows]
    bar_colors = [PRED_TYPE_COLORS.get(ty, C_MUTED) for ty in types]
    seen, color_legend = set(), []
    for ty in types:
        if ty in seen:
            continue
        seen.add(ty)
        color_legend.append({"label": _lbl_key(PRED_TYPE_KEYS.get(ty, "chart.cat.failed")),
                             "color": PRED_TYPE_COLORS.get(ty, C_MUTED)})

    return ChartResult.ok(ChartId.TOP_FLAGGED_JOBS, {
        "chart_id": ChartId.TOP_FLAGGED_JOBS.value,
        "type": "hbar",
        "width": LAYOUT[ChartId.TOP_FLAGGED_JOBS],
        "title": _lbl_key("chart.topFlaggedJobs.title"),
        "subtitle": _lbl_key("chart.topFlaggedJobs.sub"),
        "labels": [_lbl_text(f"{j['job_id']} · {j.get('user', '')}") for j in rows],
        "datasets": [{"legend": _lbl_key("chart.legend.predictedRisk"), "data": vals,
                      "colors": bar_colors}],
        "axis_max": _pct_axis_max(vals, thr_pct),
        # a swatch legend the frontend draws itself: Chart.js's dataset legend cannot express
        # per-bar colouring without inventing one fake dataset per type
        "color_legend": color_legend,
        # the predicted failure type per bar, shown in the tooltip -- a real model output, not a guess
        "point_notes": [_lbl_text(ty) for ty in types],
        "axis_x": _lbl_key("chart.axis.riskPct"),
        "reference_line": {"value": round(thr_pct, 1), "label": _lbl_key("chart.ref.jobAlert")},
        "value_suffix": "%",
    })


def _r_node_risk_watch(facts, store, t, policies):
    """Watch-list nodes by P2 risk, against the node alert threshold.

    Reports itself unavailable when every bar is a small fraction of the threshold -- see
    NODE_RISK_MIN_FRACTION_OF_ALERT for why that picture is worse than no picture.
    """
    settings = facts.get("settings") or {}
    alert = settings.get("p2_node_alert_score")
    rows = [n for n in (facts.get("high_risk_nodes") or [])
            if isinstance(n, dict) and n.get("risk_pct") is not None]
    rows = sorted(rows, key=lambda n: float(n["risk_pct"]), reverse=True)[:MAX_NODE_RISK_BARS]
    if not rows:
        return ChartResult.unavailable(ChartId.NODE_RISK_WATCH, "no_rows",
                                       "no node is in triage scope with a score at this time")
    if alert is None:
        return ChartResult.unavailable(ChartId.NODE_RISK_WATCH, "no_rows",
                                       "the node alert threshold is not available in the facts")

    alert_pct = float(alert) * 100.0
    top = max(float(n["risk_pct"]) for n in rows)
    if alert_pct > 0 and top < NODE_RISK_MIN_FRACTION_OF_ALERT * alert_pct:
        return ChartResult.unavailable(
            ChartId.NODE_RISK_WATCH, "degenerate",
            f"every watch-list node scores below {NODE_RISK_MIN_FRACTION_OF_ALERT:.0%} of the "
            f"{alert_pct:.0f}% alert threshold (highest is {top:.2f}%), so the bars would be stubs "
            f"beside the reference line and would not show relative risk")

    vals = [round(float(n["risk_pct"]), 2) for n in rows]

    # Two regimes, because one picture cannot serve both.
    #  * top bar AT/ABOVE the threshold -> the comparison to the threshold is the story. Draw the
    #    reference line and stretch the axis to include it.
    #  * top bar BELOW it -> the line would pin the axis at 50% and flatten every bar into a stub,
    #    which is what made this chart unreadable. Drop the line, scale the axis to the data so the
    #    ranking is legible, and say the distance to the threshold in words instead.
    at_threshold = top >= alert_pct
    if at_threshold:
        ref = {"value": round(alert_pct, 1), "label": _lbl_key("chart.ref.nodeAlert")}
        axis_max = _pct_axis_max(vals, alert_pct)
        subtitle = _lbl_key("chart.nodeRiskWatch.sub")
    else:
        ref = None
        axis_max = round(min(100.0, top * 1.25), 2)
        pct_of = (100.0 * top / alert_pct) if alert_pct else 0.0
        subtitle = _lbl_bi(
            f"All below the {alert_pct:.0f}% alert threshold — highest is {top:.2f}%, "
            f"{pct_of:.0f}% of the way to it. Axis scaled to the data, so bar heights compare "
            f"nodes with each other, not with the threshold.",
            f"全部低於 {alert_pct:.0f}% 告警門檻 — 最高為 {top:.2f}%，僅達門檻的 {pct_of:.0f}%。"
            f"座標軸依資料縮放，故長度為節點間的相互比較，非與門檻比較。")

    return ChartResult.ok(ChartId.NODE_RISK_WATCH, {
        "chart_id": ChartId.NODE_RISK_WATCH.value,
        "type": "bar",
        "width": LAYOUT[ChartId.NODE_RISK_WATCH],
        "title": _lbl_key("chart.nodeRiskWatch.title"),
        "subtitle": subtitle,
        "labels": [_lbl_text(f"node{int(n['node']):04d}") for n in rows],
        "datasets": [{"legend": _lbl_key("chart.legend.p2Risk"), "data": vals,
                      "colors": [(C_BAD if n.get("onset") or float(n["risk_pct"]) >= alert_pct
                                  else C_WARN) for n in rows]}],
        "point_notes": [_lbl_text(f"rack {int(n['rack'])} · {n.get('state', '')}") for n in rows],
        "axis_x": _lbl_key("chart.axis.node"),
        "axis_y": _lbl_key("chart.axis.riskPct"),
        "axis_max": axis_max,
        "reference_line": ref,
        "value_suffix": "%",
        "at_threshold": at_threshold,
    })


# ---- native-resolution sensor trace ---------------------------------------------------------------
TRACE_SPAN_H       = 3.0    # hours either side of the onset (matches build_node_raw.ONSET_SPAN_H)
MAX_TRACE_POINTS   = 1200   # above this the trace is binned to a min/max ENVELOPE, never averaged
TRACE_FLAT_EPS     = 1e-9   # a series whose whole span is one constant value carries no information

def _bin_envelope(rows, n_bins):
    """Downsample by preserving each bin's MIN and MAX, never its mean.

    Averaging is the one thing this chart must not do: the transients and drop-outs it exists to
    show are exactly what a mean erases. An envelope keeps both extremes, so a one-sample spike
    survives at any bin width.
    """
    total = len(rows)
    width = max(1, -(-total // n_bins))          # ceil
    out = []
    for i in range(0, total, width):
        chunk = rows[i:i + width]
        ts = chunk[len(chunk) // 2][0]
        def mm(j):
            # tolerant of a row tuple shorter than j: a sensor that is not in this store yields an
            # empty envelope rather than an IndexError deep inside the binning loop
            vals = [c[j] for c in chunk if len(c) > j and c[j] is not None]
            return (min(vals), max(vals)) if vals else (None, None)
        plo, phi = mm(1)
        tlo, thi = mm(2)
        slo, shi = mm(3)                          # PSU-0 input power
        out.append((ts, plo, phi, tlo, thi, slo, shi))
    return out, width


def _r_node_sensor_trace(facts, store, t, policies):
    """One node's RAW power and temperature at native 20-second sampling, onset marked.

    Node choice, in order:
      1. a node with a recorded onset INSIDE the report window that also has raw coverage -- the
         case this chart exists for, and the only case that gets a marker;
      2. otherwise the highest-scoring watch-list node that has raw coverage, drawn with NO marker
         and a subtitle saying plainly that no onset occurred and this is a healthy baseline.
    A marker is never drawn where no event was recorded.
    """
    cid = ChartId.NODE_SENSOR_TRACE
    if store is None or not getattr(store, "has_node_raw", lambda: False)():
        return ChartResult.unavailable(cid, "no_store",
                                       "native-resolution traces need the node_raw table; this "
                                       "store does not carry it (run build_node_raw.py)")
    covered = store.node_raw_nodes()
    if not covered:
        return ChartResult.unavailable(cid, "no_rows", "node_raw is present but empty")

    w = facts.get("window") or {}
    lo_w, hi_w = int(w.get("start_ts", 0)), int(w.get("end_ts", t))
    span = int(TRACE_SPAN_H * 3600)

    # ---- 1. an onset inside the window, on a node we have raw data for -------------------------
    onset_ts = onset_node = None
    for ev in (facts.get("node_onsets") or {}).get("events_full") or []:
        if int(ev["node"]) in covered:
            onset_node, onset_ts = int(ev["node"]), int(ev["ts"])
            break
    if onset_node is None:
        # events_full is the unabridged list; fall back to querying the store directly
        try:
            ons = store._df("SELECT node, ts FROM node_events WHERE ts>? AND ts<=? ORDER BY ts",
                            (lo_w, hi_w))
            for r in ons.itertuples(index=False):
                if int(r.node) in covered:
                    onset_node, onset_ts = int(r.node), int(r.ts)
                    break
        except Exception:
            pass

    # Candidates in preference order. Coverage is per-node AND per-span -- an onset node's rows only
    # exist around its own onset -- so "is this node in the roster" is not enough; each candidate is
    # tried until one actually returns rows, rather than committing to the first and giving up.
    cands = []
    if onset_node is not None:
        cands.append((onset_node, onset_ts - span, onset_ts + span, onset_ts))
    for n in sorted((n for n in (facts.get("high_risk_nodes") or [])
                     if isinstance(n, dict) and n.get("risk_pct") is not None
                     and int(n["node"]) in covered),
                    key=lambda n: -float(n["risk_pct"])):
        cands.append((int(n["node"]), int(t) - 2 * span, int(t), None))
    if not cands:
        return ChartResult.unavailable(
            cid, "no_rows",
            f"no onset node in this window has a native-resolution trace, and none of the "
            f"watch-list nodes is in the {len(covered)}-node raw roster")

    node_id = lo = hi = df = None
    for cand_node, cand_lo, cand_hi, cand_onset in cands:
        got = store.node_raw_trace(cand_node, cand_lo, cand_hi)
        if got is not None and len(got):
            node_id, lo, hi, df = cand_node, cand_lo, cand_hi, got
            onset_ts = cand_onset          # None for a baseline candidate: NO marker will be drawn
            onset_node = cand_node if cand_onset is not None else None
            break
    if df is None:
        return ChartResult.unavailable(
            cid, "no_rows",
            f"none of the {len(cands)} candidate node(s) has raw sensor rows inside this window's "
            f"span; the raw roster covers each node only around its own onset")

    def _v(x):
        return None if x is None or x != x else float(x)     # NaN-safe
    # PSU-0 input power leads, because it is the quantity the P2 triage actually runs on. It was
    # unavailable at native resolution until ps0_input_power was located in the source tar (see
    # build_node_raw.py); a store built before that has the column as NULL and this falls back to
    # total power, which is what the chart used to plot on its own.
    rows = [(int(r.ts), _v(r.power_w), _v(r.temp_c), _v(getattr(r, "ps0_w", None)))
            for r in df.itertuples(index=False)]

    # flat-line guard: a constant trace across the whole span shows nothing
    pw = [x[1] for x in rows if x[1] is not None]
    tc = [x[2] for x in rows if x[2] is not None]
    ps = [x[3] for x in rows if x[3] is not None]
    have_ps0 = bool(ps) and (max(ps) - min(ps) > TRACE_FLAT_EPS)
    if not pw and not tc and not ps:
        return ChartResult.unavailable(cid, "no_rows",
                                       f"node{node_id:04d} has rows but no sensor values in this span")
    flat_p = (not pw) or (max(pw) - min(pw) <= TRACE_FLAT_EPS)
    flat_t = (not tc) or (max(tc) - min(tc) <= TRACE_FLAT_EPS)
    if flat_p and flat_t and not have_ps0:
        return ChartResult.unavailable(
            cid, "degenerate",
            f"node{node_id:04d}'s power and temperature are both constant across the whole span, "
            f"so the trace is a flat line and shows nothing")

    # ---- resolution: native if it fits, otherwise a min/max envelope ----------------------------
    native_dt = 20
    if len(rows) > 1:
        deltas = sorted(rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1))
        native_dt = max(1, deltas[len(deltas) // 2])
    # SERIES ORDER IS THE ARGUMENT THIS CHART MAKES. PSU-0 input power is the feature the P2 triage
    # runs on and the one genuinely predictive node-level signal in this project, so it leads and is
    # drawn heaviest. Total node power stays alongside it as context on the SAME axis -- both are
    # watts, so they are directly comparable and a second axis for them would be a lie. Temperature
    # keeps the right axis. When ps0 is absent (a store written by the earlier two-metric builder)
    # total power leads exactly as it did before, and the subtitle says which one is being read.
    enveloped = len(rows) > MAX_TRACE_POINTS
    if enveloped:
        binned, width = _bin_envelope(rows, MAX_TRACE_POINTS)
        stamps = [b[0] for b in binned]
        datasets = []
        if have_ps0:
            datasets += [
                {"legend": _lbl_key("chart.trace.ps0Max"), "data": [b[6] for b in binned],
                 "color": C_BAD, "axis": "left",
                 "style": {"line_width": 2.4, "point_radius": 0, "tension": 0, "order": 1}},
                {"legend": _lbl_key("chart.trace.ps0Min"), "data": [b[5] for b in binned],
                 "color": C_BAD, "axis": "left",
                 "style": {"line_width": 1.0, "point_radius": 0, "tension": 0, "order": 2,
                           "dash": [3, 3]}},
            ]
        datasets += [
            {"legend": _lbl_key("chart.trace.powerMax"), "data": [b[2] for b in binned],
             "color": C_MUTED if have_ps0 else C_BAD, "axis": "left",
             "style": {"line_width": 1.6 if have_ps0 else 2.2, "point_radius": 0, "tension": 0,
                       "order": 3}},
            {"legend": _lbl_key("chart.trace.powerMin"), "data": [b[1] for b in binned],
             "color": C_MUTED if have_ps0 else C_BAD, "axis": "left",
             "style": {"line_width": 0.8, "point_radius": 0, "tension": 0, "order": 4,
                       "dash": [3, 3]}},
            {"legend": _lbl_key("chart.trace.tempMax"), "data": [b[4] for b in binned],
             "color": C_INFO, "axis": "right",
             "style": {"line_width": 1.4, "point_radius": 0, "tension": 0, "order": 5}},
            {"legend": _lbl_key("chart.trace.tempMin"), "data": [b[3] for b in binned],
             "color": C_INFO, "axis": "right",
             "style": {"line_width": 0.8, "point_radius": 0, "tension": 0, "order": 6,
                       "dash": [3, 3]}},
        ]
        res_en = (f"{width * native_dt}-second bins, min/max envelope "
                  f"(from {native_dt}s native samples — not averaged)")
        res_zh = f"{width * native_dt} 秒分箱，保留每箱最小/最大值（原始取樣 {native_dt} 秒，未取平均）"
    else:
        stamps = [r[0] for r in rows]
        datasets = []
        if have_ps0:
            datasets.append(
                {"legend": _lbl_key("chart.trace.ps0"), "data": [r[3] for r in rows],
                 "color": C_BAD, "axis": "left",
                 "style": {"line_width": 2.4, "point_radius": 0, "tension": 0, "order": 1}})
        datasets += [
            {"legend": _lbl_key("chart.trace.power"), "data": [r[1] for r in rows],
             "color": C_MUTED if have_ps0 else C_BAD, "axis": "left",
             "style": {"line_width": 1.6 if have_ps0 else 2.2, "point_radius": 0, "tension": 0,
                       "order": 2}},
            {"legend": _lbl_key("chart.trace.temp"), "data": [r[2] for r in rows],
             "color": C_INFO, "axis": "right",
             "style": {"line_width": 1.2, "point_radius": 0, "tension": 0, "order": 3}},
        ]
        res_en = f"{native_dt}-second native resolution, not resampled"
        res_zh = f"{native_dt} 秒原始解析度，未重新取樣"

    # ---- the onset marker sits on the TRUE timestamp, or is absent -------------------------------
    # A NODE OFTEN STOPS REPORTING BEFORE ITS OWN ONSET -- that dropout is the P2 signal, not a data
    # error (node0038's last sample is 8m20s before its recorded onset). Snapping the marker to the
    # nearest existing sample would quietly move the event to whenever the node last spoke, which is
    # a false statement about when it happened. Instead the axis is EXTENDED to the onset with empty
    # slots, so the line visibly stops, the gap is the story, and the marker lands on the true time.
    marker = None
    gap_note_en = gap_note_zh = ""
    if onset_node is not None and onset_ts is not None:
        step = max(1, (width * native_dt) if enveloped else native_dt)
        if onset_ts > stamps[-1]:
            missing = int((onset_ts - stamps[-1]) // step)
            if missing <= MAX_TRACE_POINTS // 4:          # bounded: never inflate the series
                for _k in range(missing):
                    stamps.append(stamps[-1] + step)
                    for d in datasets:
                        d["data"].append(None)
            silent = onset_ts - rows[-1][0]
            gap_note_en = (f" The node stopped reporting {silent // 60}m{silent % 60:02d}s before "
                           f"the onset — the gap before the marker is that silence.")
            gap_note_zh = (f" 該節點在 onset 前 {silent // 60} 分 {silent % 60:02d} 秒即停止回報，"
                           f"標記前的空白即為該段靜默。")
        idx = min(range(len(stamps)), key=lambda i: abs(stamps[i] - onset_ts))
        marker = {"index": idx, "ts": onset_ts, "label": _lbl_key("chart.trace.onset"),
                  "offset_s": int(abs(stamps[idx] - onset_ts))}

    labels = [_lbl_text(_hms(s)) for s in stamps]

    node_label = f"node{node_id:04d}"
    # THE POWER SENTENCE. This used to state that the raw extract carried no PSU input-power metric,
    # so the trace plotted total node power in its place. That was true of the EXTRACTED DIRECTORY
    # and false of the source: ps0_input_power was in the tar all along, simply never unpacked --
    # build_node_raw.py documents the trail from the feature name cur_ps0_inputP back to it. The
    # subtitle now names whichever series is actually leading rather than apologising for one that
    # no longer needs an apology, and it still says so plainly when a store predates the column.
    if have_ps0:
        power_en = ("The heavy red series is PSU-0 input power at native resolution — the feature "
                    "the P2 triage actually runs on; total node power is drawn behind it for "
                    "context, on the same watt axis.")
        power_zh = ("粗紅線為 PSU-0 輸入功率（原始解析度），即 P2 分流實際使用的特徵；"
                    "節點總功耗以淡色線作為對照，共用相同瓦特座標。")
    else:
        power_en = ("Power here is total node power, not the PSU-0 triage indicator: this store's "
                    "node_raw predates the PSU series and carries no ps0_w column.")
        power_zh = ("此處功耗為節點總功耗，非 PSU-0 分流指標："
                    "本資料庫的 node_raw 建置於 PSU 序列之前，未含 ps0_w 欄位。")
    if onset_node is not None:
        sub = _lbl_bi(
            f"{node_label} · anomaly onset at {_hms(onset_ts)} (marked) · ±{TRACE_SPAN_H:.0f}h · "
            f"{res_en}.{gap_note_en} {power_en}",
            f"{node_label} · 異常 onset 於 {_hms(onset_ts)}（已標記）· ±{TRACE_SPAN_H:.0f} 小時 · "
            f"{res_zh}。{gap_note_zh} {power_zh}")
    else:
        sub = _lbl_bi(
            f"{node_label} · NO onset occurred in this window — this is a healthy node's baseline "
            f"trace, shown with no marker · {res_en}. {power_en}",
            f"{node_label} · 本視窗內未發生 onset — 此為健康節點的基線軌跡，未標記任何事件 · "
            f"{res_zh}。{power_zh}")

    return ChartResult.ok(cid, {
        "chart_id": cid.value,
        "type": "line",
        "width": LAYOUT[cid],
        "title": _lbl_key("chart.trace.title.onset" if onset_node is not None
                          else "chart.trace.title.baseline"),
        "title_suffix": f" — {node_label}",
        "subtitle": sub,
        "labels": labels,
        "datasets": datasets,
        # two y-axes with units rather than normalising: watts and °C are not comparable numbers.
        # BOTH power series share the left axis on purpose -- PSU-0 input and node total are both
        # watts, and giving them separate scales would let the reader compare two shapes that are
        # not to the same size.
        "dual_axis": {"left":  {"label": _lbl_key("chart.trace.axisPower"), "unit": "W"},
                      "right": {"label": _lbl_key("chart.trace.axisTemp"),  "unit": "°C"}},
        "axis_x": _lbl_key("chart.trace.axisTime"),
        "x_marker": marker,
        "value_suffix": "",
        "subject_node": node_id,
        "onset_ts": onset_ts,
        "enveloped": enveloped,
        "native_dt": native_dt,
        "point_count": len(stamps),
        # which power signal is actually leading, so a test (or a reader) never has to infer it
        "primary_power": "ps0_input" if have_ps0 else "total",
    })


def _r_node_feature_contributions(facts, store, t, policies):
    """Per-prediction feature contributions for the single highest-risk flagged node.

    This reuses the drill-down path exactly -- store.node_detail() is the same query the node panel
    calls, so the chart and the panel can never disagree. Nothing is recomputed here.
    """
    rows = [n for n in (facts.get("high_risk_nodes") or [])
            if isinstance(n, dict) and n.get("risk_pct") is not None]
    if not rows:
        return ChartResult.unavailable(ChartId.NODE_FEATURE_CONTRIBUTIONS, "no_rows",
                                       "no scored node to explain at this time")
    if store is None:
        return ChartResult.unavailable(ChartId.NODE_FEATURE_CONTRIBUTIONS, "no_store",
                                       "per-prediction contributions need the store; none was given")

    top_node = max(rows, key=lambda n: float(n["risk_pct"]))
    node_id = int(top_node["node"])
    try:
        detail = store.node_detail(node_id, int(t), policies)
    except Exception as e:                       # a chart must never take the report down with it
        return ChartResult.unavailable(ChartId.NODE_FEATURE_CONTRIBUTIONS, "no_rows",
                                       f"node detail lookup failed ({type(e).__name__})")
    if not detail or not detail.get("why"):
        return ChartResult.unavailable(ChartId.NODE_FEATURE_CONTRIBUTIONS, "no_rows",
                                       f"no stored per-prediction contributions for node{node_id:04d}")

    why = [w for w in detail["why"] if w.get("contribution") is not None]
    why = sorted(why, key=lambda w: abs(float(w["contribution"])), reverse=True)[:MAX_CONTRIB_BARS]
    if not why or all(float(w["contribution"]) == 0 for w in why):
        return ChartResult.unavailable(
            ChartId.NODE_FEATURE_CONTRIBUTIONS, "degenerate",
            f"every stored contribution for node{node_id:04d} is zero, so the chart would show no "
            f"reason at all")

    # signed: plotted smallest-first so the bar chart reads top-down from strongest positive
    why = sorted(why, key=lambda w: float(w["contribution"]))
    vals = [round(float(w["contribution"]), 4) for w in why]
    node_label = detail.get("node_label") or f"node{node_id:04d}"

    # THE TITLE MUST NOT ASSERT WHAT THE FACTS DENY.
    # This chart explains the highest-SCORING node, which on a quiet shift is a perfectly healthy
    # one. The old title said "Why this node is flagged" unconditionally, so a report stating "0
    # nodes flagged" sat directly above a panel claiming node0109 was flagged at a 1.3% score. The
    # chart is still worth drawing -- the strongest signal on a calm shift is useful -- but it has
    # to say which question it is answering. The node's real state and score go in the subtitle so
    # the reader can see the claim and its evidence together.
    out = detail.get("output") or {}
    state = str(detail.get("state") or "HEALTHY")
    score_pct = out.get("risk_pct")
    is_flagged = bool(out.get("flagged")) or state == "CRITICAL"
    alert_pct = round(float(out.get("threshold", 0)) * 100.0, 1)
    return ChartResult.ok(ChartId.NODE_FEATURE_CONTRIBUTIONS, {
        "chart_id": ChartId.NODE_FEATURE_CONTRIBUTIONS.value,
        "type": "signed_hbar",
        "width": LAYOUT[ChartId.NODE_FEATURE_CONTRIBUTIONS],
        "title": _lbl_key("chart.nodeContrib.title.flagged" if is_flagged
                          else "chart.nodeContrib.title.topScoring"),
        "title_suffix": f" — {node_label}",
        # the subtitle carries the node's ACTUAL state and score, so the panel is self-checking
        "subtitle": _lbl_bi(
            f"{state} · P2 score {score_pct}% vs a {alert_pct}% alert threshold · "
            f"per-prediction contributions (log-odds)",
            f"{state} · P2 分數 {score_pct}%，告警門檻 {alert_pct}% · "
            f"該次預測的特徵貢獻 (log-odds)"),
        "node_state": state,
        "node_flagged": is_flagged,
        "labels": [_lbl_bi(w.get("label_en") or w["feature"], w.get("label_zh") or w["feature"])
                   for w in why],
        "datasets": [{"legend": _lbl_key("chart.legend.contribution"), "data": vals,
                      "colors": [(C_BAD if v > 0 else C_INFO) for v in vals]}],
        "point_notes": [_lbl_key("d.increases") if float(w["contribution"]) > 0
                        else _lbl_key("d.decreases") for w in why],
        "axis_x": _lbl_key("chart.axis.logOdds"),
        # the zero line is drawn but NOT labelled: the value axis already prints a 0 tick right
        # under it, so the word only overlapped the axis and repeated what the tick said
        "reference_line": {"value": 0, "label": None},
        "value_suffix": "",
        "subject_node": node_id,
    })


RENDERERS = {
    ChartId.PREDICTION_OUTCOMES:        _r_prediction_outcomes,
    ChartId.JOB_OUTCOME_MIX:            _r_job_outcome_mix,
    ChartId.FAILURES_OVER_TIME_BARS:    _r_failures_over_time_bars,
    ChartId.FAILURES_OVER_TIME_LINES:   _r_failures_over_time_lines,
    ChartId.TOP_FLAGGED_JOBS:           _r_top_flagged_jobs,
    ChartId.NODE_RISK_WATCH:            _r_node_risk_watch,
    ChartId.NODE_SENSOR_TRACE:          _r_node_sensor_trace,
    ChartId.NODE_FEATURE_CONTRIBUTIONS: _r_node_feature_contributions,
}

# The order charts appear in when Python, not the agent, is choosing. The two failures-over-time
# charts are adjacent on purpose: they plot identical numbers two ways, and the comparison only
# works if a reader can see both without scrolling past something else.
DEFAULT_ORDER = [ChartId.PREDICTION_OUTCOMES, ChartId.JOB_OUTCOME_MIX,
                 ChartId.FAILURES_OVER_TIME_BARS, ChartId.FAILURES_OVER_TIME_LINES,
                 ChartId.TOP_FLAGGED_JOBS, ChartId.NODE_RISK_WATCH,
                 ChartId.NODE_SENSOR_TRACE, ChartId.NODE_FEATURE_CONTRIBUTIONS]


# ============================================================ public helpers
def coerce_id(raw):
    """A value the agent sent -> ChartId, or None if it is not one of ours."""
    if isinstance(raw, ChartId):
        return raw
    try:
        return ChartId(str(raw).strip().lower())
    except (ValueError, AttributeError):
        return None


def render(chart_id, facts, store, t, policies) -> ChartResult:
    """Render one chart. Unknown ids cannot reach here -- select_charts filters them first."""
    cid = coerce_id(chart_id)
    if cid is None:
        return ChartResult.unavailable(str(chart_id), "unknown_id", "not a known chart id")
    try:
        return RENDERERS[cid](facts, store, t, policies)
    except Exception as e:
        # A broken renderer must degrade to "unavailable", never to a 500 on the whole report.
        return ChartResult.unavailable(cid, "no_rows", f"renderer failed ({type(e).__name__}: {e})")


# ============================================================ Python-written captions
# Used for the template fallback (no agent involved) and for the auto-appended contributions chart.
# These are generated from the facts, so they carry real numbers and never pass through the numeric
# gate -- the gate exists to police the MODEL's text, and no model wrote these.
def default_caption(chart_id, facts, lang, chart=None) -> str:
    en = lang != "zh"
    cid = coerce_id(chart_id)
    po = facts.get("prediction_outcomes") or {}
    jw = facts.get("jobs_window") or {}
    def cnt(b):
        d = po.get(b)
        return int(d.get("count", 0)) if isinstance(d, dict) else 0

    if cid is ChartId.PREDICTION_OUTCOMES:
        # the ring and the footnote are two different cohorts, so the sentence separates them too
        return (f"The ring is the flagged cohort only: of {po.get('flagged_total', 0)} jobs flagged "
                f"in this window, {cnt('correct_warnings')} have since failed, "
                f"{cnt('false_alarms')} completed and {cnt('pending_outcome')} are still running. "
                f"Separately, {cnt('misses')} failures were never flagged at all."
                if en else
                f"環圖僅涵蓋「已標記」的任務：本視窗內被標記的 {po.get('flagged_total', 0)} 個任務中，"
                f"{cnt('correct_warnings')} 個已失敗、{cnt('false_alarms')} 個已完成、"
                f"{cnt('pending_outcome')} 個仍在執行。另有 {cnt('misses')} 次失敗從未被標記，"
                f"不屬於環圖範圍。")

    if cid is ChartId.JOB_OUTCOME_MIX:
        return (f"{jw.get('ended_in_window', 0)} jobs ended inside this window, whenever they were "
                f"submitted."
                if en else
                f"本視窗內共有 {jw.get('ended_in_window', 0)} 個任務結束（提交時間不限）。")

    if cid is ChartId.TOP_FLAGGED_JOBS:
        n = len(((chart or {}).get("datasets") or [{}])[0].get("data", []))
        thr = (facts.get("settings") or {}).get("p3_alert_threshold")
        conc = facts.get("high_risk_jobs_concentration") or {}
        users = conc.get("distinct_users")
        tail_en = tail_zh = ""
        # the concentration is the interesting fact here: the bars are near-identical in height, so
        # "who owns them" is what the reader should take away
        if users == 1 and conc.get("top_user"):
            tail_en = f" All of them belong to {conc['top_user']}."
            tail_zh = f" 全部屬於同一位使用者 {conc['top_user']}。"
        elif users:
            tail_en = (f" They span {users} users"
                       + (f", with {conc['top_user']} holding {conc.get('top_user_share_pct')}%."
                          if conc.get("top_user") else "."))
            tail_zh = (f" 共涉及 {users} 位使用者"
                       + (f"，其中 {conc['top_user']} 占 {conc.get('top_user_share_pct')}%。"
                          if conc.get("top_user") else "。"))
        return ((f"The {n} highest-risk in-flight jobs at or above the {thr} alert threshold, "
                 f"coloured by predicted failure type." + tail_en)
                if en else
                (f"目前在執行/排隊中、風險達 {thr} 告警門檻的前 {n} 個任務，依預測故障型態上色。" + tail_zh))

    if cid in (ChartId.FAILURES_OVER_TIME_BARS, ChartId.FAILURES_OVER_TIME_LINES):
        c = chart or {}
        nb = c.get("bucket_count", 0)
        tot = sum(sum(ds.get("data", [])) for ds in c.get("datasets", [])
                  if c.get("chart_id") == ChartId.FAILURES_OVER_TIME_BARS.value)
        if c.get("chart_id") == ChartId.FAILURES_OVER_TIME_LINES.value:
            tot = sum((c.get("datasets") or [{}])[0].get("data", []))
        shape = ("Stacked by outcome type." if cid is ChartId.FAILURES_OVER_TIME_BARS
                 else "One line per outcome type, with the total drawn heaviest.")
        shape_zh = ("依結束型態堆疊。" if cid is ChartId.FAILURES_OVER_TIME_BARS
                    else "每種結束型態一條線，總計線最粗。")
        return ((f"{tot} failures across {nb} fifteen-minute buckets, by the time the job ended. "
                 f"{shape}")
                if en else
                f"{nb} 個 15 分鐘時段內共 {tot} 次失敗（以任務結束時間計）。{shape_zh}")

    if cid is ChartId.NODE_RISK_WATCH:
        n = len(((chart or {}).get("datasets") or [{}])[0].get("data", []))
        return (f"{n} watch-list nodes against the node alert threshold."
                if en else
                f"{n} 個關注中節點與節點告警門檻的比較。")

    if cid is ChartId.NODE_SENSOR_TRACE:
        c = chart or {}
        node = c.get("subject_node")
        label = f"node{int(node):04d}" if node is not None else "this node"
        dt = c.get("native_dt", 20)
        n = len(c.get("labels") or [])
        if c.get("onset_ts"):
            return (f"What {label} physically did around its anomaly onset: total power and GPU "
                    f"temperature at the native {dt}-second sampling rate ({n} points), with the "
                    f"onset marked at its true timestamp."
                    if en else
                    f"{label} 在異常 onset 前後的實際行為：以原始 {dt} 秒取樣率呈現總功耗與 "
                    f"GPU 溫度（共 {n} 點），並在真實時間點標記 onset。")
        return (f"No onset occurred in this window. This is {label}'s baseline: total power and GPU "
                f"temperature at the native {dt}-second sampling rate ({n} points), with no event "
                f"marker because no event was recorded."
                if en else
                f"本視窗內未發生 onset。此為 {label} 的基線：以原始 {dt} 秒取樣率呈現總功耗與 GPU "
                f"溫度（共 {n} 點）；因無事件記錄，故不標記任何事件。")

    if cid is ChartId.NODE_FEATURE_CONTRIBUTIONS:
        c = chart or {}
        node = c.get("subject_node")
        label = f"node{int(node):04d}" if node is not None else "this node"
        # the caption tracks the same flagged/not-flagged distinction as the title, so a quiet
        # shift never reads as though a healthy node had tripped the alert
        if c.get("node_flagged"):
            return (f"Why {label} is flagged: the signed per-prediction feature contributions "
                    f"behind its score, in log-odds. Positive bars push the score up."
                    if en else
                    f"{label} 被標記的原因：該次預測各特徵的帶號貢獻（log-odds）。正值代表推高風險。")
        return (f"No node is flagged right now. {label} simply carries the highest P2 score, and "
                f"these are the signed per-prediction contributions behind it, in log-odds. "
                f"Positive bars push the score up."
                if en else
                f"目前沒有節點被標記。{label} 只是 P2 分數最高者，以下為該次預測各特徵的帶號貢獻"
                f"（log-odds）。正值代表推高風險。")

    return ""


# ================================================================================================
# MODEL-LEVEL IMPORTANCE -- a STATIC panel, deliberately outside the agent-visible enum.
#
# This is not a shift chart. It does not change with the virtual clock, it does not describe
# anything that happened tonight, and the agent has no business selecting it, so it is not a
# ChartId. It is served on /api/model_info and drawn once in the model-info view.
#
# THE DISTINCTION THAT MATTERS, because these two charts look almost identical:
#
#   node_feature_contributions  -- ONE prediction, ONE node, ONE 15-minute slot. Signed log-odds
#                                  contributions explaining why THAT score came out as it did.
#                                  Changes every slot. Answers "why is this node flagged right now".
#   model importance (here)     -- the MODEL as a whole, measured across the entire test set.
#                                  Unsigned, one number per feature, fixed. Answers "what does this
#                                  model rely on in general". Says nothing about any single job or
#                                  node.
#
# The panel text states this in both languages, and the metric is named rather than left as a bare
# number, because "importance 0.0342" means nothing without knowing what was measured.
# ================================================================================================
def model_importance_panels(store) -> dict:
    """-> {"panels": [...], "unavailable": [...]}, both keyed by model id.

    A panel is only produced for a model whose global importance is actually STORED in meta.json.
    Where it is not, the model is reported as unavailable with the reason, rather than being filled
    in from something that merely resembles a global importance.
    """
    panels, unavailable = [], []
    meta = getattr(store, "meta", None) or {}

    def _feature_label(name):
        """Human (en, zh) for a model feature, via the same resolver the drill-down uses."""
        try:
            import feature_labels
            return feature_labels.label(name)
        except Exception:
            return (name, name)

    # ---- P3: stored. precompute.py computes it and meta.json carries it. -----------------------
    p3 = meta.get("p3_global_importance") or []
    labels_p3 = {}
    try:
        from models import P3_LABELS
        labels_p3 = P3_LABELS
    except Exception:
        pass
    if p3:
        rows = sorted(p3, key=lambda d: float(d.get("importance", 0)),
                      reverse=True)[:MAX_IMPORTANCE_BARS]
        rows = list(reversed(rows))               # largest at the top of a horizontal bar
        vals = [round(float(r["importance"]), 4) for r in rows]
        panels.append({
            "chart_id": "model_importance_p3",
            "model_id": "P3",
            "type": "hbar",
            "width": WIDTH_FULL,
            "title": _lbl_key("mi.importance.p3.title"),
            "subtitle": _lbl_key("mi.importance.p3.sub"),
            "scope_note": _lbl_key("mi.importance.scope"),
            "contrast_note": _lbl_key("mi.importance.vsPerPrediction"),
            "metric_note": _lbl_key("mi.importance.p3.metric"),
            "labels": [_lbl_bi(*(labels_p3[r["feature"]][:2] if r["feature"] in labels_p3
                                 else _feature_label(r["feature"]))) for r in rows],
            "datasets": [{"legend": _lbl_key("mi.importance.legend"), "data": vals,
                          "colors": [C_INFO] * len(vals)}],
            "axis_x": _lbl_key("mi.importance.p3.axis"),
            "value_suffix": "",
        })
    else:
        unavailable.append({"model_id": "P3", "code": "not_stored",
                            "reason": "meta.json carries no p3_global_importance"})

    # ---- P2: LightGBM native GAIN, exported by export_p2_importance.py --------------------------
    # A DIFFERENT METRIC from P3's, deliberately, because it is the one the model gives for free:
    # the trained booster already carries gain, so this cost 0.2 ms rather than a permutation pass
    # over the test set. The panels therefore measure different things -- P3 how much ACCURACY
    # depends on a feature, P2 how much the fit USED it -- and the UI states each metric on its own
    # panel and warns that the two are not comparable, rather than letting them sit side by side as
    # if they were one measurement.
    #
    # Still refused: averaging node_slots.contrib_json into a pseudo-importance. That would be a
    # dataset-conditional average of LOCAL attributions over a sample biased towards anomalous nodes
    # (the slim edition thins quiet nodes to hourly), not a global importance.
    p2 = meta.get("p2_global_importance") or []
    if p2:
        rows = sorted(p2, key=lambda d: float(d.get("importance", 0)),
                      reverse=True)[:MAX_IMPORTANCE_BARS]
        rows = list(reversed(rows))
        vals = [round(float(r["importance"]), 1) for r in rows]
        panels.append({
            "chart_id": "model_importance_p2",
            "model_id": "P2",
            "type": "hbar",
            "width": WIDTH_FULL,
            "title": _lbl_key("mi.importance.p2.title"),
            "subtitle": _lbl_key("mi.importance.p2.sub"),
            "scope_note": _lbl_key("mi.importance.scope"),
            "contrast_note": _lbl_key("mi.importance.vsPerPrediction"),
            "metric_note": _lbl_key("mi.importance.p2.metric"),
            # the same resolver the drill-down uses, so `fleetmax_nz_g0_cT` never reaches a screen
            "labels": [_lbl_bi(*_feature_label(r["feature"])) for r in rows],
            "datasets": [{"legend": _lbl_key("mi.importance.p2.legend"), "data": vals,
                          "colors": [C_WARN] * len(vals)}],
            "axis_x": _lbl_key("mi.importance.p2.axis"),
            "point_notes": [_lbl_text(f"{r.get('share_pct', 0)}% of total gain") for r in rows],
            "value_suffix": "",
        })
    else:
        unavailable.append({
            "model_id": "P2", "code": "not_stored",
            "reason": "no stored global importance for the node model; run "
                      "export_p2_importance.py to add p2_global_importance to meta.json"})

    # the two panels do not measure the same thing, and the UI must not imply otherwise
    return {"panels": panels, "unavailable": unavailable,
            "comparable": False,
            "incomparable_note": _lbl_key("mi.importance.incomparable")}
