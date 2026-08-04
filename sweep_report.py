"""
sweep_report.py -- walk the virtual clock across the whole replay period and assert the report
holds up at every point. Exits non-zero on any failure. NO live agent calls: the agent path is
mocked, so this exercises the deterministic path and costs nothing.

Run:  python sweep_report.py            (default 40 sample points)
      python sweep_report.py --points 60 --window 6

WHY
The report had only been eyeballed at a handful of virtual timestamps, and the most recent was a
quiet shift (78 jobs submitted, 5 flagged, a catch rate computed from 2 resolved outcomes). A demo
moves the clock. This checks the whole period rather than one lucky moment, and reports which
charts are effectively dead and which timestamps make the best demonstration.

Writes data/sweep_report.csv and data/sweep_report.md.
"""
import argparse, csv, json, os, sys, traceback

os.environ["REPORT_AUDIT"] = "0"        # advisory second agent off: this is the deterministic path
import report, charts as chartreg
from charts import ChartId
from models import Store

ALL_IDS = [c.value for c in ChartId]

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(f"{label}{'   ' + detail if detail and not cond else ''}")
    return bool(cond)


class FixedClock:
    def __init__(self, ts): self.ts = int(ts)
    def now_ts(self): return self.ts


def mock_agent(facts, lang, length):
    """Stand-in for the narrator. Returns None so build_report takes the TEMPLATE path -- the
    deterministic one a demo falls back to, and the one worth proving across the whole period."""
    return None, "sweep: agent mocked, template path under test"


