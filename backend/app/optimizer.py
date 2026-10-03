"""SciPy 线性规划求解（HiGHS）。

决策变量：x_i = 各原料干基份额（小数），Σx_i=1。
率值约束全部线性化，例如 SM=SiO2/(Al2O3+Fe2O3) ∈ [lo,hi] 等价于：
    Σ(SiO2_i - hi*(Al2O3_i+Fe2O3_i)) x_i ≤ 0
    Σ(lo*(Al2O3_i+Fe2O3_i) - SiO2_i) x_i ≤ 0
其余同理。无解时用“最小违约松弛模型”定位冲突项。
"""
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from . import chemistry

COMPONENT_ORDER = [
    "CaO", "SiO2", "Al2O3", "Fe2O3",
    "MgO", "SO3", "K2O", "Na2O", "Cl", "LOI",
]
HAZARD_CALC = ["MgO", "SO3", "K2O", "Na2O", "Cl"]


@dataclass
class Row:
    material_id: int
    code: str
    name: str
    moisture_pct: float
    cost_per_t_wet: float
    availability_t_wet: float | None
    min_share_pct: float
    assay_version_id: int
    version: str
    lab_report_no: str
    basis: str
    composition_raw: dict          # 原始化验单（可能湿基）
    composition_dry: dict          # 换算后干基 %
    measured: set[str]
    uncertainties_native: dict = None  # 与化验单同基准的偏差（归一化后）
    bounds_dry: dict = None            # 各组分干基 nominal/lower/upper/zero_tolerance


def prepare_rows(candidates: list) -> list[Row]:
    """candidates: 已联表查询好的 (material, assay_version) 二元组列表。"""
    rows = []
    for mat, ass in candidates:
        comp_dry = chemistry.convert_composition(
            ass.composition, ass.basis, mat.moisture_pct
        )
        unc_native = chemistry.normalize_uncertainties(
            getattr(ass, "uncertainties", None) or {},
            ass.measured_oxides,
            material_code=mat.code, material_name=mat.name,
            assay_version=ass.version, lab_report_no=ass.lab_report_no,
        )
        bdry = chemistry.bounds_dry(
            comp_dry, unc_native, ass.basis, mat.moisture_pct
        )
        rows.append(
            Row(
                material_id=mat.id,
                code=mat.code,
                name=mat.name,
                moisture_pct=mat.moisture_pct,
                cost_per_t_wet=mat.cost_per_t_wet,
                availability_t_wet=mat.availability_t_wet,
                min_share_pct=mat.min_share_pct,
                assay_version_id=ass.id,
                version=ass.version,
                lab_report_no=ass.lab_report_no,
                basis=ass.basis,
                composition_raw=dict(ass.composition),
                composition_dry=comp_dry,
                measured=set(ass.measured_oxides),
                uncertainties_native=unc_native,
                bounds_dry=bdry,
            )
        )
    return rows


def has_uncertainty(rows: list[Row]) -> bool:
    return any(r.uncertainties_native for r in rows)


def _comp_arrays(rows: list[Row], worst: bool):
    """返回 {comp: (lo_arr, nominal_arr, hi_arr)}（干基百分点）。worst=False 三者相同。"""
    comps = set()
    for r in rows:
        comps.update(r.composition_dry.keys())
    arrays = {}
    for c in comps:
        nom, lo, hi = [], [], []
        for r in rows:
            b = (r.bounds_dry or {}).get(c)
            v = r.composition_dry.get(c, 0.0)
            nom.append(v)
            if worst and b is not None and not b["zero_tolerance"]:
                lo.append(b["lower"]); hi.append(b["upper"])
            else:
                lo.append(v); hi.append(v)
        arrays[c] = (np.array(lo), np.array(nom), np.array(hi))
    return arrays


# 各率值约束在最坏边界下的组分取值方向（稳健可行要求每个独立组分都取最不利端）
WORST_DIR = {
    "SM_max": {"SiO2": "hi", "Al2O3": "lo", "Fe2O3": "lo"},
    "SM_min": {"SiO2": "lo", "Al2O3": "hi", "Fe2O3": "hi"},
    "IM_max": {"Al2O3": "hi", "Fe2O3": "lo"},
    "IM_min": {"Al2O3": "lo", "Fe2O3": "hi"},
    "KH_max": {"CaO": "hi", "SiO2": "lo", "Al2O3": "lo", "Fe2O3": "lo"},
    "KH_min": {"CaO": "lo", "SiO2": "hi", "Al2O3": "hi", "Fe2O3": "hi"},
}


def _pick_comp(arrays: dict, comp: str, end: str | None) -> np.ndarray:
    if comp not in arrays:
        return None
    lo, nom, hi = arrays[comp]
    return {"lo": lo, "hi": hi, None: nom}[end]


