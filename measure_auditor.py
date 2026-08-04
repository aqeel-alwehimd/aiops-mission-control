"""
measure_auditor.py -- does the rebuilt Data Auditor actually catch anything? MEASURED, not asserted.

    python measure_auditor.py                 # live: calls the auditor agent (slow)
    python measure_auditor.py --runs 3        # repetitions per fixture (agent output varies)
    python measure_auditor.py --det-only      # deterministic layer only, no network at all

This is NOT a verify_* suite: it makes live agent calls and reports rates rather than passing or
failing. It exists because the previous auditor returned is_valid=true on five live narratives that
contained four confirmed false claims, and "we improved the prompt" is not a result.

WHAT IS BEING MEASURED
  * catch rate       -- fraction of (false fixture, run) pairs where the auditor raised at least one
                        high/medium finding whose quoted span overlaps the known-false sentence.
  * false-positive   -- fraction of (clean fixture, run) pairs where it raised ANY high/medium
    rate               finding. Clean fixtures are correct narratives; every finding on one is wrong.

THE DECISION RULE WAS FIXED BEFORE ANY FIXTURE WAS RUN, and is printed at the top of the output so
it cannot be quietly moved afterwards:
      SUCCESS  catch >= 60%  AND  false positives <= 25%
      PARTIAL  catch 30-60%  -- a real gain on the 0/5 baseline, not a solved problem
      FAILURE  catch < 30%   -- report it and stop; do not tune the prompt until it looks better

EVERY FIXTURE IS ANCHORED TO REAL FACTS at a real virtual timestamp, and every number in every
fixture is a genuine value from those facts. That is essential rather than tidy: a fixture using an
invented number would be caught by the deterministic numeric gate long before the auditor sees it,
so it would measure the wrong layer. These fixtures all pass the hard gate, exactly as the live
failures did.
"""
import argparse, json, os, statistics, sys, time

os.environ.setdefault("REPORT_AUDIT", "1")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import report
from models import Store

# ---- the pre-committed rule -----------------------------------------------------------------------
THRESHOLD_CATCH = 0.60
THRESHOLD_FP    = 0.25
PARTIAL_CATCH   = 0.30

POL = {"alert_threshold": 0.30, "node_filter_pct": 25}
WINDOW_H = 6

# ---- fixtures ---------------------------------------------------------------------------------------
# (id, virtual_ts, kind, target sentence, full narrative, captions)
# `target` is the span that is false; a finding counts as a catch only if it quotes something that
# overlaps it, so an auditor objecting to an unrelated sentence does not score.

def _f(ts, store, cache={}):
    if ts not in cache:
        cache[ts] = report.assemble_facts(store, ts, POL, WINDOW_H)
    return cache[ts]


