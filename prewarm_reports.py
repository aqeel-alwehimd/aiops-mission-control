"""
prewarm_reports.py -- generate and cache reports ahead of a demo. Exits; starts no server.

A LaplaceAI 504 during a live demonstration is the worst possible moment for it. This walks a set of
virtual timestamps -- including every hero example -- for both languages and both lengths, and writes
each successful report to data/report_cache.json. The running app reads that file on a cache miss,
so the demo path is served from disk and never depends on a live call.

    python prewarm_reports.py                # live: calls the agent (slow, ~1 min per entry)
    python prewarm_reports.py --mock         # no network: exercises the whole path with a stub
    python prewarm_reports.py --limit 4      # only the first N timestamps
    python prewarm_reports.py --clear        # delete the prewarm cache and exit

Because only mode == "llm" results are cached, a prewarm entry is by construction a report that
passed the hard gate; a fallback is never pinned.
"""
import argparse, json, os, re, sys, time

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import report
from models import Store
from replay import VirtualClock

HERE = os.path.dirname(os.path.abspath(__file__))
HEROES = os.path.join(HERE, "data", "hero_examples.json")
MOMENTS = os.path.join(HERE, "data", "demo_moments.json")

# The four moments the 40-point sweep picked out (see data/sweep_report.md, "Suggested demo
# timestamps"): the flagged cohort is large enough that the catch rate means something, there is at
# least one node onset, and every chart renders. These are what `--demo` prepares and what
# /api/demo_moments serves to the clock's `goto` control.
DEMO_TIMESTAMPS = [1664303261, 1664296615, 1664223507, 1664216861]
DEMO_ATTEMPTS = 8         # per (timestamp, language); the endpoint fails roughly 1 call in 3


class FixedClock:
    """build_report only needs now_ts(); prewarm drives the virtual time itself."""
    def __init__(self, ts): self.ts = int(ts)
    def now_ts(self): return self.ts


def hero_timestamps():
    """Virtual timestamps from data/hero_examples.json, if it has any."""
    out = []
    if os.path.exists(HEROES):
        try:
            for ex in json.load(open(HEROES, encoding="utf-8")).get("examples", []):
                for k in ("virtual_ts", "ts", "at_ts", "jump_ts"):
                    if isinstance(ex.get(k), (int, float)):
                        out.append(int(ex[k])); break
        except Exception as e:
            print(f"  ! could not read hero_examples.json ({type(e).__name__}); continuing")
    return out


def spread_timestamps(win_start, win_end, n=6):
    """An even spread across the replay window, so scrubbing lands on warm entries too."""
    span = max(1, win_end - win_start)
    return [int(win_start + span * (i + 0.5) / n) for i in range(n)]


# ================================================================ prepared demo moments
# WHY THIS IS NOT JUST prewarm WITH A DIFFERENT LIST.
#
# Two things have to be true of a demo report that are not required of a warm cache entry:
#
#   1. It must be an LLM report, not a template. A fallback loses the AI-generated badge and with it
#      the point of the whole integration, so this RETRIES until the agent succeeds rather than
#      recording a failure and moving on. Measured: the narrator answers about two calls in three.
#
#   2. It must be TRUE. A cached report is served for as long as it is committed, so a fluent
#      narrative containing a false relational claim would be pinned into the demo permanently --
#      strictly worse than a template. Every candidate is therefore checked before it is blessed:
#      the deterministic cohort-containment check first (free, exact), then the auditor if it is
#      reachable. Anything with a high/medium finding is DISCARDED and regenerated, and the discard
#      is reported rather than quietly retried.
# A finding that says "the facts contain X and the narrative does not mention it". Heuristic, and
# labelled as one: the auditor is not asked to tag its own findings by class, so this reads the
# explanation text. It is used ONLY to decide what blocks, never to hide a finding -- every finding
# is printed either way.
# A LABEL ONLY. This used to decide what blocks, and it was the wrong tool for that job: it classifies
# a free-text explanation by keyword, and the auditor writes those explanations however it likes. A
# report saying "the four node isolation events at 13:45" where the facts record FIVE was explained as
# "understates the bad news", matched as an omission, and passed the gate -- a wrong count is an
# assertion, not silence. Rather than keep patching the pattern, every high/medium finding now blocks
# and this only annotates the printed line, where being wrong costs nothing.
_OMISSION = re.compile(
    r"omit|absent from|not mention|no mention|silently drop|missing from|left out|unreported"
    r"|appears? nowhere|does not (?:mention|state|report|surface)", re.I)

