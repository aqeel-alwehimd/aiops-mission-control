"""
verify_labels.py -- every model feature has a human label, and the contributions panel never
claims a node is flagged when it is not. Exits; no server, no network.

Run:  python verify_labels.py

THIS FILE IS THE TRIPWIRE.
The chosen answer to "a missing mapping should be visible during development rather than silently
falling through to the raw name" is a build failure, not a runtime warning: if a feature the model
can surface has no label, this suite fails. A log line would scroll past; a red test does not.
(FEATURE_LABEL_STRICT=1 additionally makes feature_labels.label() raise rather than fall back --
this suite sets it, so a fallthrough anywhere in the covered paths is an exception.)
"""
import json, os, sys, collections

os.environ["REPORT_AUDIT"] = "0"
os.environ["FEATURE_LABEL_STRICT"] = "1"     # a fallthrough must raise, not degrade, in tests
import feature_labels as fl
from models import Store, P3_LABELS

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))

store = Store()
P2 = store.meta["p2_feats"]
P3 = store.meta["p3_input_feats"]

# ============================================================ 1. total coverage
print("=" * 100)
print("1. every feature either model can surface has a label")
print("=" * 100)
cov, tot, miss = fl.coverage(P2)
print(f"  P2 node model : {cov}/{tot} labelled")
if miss:
    print(f"  MISSING       : {miss}")
check("P2: no feature falls through to its raw identifier", not miss, str(miss[:8]))
p3_miss = [f for f in P3 if f not in P3_LABELS]
print(f"  P3 job model  : {len(P3) - len(p3_miss)}/{len(P3)} labelled")
check("P3: no feature falls through to its raw identifier", not p3_miss, str(p3_miss))

# strict mode really does raise, so the tripwire cannot be silently disarmed
try:
    fl.label("totally_unknown_sensor_xyz")
    check("FEATURE_LABEL_STRICT makes an unmapped feature raise", False, "it returned instead")
except KeyError as e:
    check("FEATURE_LABEL_STRICT makes an unmapped feature raise", True)
    check("   the error says how to fix it",
          "BASE_SENSORS" in str(e) and "STATISTICS" in str(e))

# ============================================================ 2. labels are DISTINCT
print()
print("=" * 100)
print("2. distinct features get distinct labels (the 'duplicate feature' report)")
print("=" * 100)
en = [fl.label(f)[0] for f in P2]
zh = [fl.label(f)[1] for f in P2]
dup_en = {k: v for k, v in collections.Counter(en).items() if v > 1}
dup_zh = {k: v for k, v in collections.Counter(zh).items() if v > 1}
print(f"  EN distinct: {len(set(en))}/{len(en)}   ZH distinct: {len(set(zh))}/{len(zh)}")
check("no two P2 features share an English label", not dup_en, str(dup_en))
check("no two P2 features share a Chinese label", not dup_zh, str(dup_zh))

# the exact pairs that were reported as duplicates
print()
print("  the reported 'duplicates', checked against the model's own feature list:")
for a, b in (("s30m_p0_vddT", "slope30m_p0_vddT"), ("slope30m_totP", "slope30m_p0P")):
    ia, ib = P2.index(a), P2.index(b)
    la, lb = fl.label(a)[0], fl.label(b)[0]
    print(f"    {a:20} idx {ia:>3}  ->  {la}")
    print(f"    {b:20} idx {ib:>3}  ->  {lb}")
    check(f"{a} and {b} are genuinely different model features", ia != ib, f"{ia} vs {ib}")
    check(f"   ...and their labels now distinguish them", la != lb)

check("the statistic is what differs for s30m_ vs slope30m_",
      "variability" in fl.label("s30m_p0_vddT")[0] and "trend" in fl.label("slope30m_p0_vddT")[0])
check("the sensor is what differs for totP vs p0P",
      "Total node power" in fl.label("slope30m_totP")[0]
      and "CPU0 power" in fl.label("slope30m_p0P")[0])
check("longest-prefix matching: slope30m_ never parses as s30m_",
      fl.split_feature("slope30m_p0_vddT")[0] == "slope30m_")

# ============================================================ 3. the reported raw identifiers
print()
print("=" * 100)
print("3. the identifiers seen raw in the rendered report now read as text")
print("=" * 100)
for f in ("fleetmax_nz_g0_cT", "cur_ps1_inputP", "slope30m_p0_vddT", "slope30m_p0P", "s30m_p0_vddT"):
    e, z = fl.label(f)
    print(f"  {f:20} | {e:46} | {z}")
    check(f"{f} is not shown as its raw name", e != f and z != f)

