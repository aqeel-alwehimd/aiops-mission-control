# In-progress: LaplaceAI 504 on report-sized prompts — state as of 2026-08-04 18:36

Stopped mid-task at the user's request. **All code changes are saved to disk and the full suite is
green.** What remains is one step (regenerating the demo reports) that needs the live endpoint.

---

## The diagnosis, settled

Not Render. `https://aiops-mission-control.onrender.com` answers `/health` and `/api/clock` in 0.2–0.3 s
(`server: cloudflare`, `rndr-id` present), and Render's own docs state they allow **100 minutes** per
HTTP request — nowhere near the 210 s budget. The template fallback is working as designed.

**The real limit is the LaplaceAI agent's own ~60-second generation wall**, not input size, and not a
total outage:

| prompt chars | successes | success latency | failure latency |
|---:|---|---|---|
| 4,543 | **3 / 4** | 54.4, 54.8, 57.2 s | 60.7 s |
| 5,986 | **4 / 8** | 42.1 – 59.9 s | 60.4 – 60.8 s |
| 7,961 | 0 / 2 | — | 60.7 s |
| 9,090 | 0 / 2 | — | 60.7 s |
| 14,446 | 0 / 2 | — | 60.9 s |
| 18,463 | 0 / 2 | — | 60.9 s / 61.0 s |

Every failure lands at **60.4–61.0 s**; every success is under 60 s. The boundary is **probabilistic,
not sharp** — the identical 5,986-char prompt failed and succeeded on consecutive attempts.
Successful calls return 3,200–5,100 characters of report, taking 42–60 s, so **the report generation
itself consumes most of the 60 s budget**; prompt size shifts the total by a few seconds, which is
enough to move success from 0 % to ~75 %, but not enough to make it comfortable.

Corroborating: the **auditor** answered 39/39 calls at an ~11,259-char prompt on the same host the
same day — larger than the narrator prompt that fails 100 % — because it emits a few hundred
characters of JSON rather than a full report. Input size is demonstrably not the binding constraint.

**Rows to ignore**: `4,938` (one connection error) and everything at `5,362 / 5,572 / 5,892` in the
fine ladder returned **HTTP 502 in 0.7 s** — a transient endpoint outage mid-run, not a size effect.
Raw logs are in `data/_measure_*.log`.

---

## What changed (all saved)

### Narrator prompt: 9,090 → 5,156 chars (−43 %)

Measured at the busiest virtual moment (ts 1664216861), lang=en.

| component | before | after |
|---|---:|---:|
| facts payload (JSON) | 3,684 | 2,749 |
| chart menu block | 2,893 | 1,177 |
| closed section vocabulary | 404 | 297 |
| instructions + rules + headers | 2,109 | 1,358 |
| **total** | **9,090** | **5,156** |

History, same facts: **5,881** before charts and sections existed → **8,685** when eight
richly-described chart ids were added → **9,090** when the closed section vocabulary was added.

Capability preserved and asserted: all **8** chart ids still offered, all **14** sections still in
the closed vocabulary sent, chart contract and facts block still labelled.

Facts fields dropped (each one the narration could only restate):
`watch_counts` (= `len(high_risk_nodes)` / `len(high_risk_jobs)`, both still present) ·
`now_iso` (byte-identical to `window.end_iso`) · `model_note.p3_threshold` + `p2_triage_pct`
(verbatim duplicates of `settings.*`) · the other language's cohort/caveat prose (98 chars of Chinese
sat in every English prompt). The two long cohort notes are merged into one short line **for the
prompt only** — the full bilingual notes remain in `facts` for the template and the raw-facts panel.
`PROMPT_EXAMPLES_CAP` 2→1 and `PROMPT_LIST_CAP` 4→3.

### Auditor prompt: 11,259 → 9,651 chars (−14 %)

| component | before | after |
|---|---:|---:|
| facts payload | 3,684 | 2,749 |
| cohort identities prose | 2,443 | 1,790 |
| worked examples + counter-example | 1,785 | 1,148 |
| instructions + schema + draft | 3,347 | 2,750 |
| **total** | **11,259** | **9,651** |

