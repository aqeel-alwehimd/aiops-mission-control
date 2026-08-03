"""
feature_labels.py -- human-readable bilingual names for EVERY model feature.

WHY THIS EXISTS
The P2 node model has 363 features. meta.json carried hand-written labels for 20 of them, and
models.py resolved names with `p2_label.get(name, (name, name))` -- so the other 343 fell through
to the raw identifier, silently. The per-prediction contributions chart picks its bars by
|contribution| over all 363, so in practice most shifts surfaced a mix of "GPU0 core temp (°C)" and
`fleetmax_nz_g0_cT`, which reads like a rendering bug and is unusable in front of a mentor.

Hand-writing 363 labels would be unmaintainable and would rot the moment the feature set changed.
The names are not arbitrary, though: they are COMPOSITIONAL, `<statistic><window>_<sensor>`. So this
module decomposes a name into a statistic and a base sensor and composes a label from two small
tables. 40 sensors x ~28 statistics covers all 363, and a new feature built from known parts is
labelled automatically.

TWO NAMES THAT LOOK LIKE ONE, AND ARE NOT
`s30m_p0_vddT` and `slope30m_p0_vddT` are distinct model features (indices 266 and 269): `s30m_` is
the 30-minute standard deviation, `slope30m_` the 30-minute slope. They were indistinguishable on
screen only because neither had a label. Likewise `slope30m_totP` and `slope30m_p0P` are total node
power vs CPU-0 power -- the old curated label for the first, "Power, 30-min slope", named neither,
which is why they read as duplicates. Compositional labels state the sensor and the statistic
separately, so sibling features can never collapse into the same text.

MISSING LABELS ARE A TEST FAILURE, NOT A SILENT FALLBACK
`missing_labels()` returns every feature this module cannot name, and verify_labels.py fails the
build when that list is non-empty. Set FEATURE_LABEL_STRICT=1 to make `label()` itself raise instead
of falling back -- that is what the test uses.
"""
import os
import re

# ============================================================ base sensors (the thing measured)
# (en, zh). Units belong here, not in the statistic, because a slope or a z-score of a quantity
# does not carry the quantity's unit.
BASE_SENSORS = {
    # --- node / CPU power ---
    "totP":        ("Total node power",        "節點總功耗"),
    "totPMX":      ("Total node power, peak",  "節點總功耗 (峰值)"),
    "totPSD":      ("Total node power, spread", "節點總功耗 (離散度)"),
    "p0P":         ("CPU0 power",              "CPU0 功耗"),
    "p1P":         ("CPU1 power",              "CPU1 功耗"),
    "p0_ioP":      ("CPU0 I/O power",          "CPU0 I/O 功耗"),
    "p1_ioP":      ("CPU1 I/O power",          "CPU1 I/O 功耗"),
    "p0_memP":     ("CPU0 memory power",       "CPU0 記憶體功耗"),
    "p1_memP":     ("CPU1 memory power",       "CPU1 記憶體功耗"),
    # --- PSU ---
    "ps0_inputP":  ("PSU-0 input power",       "PSU-0 輸入功率"),
    "ps1_inputP":  ("PSU-1 input power",       "PSU-1 輸入功率"),
    # --- temperatures ---
    "g0_cT":       ("GPU0 core temp",          "GPU0 核心溫度"),
    "g1_cT":       ("GPU1 core temp",          "GPU1 核心溫度"),
    "g3_cT":       ("GPU3 core temp",          "GPU3 核心溫度"),
    "g4_cT":       ("GPU4 core temp",          "GPU4 核心溫度"),
    "g0_cTMX":     ("GPU0 core temp, peak",    "GPU0 核心溫度 (峰值)"),
    "g0_cTSD":     ("GPU0 core temp, spread",  "GPU0 核心溫度 (離散度)"),
    "g0_memT":     ("GPU0 memory temp",        "GPU0 記憶體溫度"),
    "g1_memT":     ("GPU1 memory temp",        "GPU1 記憶體溫度"),
    "p0_vddT":     ("CPU0 Vdd temp",           "CPU0 Vdd 溫度"),
    "p1_vddT":     ("CPU1 Vdd temp",           "CPU1 Vdd 溫度"),
    "dimm0T":      ("DIMM 0 temp",             "DIMM 0 溫度"),
    "dimm4T":      ("DIMM 4 temp",             "DIMM 4 溫度"),
    "dimm8T":      ("DIMM 8 temp",             "DIMM 8 溫度"),
    "dimm12T":     ("DIMM 12 temp",            "DIMM 12 溫度"),
    "pcie":        ("PCIe temp",               "PCIe 溫度"),
    "amb":         ("Ambient temp",            "環境溫度"),
    # --- fans ---
    "fan0_0":      ("Fan 0 speed",             "風扇 0 轉速"),
    "fan1_0":      ("Fan 1 speed",             "風扇 1 轉速"),
    "fan2_0":      ("Fan 2 speed",             "風扇 2 轉速"),
    "fan3_0":      ("Fan 3 speed",             "風扇 3 轉速"),
    "fan_diskP":   ("Disk-fan power",          "磁碟風扇功耗"),
    # --- fleet-level scalars ---
    "anom":        ("Fleet anomaly count",     "全機群異常數"),
    "anom_frac":   ("Fleet anomaly fraction",  "全機群異常比例"),
}
# the "non-zero" variants used by the fleet aggregates: same sensor, restricted to reporting nodes
for _b in ("totP", "g0_cT", "p0_vddT", "amb", "fan0_0", "p0P"):
    BASE_SENSORS["nz_" + _b] = (BASE_SENSORS[_b][0], BASE_SENSORS[_b][1])