# ============================================================ 4. units survive only where valid
print()
print("=" * 100)
print("4. units appear only on statistics that preserve them")
print("=" * 100)
cases = [("cur_totP", True, "W"), ("m30m_totP", True, "W"), ("s30m_totP", True, "W"),
         ("rng6h_totP", True, "W"), ("lag2_totP", True, "W"),
         ("slope30m_totP", False, "W"), ("accel_totP", False, "W"),
         ("z7d_totP", False, "W"), ("z30d_g0_cT", False, "°C")]
for f, want_unit, unit in cases:
    lab = fl.label(f)[0]
    has = f"({unit})" in lab
    print(f"  {f:16} unit shown={str(has):5} (want {want_unit})   {lab}")
    check(f"{f}: unit {'kept' if want_unit else 'dropped'}", has == want_unit, lab)

# ============================================================ 5. both drill-down blocks agree
print()
print("=" * 100)
print("5. the INPUT and WHY blocks of one panel use the same resolver")
print("=" * 100)
ev = store._df("SELECT node, ts FROM node_events ORDER BY ts LIMIT 1")
node, ts = int(ev.iloc[0].node), int(ev.iloc[0].ts)
d = store.node_detail(node, ts, {"alert_threshold": 0.30, "node_filter_pct": 25})
check("node_detail returned", bool(d))
raw_in = [x for x in d["input"] if x["label_en"] == x["feature"]]
raw_why = [x for x in d["why"] if x["label_en"] == x["feature"]]
print(f"  INPUT rows showing a raw identifier: {len(raw_in)}/{len(d['input'])}")
print(f"  WHY   rows showing a raw identifier: {len(raw_why)}/{len(d['why'])}")
check("no INPUT row shows a raw identifier", not raw_in, str([x["feature"] for x in raw_in][:5]))
check("no WHY row shows a raw identifier", not raw_why, str([x["feature"] for x in raw_why][:5]))
shared = {x["feature"] for x in d["input"]} & {x["feature"] for x in d["why"]}
mismatched = [f for f in shared
              if next(x["label_en"] for x in d["input"] if x["feature"] == f)
              != next(x["label_en"] for x in d["why"] if x["feature"] == f)]
check("a feature in both blocks is labelled identically in both", not mismatched, str(mismatched))

# ============================================================ 6. the contributions title
print()
print("=" * 100)
print("6. the contributions panel never claims a node is flagged when it is not")
print("=" * 100)
import charts as chartreg
from charts import ChartId
from diagnose_guardrail import FACTS
POL = {"alert_threshold": 0.30, "node_filter_pct": 25}

class FakeStore:
    """node_detail for a node in a chosen state."""
    def __init__(self, flagged, state, risk_pct):
        self.flagged, self.state, self.risk_pct = flagged, state, risk_pct
    def node_detail(self, node_id, t, policies):
        return {"node_label": f"node{node_id:04d}", "state": self.state,
                "output": {"risk_pct": self.risk_pct, "threshold": 0.50,
                           "flagged": self.flagged, "onset": self.state == "CRITICAL"},
                "why": [{"feature": "cur_totP", "label_en": "Total node power (W)",
                         "label_zh": "節點總功耗 (W)", "value": 900.0, "contribution": 0.4},
                        {"feature": "slope30m_p0P", "label_en": "CPU0 power, 30-min trend",
                         "label_zh": "CPU0 功耗 30 分鐘趨勢", "value": -1.2, "contribution": -0.2}]}

# the exact reported case: 0 nodes flagged, node0109 HEALTHY at 1.3%
quiet = chartreg.render(ChartId.NODE_FEATURE_CONTRIBUTIONS,
                        {**FACTS, "high_risk_nodes": [{"node": 109, "rack": 5, "risk_pct": 1.3,
                                                       "state": "HEALTHY", "onset": False}]},
                        FakeStore(False, "HEALTHY", 1.3), 1, POL).chart
print(f"  not-flagged title key : {quiet['title']['key']}")
print(f"  subtitle (en)         : {quiet['subtitle']['en']}")
check("a healthy node does NOT get the 'is flagged' title",
      quiet["title"]["key"] == "chart.nodeContrib.title.topScoring")
check("the chart still renders (the top scorer is useful on a quiet shift)", bool(quiet["datasets"]))
check("the subtitle states the node's real state", "HEALTHY" in quiet["subtitle"]["en"])
check("   ...its real score", "1.3%" in quiet["subtitle"]["en"])
check("   ...and the threshold it is being compared against", "50.0%" in quiet["subtitle"]["en"])
check("the subtitle is bilingual", "HEALTHY" in quiet["subtitle"]["zh"]
      and "告警門檻" in quiet["subtitle"]["zh"])