def _hazard_value(row: Row, key: str, end: str | None = None) -> float:
    """干基有害组分值；end="hi"/"lo" 取不确定边界，None 取名义值。"""
    comp = row.composition_dry
    b = row.bounds_dry

    def one(cname: str) -> float:
        v = comp.get(cname, 0.0)
        if end and b and cname in b and not b[cname]["zero_tolerance"]:
            return b[cname]["upper"] if end == "hi" else b[cname]["lower"]
        return v

    if key == "alkali_eq":
        if "K2O" not in row.measured or "Na2O" not in row.measured:
            raise chemistry.MissingAssayError(
                [{
                    "material_code": row.code, "material_name": row.name,
                    "assay_version": row.version, "lab_report_no": row.lab_report_no,
                    "component": "alkali_eq(需 Na2O 与 K2O 均实测)",
                }]
            )
        return one("Na2O") + chemistry.ALKALI_EQ_FACTOR_K2O * one("K2O")
    if key not in row.measured:
        raise chemistry.MissingAssayError(
            [{
                "material_code": row.code, "material_name": row.name,
                "assay_version": row.version, "lab_report_no": row.lab_report_no,
                "component": key,
            }]
        )
    return one(key)


def _build_constraints(rows: list[Row], req, worst: bool = False) -> tuple[list, list, list[dict]]:
    """构造 A_ub x ≤ b_ub（含上下界/率值/有害组分/可用量）。

    worst=True 时，每个系数取使该约束最不利的检测误差端点（独立组分各自取界），
    即稳健可行方案必须在最坏边界组合下同时满足全部约束。
    返回 A, b, meta；meta 每项带 kind/label/limit，供冲突诊断换算物理量。
    """
    n = len(rows)
    A, b, meta = [], [], []

    def add(coefs, rhs, kind, label, limit=None):
        A.append([float(v) for v in coefs])
        b.append(float(rhs))
        meta.append({"kind": kind, "label": label, "limit": limit,
                     "rhs": float(rhs), "worst": worst})

    arrays = _comp_arrays(rows, worst)

    def V(comp, end):
        return _pick_comp(arrays, comp, end if worst else None)

    S, Aa, F, Cc = V("SiO2", None), V("Al2O3", None), V("Fe2O3", None), V("CaO", None)
    if worst:
        # 率值行在各约束里方向不同，下面逐条重取；名义数组保留备用
        S_n, A_n, F_n, C_n = S, Aa, F, Cc

    t = req.targets

    def line(kind, comps_dirs, rhs, label, limit):
        """comps_dirs: [(comp, coef, end_hi?, end_lo?)]——按最坏方向取端。"""
        if not worst:
            coefs = sum(coef * arrays[c][1] for c, coef, _e in comps_dirs)
        else:
            parts = []
            for c, coef, end in comps_dirs:
                arr = _pick_comp(arrays, c, end)
                if arr is not None:
                    parts.append(coef * arr)
            coefs = sum(parts) if parts else np.zeros(n)
        add(coefs, rhs, kind, label, limit)

    if t.SM.max is not None:
        line("SM_max",
             [("SiO2", 1.0, "hi"), ("Al2O3", -t.SM.max, "lo"),
              ("Fe2O3", -t.SM.max, "lo")],
             0.0, f"硅率上限 SM≤{t.SM.max}（{'最坏边界' if worst else '名义值'}）", t.SM.max)
    if t.SM.min is not None:
        line("SM_min",
             [("SiO2", -1.0, "lo"), ("Al2O3", t.SM.min, "hi"),
              ("Fe2O3", t.SM.min, "hi")],
             0.0, f"硅率下限 SM≥{t.SM.min}（{'最坏边界' if worst else '名义值'}）", t.SM.min)
    if t.IM.max is not None:
        line("IM_max",
             [("Al2O3", 1.0, "hi"), ("Fe2O3", -t.IM.max, "lo")],
             0.0, f"铝率上限 IM≤{t.IM.max}（{'最坏边界' if worst else '名义值'}）", t.IM.max)
    if t.IM.min is not None:
        line("IM_min",
             [("Al2O3", -1.0, "lo"), ("Fe2O3", t.IM.min, "hi")],
             0.0, f"铝率下限 IM≥{t.IM.min}（{'最坏边界' if worst else '名义值'}）", t.IM.min)
    if t.KH.max is not None:
        line("KH_max",
             [("CaO", 1.0, "hi"), ("SiO2", -t.KH.max * 2.8, "lo"),
              ("Al2O3", -1.65, "lo"), ("Fe2O3", -0.35, "lo")],
             0.0, f"石灰饱和上限 KH≤{t.KH.max}（{'最坏边界' if worst else '名义值'}）", t.KH.max)
    if t.KH.min is not None:
        line("KH_min",
             [("CaO", -1.0, "lo"), ("SiO2", t.KH.min * 2.8, "hi"),
              ("Al2O3", 1.65, "hi"), ("Fe2O3", 0.35, "hi")],
             0.0, f"石灰饱和下限 KH≥{t.KH.min}（{'最坏边界' if worst else '名义值'}）", t.KH.min)

    # --- 有害组分干基上限（最坏情况：各原料独立取上界） ---
    for key, limit in (req.hazard_limits_pct or {}).items():
        add([_hazard_value(r, key, "hi" if worst else None) for r in rows],
            float(limit), "hazard_" + key,
            f"有害组分上限 {key}≤{limit}%(干基，{'最坏边界' if worst else '名义值'})",
            float(limit))

    # --- 可用量（湿基）：B*x/(1-m) ≤ avail → x ≤ avail*(1-m)/B ---
    B = req.batch_t_dry
    for i, r in enumerate(rows):
        if r.availability_t_wet is not None:
            ub = r.availability_t_wet * (1.0 - r.moisture_pct / 100.0) / B
            e = np.zeros(n)
            e[i] = 1.0
            add(e, ub, "avail",
                f"{r.name} 可用量≤{r.availability_t_wet:g}t(湿基)", ub)

    return A, b, meta