def build_fixtures(store):
    F = []

    # ---------- FALSE: the four confirmed live failures -------------------------------------------
    # 1 & 2 are the exact class the auditor was built to catch: a resolved-failure count described as
    # sitting inside the flagged cohort, when that count includes misses which were never flagged.
    t1 = 1664303261     # flagged 331, failures_resolved 231, correct 153, false 86, pending 92, miss 78
    F.append(dict(
        id="live1_resolved_within_flagged", ts=t1, kind="false",
        target="the 231 resolved failures sit within that cohort",
        text=("## Executive summary\n"
              "Over the last 6 h the cluster submitted 2245 jobs and 2029 ended, of which 1669 "
              "completed. 1839 jobs are active now and utilisation sits at 68.1%.\n\n"
              "## Risk assessment\n"
              "P3 flagged 331 jobs at submission, and the 231 resolved failures sit within that "
              "cohort, giving a catch rate of 66.2%. 86 were false alarms and 92 have not yet "
              "finished.\n\n"
              "## Recommended actions\n"
              "Review the highest-risk in-flight jobs before the next shift.\n"),
        captions=[]))

    t2 = 1664296615     # flagged 288, failures_resolved 151, correct 115, false 66, pending 107, miss 36
    F.append(dict(
        id="live2_of_the_flagged_resolved", ts=t2, kind="false",
        target="Of the 288 jobs flagged at submission, 151 have resolved as genuine failures",
        text=("## Executive summary\n"
              "The last 6 h saw 2118 jobs submitted. P3's catch rate stands at 76.2%.\n\n"
              "## Risk assessment\n"
              "Of the 288 jobs flagged at submission, 151 have resolved as genuine failures, while "
              "66 turned out to be false alarms and 107 are still running.\n\n"
              "## Recommended actions\n"
              "Check the watch list before handover.\n"),
        captions=[]))

    # 3 is the caption with NO numbers in it at all, so no numeric check could ever see it.
    t3 = 1664223507     # flagged 332, correct 55 (17%), pending 236 (71%)
    F.append(dict(
        id="live3_qualitative_most_caught", ts=t3, kind="false",
        target="Most of the jobs flagged this shift have already been caught as real failures",
        text=("## Executive summary\n"
              "A quieter shift: 332 jobs were flagged at submission out of 1533 submitted.\n\n"
              "## Risk assessment\n"
              "Most of the jobs flagged this shift have already been caught as real failures, so "
              "the picture is reassuring. 41 were false alarms.\n\n"
              "## Recommended actions\n"
              "No escalation required.\n"),
        captions=[("prediction_outcomes",
                   "Most of the flagged jobs have already been confirmed as real failures.")]))

    # 4 is a quantity written as a WORD, so _NUM never sees it.
    F.append(dict(
        id="live4_word_quantity_four_of_six", ts=t1, kind="false",
        target="four TIMEOUT-predicted jobs",
        text=("## Executive summary\n"
              "2245 jobs were submitted in the last 6 h; 331 were flagged.\n\n"
              "## Risk assessment\n"
              "The watch list is narrow: four TIMEOUT-predicted jobs from a single user account "
              "occupy it, all at the same risk level.\n\n"
              "## Recommended actions\n"
              "Contact the owning user before the next submission burst.\n"),
        captions=[]))

    # ---------- FALSE: the three earlier documented instances ---------------------------------------
    # the doughnut cohort mix -- misses drawn as a slice of a ring that is the flagged cohort
    t4 = 1664216861     # flagged 442, misses 30
    F.append(dict(
        id="doc1_doughnut_cohort_mix", ts=t4, kind="false",
        target="all 442 flagged jobs, including the 30 missed failures",
        text=("## Executive summary\n"
              "P3 flagged 442 jobs at submission during this 6 h window.\n\n"
              "## Risk assessment\n"
              "97 flagged jobs were confirmed failures and 148 were false alarms.\n\n"
              "## Recommended actions\n"
              "Review the false-alarm rate with the scheduler team.\n"),
        captions=[("prediction_outcomes",
                   "The ring covers all 442 flagged jobs, including the 30 missed failures.")]))

    # a caption citing a figure that belongs to a DIFFERENT cohort (ended-in-window, not submitted)
    F.append(dict(
        id="doc2_caption_other_cohort", ts=t1, kind="false",
        target="Of the 331 flagged jobs, 254 ended in failure",
        text=("## Executive summary\n"
              "2245 jobs submitted, 331 flagged at submission, 2029 ended in the window.\n\n"
              "## Risk assessment\n"
              "153 flagged jobs were confirmed failures, 86 were false alarms, 92 are pending.\n\n"
              "## Recommended actions\n"
              "Nothing requires escalation this shift.\n"),
        captions=[("prediction_outcomes",
                   "Of the 331 flagged jobs, 254 ended in failure during this shift.")]))

    # a catch rate of 50% stated alongside BOTH resolved failures described as caught
    t5 = 1664336492     # flagged 4, failures_resolved 2, catch 50.0
    F.append(dict(
        id="doc3_catch50_but_both_caught", ts=t5, kind="false",
        target="both of the resolved failures were correctly warned in advance",
        text=("## Executive summary\n"
              "A very quiet shift. Only 4 jobs were flagged at submission.\n\n"
              "## Risk assessment\n"
              "The catch rate for this window is 50.0%, and both of the resolved failures were "
              "correctly warned in advance.\n\n"
              "## Recommended actions\n"
              "No action needed.\n"),
        captions=[]))

    # ---------- CLEAN: correct narratives that must produce NOTHING --------------------------------
    # Several are deliberately near the line: they mention misses next to the flagged cohort, quote
    # both cohorts in one report, and use qualitative words that ARE supported. An auditor that has
    # merely been taught to object will fail here, which is the point of including them.
    F.append(dict(
        id="clean1_plain_correct", ts=t1, kind="clean", target=None,
        text=("## Executive summary\n"
              "Over the last 6 h the cluster submitted 2245 jobs; 2029 ended, of which 1669 "
              "completed. Utilisation is 68.1%.\n\n"
              "## Risk assessment\n"
              "P3 flagged 331 jobs at submission: 153 have been confirmed as real failures, 86 were "
              "false alarms and 92 have not finished yet. 5 node anomaly onsets were recorded.\n\n"
              "## Recommended actions\n"
              "Review the in-flight watch list before handover.\n"),
        captions=[]))

    F.append(dict(
        id="clean2_misses_stated_separately", ts=t1, kind="clean", target=None,
        text=("## Executive summary\n"
              "2245 jobs submitted in the last 6 h, 331 of them flagged by P3 at submission.\n\n"
              "## Risk assessment\n"
              "Of those 331, 153 were confirmed failures and 86 were false alarms; 92 are still "
              "running. Separately, 78 failures were never flagged at all, which is the honest cost "
              "of the current 0.3 threshold and gives a catch rate of 66.2%.\n\n"
              "## Recommended actions\n"
              "Consider whether the threshold is set where the operators want it.\n"),
        captions=[]))

    F.append(dict(
        id="clean3_both_cohorts_correct", ts=t1, kind="clean", target=None,
        text=("## Executive summary\n"
              "Two different views of the shift. 2245 jobs were submitted inside the window, and "
              "separately 2029 jobs ended inside it whenever they were submitted; of those, 1669 "
              "completed, 254 failed, 98 timed out and 8 ran out of memory.\n\n"
              "## Risk assessment\n"
              "Among the jobs submitted in the window, 331 were flagged and 78 failures were "
              "missed.\n\n"
              "## Recommended actions\n"
              "None beyond the standard watch list.\n"),
        captions=[]))

    F.append(dict(
        id="clean4_supported_qualitative", ts=t1, kind="clean", target=None,
        text=("## Executive summary\n"
              "Most of the jobs that ended this shift completed successfully: 1669 of the 2029 that "
              "ended.\n\n"
              "## Risk assessment\n"
              "331 jobs were flagged at submission and 5 node anomaly onsets were recorded. The "
              "watch list is dominated by a single user account.\n\n"
              "## Recommended actions\n"
              "Contact that user before the next burst.\n"),
        captions=[("job_outcome_mix", "Most of the jobs that ended in this window completed.")]))

    F.append(dict(
        id="clean5_pending_majority_true", ts=t3, kind="clean", target=None,
        text=("## Executive summary\n"
              "332 jobs were flagged at submission during this window.\n\n"
              "## Risk assessment\n"
              "Most of the flagged jobs have not finished yet — 236 of the 332 are still pending, "
              "so the 78.6% catch rate rests on only 70 resolved failures and should be read as "
              "provisional. 15 failures were missed.\n\n"
              "## Recommended actions\n"
              "Re-read the catch rate at the end of the shift.\n"),
        captions=[("prediction_outcomes",
                   "Most of the flagged jobs in this window have not finished yet.")]))

    F.append(dict(
        id="clean6_honest_bad_news", ts=t4, kind="clean", target=None,
        text=("## Executive summary\n"
              "442 jobs were flagged at submission, and the false-alarm count is high: 148 of the "
              "flagged jobs completed normally.\n\n"
              "## Risk assessment\n"
              "97 flagged jobs were confirmed failures, 197 have not finished, and a further 30 "
              "failures were never flagged. One node anomaly onset was recorded.\n\n"
              "## Recommended actions\n"
              "Review the threshold: the false-alarm count exceeds the confirmed-failure count.\n"),
        captions=[]))

    return F