cap = chartreg.default_caption(ChartId.NODE_FEATURE_CONTRIBUTIONS, FACTS, "en", quiet)
print(f"  caption (en)          : {cap[:96]}")
# substring-test carefully: the correct caption legitimately contains "is flagged" inside
# "No node is flagged right now". What must be absent is the CLAIM about this node.
check("the caption does not claim THIS node is flagged", "node0109 is flagged" not in cap
      and "Why node0109" not in cap, cap[:80])
check("   it says plainly that nothing is flagged", "No node is flagged" in cap)
capzh = chartreg.default_caption(ChartId.NODE_FEATURE_CONTRIBUTIONS, FACTS, "zh", quiet)
check("   and so does the Chinese caption", "目前沒有節點被標記" in capzh)

flagged = chartreg.render(ChartId.NODE_FEATURE_CONTRIBUTIONS,
                          {**FACTS, "high_risk_nodes": [{"node": 697, "rack": 34, "risk_pct": 74.0,
                                                         "state": "WARNING", "onset": False}]},
                          FakeStore(True, "WARNING", 74.0), 1, POL).chart
print(f"  flagged title key     : {flagged['title']['key']}")
check("a genuinely flagged node DOES get the 'is flagged' title",
      flagged["title"]["key"] == "chart.nodeContrib.title.flagged")
check("   its subtitle carries its state and score",
      "WARNING" in flagged["subtitle"]["en"] and "74.0%" in flagged["subtitle"]["en"])
check("   and its caption says flagged",
      "is flagged" in chartreg.default_caption(ChartId.NODE_FEATURE_CONTRIBUTIONS,
                                               FACTS, "en", flagged))
check("the zero reference line carries no label (the axis already prints 0)",
      quiet["reference_line"]["value"] == 0 and quiet["reference_line"]["label"] is None)

# ============================================================ 7. P2 global importance panel
print()
print("=" * 100)
print("7. the P2 importance panel: real metric, human labels, explicitly not comparable to P3")
print("=" * 100)
imp = chartreg.model_importance_panels(store)
ids = [p["chart_id"] for p in imp["panels"]]
print(f"  panels: {ids}")
check("both models now have an importance panel",
      {"model_importance_p3", "model_importance_p2"} <= set(ids), str(ids))
check("no model is left reported as not_stored", not imp["unavailable"], str(imp["unavailable"]))
p2 = next(p for p in imp["panels"] if p["chart_id"] == "model_importance_p2")
p3 = next(p for p in imp["panels"] if p["chart_id"] == "model_importance_p3")
print(f"  P2 top feature: {p2['labels'][-1]}")
check("P2 labels go through feature_labels, not raw identifiers",
      all(not l["en"].startswith(("fleet", "cur_", "z7d_", "slope")) for l in p2["labels"]),
      str([l["en"] for l in p2["labels"][-3:]]))
check("P2 labels are bilingual", all("en" in l and "zh" in l for l in p2["labels"]))
check("the two panels declare DIFFERENT metric keys",
      p2["metric_note"]["key"] != p3["metric_note"]["key"],
      f"{p2['metric_note']['key']} vs {p3['metric_note']['key']}")
check("the registry marks them as NOT comparable", imp.get("comparable") is False)
check("...and carries a note saying why", bool(imp.get("incomparable_note")))
check("P2 keeps the per-prediction contrast note",
      p2["contrast_note"]["key"] == "mi.importance.vsPerPrediction")
check("neither panel is in the agent-visible enum",
      all(i not in {c.value for c in ChartId} for i in ids))
check("the agent cannot select either by name",
      all(chartreg.coerce_id(i) is None for i in ids))
check("P2 bars carry their share of total gain as a note", bool(p2.get("point_notes")))
check("P2 importances are descending up the axis",
      p2["datasets"][0]["data"] == sorted(p2["datasets"][0]["data"]),
      str(p2["datasets"][0]["data"][:3]))

# ============================================================ 8. the raw sensor trace
print()
print("=" * 100)
print("8. node_sensor_trace: native resolution, honest marker, dual axes")
print("=" * 100)
import report as _rep
POL2 = {"alert_threshold": 0.30, "node_filter_pct": 25}
check("node_raw table is present", store.has_node_raw())
covered = store.node_raw_nodes()
print(f"  raw roster: {len(covered)} nodes {sorted(covered)}")
check("the roster is non-empty", bool(covered))