def _bounds(rows: list[Row]):
    return [(max(0.0, r.min_share_pct / 100.0), 1.0) for r in rows]


def _cost_coef(rows: list[Row]) -> np.ndarray:
    """每吨干生料的湿料成本系数：c_wet/(1-m)。"""
    return np.array([
        r.cost_per_t_wet / (1.0 - r.moisture_pct / 100.0) for r in rows
    ])


def _solve_lp(rows, req, c_obj, extra_ub=None, worst=False):
    n = len(rows)
    A, b, meta = _build_constraints(rows, req, worst=worst)
    if extra_ub:
        for (coefs, rhs, info) in extra_ub:
            A.append(list(coefs)); b.append(float(rhs)); meta.append(info)
    res = linprog(
        c_obj,
        A_ub=np.array(A) if A else None,
        b_ub=np.array(b) if b else None,
        A_eq=np.ones((1, n)),
        b_eq=np.array([1.0]),
        bounds=_bounds(rows),
        method="highs",
    )
    return res, A, b, meta


def _worst_blend_values(rows: list[Row], kind: str, x: np.ndarray):
    """在固定份额 x 下，按 kind 的最不利方向取组分端，返回 (numerator, denominator, hazard)。"""
    dirs = WORST_DIR.get(kind)
    if dirs:
        num_specs, den_specs = _RATIO_PARTS.get(kind, ([], []))
        num = den = 0.0
        for c, coef, end in num_specs:
            num += coef * sum(_hazard_like(r, c, end) * xi
                              for r, xi in zip(rows, x))
        for c, coef, end in den_specs:
            den += coef * sum(_hazard_like(r, c, end) * xi
                              for r, xi in zip(rows, x))
        return num, den
    if kind.startswith("hazard_"):
        key = kind[len("hazard_"):]
        return sum(_hazard_value(r, key, "hi") * xi for r, xi in zip(rows, x)), None
    return None, None


def _hazard_like(row: Row, comp: str, end: str) -> float:
    b = (row.bounds_dry or {}).get(comp)
    v = row.composition_dry.get(comp, 0.0)
    if end == "hi" and b is not None and not b["zero_tolerance"]:
        return b["upper"]
    if end == "lo" and b is not None and not b["zero_tolerance"]:
        return b["lower"]
    return v


# 各率值线性化时的分子/分母构成（与 WORST_DIR 方向一致），用于回算物理量
_RATIO_PARTS = {
    "SM_max": ([("SiO2", 1.0, "hi")], [("Al2O3", 1.0, "lo"), ("Fe2O3", 1.0, "lo")]),
    "SM_min": ([("SiO2", 1.0, "lo")], [("Al2O3", 1.0, "hi"), ("Fe2O3", 1.0, "hi")]),
    "IM_max": ([("Al2O3", 1.0, "hi")], [("Fe2O3", 1.0, "lo")]),
    "IM_min": ([("Al2O3", 1.0, "lo")], [("Fe2O3", 1.0, "hi")]),
    "KH_max": ([("CaO", 1.0, "hi"), ("Al2O3", -1.65, "lo"), ("Fe2O3", -0.35, "lo")],
               [("SiO2", 2.8, "lo")]),
    "KH_min": ([("CaO", -1.0, "lo"), ("Al2O3", 1.65, "hi"), ("Fe2O3", 0.35, "hi")],
               [("SiO2", 2.8, "hi")]),
}


