"""
audit_heuristics.py -- false-positive risk in the ADVISORY heuristics. Exits; no server.

Run:  python audit_heuristics.py

Specifically probes the interaction the brief called out: facts carry model_note.caveat_zh, a
Chinese string, so an English report that legitimately quotes it raises the CJK ratio.
"""
import report
from report import _cjk_ratio, MIN_REPORT_CHARS, advisory_review, no_report_content
from diagnose_guardrail import FACTS

CAVEAT_ZH = ("OOM 為低估項 (此資料集 SLURM 記憶體請求欄位為空)，故 OOM 鮮少被預測為首要故障型態；"
             "此為 2022-09 測試期單一片段的回放。")

EN_FULL = (
    "## Executive summary\nOver the last 24 h the cluster submitted 1501 jobs and 1244 ended, of "
    "which 1066 COMPLETED. 1374 jobs are currently active and utilisation sits near 68.6%.\n\n"
    "## Risk assessment\nP2 detected 6 node anomaly onsets. node0697 in rack 34 carries the highest "
    "score at 41.2% while running 38.8 C. P3 caught 30 of 77 failures and missed 47, with 182 false "
    "alarms against a 0.3 threshold.\n\n"
    "## Action playbook\n- Inspect node0697 in rack 34.\n- Review the 47 missed failures.\n")
EN_BRIEF = ("Last 24 h: 1501 jobs submitted, 1244 ended, 1066 COMPLETED. 6 node anomaly onsets; "
            "node0697 leads at 41.2%.")

print("=" * 100)
print("AUDIT 1 - CJK ratio when an English report quotes model_note.caveat_zh")
print("=" * 100)
print(f"  caveat_zh is {len(CAVEAT_ZH)} chars, CJK ratio of the caveat alone: {_cjk_ratio(CAVEAT_ZH):.0%}")
print(f"  advisory threshold for lang='en': CJK > 15%")
print()
rows = [
    ("full report, no caveat",                 EN_FULL,                          "full"),
    ("full report + caveat quoted once",       EN_FULL + "\n## Model note\n- " + CAVEAT_ZH, "full"),
    ("full report + caveat quoted twice",      EN_FULL + "\n" + CAVEAT_ZH + "\n" + CAVEAT_ZH, "full"),
    ("BRIEF report, no caveat",                EN_BRIEF,                         "brief"),
    ("BRIEF report + caveat quoted",           EN_BRIEF + " " + CAVEAT_ZH,       "brief"),
]
print(f"  {'scenario':<38} {'chars':>6} {'CJK':>6}  advisory raised?")
print("  " + "-" * 84)
for name, text, length in rows:
    adv = advisory_review(text, "en", length)
    codes = [a["code"] for a in adv]
    print(f"  {name:<38} {len(text):>6} {_cjk_ratio(text):>5.0%}  "
          f"{'YES -> ' + ','.join(codes) if codes else 'no'}")

print()
print("  FINDING:")
brief_adv = advisory_review(EN_BRIEF + " " + CAVEAT_ZH, "en", "brief")
full_adv = advisory_review(EN_FULL + "\n## Model note\n- " + CAVEAT_ZH, "en", "full")
print(f"    - a FULL English report quoting the caveat once: "
      f"{'trips' if any(a['code']=='language_mismatch' for a in full_adv) else 'does NOT trip'} the check")
print(f"    - a BRIEF English report quoting the caveat:     "
      f"{'TRIPS' if any(a['code']=='language_mismatch' for a in brief_adv) else 'does not trip'} the check")
print("    - the caveat is ~1/3 of a brief report's length, so the ratio crosses 15% easily there.")
print("    - Because language is now ADVISORY, this costs a visible flag, not a discarded report.")
print("      That is precisely the class of false positive the demotion was meant to de-fang.")

print()
print("=" * 100)
print("AUDIT 2 - are the per-length minimum-content thresholds still sane?")
print("=" * 100)
print(f"  thresholds: {MIN_REPORT_CHARS}")
tpl_brief = report.render_template(FACTS, "en", "brief")
tpl_full = report.render_template(FACTS, "en", "full")
print(f"  a real template BRIEF report is {len(tpl_brief)} chars  -> "
      f"{'above' if len(tpl_brief) >= MIN_REPORT_CHARS['brief'] else 'BELOW'} the {MIN_REPORT_CHARS['brief']} floor")
print(f"  a real template FULL  report is {len(tpl_full)} chars  -> "
      f"{'above' if len(tpl_full) >= MIN_REPORT_CHARS['full'] else 'BELOW'} the {MIN_REPORT_CHARS['full']} floor")
print(f"  the LLM brief fixture above is {len(EN_BRIEF)} chars -> "
      f"{'above' if len(EN_BRIEF) >= MIN_REPORT_CHARS['brief'] else 'BELOW'} the floor")
print("  => margins are comfortable; and a short report is now only flagged, never rejected.")

print()
print("=" * 100)
print("AUDIT 3 - what still HARD-fails (the one blocking content check)")
print("=" * 100)
for name, text, length in [
    ("empty string",                       "", "full"),
    ("whitespace only",                    "   \n  ", "full"),
    ("pure meta-commentary, no body",
     "我已為您產生完整的交接報告，並已輸出為 shift_report.json 檔案。", "full"),
    ("a genuinely short but real report",   EN_BRIEF, "brief"),
    ("a full report in the wrong language", CAVEAT_ZH * 3, "en"),
]:
    r = no_report_content(text, length if length in ("brief", "full") else "full")
    print(f"  {name:<40} -> {'HARD FAIL: ' + r if r else 'passes the gate (advisory only)'}")