# ---- scoring ----------------------------------------------------------------------------------------
def _norm(s):
    return " ".join(str(s or "").lower().split())

def overlaps(quote, target):
    """Does a finding's quoted span refer to the known-false sentence?

    Deliberately generous about form and strict about substance: substring either way, or a shared
    run of >= 5 words. An auditor that identifies the right sentence in its own words should score;
    one that objects to a different sentence should not.
    """
    q, t = _norm(quote), _norm(target)
    if not q or not t:
        return False
    if q in t or t in q:
        return True
    qw, tw = q.split(), t.split()
    for i in range(len(qw) - 4):
        if " ".join(qw[i:i + 5]) in t:
            return True
    for i in range(len(tw) - 4):
        if " ".join(tw[i:i + 5]) in q:
            return True
    return False

def scores(findings):
    return [f for f in findings if f.get("severity") in ("high", "medium")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--det-only", action="store_true",
                    help="deterministic containment check only; makes no network call")
    ap.add_argument("--out", default=os.path.join("data", "auditor_measurement.json"))
    args = ap.parse_args()

    store = Store()
    fixtures = build_fixtures(store)
    n_false = sum(1 for f in fixtures if f["kind"] == "false")
    n_clean = len(fixtures) - n_false

    print("=" * 110)
    print("AUDITOR MEASUREMENT")
    print("=" * 110)
    print(f"  PRE-COMMITTED DECISION RULE (fixed before any fixture was run):")
    print(f"    SUCCESS  catch >= {THRESHOLD_CATCH:.0%}  AND  false positives <= {THRESHOLD_FP:.0%}")
    print(f"    PARTIAL  catch {PARTIAL_CATCH:.0%}-{THRESHOLD_CATCH:.0%}")
    print(f"    FAILURE  catch <  {PARTIAL_CATCH:.0%}   -> report it and stop, do not tune")
    print(f"  baseline being beaten: the previous auditor caught 0 of 5 live false narratives")
    print(f"  fixtures: {n_false} known-false, {n_clean} clean, {args.runs} run(s) each")
    print()

    # ---------------- deterministic layer, over every fixture ------------------------------------
    print("=" * 110)
    print("DETERMINISTIC COHORT-CONTAINMENT CHECK (no LLM, no network)")
    print("=" * 110)
    det = {}
    for fx in fixtures:
        facts = _f(fx["ts"], store)
        adv = report.cohort_containment_review(
            fx["text"], facts,
            [(f"caption on '{cid}'", cap) for cid, cap in fx["captions"]])
        det[fx["id"]] = adv
        mark = ("CAUGHT" if adv else "missed") if fx["kind"] == "false" else \
               ("FALSE POSITIVE" if adv else "clean")
        print(f"  {fx['kind']:<5} {fx['id']:<36} {len(adv)} finding(s)  {mark}")
        for a in adv:
            print(f"        {a['message'][:150]}")
    det_catch = sum(1 for fx in fixtures if fx["kind"] == "false" and det[fx["id"]])
    det_fp = sum(1 for fx in fixtures if fx["kind"] == "clean" and det[fx["id"]])
    print()
    print(f"  deterministic: {det_catch}/{n_false} false fixtures caught, "
          f"{det_fp}/{n_clean} clean fixtures falsely flagged")

    # ---------------- the same check against the TEMPLATE corpus ----------------------------------
    # The template is generated from these same facts and cannot state a false containment, so every
    # hit here is a proven false positive. This is a much harder FP test than six hand-written clean
    # fixtures, because it is real production text at 40 points in both languages.
    print()
    print("=" * 110)
    print("FALSE-POSITIVE CORPUS: render_template() across the replay window, both languages")
    print("=" * 110)
    W0, W1 = int(store.meta["window_start_ts"]), int(store.meta["window_end_ts"])
    corpus_hits, corpus_n = [], 0
    for i in range(40):
        ts = int(W0 + (W1 - W0) * i / 39)
        facts = report.assemble_facts(store, ts, POL, WINDOW_H)
        for lang in ("en", "zh"):
            for length in ("brief", "full"):
                txt = report.render_template(facts, lang, length)
                corpus_n += 1
                hits = report.cohort_containment_review(txt, facts)
                if hits:
                    corpus_hits.append((ts, lang, length, hits[0]["message"][:160]))
    print(f"  {corpus_n} correct-by-construction template reports scanned")
    print(f"  false positives: {len(corpus_hits)}")
    for h in corpus_hits[:8]:
        print(f"    {h[0]} {h[1]}/{h[2]}: {h[3]}")

    result = {"threshold_catch": THRESHOLD_CATCH, "threshold_fp": THRESHOLD_FP,
              "runs": args.runs,
              "deterministic": {"catch": det_catch, "n_false": n_false,
                                "fp": det_fp, "n_clean": n_clean,
                                "template_corpus_n": corpus_n,
                                "template_corpus_fp": len(corpus_hits)},
              "llm": None}

    if args.det_only:
        _write(args.out, result)
        return 0

    # ---------------- the LLM auditor -------------------------------------------------------------
    print()
    print("=" * 110)
    print(f"LLM AUDITOR — {len(fixtures)} fixtures x {args.runs} run(s) = "
          f"{len(fixtures) * args.runs} live calls")
    print("=" * 110)
    rows, lat = [], []
    for fx in fixtures:
        facts = _f(fx["ts"], store)
        for r in range(args.runs):
            report.clear_cooldowns()          # one fixture's failure must not skip the next
            a = report.audit_llm(facts, fx["text"], lang="en", captions=fx["captions"])
            lat.append(a["latency_s"])
            sev = scores(a.get("findings") or [])
            if fx["kind"] == "false":
                hit = any(overlaps(f.get("quote"), fx["target"])
                          or overlaps(f.get("why"), fx["target"]) for f in sev)
                verdict = "CAUGHT" if hit else ("wrong-target" if sev else "MISSED")
            else:
                hit = bool(sev)
                verdict = "FALSE POSITIVE" if hit else "clean"
            rows.append({"id": fx["id"], "kind": fx["kind"], "run": r, "ran": a["ran"],
                         "state": a["state"], "hit": hit, "verdict": verdict,
                         "n_findings": len(a.get("findings") or []),
                         "n_checked": len(a.get("checked") or []),
                         "latency_s": a["latency_s"],
                         "findings": a.get("findings") or [],
                         "checked": a.get("checked") or []})
            print(f"  {fx['kind']:<5} {fx['id']:<36} run{r + 1}  {a['state']:<12} "
                  f"findings={len(a.get('findings') or []):<2} checked="
                  f"{len(a.get('checked') or []):<2} {a['latency_s']:>6.1f}s  {verdict}")
            for f in (a.get("findings") or [])[:3]:
                print(f"        [{f['severity']}/{f['location']}] \"{(f['quote'] or '')[:90]}\" "
                      f"-> {(f['why'] or '')[:90]}")

    ran = [r for r in rows if r["ran"]]
    fal = [r for r in ran if r["kind"] == "false"]
    cln = [r for r in ran if r["kind"] == "clean"]
    catch = (sum(1 for r in fal if r["hit"]) / len(fal)) if fal else 0.0
    fp = (sum(1 for r in cln if r["hit"]) / len(cln)) if cln else 0.0

    print()
    print("=" * 110)
    print("RESULT")
    print("=" * 110)
    print(f"  auditor answered on {len(ran)}/{len(rows)} calls "
          f"(median latency {statistics.median(lat):.1f}s, max {max(lat):.1f}s)")
    print(f"  CATCH RATE          {catch:>6.1%}   ({sum(1 for r in fal if r['hit'])}/{len(fal)} "
          f"false-fixture runs)   threshold {THRESHOLD_CATCH:.0%}")
    print(f"  FALSE-POSITIVE RATE {fp:>6.1%}   ({sum(1 for r in cln if r['hit'])}/{len(cln)} "
          f"clean-fixture runs)    threshold {THRESHOLD_FP:.0%}")
    per = {}
    for r in fal:
        per.setdefault(r["id"], []).append(r["hit"])
    print()
    print("  per-fixture catch (a fixture caught in some runs and not others is agent variance):")
    for k, v in per.items():
        print(f"    {k:<36} {sum(v)}/{len(v)}")
    if catch >= THRESHOLD_CATCH and fp <= THRESHOLD_FP:
        outcome = "SUCCESS -- meets the pre-committed rule"
    elif catch >= PARTIAL_CATCH:
        outcome = "PARTIAL -- beats the 0/5 baseline but does not meet the pre-committed rule"
    else:
        outcome = "FAILURE -- report it plainly and stop; do not tune the prompt"
    print()
    print(f"  VERDICT: {outcome}")

    result["llm"] = {"catch_rate": catch, "fp_rate": fp, "n_false_runs": len(fal),
                     "n_clean_runs": len(cln), "answered": len(ran), "attempted": len(rows),
                     "median_latency_s": statistics.median(lat), "outcome": outcome,
                     "per_fixture": {k: [bool(x) for x in v] for k, v in per.items()},
                     "rows": rows}
    _write(args.out, result)
    return 0


def _write(path, result):
    """Write the measurement, PRESERVING an existing `llm` section when this run did not produce one.

    `--det-only` makes no agent calls, so it has nothing to say about the LLM auditor. Overwriting
    the file wholesale meant a cheap deterministic re-run silently destroyed the expensive 39-call
    measurement sitting next to it -- which is exactly the sort of result that must not be casually
    recomputable, since it is the evidence behind a pre-committed decision.
    """
    p = path if os.path.isabs(path) else os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if result.get("llm") is None and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                prev = json.load(fh)
            if prev.get("llm") is not None:
                result["llm"] = prev["llm"]
                result["llm_note"] = "carried over from an earlier run; this run was --det-only"
                print("  (kept the existing `llm` measurement -- this run made no agent calls)")
        except Exception:
            pass
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {p}")


if __name__ == "__main__":
    sys.exit(main())