def _achieved(rows: list[Row], kind: str, x: np.ndarray, worst: bool = False):
    """按约束类型把违约解 x 换算回物理量（率值/百分比/掺量）。"""
    if worst:
        num, den = _worst_blend_values(rows, kind, x)
        if kind in _RATIO_PARTS:
            # KH_min 的线性化分子为 −(C−1.65A−0.35F)，回算物理率值时取反
            if kind == "KH_min":
                num = -num
            return round(num / den, 4) if den and den > 1e-12 else float("inf")
        if kind.startswith("hazard_"):
            return round(num, 4)
        if kind == "avail":
            return None
        return None
    cdry = [r.composition_dry for r in rows]
    S = sum(c.get("SiO2", 0.0) * xi for c, xi in zip(cdry, x))
    Aa = sum(c.get("Al2O3", 0.0) * xi for c, xi in zip(cdry, x))
    F = sum(c.get("Fe2O3", 0.0) * xi for c, xi in zip(cdry, x))
    Cc = sum(c.get("CaO", 0.0) * xi for c, xi in zip(cdry, x))
    table = {
        "SM_max": (S / (Aa + F) if Aa + F > 1e-12 else float("inf")),
        "SM_min": (S / (Aa + F) if Aa + F > 1e-12 else float("inf")),
        "IM_max": (Aa / F if F > 1e-12 else float("inf")),
        "IM_min": (Aa / F if F > 1e-12 else float("inf")),
        "KH_max": ((Cc - 1.65 * Aa - 0.35 * F) / (2.8 * S) if S > 1e-12 else float("inf")),
        "KH_min": ((Cc - 1.65 * Aa - 0.35 * F) / (2.8 * S) if S > 1e-12 else -float("inf")),
    }
    if kind in table:
        return round(table[kind], 4)
    if kind.startswith("hazard_"):
        key = kind[len("hazard_"):]
        return round(sum(_hazard_value(r, key) * xi for r, xi in zip(rows, x)), 4)
    if kind == "avail":
        return None
    return None


def _trigger_reports(rows: list[Row], kind: str) -> list[dict]:
    """列出导致该最坏约束被收紧的化验单与组分取界方向。"""
    dirs = WORST_DIR.get(kind)
    out = []
    if dirs:
        for r in rows:
            moved = []
            for comp, end in dirs.items():
                b = (r.bounds_dry or {}).get(comp)
                if b is not None and not b["zero_tolerance"]:
                    v = b["upper"] if end == "hi" else b["lower"]
                    moved.append({
                        "component": comp, "bound": ("upper" if end == "hi" else "lower"),
                        "value_dry": round(v, 4),
                    })
            if moved:
                out.append({
                    "material_code": r.code, "material_name": r.name,
                    "assay_version": r.version, "lab_report_no": r.lab_report_no,
                    "basis": r.basis, "moisture_pct": r.moisture_pct,
                    "components": moved,
                })
    elif kind.startswith("hazard_"):
        key = kind[len("hazard_"):]
        comps = [("Na2O", "upper"), ("K2O", "upper")] if key == "alkali_eq" \
            else [(key, "upper")]
        for r in rows:
            moved = []
            for comp, edge in comps:
                b = (r.bounds_dry or {}).get(comp)
                if b is not None and not b["zero_tolerance"]:
                    moved.append({
                        "component": comp, "bound": edge,
                        "value_dry": round(b[edge], 4),
                    })
            if moved:
                out.append({
                    "material_code": r.code, "material_name": r.name,
                    "assay_version": r.version, "lab_report_no": r.lab_report_no,
                    "basis": r.basis, "moisture_pct": r.moisture_pct,
                    "components": moved,
                })
    return out