def vet(d, facts):
    """-> (ok_to_cache, [line, ...]). The content gate a demo report must pass before it is cached.

    A cached demo report is served for as long as it is committed, so anything wrong in it is
    pinned. Four things block, and each earned its place by appearing in a real generated report
    during this run rather than by being imagined:

      1. A FALSE CONTAINMENT, proved in Python by cohort_containment_review(). Exact, no judgment,
         measured zero false positives across 160 template reports.
      2. THE WRONG LANGUAGE. Two of the first eight reports were written entirely in Chinese under
         lang="en". The numbers were right and the report was unservable.
      3. A RAW JSON LEAK. One agent reply was invalid JSON (a stray "]" after the model_note value),
         so _parse_json_object() fell through and the composed "narrative" was the literal object,
         braces and keys included. The hard gate passes it because every number in it is real.
      4. ANY AUDITOR FINDING OF high/medium SEVERITY. This gate is deliberately stricter than the
         measurement would justify on its own. On the fixture set the auditor's false positives were
         all the MATERIAL OMISSION class, so scoping the block to non-omission findings looked
         reasonable -- but that scoping depended on classifying a free-text explanation by keyword,
         and it demonstrably failed: a report calling five onset events "four" was explained as
         "understates the bad news", read as an omission, and passed. Eight reports is a small
         enough job that the cost of a stricter gate is a few more retries, and the cost of a
         looser one is a false claim pinned into the demo for as long as it is committed. Findings
         are still LABELLED as omissions in the output, because that label is useful to read and
         harmless to get wrong.

    None of this replaces reading the narrative, and it is not claimed to. Two false claims in the
    first batch were invisible to every check here -- "all six jobs scored 98.6%" when one scored
    98.0, and "four of these carry 98.6%" when five do. Both are over-generalisations across a list,
    and neither the numeric gate, the containment check nor the auditor caught them. The gate
    converges the retry loop onto candidates worth reading; the final judgment on every cached
    report was made by hand against the facts.
    """
    blocking, notes = [], []
    text = d.get("text") or ""
    captions = [(c.get("chart_id"), c.get("caption")) for c in (d.get("charts") or [])
                if c.get("caption_source") == "agent"]

    for a in report.cohort_containment_review(
            text, facts, [(f"caption on '{cid}'", cap) for cid, cap in captions]):
        blocking.append("deterministic containment: " + a["message"][:240])

    for adv in (d.get("advisories") or []):
        if adv["code"] in (report.ADV_LANGUAGE, report.ADV_META):
            blocking.append(f"{adv['code']}: {adv['message'][:160]}")

    # the JSON-leak signature: composition never emits a brace-delimited object or a bare schema key
    stripped = text.strip()
    if stripped.startswith("{") or '"chart_configs"' in text or '"executive_summary"' in text:
        blocking.append("raw JSON leaked into the narrative (the reply was not valid JSON, so it "
                        "was displayed verbatim instead of composed)")

    for f in (d.get("auditor", {}).get("findings") or []):
        line = (f"auditor[{f.get('severity')}] \"{(f.get('quote') or '')[:80]}\" "
                f"-- {(f.get('why') or '')[:150]}")
        omission = bool(_OMISSION.search((f.get("why") or "") + " " + (f.get("contradicts") or "")))
        if f.get("severity") in ("high", "medium"):
            blocking.append(line + ("  [looks like an omission]" if omission else ""))
        else:
            notes.append(line + "  [low severity]")

    return (not blocking), blocking + [("(non-blocking) " + n) for n in notes]