# ============================================================ statistics (what is done to it)
# "{}" is filled with the base sensor's name.
STATISTICS = {
    "cur_":         ("{}",                          "{}"),
    # rolling moments
    "m30m_":        ("{}, 30-min mean",             "{} 30 分鐘均值"),
    "m2h_":         ("{}, 2-h mean",                "{} 2 小時均值"),
    "m6h_":         ("{}, 6-h mean",                "{} 6 小時均值"),
    "s30m_":        ("{}, 30-min variability",      "{} 30 分鐘變異度"),
    "s2h_":         ("{}, 2-h variability",         "{} 2 小時變異度"),
    "s6h_":         ("{}, 6-h variability",         "{} 6 小時變異度"),
    # trend
    "slope30m_":    ("{}, 30-min trend",            "{} 30 分鐘趨勢"),
    "slope2h_":     ("{}, 2-h trend",               "{} 2 小時趨勢"),
    "slope6h_":     ("{}, 6-h trend",               "{} 6 小時趨勢"),
    "accel_":       ("{}, trend acceleration",      "{} 趨勢加速度"),
    "ewmaf_":       ("{}, fast moving average",     "{} 快速移動平均"),
    "ewmas_":       ("{}, slow moving average",     "{} 慢速移動平均"),
    # range
    "lo30m_":       ("{}, 30-min low",              "{} 30 分鐘最低"),
    "hi30m_":       ("{}, 30-min high",             "{} 30 分鐘最高"),
    "rng30m_":      ("{}, 30-min range",            "{} 30 分鐘全距"),
    "lo2h_":        ("{}, 2-h low",                 "{} 2 小時最低"),
    "hi2h_":        ("{}, 2-h high",                "{} 2 小時最高"),
    "rng2h_":       ("{}, 2-h range",               "{} 2 小時全距"),
    "lo6h_":        ("{}, 6-h low",                 "{} 6 小時最低"),
    "hi6h_":        ("{}, 6-h high",                "{} 6 小時最高"),
    "rng6h_":       ("{}, 6-h range",               "{} 6 小時全距"),
    # baselines
    "z7d_":         ("{}, z-score vs 7-day",        "{} z 分數 (對 7 日基線)"),
    "z30d_":        ("{}, z-score vs 30-day",       "{} z 分數 (對 30 日基線)"),
    # spatial context
    "nb_mean_nz_":  ("{}, rack-neighbour mean",     "{} 同機櫃鄰居平均"),
    "nb_max_nz_":   ("{}, rack-neighbour max",      "{} 同機櫃鄰居最大"),
    "fleetmax_nz_": ("{}, fleet max",               "{} 全機群最大"),
    "fleet_":       ("{}",                          "{}"),
    "dev_med_nz_":  ("{}, deviation from fleet median", "{} 相對全機群中位數偏差"),
}
_LAG = re.compile(r"^lag(\d+)_(.+)$")