def _diagnose(rows: list[Row], req, base_A, base_b, meta, worst: bool = False) -> dict:
    """最小违约模型：每条不等式加非负松弛 s_k，min Σ s_k/scale。

    松弛显著非零的约束即为冲突项；并用实际合成物的物理量描述超限方向。
    worst=True 时，冲突按检测不确定度的最坏边界评估，并列出触发报告/组分/突破量。
    """
    n = len(rows)
    m = len(base_A)
    scales, An = [], []
    for row in base_A:
        sc = max(float(np.max(np.abs(row))), 1.0)
        scales.append(sc)
        An.append([v / sc for v in row])
    bn = [bv / sc for bv, sc in zip(base_b, scales)]

    Au = []
    for i in range(m):
        Au.append(An[i] + [-1.0 if j == i else 0.0 for j in range(m)])
    c = [0.0] * n + [1.0] * m
    bounds = _bounds(rows) + [(0.0, None)] * m
    res = linprog(
        c,
        A_ub=np.array(Au), b_ub=np.array(bn),
        A_eq=np.concatenate([np.ones((1, n)), np.zeros((1, m))], axis=1),
        b_eq=np.array([1.0]),
        bounds=bounds,
        method="highs",
    )
    conflicts = []
    if res.success:
        sl = res.x[n:]
        xpart = res.x[:n]
        for k, v in enumerate(sl):
            raw = float(v * scales[k])
            if raw <= 1e-4:
                continue
            info = meta[k]
            entry = {
                "constraint": info["label"],
                "kind": info["kind"],
                "limit": round(info["limit"], 4) if info["limit"] is not None else None,
                "normalized_gap": round(raw, 4),
                "boundary": "worst_case_uncertainty" if worst else "nominal",
            }
            achieved = _achieved(rows, info["kind"], xpart, worst=worst and info.get("worst"))
            if achieved is not None:
                entry["achieved"] = achieved
                if entry["limit"] is not None:
                    # 突破量（绝对量；率值为率值差，有害组分单位为干基百分点）
                    entry["breakthrough"] = round(abs(achieved - entry["limit"]), 4)
            if worst:
                triggers = _trigger_reports(rows, info["kind"])
                if triggers:
                    entry["trigger_reports"] = triggers
            conflicts.append(entry)
    mins = [r.min_share_pct / 100.0 for r in rows]
    if sum(mins) > 1.0 + 1e-9:
        conflicts.append({
            "constraint": "最低掺量之和",
            "limit": 100.0,
            "achieved": round(sum(mins) * 100.0, 4),
            "normalized_gap": round((sum(mins) - 1.0) * 100.0, 4),
        })
    reason = ("ROBUST_INFEASIBLE_BOUNDARY"
              if worst else "INFEASIBLE_CONSTRAINT_SET")
    message = (
        "检测不确定度最坏边界组合下无稳健可行解：名义化验单合格，但下列指标在"
        "误差边界上仍被突破（已列出触发的化验报告、组分取界与突破量），"
        "不能只抽一个幸运样本。"
        if worst else
        "当前约束组合无可行解（求解器状态 infeasible）。下列约束在最小违约解中仍被突破，"
        "即为冲突项，请放宽其中之一。"
    )
    return {
        "reason": reason,
        "message": message,
        "conflicts": conflicts,
        "min_violation_objective": round(float(res.fun), 6) if res.success else None,
    }


def _worst_case_margins(rows: list[Row], req, x: np.ndarray) -> dict:
    """固定份额 x 下，SM/IM/KH/有害组分在检测误差下的最坏边界与对限值的余量。"""
    ranges = chemistry.worst_case_vector(
        rows, x, hazard_keys=list((req.hazard_limits_pct or {}).keys())
    )
    t = req.targets
    checks = [
        ("SM", t.SM.min, t.SM.max),
        ("IM", t.IM.min, t.IM.max),
        ("KH", t.KH.min, t.KH.max),
    ]
    indicators = []
    for key, lo, hi in checks:
        rng = ranges[key]
        entry = {"indicator": key, "limit_min": lo, "limit_max": hi}
        if rng.get("undefined"):
            entry.update({"ok": False, "reason": "DENOMINATOR_SPAN_ZERO"})
        else:
            entry["nominal"] = None  # 由调用方填入名义率值
            entry["worst_min"] = float(rng["worst_min"])
            entry["worst_max"] = float(rng["worst_max"])
            margin_lo = (float(rng["worst_min"]) - lo) if lo is not None else None
            margin_hi = (hi - float(rng["worst_max"])) if hi is not None else None
            entry["margin_min"] = round(margin_lo, 4) if margin_lo is not None else None
            entry["margin_max"] = round(margin_hi, 4) if margin_hi is not None else None
            entry["ok"] = bool(
                (margin_lo is None or margin_lo >= -1e-6)
                and (margin_hi is None or margin_hi >= -1e-6)
            )
        indicators.append(entry)
    hazards = []
    for key, limit in (req.hazard_limits_pct or {}).items():
        rng = ranges[key]
        margin = float(limit) - float(rng["worst_max"])
        hazards.append({
            "component": key, "limit_max": float(limit),
            "worst_min": float(rng["worst_min"]), "worst_max": float(rng["worst_max"]),
            "margin_max": round(margin, 4),
            "ok": bool(margin >= -1e-6),
        })
    return {
        "indicators": indicators,
        "hazards": hazards,
        "all_ok": bool(all(e["ok"] for e in indicators)
                       and all(h["ok"] for h in hazards)),
        "note": "各组分独立取最不利检测误差端点（非同一幸运样本）；"
                "湿基化验单的上下界已先按 /（1−含水率）换到干基。",
    }


