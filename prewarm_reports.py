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
import argparse, json, os, sys, time

import report
from models import Store
from replay import VirtualClock

HERE = os.path.dirname(os.path.abspath(__file__))
HEROES = os.path.join(HERE, "data", "hero_examples.json")


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="stub the agent; no network calls")
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
        # a faithful-looking reply built FROM the facts, so it passes the hard gate exactly as a
        # real one would -- this exercises compose -> gate -> cache, only the network is stubbed.
        def _mock(facts, lang, length):
            jw, cn = facts["jobs_window"], facts["cluster_now"]
            if length == "brief":
                return (f"Last {facts['window']['hours']} h: {jw['submitted']} jobs submitted, "
                        f"{jw['ended']} ended, {jw['ended_completed']} completed. "
                        f"{cn['active_jobs']} active now."), None
            return json.dumps({
                "executive_summary": f"Over the last {facts['window']['hours']} h the cluster "
                                     f"submitted {jw['submitted']} jobs and {jw['ended']} ended, of "
                                     f"which {jw['ended_completed']} completed. "
                                     f"{cn['active_jobs']} jobs are active.",
                "risk_assessment":   f"{facts['node_onsets']['count']} node anomaly onsets were "
                                     f"detected in the window.",
                "action_playbook":   ["Review the flagged jobs before the next shift."],
            }, ensure_ascii=False), None
        report.generate_llm = _mock

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