def prepare_demo(store, policies, window_h=6, stamps=None, attempts=DEMO_ATTEMPTS):
    stamps = stamps or DEMO_TIMESTAMPS
    print(f"preparing {len(stamps)} demo moment(s) x 2 languages, length=full, window={window_h}h")
    print(f"cache file: {report.PREWARM_PATH}")
    print(f"up to {attempts} attempts each; a candidate with a relational finding is discarded\n")

    log, ok_all = [], True
    for ts in stamps:
        facts = report.assemble_facts(store, ts, policies, window_h)
        for lang in ("en", "zh"):
            tries = discarded = 0
            accepted = None
            while tries < attempts and accepted is None:
                tries += 1
                report.clear_cooldowns()      # a transient 504 must not skip the next attempt
                t0 = time.time()
                try:
                    d = report.build_report(store, FixedClock(ts), policies, window_h=window_h,
                                            lang=lang, length="full", nocache=True, persist=False)
                except Exception as e:
                    print(f"  {ts} {lang} attempt {tries}: ERROR {type(e).__name__}: {e}")
                    continue
                dt = time.time() - t0
                if d["mode"] != "llm":
                    print(f"  {ts} {lang} attempt {tries}: template ({d['fallback_reason']}) "
                          f"[{dt:.0f}s]")
                    continue
                good, why = vet(d, facts)
                if not good:
                    discarded += 1
                    print(f"  {ts} {lang} attempt {tries}: llm but DISCARDED ON CONTENT [{dt:.0f}s]")
                    for w in why:
                        print(f"        {w}")
                    continue
                for w in why:                       # non-blocking notes, for a human to read
                    print(f"        {w}")
                accepted = d
                # write it to the disk cache under the key the SERVER will look for
                key = report.cache_key(ts, window_h, lang, "full", policies)
                report._cache_put(key, d, to_disk=True)
                print(f"  {ts} {lang} attempt {tries}: ACCEPTED and cached [{dt:.0f}s] "
                      f"charts={len(d['charts'])} auditor={d['auditor']['state']}")
            log.append({"virtual_ts": ts, "lang": lang, "attempts": tries,
                        "discarded_on_content": discarded, "ok": accepted is not None,
                        "charts": len(accepted["charts"]) if accepted else 0,
                        "auditor_state": accepted["auditor"]["state"] if accepted else None})
            if accepted is None:
                ok_all = False
                print(f"  {ts} {lang}: GAVE UP after {tries} attempts")

    # the frontend's demo list, so no timestamp is hardcoded in the UI
    moments = []
    for ts in stamps:
        langs = [r["lang"] for r in log if r["virtual_ts"] == ts and r["ok"]]
        f = report.assemble_facts(store, ts, policies, window_h)
        moments.append({"virtual_ts": ts, "iso": report._iso(ts),
                        "window_h": window_h, "languages": sorted(langs),
                        "flagged": f["prediction_outcomes"]["flagged_total"],
                        "catch_rate_pct": f["prediction_outcomes"]["catch_rate_pct"],
                        "onsets": f["node_onsets"]["count"]})
    with open(MOMENTS, "w", encoding="utf-8") as fh:
        json.dump({"note": "Virtual moments with a committed, pre-generated LLM report. The UI "
                           "pins the clock here with POST /api/clock {action:'goto'}.",
                   "window_h": window_h, "moments": moments}, fh, ensure_ascii=False, indent=2)

    print(f"\nwrote {MOMENTS}")
    print(f"{'attempts by entry:':<22}" + "  ".join(
        f"{r['virtual_ts']}/{r['lang']}={r['attempts']}"
        + (f"(-{r['discarded_on_content']})" if r["discarded_on_content"] else "")
        for r in log))
    print(f"entries on disk: {len(report._disk_load())}")
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="stub the agent; no network calls")
    ap.add_argument("--demo", action="store_true",
                    help="prepare the four sweep-selected demo moments, retrying until each is an "
                         "LLM report that passes the relational content check")
    ap.add_argument("--attempts", type=int, default=DEMO_ATTEMPTS)
    ap.add_argument("--limit", type=int, default=0, help="only the first N timestamps")
    ap.add_argument("--spread", type=int, default=6, help="extra timestamps across the window")
    ap.add_argument("--clear", action="store_true", help="delete the prewarm cache and exit")
    args = ap.parse_args()

    if args.clear:
        if os.path.exists(report.PREWARM_PATH):
            os.remove(report.PREWARM_PATH); print(f"removed {report.PREWARM_PATH}")
        else:
            print("nothing to remove")
        return 0

    store = Store()
    clock = VirtualClock(store.meta["window_start_ts"], store.meta["window_end_ts"])
    policies = dict(store.meta.get("default_policies", {}))
    policies.setdefault("alert_threshold", 0.30)
    policies.setdefault("node_filter_pct", 25)

    if args.mock:
        # --mock is documented as "no network", so it must ENFORCE that rather than rely on the
        # caller. Stubbing generate_llm alone stopped being enough once a second agent existed:
        # measured, `--mock` was making one live auditor call per language/length combination.
        os.environ["REPORT_AUDIT"] = "0"
        # a faithful-looking reply built FROM the facts, so it passes the hard gate exactly as a
        # real one would -- this exercises compose -> gate -> cache, only the network is stubbed.
        def _mock(facts, lang, length):
            jw, cn = facts["jobs_window"], facts["cluster_now"]
            if length == "brief":
                return (f"Last {facts['window']['hours']} h: {jw['submitted']} jobs submitted, "
                        f"{jw['ended_in_window']} ended, {jw['ended_in_window_completed']} completed. "
                        f"{cn['active_jobs']} active now."), None
            return json.dumps({
                "executive_summary": f"Over the last {facts['window']['hours']} h the cluster "
                                     f"submitted {jw['submitted']} jobs and {jw['ended_in_window']} "
                                     f"ended, of which {jw['ended_in_window_completed']} completed. "
                                     f"{cn['active_jobs']} jobs are active.",
                "risk_assessment":   f"{facts['node_onsets']['count']} node anomaly onsets were "
                                     f"detected in the window.",
                "action_playbook":   ["Review the flagged jobs before the next shift."],
                # the new contract: ids from the enum plus a caption, no data
                "chart_configs": [
                    {"chart_id": "prediction_outcomes",
                     "caption": "How the jobs flagged this window actually turned out."},
                    {"chart_id": "job_outcome_mix",
                     "caption": "Final states of the jobs that ended during the window."},
                ],
            }, ensure_ascii=False), None
        report.generate_llm = _mock

    if args.demo:
        return prepare_demo(store, policies, window_h=6, attempts=args.attempts)

    stamps = hero_timestamps()
    n_hero = len(stamps)
    stamps += spread_timestamps(store.meta["window_start_ts"], store.meta["window_end_ts"], args.spread)
    seen, ordered = set(), []
    for s in stamps:
        b = s // report.CACHE_BUCKET                    # one entry per cache bucket is enough
        if b not in seen:
            seen.add(b); ordered.append(s)
    if args.limit:
        ordered = ordered[:args.limit]

    combos = [(lang, length) for lang in ("en", "zh") for length in ("brief", "full")]
    total = len(ordered) * len(combos)
    print(f"prewarming {total} entries: {len(ordered)} timestamps "
          f"({n_hero} hero + {len(ordered) - min(n_hero, len(ordered))} spread) x {len(combos)} "
          f"language/length combinations")
    print(f"cache file: {report.PREWARM_PATH}")
    print(f"mode      : {'MOCK (no network)' if args.mock else 'LIVE (calls the agent)'}\n")

    t0 = time.time()
    ok = failed = 0
    for i, ts in enumerate(ordered, 1):
        for lang, length in combos:
            a = time.time()
            try:
                d = report.build_report(store, FixedClock(ts), policies, window_h=6,
                                        lang=lang, length=length, nocache=True, persist=True)
            except Exception as e:
                failed += 1
                print(f"  [{i}/{len(ordered)}] {ts} {lang}/{length:5s} ERROR {type(e).__name__}")
                continue
            if d["mode"] == "llm":
                ok += 1
                print(f"  [{i}/{len(ordered)}] {ts} {lang}/{length:5s} cached  ({time.time()-a:.1f}s)")
            else:
                failed += 1
                print(f"  [{i}/{len(ordered)}] {ts} {lang}/{length:5s} NOT cached: {d['fallback_reason']}")

    dt = time.time() - t0
    on_disk = len(report._disk_load())
    print(f"\ndone in {dt:.1f}s  |  cached {ok}/{total}  |  {failed} not cached  |  "
          f"{on_disk} entries on disk")
    if not args.mock and ok:
        print(f"average {dt / max(ok, 1):.1f}s per successful entry")
    print("the running app will serve these from disk on a cache miss (no live call needed)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