def _item_uncertainty_snapshot(r: Row) -> dict:
    """逐原料：原始基准偏差、干基上下界、零容差组分声明。"""
    bd = r.bounds_dry or {}
    components = []
    for comp, nominal in r.composition_dry.items():
        b = bd.get(comp)
        native = r.uncertainties_native.get(comp) if r.uncertainties_native else None
        if b is None:
            b = {"nominal": float(nominal), "lower": float(nominal),
                 "upper": float(nominal), "zero_tolerance": True}
        components.append({
            "component": comp,
            "basis": r.basis,
            "nominal_native": round(float(r.composition_raw.get(comp, b["nominal"])), 4),
            "uncertainty_native": (
                {"lower": round(native["lower"], 4), "upper": round(native["upper"], 4)}
                if native is not None else {"lower": 0.0, "upper": 0.0}
            ),
            "nominal_dry": round(b["nominal"], 4),
            "lower_dry": round(b["lower"], 4),
            "upper_dry": round(b["upper"], 4),
            "zero_tolerance": b["zero_tolerance"],
        })
    return {
        "material_code": r.code, "material_name": r.name,
        "assay_version": r.version, "lab_report_no": r.lab_report_no,
        "basis": r.basis, "moisture_pct": r.moisture_pct,
        "components": components,
    }


def _build_solution(mode, mode_label, rows, req, x, diagnostic=None,
                    robust: bool = False) -> dict:
    B = req.batch_t_dry
    components = [c for c in COMPONENT_ORDER if any(
        c in r.composition_dry for r in rows
    )]
    # 追加各化验单上出现但不在固定顺序里的组分
    for r in rows:
        for c in r.composition_dry:
            if c not in components:
                components.append(c)

    # 缺测在 prepare 后已由约束/合成前检查保证；这里合成只使用已测键
    pick_dicts = [{
        "code": r.code, "name": r.name,
        "moisture_pct": r.moisture_pct,
        "composition_dry": {k: r.composition_dry.get(k, 0.0) for k in components},
    } for r in rows]
    synth = chemistry.synthesize(pick_dicts, list(x), components)

    # 有害组分合成值（干基）
    hazard_vals = {}
    for key in set(HAZARD_CALC) | set((req.hazard_limits_pct or {}).keys()):
        if key == "alkali_eq":
            hazard_vals["alkali_eq"] = round(sum(
                (r.composition_dry.get("Na2O", 0.0)
                 + chemistry.ALKALI_EQ_FACTOR_K2O * r.composition_dry.get("K2O", 0.0)) * xi
                for r, xi in zip(rows, x)
            ), 4)
        elif any(key in r.composition_dry for r in rows):
            hazard_vals[key] = round(sum(
                r.composition_dry.get(key, 0.0) * xi for r, xi in zip(rows, x)
            ), 4)

    indicators = chemistry.calc_indicators(synth["dry_pct"]).as_dict()

    # 最坏边界余量（名义方案也展示“若按容差最不利端，该配比还剩多少余量”）
    worst_case = _worst_case_margins(rows, req, x)
    for e in worst_case["indicators"]:
        if "nominal" in e:
            e["nominal"] = indicators.get(e["indicator"])
    worst_case["hazard_nominal"] = {
        k: v for k, v in hazard_vals.items()
        if k in set((req.hazard_limits_pct or {}).keys())
    }

    items, total_cost = [], 0.0
    for r, xi in zip(rows, x):
        if xi < 1e-10:
            continue
        mass_dry = B * xi
        mass_wet = mass_dry / (1.0 - r.moisture_pct / 100.0)
        water = mass_wet - mass_dry
        cost = mass_wet * r.cost_per_t_wet
        total_cost += cost
        trace = chemistry.build_conversion_trace(
            r.code, r.name, r.composition_raw, r.basis, r.moisture_pct
        )
        trace["mass_balance"] = {
            "share_pct_dry": round(xi * 100.0, 4),
            "mass_t_dry": round(mass_dry, 4),
            "formula_mass_wet": f"{mass_dry:.4f} / (1 - {r.moisture_pct}/100)",
            "mass_t_wet": round(mass_wet, 4),
            "water_t": round(water, 4),
            "cost": f"{mass_wet:.4f} t × {r.cost_per_t_wet} 元/t = {cost:.2f} 元",
        }
        items.append({
            "material_code": r.code,
            "material_name": r.name,
            "assay_version": r.version,
            "lab_report_no": r.lab_report_no,
            "share_pct_dry": round(float(xi) * 100.0, 4),
            "mass_t_dry": round(float(mass_dry), 4),
            "mass_t_wet": round(float(mass_wet), 4),
            "water_t": round(float(water), 4),
            "cost": round(float(cost), 2),
            "conversion_trace": trace,
            "uncertainty_snapshot": _item_uncertainty_snapshot(r),
            "_material_id": r.material_id,
            "_assay_version_id": r.assay_version_id,
        })

    label = mode_label + ("（稳健·最坏边界）" if robust else "（名义化验值）")
    return {
        "mode": mode,
        "mode_label": label,
        "success": True,
        "robust": robust,
        "total_cost": round(float(total_cost), 2),
        "cost_per_t_dry": round(float(total_cost) / B, 2),
        "indicators": indicators,
        "composition_dry_pct": synth["dry_pct"],
        "composition_wet_pct": synth["wet_pct"],
        "water_pct_in_wet_mix": synth["water_pct_in_wet_mix"],
        "worst_case": worst_case,
        "items": items,
        "diagnostic": diagnostic,
    }


