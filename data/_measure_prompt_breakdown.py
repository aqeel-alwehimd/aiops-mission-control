"""Byte-level breakdown of the narrator and auditor prompts, now and at each historical commit.

Every version is measured against the SAME facts at the same virtual timestamp, so the numbers are
comparable: only the prompt scaffolding differs between versions, not the data.
"""
import importlib.util, json, os, sys

HERE = "D:/M100/P2/dashboard"
sys.path.insert(0, HERE)
os.chdir(HERE)
os.environ["REPORT_AUDIT"] = "0"

import report as cur
from models import Store

TS, POL = 1664303261, {"alert_threshold": 0.30, "node_filter_pct": 25}
store = Store()
FACTS = cur.assemble_facts(store, TS, POL, 6)


def load(path, name):
    """Import a historical report.py, with its own charts.py alongside it if it had one."""
    d = os.path.dirname(path)
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    except Exception as e:
        print(f"  ! could not load {path}: {type(e).__name__}: {e}")
        return None
    finally:
        sys.path.remove(d)


def breakdown(mod, label):
    """-> dict of component -> bytes, for the narrator prompt this module would build."""
    try:
        payload = mod.trim_facts_for_prompt(FACTS, "en")
    except Exception:
        payload = FACTS
    prompt = mod._build_prompt(payload, "en", "full")
    facts_json = json.dumps(payload, ensure_ascii=False)

    menu = ""
    if hasattr(mod, "_chart_menu_block"):
        try:
            menu = mod._chart_menu_block("en")
        except Exception:
            menu = ""
    # the section-vocabulary sentence, when the version has a closed set
    sect = ""
    if hasattr(mod, "_SECTION_TITLES"):
        joined = ", ".join(sorted(mod._SECTION_TITLES))
        for line in prompt.split("\n"):
            if joined[:40] in line:
                sect = line
                break

    total = len(prompt)
    comp = {
        "facts payload (JSON)": len(facts_json),
        "chart menu block": len(menu),
        "closed section vocabulary": len(sect),
    }
    comp["instructions + rules + headers"] = total - sum(comp.values())
    return {"label": label, "total": total, "components": comp,
            "n_chart_ids": (len(getattr(mod, "chartreg", None).CHART_MENU)
                            if getattr(mod, "chartreg", None) is not None else 0),
            "n_sections": len(getattr(mod, "_SECTION_TITLES", {})),
            "n_rules": prompt.count("\n- ")}


def audit_size(mod, label):
    if not hasattr(mod, "_audit_prompt"):
        return None
    try:
        payload = mod.trim_facts_for_prompt(FACTS, "en")
        kw = {}
        import inspect
        sig = inspect.signature(mod._audit_prompt)
        if "identities" in sig.parameters:
            kw["identities"] = mod.cohort_prose(FACTS)
        if "captions" in sig.parameters:
            kw["captions"] = [("prediction_outcomes",
                               "How the jobs flagged this window actually turned out.")]
        p = mod._audit_prompt(payload, "DRAFT NARRATIVE " * 40, **kw)
        facts_json = json.dumps(payload, ensure_ascii=False)
        ident = kw.get("identities", "")
        examples = getattr(mod, "_AUDIT_EXAMPLES", "")
        comp = {"facts payload (JSON)": len(facts_json),
                "cohort identities prose": len(ident),
                "worked examples + counter-example": len(examples)}
        comp["instructions + schema + draft"] = len(p) - sum(comp.values())
        return {"label": label, "total": len(p), "components": comp}
    except Exception as e:
        print(f"  ! audit prompt for {label}: {type(e).__name__}: {e}")
        return None


VERSIONS = [
    ("C:/Users/aqeel/AppData/Local/Temp/hist/24ee082/report.py", "24ee082  before charts + sections"),
    ("C:/Users/aqeel/AppData/Local/Temp/hist/51f8b58/report.py", "51f8b58  charts added"),
    ("C:/Users/aqeel/AppData/Local/Temp/hist/818c516/report.py", "818c516  auditor added"),
]

rows = []
for path, label in VERSIONS:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        continue
    m = load(path, "hist_" + label.split()[0])
    if m is None:
        continue
    try:
        rows.append((breakdown(m, label), audit_size(m, label)))
    except Exception as e:
        print(f"  ! breakdown for {label}: {type(e).__name__}: {e}")
rows.append((breakdown(cur, "HEAD     current"), audit_size(cur, "HEAD     current")))

print("=" * 96)
print("NARRATOR PROMPT, same facts at ts=1664303261, lang=en, length=full")
print("=" * 96)
keys = ["facts payload (JSON)", "chart menu block", "closed section vocabulary",
        "instructions + rules + headers"]
print(f"  {'version':<34} {'total':>7} " + " ".join(f"{k.split('(')[0][:13]:>14}" for k in keys))
for b, _ in rows:
    print(f"  {b['label']:<34} {b['total']:>7} "
          + " ".join(f"{b['components'][k]:>14}" for k in keys)
          + f"   ids={b['n_chart_ids']} sections={b['n_sections']} rules={b['n_rules']}")

print()
print("=" * 96)
print("AUDITOR PROMPT")
print("=" * 96)
akeys = ["facts payload (JSON)", "cohort identities prose",
         "worked examples + counter-example", "instructions + schema + draft"]
print(f"  {'version':<34} {'total':>7} " + " ".join(f"{k.split('(')[0][:15]:>17}" for k in akeys))
for _, a in rows:
    if a is None:
        continue
    print(f"  {a['label']:<34} {a['total']:>7} "
          + " ".join(f"{a['components'][k]:>17}" for k in akeys))