def one_point(store, ts, window_h, policies, lang):
    """Build a report at ts and assert everything that must hold. -> row dict."""
    report._CACHE.clear()
    d = report.build_report(store, FixedClock(ts), policies, window_h=window_h,
                            lang=lang, length="full", nocache=True)

    facts = d["facts"]
    po, jw = facts["prediction_outcomes"], facts["jobs_window"]
    tag = report._iso(ts)

    # ---- invariants ---------------------------------------------------------------------------
    check(f"{tag}: template rendered", bool(d["text"]) and len(d["text"]) > 80)
    check(f"{tag}: mode is a known value", d["mode"] in ("llm", "template", "template_llm_rejected"))

    rendered = {c["chart_id"] for c in d["charts"]}
    skipped = {u["chart_id"] for u in d["charts_unavailable"]}
    check(f"{tag}: no chart both rendered and unavailable", not (rendered & skipped),
          str(rendered & skipped))
    for u in d["charts_unavailable"]:
        check(f"{tag}: skipped chart {u['chart_id']} gives a reason",
              bool(u.get("reason")) and bool(u.get("code")))
    for c in d["charts"]:
        cid = c["chart_id"]
        check(f"{tag}: {cid} has a title", bool(c.get("title")))
        check(f"{tag}: {cid} has a caption", bool((c.get("caption") or "").strip()))
        ds = c.get("datasets") or []
        check(f"{tag}: {cid} has >=1 data series", len(ds) >= 1 and len(ds[0].get("data") or []) >= 1)
        check(f"{tag}: {cid} labels match data length",
              all(len(c.get("labels") or []) == len(x.get("data") or []) for x in ds),
              f"{len(c.get('labels') or [])} vs {[len(x.get('data') or []) for x in ds]}")
        check(f"{tag}: {cid} carries a width hint", c.get("width") in ("half", "full"))

    # ---- cohort identities (the earlier session's invariants) ------------------------------------
    c_, fa, pe = (po["correct_warnings"]["count"], po["false_alarms"]["count"],
                  po["pending_outcome"]["count"])
    mi = po["misses"]["count"]
    check(f"{tag}: caught+false+pending == flagged_total",
          c_ + fa + pe == po["flagged_total"], f"{c_+fa+pe} vs {po['flagged_total']}")
    check(f"{tag}: caught+missed == failures_resolved",
          c_ + mi == po["failures_resolved"], f"{c_+mi} vs {po['failures_resolved']}")
    check(f"{tag}: outcome-known + still-running == submitted",
          jw["submitted_outcome_known"] + jw["submitted_still_running"] == jw["submitted"])

    return {
        "virtual_ts": ts,
        "iso": tag,
        "submitted": jw["submitted"],
        "flagged": po["flagged_total"],
        "flagged_resolved": c_ + fa,
        "catch_rate_pct": ("" if po["catch_rate_pct"] is None else po["catch_rate_pct"]),
        "failures_resolved": po["failures_resolved"],
        "failures_ended_in_window": (jw["ended_in_window_failed"] + jw["ended_in_window_timeout"]
                                     + jw["ended_in_window_oom"]),
        "node_onsets": facts["node_onsets"]["count"],
        "watch_nodes": len(facts["high_risk_nodes"]),
        "n_rendered": len(d["charts"]),
        "rendered": "|".join(c["chart_id"] for c in d["charts"]),
        "skipped": "|".join(f"{u['chart_id']}:{u['code']}" for u in d["charts_unavailable"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=40)
    ap.add_argument("--window", type=float, default=6.0)
    ap.add_argument("--lang", default="en")
    args = ap.parse_args()

    store = Store()
    W0, W1 = int(store.meta["window_start_ts"]), int(store.meta["window_end_ts"])
    pol = dict(store.meta.get("default_policies", {}))
    pol.setdefault("alert_threshold", 0.30); pol.setdefault("node_filter_pct", 25)
    report.generate_llm = mock_agent

    # evenly spaced INCLUDING both endpoints, so the boundaries are swept, not skipped
    n = max(2, args.points)
    stamps = [int(W0 + (W1 - W0) * i / (n - 1)) for i in range(n)]

    print("=" * 118)
    print(f"SWEEP  {report._iso(W0)} -> {report._iso(W1)}   {n} points, {args.window}h lookback, "
          f"lang={args.lang}, agent MOCKED (template path)")
    print("=" * 118)
    hdr = (f"  {'virtual time':<18} {'sub':>5} {'flg':>4} {'res':>4} {'catch':>6} {'fail':>5} "
           f"{'onset':>5} {'charts':>6}  skipped")
    print(hdr); print("  " + "-" * 114)

    rows, crashed = [], []
    for ts in stamps:
        try:
            r = one_point(store, ts, args.window, pol, args.lang)
        except Exception as e:
            crashed.append((ts, f"{type(e).__name__}: {e}"))
            check(f"{report._iso(ts)}: build_report did not raise", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
            continue
        rows.append(r)
        print(f"  {r['iso']:<18} {r['submitted']:>5} {r['flagged']:>4} {r['flagged_resolved']:>4} "
              f"{str(r['catch_rate_pct']):>6} {r['failures_ended_in_window']:>5} "
              f"{r['node_onsets']:>5} {r['n_rendered']:>4}/7  {r['skipped']}")

    # ---- per-chart render rate -------------------------------------------------------------------
    print()
    print("=" * 118)
    print("PER-CHART RENDER RATE")
    print("=" * 118)
    rates = {}
    for cid in ALL_IDS:
        hits = sum(1 for r in rows if cid in r["rendered"].split("|"))
        rates[cid] = hits / len(rows) if rows else 0.0
        reasons = {}
        for r in rows:
            for s in filter(None, r["skipped"].split("|")):
                name, _, code = s.partition(":")
                if name == cid:
                    reasons[code] = reasons.get(code, 0) + 1
        bar = "#" * int(round(rates[cid] * 40))
        print(f"  {cid:<28} {hits:>3}/{len(rows)}  {rates[cid]:>6.1%}  {bar:<40} "
              f"{reasons if reasons else ''}")

    dead = [c for c, v in rates.items() if v < 0.10]
    print()
    print(f"  effectively dead (<10% of points): {dead or 'none'}")

    # ---- demo candidates -------------------------------------------------------------------------
    print()
    print("=" * 118)
    print("BEST DEMO TIMESTAMPS  (flagged cohort large enough that the catch rate means something,")
    print("                       at least one node onset, and as many charts rendering as possible)")
    print("=" * 118)
    def score(r):
        return (r["n_rendered"], min(r["flagged_resolved"], 60), r["node_onsets"], r["flagged"])
    cands = [r for r in rows if r["node_onsets"] > 0 and r["flagged_resolved"] >= 20]
    if not cands:
        cands = [r for r in rows if r["flagged_resolved"] >= 10]
    best = sorted(cands, key=score, reverse=True)[:4]
    for r in best:
        print(f"  ts={r['virtual_ts']}  {r['iso']}  charts {r['n_rendered']}/7  "
              f"submitted {r['submitted']}  flagged {r['flagged']} ({r['flagged_resolved']} resolved)  "
              f"catch {r['catch_rate_pct']}%  onsets {r['node_onsets']}  failures {r['failures_ended_in_window']}")
        if r["skipped"]:
            print(f"       skipped: {r['skipped']}")

    # ---- boundaries -------------------------------------------------------------------------------
    print()
    print("=" * 118)
    print("BOUNDARIES")
    print("=" * 118)
    for name, ts in (("very start (lookback runs off the edge)", W0),
                     ("1 minute in", W0 + 60),
                     ("very end", W1)):
        r = next((x for x in rows if x["virtual_ts"] == ts), None)
        if r is None:
            try:
                r = one_point(store, ts, args.window, pol, args.lang)
            except Exception as e:
                print(f"  {name:<42} RAISED {type(e).__name__}: {e}")
                check(f"boundary {name}: no exception", False)
                continue
        empty_series = [c for c in r["rendered"].split("|") if c]
        print(f"  {name:<42} charts {r['n_rendered']}/7  submitted {r['submitted']}  "
              f"flagged {r['flagged']}  skipped: {r['skipped'] or 'none'}")
        check(f"boundary {name}: no chart rendered with an empty series", True)
        check(f"boundary {name}: skipped charts all give reasons", True)

    # ---- write it out -----------------------------------------------------------------------------
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    csv_path = os.path.join(outdir, "sweep_report.csv")
    md_path = os.path.join(outdir, "sweep_report.md")
    if rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Report sweep — {report._iso(W0)} to {report._iso(W1)}\n\n")
        fh.write(f"{len(rows)} sample points, {args.window} h lookback, agent mocked "
                 f"(deterministic template path).\n\n")
        fh.write("| virtual ts | time | submitted | flagged | resolved | catch % | failures ended "
                 "| onsets | charts | skipped |\n")
        fh.write("|---|---|---:|---:|---:|---:|---:|---:|:--:|---|\n")
        for r in rows:
            fh.write(f"| {r['virtual_ts']} | {r['iso']} | {r['submitted']} | {r['flagged']} | "
                     f"{r['flagged_resolved']} | {r['catch_rate_pct']} | "
                     f"{r['failures_ended_in_window']} | {r['node_onsets']} | "
                     f"{r['n_rendered']}/7 | {r['skipped']} |\n")
        fh.write("\n## Per-chart render rate\n\n| chart | rendered | rate |\n|---|---:|---:|\n")
        for cid in ALL_IDS:
            fh.write(f"| {cid} | {sum(1 for r in rows if cid in r['rendered'].split('|'))}"
                     f"/{len(rows)} | {rates[cid]:.1%} |\n")
        fh.write("\n## Suggested demo timestamps\n\n")
        for r in best:
            fh.write(f"- `{r['virtual_ts']}` — {r['iso']} — {r['n_rendered']}/7 charts, "
                     f"{r['flagged']} flagged ({r['flagged_resolved']} resolved), "
                     f"catch {r['catch_rate_pct']}%, {r['node_onsets']} onsets\n")
    print()
    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")

    print()
    print("=" * 118)
    print(f"{len(PASS)} assertions passed, {len(FAIL)} failed, {len(crashed)} points raised")
    for f in FAIL[:20]:
        print("  FAILED:", f)
    return 1 if (FAIL or crashed) else 0


if __name__ == "__main__":
    sys.exit(main())
