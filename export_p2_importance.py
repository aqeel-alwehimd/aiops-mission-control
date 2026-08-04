"""
export_p2_importance.py -- ADD the node model's global importance to data/meta.json.

Run:  python export_p2_importance.py            (offline; needs joblib + lightgbm)
      python export_p2_importance.py --dry-run

WHAT THIS COSTS, AND WHY IT IS THIS METRIC
The P2 model is a LightGBM classifier, so a global importance is a FREE export -- the trained
booster already carries it. `booster_.feature_importance("gain")` returns in ~0 ms once the model
is loaded (the 30 s cost is joblib.load of the 363-feature model, once, offline). No re-fit, no
notebook re-run, and no permutation pass over the test set.

It is therefore a DIFFERENT METRIC from P3's, and that difference is not cosmetic:

  P3  permutation importance -- mean drop in test ROC-AUC when the feature's values are shuffled.
                                Measures how much the model's ACCURACY depends on the feature.
  P2  native gain            -- total reduction in the training loss across every split made on
                                the feature. Measures how much the model USED the feature while
                                fitting.

A feature can score high on one and low on the other. The two panels are consequently NOT
comparable, and the UI says so in both languages rather than letting them sit side by side as if
they were the same measurement.

Writing: meta.json gains one key, `p2_global_importance`. Nothing else in the file is touched, and
the existing keys are fingerprinted before and after to prove it. The serving app then reads it like
P3's, so lightgbm and joblib stay out of requirements.txt.
"""
import argparse, hashlib, json, os, sys, time

P2DIR      = "D:/M100/P2"
P09_MODELS = P2DIR + "/p09_models.joblib"
P09_META   = P2DIR + "/p09_meta.json"
MODEL_KEY  = ("combined", 30, "STRICT", "LightGBM")
TOP_N      = 20        # the panel shows 12; keep a little headroom in the file
IMPORTANCE_TYPE = "gain"

HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "data", "meta.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import joblib
    import numpy as np

    meta = json.load(open(META, encoding="utf-8"))
    before = {k: hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False).encode()
                                ).hexdigest()[:12] for k, v in meta.items()}

    t0 = time.time()
    mdl = joblib.load(P09_MODELS)[0][MODEL_KEY]
    names = json.load(open(P09_META, encoding="utf-8"))["all"]
    assert mdl.n_features_in_ == len(names) == 363, "feature order does not match the model"
    load_s = time.time() - t0

    t1 = time.time()
    imp = mdl.booster_.feature_importance(importance_type=IMPORTANCE_TYPE)
    calc_s = time.time() - t1
    total = float(imp.sum())

    order = np.argsort(-imp)[:TOP_N]
    rows = [{"feature": names[i],
             "importance": round(float(imp[i]), 1),
             "share_pct": round(100.0 * float(imp[i]) / total, 2)} for i in order]

    print(f"model load      : {load_s:.1f}s")
    print(f"importance calc : {calc_s*1000:.1f}ms   <-- free, the booster already carries it")
    print(f"non-zero feats  : {int((imp > 0).sum())}/{len(imp)}")
    print(f"top {TOP_N} written, led by:")
    for r in rows[:6]:
        print(f"    {r['feature']:24} {r['importance']:>12.1f}  {r['share_pct']:>5.2f}%")

    if a.dry_run:
        print("\ndry run: meta.json not written")
        return 0

    meta["p2_global_importance"] = rows
    meta["p2_importance_metric"] = IMPORTANCE_TYPE
    tmp = META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp, META)

    after = json.load(open(META, encoding="utf-8"))
    changed = [k for k, h in before.items()
               if hashlib.sha256(json.dumps(after.get(k), sort_keys=True, ensure_ascii=False)
                                 .encode()).hexdigest()[:12] != h]
    print(f"\nmeta.json: added p2_global_importance ({len(rows)} rows) + p2_importance_metric")
    print(f"  pre-existing keys changed: {changed or 'none'}")
    return 0 if not changed else 1


if __name__ == "__main__":
    sys.exit(main())