MODE_LABELS = {
    "min_cost": "成本最优",
    "max_cheap": "廉价原料用量最大",
    "balanced": "率值居中平衡方案",
}


def _precheck(rows: list[Row], req, robust: bool) -> None:
    """缺测 / 分母跨零 / 最低掺量预检。缺测绝不按零含量；
    稳健模式额外拒绝会使率值分母区间触及零的不确定度。"""
    material_rows = [{
        "code": r.code, "name": r.name, "version": r.version,
        "lab_report_no": r.lab_report_no,
        "composition": r.composition_dry, "measured_oxides": list(r.measured),
    } for r in rows]
    chemistry.require_measured(material_rows, ["CaO", "SiO2", "Al2O3", "Fe2O3"])
    for key in (req.hazard_limits_pct or {}):
        chemistry.require_measured(
            material_rows,
            ["Na2O", "K2O"] if key == "alkali_eq" else [key],
        )
    if robust:
        chemistry.check_denominator_intervals(rows)


def _min_share_diagnostic(rows: list[Row], robust: bool):
    mins_sum = sum(r.min_share_pct for r in rows)
    return {
        "reason": "MIN_SHARE_OVERFLOW",
        "message": f"最低掺量之和 {mins_sum:.2f}% 超过 100%，配比无解。",
        "conflicts": [{
            "constraint": "最低掺量之和",
            "violation_pct_or_t": round(mins_sum - 100.0, 4),
            "lhs": round(mins_sum, 4), "rhs": 100.0,
        }],
    }


def solve(rows: list[Row], req) -> list[dict]:
    """名义求解（化验单按精确常量）。口径与既有求解/历史计算完全一致。"""
    _precheck(rows, req, robust=False)
    if sum(r.min_share_pct for r in rows) > 100.0 + 1e-9:
        diag = _min_share_diagnostic(rows, False)
        return [_failed_solution(m, diag) for m in req.modes]
    return _solve_modes(rows, req, worst=False)


def solve_robust(rows: list[Row], req) -> list[dict]:
    """稳健求解：在所有使 SM/IM/KH 与有害组分最不利的检测误差边界上仍须满足约束。

    与名义求解使用完全相同的候选/约束/模式，仅约束系数逐组分取最坏端，
    不允许只抽一个幸运样本。
    """
    _precheck(rows, req, robust=True)
    if sum(r.min_share_pct for r in rows) > 100.0 + 1e-9:
        diag = _min_share_diagnostic(rows, True)
        return [_failed_solution(m, diag, robust=True) for m in req.modes]
    return _solve_modes(rows, req, worst=True)


def _solve_modes(rows: list[Row], req, worst: bool) -> list[dict]:
    n = len(rows)
    cost_c = _cost_coef(rows)
    results = []

    for mode in req.modes:
        extra_ub = None
        if mode == "min_cost":
            c = cost_c
        elif mode == "max_cheap":
            if req.cheap_material_id is None:
                raise chemistry.BlendError(
                    "NO_CHEAP_TARGET",
                    "max_cheap 模式必须指定 cheap_material_id（要最大化的廉价原料）。",
                )
            idx = next((i for i, r in enumerate(rows)
                        if r.material_id == req.cheap_material_id), None)
            if idx is None:
                raise chemistry.BlendError(
                    "CHEAP_NOT_CANDIDATE",
                    "廉价原料不在候选列表中。",
                    {"cheap_material_id": req.cheap_material_id},
                )
            # 第一阶段：在最坏边界约束下最大化廉价料份额
            c1 = np.zeros(n); c1[idx] = -1.0
            res1, A1, b1, meta1 = _solve_lp(rows, req, c1, worst=worst)
            if not res1.success:
                results.append(_failed_solution(
                    mode, _diagnose(rows, req, A1, b1, meta1, worst=worst),
                    robust=worst))
                continue
            xbest = -res1.fun
            # 第二阶段：锁定廉价料份额（容差 1e-6）后再最小化成本，打破平局
            e = np.zeros(n); e[idx] = -1.0
            info = {"kind": "lock_cheap",
                    "label": f"锁定廉价料最大份额 {r_name(rows, idx)}≥{xbest*100:.2f}%"
                             + ("（最坏边界）" if worst else ""),
                    "limit": None, "rhs": -(xbest - 1e-6), "worst": worst}
            extra_ub = [(e, -(xbest - 1e-6), info)]
            c = cost_c
        elif mode == "balanced":
            mid, cols, names = _midpoint_rows(rows, req, worst=worst)
            results.append(_solve_balanced(rows, req, mid, cols, names, cost_c,
                                           worst=worst))
            continue
        else:
            raise chemistry.BlendError("BAD_MODE", f"未知求解模式: {mode}")

        res, A, b, meta = _solve_lp(rows, req, c, extra_ub, worst=worst)
        if not res.success:
            results.append(_failed_solution(
                mode, _diagnose(rows, req, A, b, meta, worst=worst), robust=worst))
        else:
            results.append(_build_solution(mode, MODE_LABELS[mode], rows, req,
                                           np.clip(res.x[:n], 0, 1), robust=worst))
    return results