# ============================================================ standalone features (no sensor base)
STANDALONE = {
    "rk_anom_o":  ("Rack anomaly rank",              "機櫃異常排名"),
    "rk_pre_o":   ("Rack pre-anomaly rank",          "機櫃前兆排名"),
    "rk_n":       ("Nodes reporting in rack",        "機櫃回報節點數"),
    "hour":       ("Hour of day (UTC)",              "當日時段 (UTC)"),
    "dow":        ("Day of week",                    "星期"),
    "is_weekend": ("Weekend",                        "週末"),
}

# longest-first so "nb_mean_nz_" wins over "nb_", and "slope30m_" over "s30m_"... which it must:
# `s30m_p0_vddT` and `slope30m_p0_vddT` are DIFFERENT FEATURES, and a shortest-first match would
# label the slope as a variability and hide the distinction that made them look duplicated.
_PREFIXES = sorted(STATISTICS, key=len, reverse=True)

_warned = set()


def _strict() -> bool:
    return str(os.environ.get("FEATURE_LABEL_STRICT", "")).strip().lower() in ("1", "true", "yes")


def split_feature(name: str):
    """-> (statistic_prefix, base_sensor) or None when the name does not decompose."""
    if name in STANDALONE:
        return ("", name)
    m = _LAG.match(name)
    if m and m.group(2) in BASE_SENSORS:
        return (f"lag{m.group(1)}_", m.group(2))
    for p in _PREFIXES:
        if name.startswith(p):
            base = name[len(p):]
            if base in BASE_SENSORS:
                return (p, base)
    return None


def label(name: str, curated: dict = None):
    """-> (en, zh) for one feature name.

    `curated` is an optional {feature: (en, zh)} override -- meta.json's hand-written labels, which
    stay authoritative where they exist so existing screens keep their wording.
    """
    name = str(name)
    if curated and name in curated:
        return curated[name]
    if name in STANDALONE:
        return STANDALONE[name]

    parts = split_feature(name)
    if parts is None:
        if _strict():
            raise KeyError(f"no label mapping for feature {name!r} -- add its base sensor to "
                           f"BASE_SENSORS or its statistic to STATISTICS in feature_labels.py")
        if name not in _warned:                 # once per name, not once per render
            _warned.add(name)
            print(f"[feature_labels] WARNING: no mapping for {name!r}; showing the raw identifier",
                  flush=True)
        return (name, name)

    prefix, base = parts
    if prefix == "" or base in STANDALONE:
        return STANDALONE.get(base, (base, base))
    ben, bzh = BASE_SENSORS[base]
    m = _LAG.match(name)
    if m:
        n = m.group(1)
        return (f"{ben}, {n} slot{'s' if n != '1' else ''} ago", f"{bzh} (前 {n} 個時槽)")
    ten, tzh = STATISTICS[prefix]
    return (ten.format(ben), tzh.format(bzh))


def missing_labels(features) -> list:
    """Every feature this module cannot name. verify_labels.py fails when this is non-empty."""
    return [f for f in features if f not in STANDALONE and split_feature(f) is None]


def coverage(features):
    """-> (n_covered, n_total, [missing]). Handy in a console."""
    miss = missing_labels(features)
    return (len(features) - len(miss), len(features), miss)