# a timestamp with an onset in the window -> marker
f_on = _rep.assemble_facts(store, 1664216861, POL2, 6)
r_on = chartreg.render(ChartId.NODE_SENSOR_TRACE, f_on, store, 1664216861, POL2)
check("renders when an onset node has coverage", r_on.available, str(r_on.reason))
c = r_on.chart
print(f"  node={c['subject_node']} pts={c['point_count']} dt={c['native_dt']}s "
      f"enveloped={c['enveloped']} marker_index={c['x_marker']['index']}")
check("plotted at the native 20-second cadence", c["native_dt"] == 20, str(c["native_dt"]))
check("not resampled at this span", c["enveloped"] is False)
check("the subtitle states the resolution actually plotted",
      "20-second native resolution" in c["subtitle"]["en"], c["subtitle"]["en"][:90])
check("the onset title is used", c["title"]["key"] == "chart.trace.title.onset")
check("a marker exists and sits on the TRUE onset timestamp",
      c["x_marker"] is not None and c["x_marker"]["ts"] == c["onset_ts"])
lbl_at_marker = c["labels"][c["x_marker"]["index"]]["text"]
from datetime import datetime, timezone
true_hms = datetime.fromtimestamp(c["onset_ts"], timezone.utc).strftime("%H:%M:%S")
print(f"  marker label {lbl_at_marker!r} vs true onset {true_hms!r}")
check("the marker index resolves to the true onset time", lbl_at_marker == true_hms)
check("two y-axes, not normalised onto one",
      c["dual_axis"]["left"]["unit"] == "W" and c["dual_axis"]["right"]["unit"] == "°C")
check("power is on the left axis and drawn heaviest",
      c["datasets"][0]["axis"] == "left"
      and c["datasets"][0]["style"]["line_width"] > c["datasets"][1]["style"]["line_width"])
check("no smoothing on any series", all(d["style"]["tension"] == 0 for d in c["datasets"]))
# The PSU-0 series. The trace used to plot total node power and say in its subtitle that the raw
# extract carried no PSU input-power metric. It does: ps0_input_power was present in
# D:/M100/data/22-09.tar all along and simply absent from the unpacked subset that was searched.
# The triage indicator is now the primary series, so these assert the CURRENT claim rather than the
# apology it replaced.
check("PSU-0 input power is the leading series, not total node power",
      c["primary_power"] == "ps0_input"
      and c["datasets"][0]["legend"]["key"] == "chart.trace.ps0", c["primary_power"])
check("total node power is kept as context, on the SAME watt axis",
      any(d["legend"]["key"] == "chart.trace.power" and d["axis"] == "left"
          for d in c["datasets"]))
check("the subtitle names PSU-0 input power as what is being read",
      "PSU-0 input power" in c["subtitle"]["en"], c["subtitle"]["en"][-160:])
check("   and no longer claims the raw extract does not carry it",
      "does not carry" not in c["subtitle"]["en"])
check("the subtitle is bilingual", "PSU-0" in c["subtitle"]["zh"])
ps0_vals = [v for v in c["datasets"][0]["data"] if v is not None]
print(f"  PSU-0 series: {len(ps0_vals)} points, {min(ps0_vals):.0f}-{max(ps0_vals):.0f} W")
check("the PSU-0 series carries real varying values", len(ps0_vals) > 100
      and max(ps0_vals) > min(ps0_vals))
check("full-width layout hint", c["width"] == chartreg.WIDTH_FULL)

# no coverage at all -> unavailable, never a fabricated marker
class NoRawStore:
    def has_node_raw(self): return False
r_none = chartreg.render(ChartId.NODE_SENSOR_TRACE, f_on, NoRawStore(), 1664216861, POL2)
check("a store without node_raw reports unavailable, not an empty chart",
      not r_none.available and r_none.code == "no_store", str(r_none.reason))
check("   and names the script that adds it", "build_node_raw" in (r_none.reason or ""))

# the envelope path preserves extremes rather than averaging
rows = [(i * 20, 100.0 + (500.0 if i == 7 else 0.0), 40.0, 90.0) for i in range(100)]
binned, width = chartreg._bin_envelope(rows, 10)
spike_kept = any(b[2] == 600.0 for b in binned)
mean_would_hide = all(b[2] != 600.0 for b in
                      [(0, 0, sum(r[1] for r in rows[:10]) / 10, 0, 0)])
print(f"  envelope: {len(rows)} rows -> {len(binned)} bins of {width}; "
      f"one-sample 600 W spike preserved: {spike_kept}")
check("binning preserves a single-sample spike in the MAX series", spike_kept)
check("...which an average would have erased", mean_would_hide)

print()
print("=" * 100)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL: print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