def r_name(rows, idx):
    return rows[idx].name


def _midpoint_rows(rows, req, worst: bool = False):
    arrays = _comp_arrays(rows, worst)
    # 平衡模式的目标函数仍按名义中点偏差；最坏边界只作用于可行性约束
    def nom(comp):
        return arrays[comp][1] if comp in arrays else np.zeros(len(rows))
    S, Aa, F, Cc = nom("SiO2"), nom("Al2O3"), nom("Fe2O3"), nom("CaO")
    mids, cols, names = {}, {}, {}
    t = req.targets
    if t.SM.min is not None and t.SM.max is not None:
        m = (t.SM.min + t.SM.max) / 2
        cols["SM"] = S - m * (Aa + F); names["SM"] = f"SM 偏离中点 {m:.2f}"
    if t.IM.min is not None and t.IM.max is not None:
        m = (t.IM.min + t.IM.max) / 2
        cols["IM"] = Aa - m * F; names["IM"] = f"IM 偏离中点 {m:.2f}"
    if t.KH.min is not None and t.KH.max is not None:
        m = (t.KH.min + t.KH.max) / 2
        cols["KH"] = Cc - 1.65 * Aa - 0.35 * F - m * 2.8 * S
        names["KH"] = f"KH 偏离中点 {m:.3f}"
    return mids, cols, names


def _solve_balanced(rows, req, mid, cols, names, cost_c, worst=False):
    n = len(rows)
    k = len(cols)
    keys = list(cols.keys())
    G = np.array([cols[kk] for kk in keys])  # k×n，偏差行向量（单位约为成分百分点）

    A_base, b_base, meta_base = _build_constraints(rows, req, worst=worst)
    # 扩展变量 [x, u_1..u_k]，-u ≤ Gx ≤ u，Σx=1，u≥0
    Au, bu = [], []
    for rowA, rhs in zip(A_base, b_base):
        Au.append(list(rowA) + [0.0] * k); bu.append(rhs)
    for j, kk in enumerate(keys):
        Au.append(list(G[j]) + [-1.0 if t == j else 0.0 for t in range(k)])
        bu.append(0.0)
        Au.append(list(-G[j]) + [-1.0 if t == j else 0.0 for t in range(k)])
        bu.append(0.0)
    c = [0.0] * n + [1.0] * k
    # 轻微成本偏好（单位：元/吨干生料，缩放后加入）
    c[:n] = list(1e-3 * cost_c / max(float(np.max(np.abs(cost_c))), 1.0))
    bounds = _bounds(rows) + [(0.0, None)] * k
    res = linprog(
        c,
        A_ub=np.array(Au), b_ub=np.array(bu),
        A_eq=np.concatenate([np.ones((1, n)), np.zeros((1, k))], axis=1),
        b_eq=np.array([1.0]),
        bounds=bounds,
        method="highs",
    )
    if not res.success:
        return _failed_solution("balanced",
                                _diagnose(rows, req, A_base, b_base, meta_base,
                                          worst=worst), robust=worst)
    return _build_solution("balanced", MODE_LABELS["balanced"], rows, req,
                           np.clip(res.x[:n], 0, 1), robust=worst)


def _failed_solution(mode, diagnostic, robust: bool = False) -> dict:
    label = MODE_LABELS.get(mode, mode)
    return {
        "mode": mode,
        "mode_label": label + ("（稳健·最坏边界）" if robust else "（名义化验值）"),
        "success": False,
        "robust": robust,
        "diagnostic": diagnostic,
        "items": [],
    }