Kept intact and asserted: **every** cohort identity and non-containment, one example of **each**
failure class (containment / qualitative-no-number / quantity-as-a-word) plus the **CORRECT
counter-example**, and all five class names. Key names in `cohort_prose()` shortened to their last
segment (the dotted paths cost ~500 chars for no added meaning).

### Permanent guard

* `report.PROMPT_CEILING` — **two** ceilings, because the constraint is generation time and the two
  agents produce very different output volumes: `narrator 5200`, `auditor 11000`. Each sits below a
  size observed to work for that agent and above its current size. Env-overridable.
* `report.prompt_sizes(facts)` — both prompts, both languages, at their realistic worst.
* `app.py` logs `[report.prompt] …` at startup and warns if over ceiling.
* **`verify_prompts.py`** (new, 43 assertions) fails the build if either prompt exceeds its ceiling
  **and** if any chart id, section, cohort identity, non-containment or example class went missing —
  so the ceiling can never be met by deleting capability instead of verbosity.

Current margins: narrator_en 5,156 (44 spare) · narrator_zh 5,068 (132) · auditor_en 9,651 (1,349) ·
auditor_zh 9,487 (1,513). **The narrator margin is thin — 44 chars at the worst case.**

### Suite: 10 suites, 622 assertions, 0 failures

`cohort 31 · guardrail 24 · charts 154 · layers 37 · narration 34 · fallback 29 · resilience 43 ·
labels 79 · auditor 116 · prompts 43`

Assertions updated for the new wording (each re-pinned to the *property*, not the phrasing):
`verify_charts` (menu cohort statement), `verify_cohort` (short key names in prose),
`verify_resilience` (`watch_counts` / `now_iso` now deliberately dropped), `verify_auditor`
(4 prompt-wording checks).

---

## The trim works, and this is where it stopped

`python prewarm_reports.py --demo --attempts 10`, run at 18:18–18:36:

```
1664303261 en attempt 1: template (read timeout after 3 attempts) [151s]
1664303261 en attempt 2: template (read timeout after 3 attempts) [151s]
1664303261 en attempt 3: template (read timeout after 3 attempts) [151s]
1664303261 en attempt 4: llm but DISCARDED ON CONTENT [130s]   <- HTTP 200, 54.3s
1664303261 en attempt 5: llm but DISCARDED ON CONTENT [100s]
1664303261 en attempt 6: template (read timeout after 3 attempts) [151s]
1664303261 en attempt 7: llm but DISCARDED ON CONTENT  [91s]
```

**Three narrations got through** (54–91 s) where the untrimmed prompt produced **0 successes in ~42
consecutive attempts**. So the trim materially changed the outcome. All three were then rejected by
the content gate — I did not see which findings, because the run was stopped before the per-attempt
detail was read back.

`data/report_cache.json` and `data/demo_moments.json` do **not** exist — nothing has been cached, and
nothing stale is being advertised. That is a clean state.

---

## To resume

1. `python verify_prompts.py` — confirm sizes still fit (fast, no network).
2. `python prewarm_reports.py --demo --attempts 12 2>&1 | tee /tmp/demo3.log`
   Read the `DISCARDED ON CONTENT` lines: the gate blocks on deterministic containment /
   over-generalisation, wrong language, raw JSON leak, meta-commentary, and any high/medium auditor
   finding. If discards are dominated by auditor **omission** findings, that is the known
   over-blocking discussed earlier — recommend, do not silently loosen.
3. Adjudicate each accepted narrative by hand against `facts` before blessing it.
4. Commit `data/report_cache.json` + `data/demo_moments.json` together.
5. `python verify_demo_path.py` (real mode, not `--mock`).
6. Full suite + `python sweep_report.py --points 40`.

## Open

* Narrator margin is **44 chars** at the worst-case timestamp. Any facts growth trips
  `verify_prompts.py` — which is the guard working, but it will need another trim pass to absorb.
* Even at 5,156 chars, success is **not** guaranteed: report generation alone takes 42–60 s of a
  ~60 s wall. Prompt size is a contributing factor, not the whole cause. If reliability matters more
  than report length, the lever is asking for a **shorter report**, which is a capability change and
  therefore the user's call.
* The endpoint intermittently drops into instant-502 mode (observed ~17:55–18:05). Measurements taken
  during those windows are invalid and must be discarded, not averaged in.
