"""SciPy 线性规划求解（HiGHS）。

决策变量：x_i = 各原料干基份额（小数），Σx_i=1。
率值约束全部线性化，例如 SM=SiO2/(Al2O3+Fe2O3) ∈ [lo,hi] 等价于：
    Σ(SiO2_i - hi*(Al2O3_i+Fe2O3_i)) x_i ≤ 0
    Σ(lo*(Al2O3_i+Fe2O3_i) - SiO2_i) x_i ≤ 0
其余同理。无解时用“最小违约松弛模型”定位冲突项。

检测不确定度（robust_mode="robust"）：
同一化验基准声明的逐组分上下偏置先换算到干基，随后每条不等式取
“对该约束最不利”的组分边界（不同约束的最坏组合不同，不能只抽一个样本）：
    SM≤hi 最不利：SiO2 取上界，Al2O3/Fe2O3 取下界；
    SM≥lo 最不利：SiO2 取下界，Al2O3/Fe2O3 取上界；其余同理。
全部容差为零时，最坏边界即名义值，稳健解与名义解完全一致。
"""
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

from . import chemistry

COMPONENT_ORDER = [
    "CaO", "SiO2", "Al2O3", "Fe2O3",
    "MgO", "SO3", "K2O", "Na2O", "Cl", "LOI",
]
HAZARD_CALC = ["MgO", "SO3", "K2O", "Na2O", "Cl"]

# 每条率值不等式最不利的组分边界方向（component -> 'lo'/'hi'）
WORST_SIDES = {
    "SM_max": {"SiO2": "hi", "Al2O3": "lo", "Fe2O3": "lo"},
    "SM_min": {"SiO2": "lo", "Al2O3": "hi", "Fe2O3": "hi"},
    "IM_max": {"Al2O3": "hi", "Fe2O3": "lo"},
    "IM_min": {"Al2O3": "lo", "Fe2O3": "hi"},
    "KH_max": {"CaO": "hi", "Al2O3": "lo", "Fe2O3": "lo", "SiO2": "lo"},
    "KH_min": {"CaO": "lo", "Al2O3": "hi", "Fe2O3": "hi", "SiO2": "hi"},
}


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
    uncertainty_raw: dict | None = None      # 输入基准的原始容差声明（留痕）
    bounds: dict = field(default_factory=dict)  # 干基上下界（normalize 结果）


def prepare_rows(candidates: list) -> list[Row]:
    """candidates: 已联表查询好的 (material, assay_version) 二元组列表。"""
    rows = []
    for mat, ass in candidates:
        comp_dry = chemistry.convert_composition(
            ass.composition, ass.basis, mat.moisture_pct
        )
        bounds = chemistry.normalize_uncertainty(
            getattr(ass, "uncertainty", None),
            composition=dict(ass.composition),
            measured=set(ass.measured_oxides),
            basis=ass.basis,
            moisture_pct=mat.moisture_pct,
            material_code=mat.code,
            assay_version=ass.version,
            lab_report_no=ass.lab_report_no,
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
                uncertainty_raw=getattr(ass, "uncertainty", None),
                bounds=bounds,
            )
        )
    return rows


def _comp(row: Row, cname: str, side: str | None = None) -> float:
    """取某组分干基值；side=None 名义值，'lo'/'hi' 取检测边界（缺省=零容差点值）。"""
    nominal = row.composition_dry.get(cname, 0.0)
    if side is None or cname not in row.bounds:
        return nominal
    return row.bounds[cname]["lo"] if side == "lo" else row.bounds[cname]["hi"]


def _v(rows: list[Row], cname: str, side: str | None) -> np.ndarray:
    return np.array([_comp(r, cname, side) for r in rows])


def _hazard_value(row: Row, key: str, side: str | None = None) -> float:
    comp = row.composition_dry
    if key == "alkali_eq":
        if "K2O" not in row.measured or "Na2O" not in row.measured:
            raise chemistry.MissingAssayError(
                [{
                    "material_code": row.code, "material_name": row.name,
                    "assay_version": row.version, "lab_report_no": row.lab_report_no,
                    "component": "alkali_eq(需 Na2O 与 K2O 均实测)",
                }]
            )
        # 碱当量上限最不利：Na2O、K2O 同时取上界（线性和，下界对称处理）
        if side is not None:
            return _comp(row, "Na2O", side) + chemistry.ALKALI_EQ_FACTOR_K2O * _comp(row, "K2O", side)
        return comp["Na2O"] + chemistry.ALKALI_EQ_FACTOR_K2O * comp["K2O"]
    if key not in row.measured:
        raise chemistry.MissingAssayError(
            [{
                "material_code": row.code, "material_name": row.name,
                "assay_version": row.version, "lab_report_no": row.lab_report_no,
                "component": key,
            }]
        )
    return _comp(row, key, side)


def _build_constraints(rows: list[Row], req, robust: bool = False) -> tuple[list, list, list[dict]]:
    """构造 A_ub x ≤ b_ub（含上下界/率值/有害组分/可用量）。

    robust=True 时，每条率值/有害组分约束的系数按该约束“最不利”的检测边界
    逐组分选取（见 WORST_SIDES）；可用量/最低掺量不涉及化验误差，边界相同。
    返回 A, b, meta；meta 每项带 kind/label/limit，供冲突诊断换算物理量。
    """
    n = len(rows)
    A, b, meta = [], [], []

    def add(coefs, rhs, kind, label, limit=None):
        A.append([float(v) for v in coefs])
        b.append(float(rhs))
        meta.append({"kind": kind, "label": label, "limit": limit,
                     "rhs": float(rhs)})

    def sides(kind):
        return WORST_SIDES.get(kind, {}) if robust else {}

    # --- 率值线性约束（各约束独立取最坏组分边界） ---
    S_by = {sd: _v(rows, "SiO2", sd) for sd in (None, "lo", "hi")}
    A_by = {sd: _v(rows, "Al2O3", sd) for sd in (None, "lo", "hi")}
    F_by = {sd: _v(rows, "Fe2O3", sd) for sd in (None, "lo", "hi")}
    C_by = {sd: _v(rows, "CaO", sd) for sd in (None, "lo", "hi")}

    t = req.targets
    if t.SM.max is not None:
        w = sides("SM_max")
        add(S_by[w.get("SiO2")] - t.SM.max * (A_by[w.get("Al2O3")] + F_by[w.get("Fe2O3")]),
            0.0, "SM_max",
            f"硅率上限 SM≤{t.SM.max}" + ("（稳健：SiO2↑ A/F↓）" if robust else ""),
            t.SM.max)
    if t.SM.min is not None:
        w = sides("SM_min")
        add(t.SM.min * (A_by[w.get("Al2O3")] + F_by[w.get("Fe2O3")]) - S_by[w.get("SiO2")],
            0.0, "SM_min",
            f"硅率下限 SM≥{t.SM.min}" + ("（稳健：SiO2↓ A/F↑）" if robust else ""),
            t.SM.min)
    if t.IM.max is not None:
        w = sides("IM_max")
        add(A_by[w.get("Al2O3")] - t.IM.max * F_by[w.get("Fe2O3")], 0.0, "IM_max",
            f"铝率上限 IM≤{t.IM.max}" + ("（稳健：Al2O3↑ Fe2O3↓）" if robust else ""),
            t.IM.max)
    if t.IM.min is not None:
        w = sides("IM_min")
        add(t.IM.min * F_by[w.get("Fe2O3")] - A_by[w.get("Al2O3")], 0.0, "IM_min",
            f"铝率下限 IM≥{t.IM.min}" + ("（稳健：Al2O3↓ Fe2O3↑）" if robust else ""),
            t.IM.min)
    if t.KH.max is not None:
        w = sides("KH_max")
        add(C_by[w.get("CaO")] - 1.65 * A_by[w.get("Al2O3")] - 0.35 * F_by[w.get("Fe2O3")]
            - t.KH.max * 2.8 * S_by[w.get("SiO2")], 0.0, "KH_max",
            f"石灰饱和上限 KH≤{t.KH.max}"
            + ("（稳健：CaO↑ A/F/SiO2 最不利）" if robust else ""), t.KH.max)
    if t.KH.min is not None:
        w = sides("KH_min")
        add(t.KH.min * 2.8 * S_by[w.get("SiO2")]
            - (C_by[w.get("CaO")] - 1.65 * A_by[w.get("Al2O3")] - 0.35 * F_by[w.get("Fe2O3")]),
            0.0, "KH_min",
            f"石灰饱和下限 KH≥{t.KH.min}"
            + ("（稳健：CaO↓ A/F/SiO2 最不利）" if robust else ""), t.KH.min)

    # --- 有害组分干基上限（线性：Σ comp_i x_i ≤ limit）；稳健取上界 ---
    for key, limit in (req.hazard_limits_pct or {}).items():
        add([_hazard_value(r, key, "hi" if robust else None) for r in rows], float(limit),
            "hazard_" + key,
            f"有害组分上限 {key}≤{limit}%(干基)" + ("（稳健：组分取上界）" if robust else ""),
            float(limit))

    # --- 可用量（湿基）：B*x/(1-m) ≤ avail → x ≤ avail*(1-m)/B ---
    # 含水率来自原料档案的确定性字段，不纳入化验不确定度。
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


def _solve_lp(rows, req, c_obj, extra_ub=None, robust=False):
    n = len(rows)
    A, b, meta = _build_constraints(rows, req, robust=robust)
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


# 率值最坏方向：(分子端方向, 分母端方向)
def _blend_oxide(rows, x, cname, side):
    return sum(_comp(r, cname, side) * xi for r, xi in zip(rows, x))


def worst_indicators(rows, x) -> dict:
    """给定份额 x，SM/IM/KH 在检测边界上的最坏可达值与对应边界组合。"""
    S = {sd: _blend_oxide(rows, x, "SiO2", sd) for sd in (None, "lo", "hi")}
    Aa = {sd: _blend_oxide(rows, x, "Al2O3", sd) for sd in (None, "lo", "hi")}
    F = {sd: _blend_oxide(rows, x, "Fe2O3", sd) for sd in (None, "lo", "hi")}
    Cc = {sd: _blend_oxide(rows, x, "CaO", sd) for sd in (None, "lo", "hi")}

    def num_kh(c_sd, a_sd, f_sd):
        return Cc[c_sd] - 1.65 * Aa[a_sd] - 0.35 * F[f_sd]

    def r4(v):
        return round(float(v), 4)

    out = {
        "SM": {
            "nominal": r4(S[None] / (Aa[None] + F[None])),
            "worst_low": r4(S["lo"] / (Aa["hi"] + F["hi"])),
            "worst_high": r4(S["hi"] / (Aa["lo"] + F["lo"])),
            "boundary_combo_low": {"SiO2": "lower", "Al2O3": "upper", "Fe2O3": "upper"},
            "boundary_combo_high": {"SiO2": "upper", "Al2O3": "lower", "Fe2O3": "lower"},
            "denominator_lower": float(Aa["lo"] + F["lo"]),
            "denominator_upper": float(Aa["hi"] + F["hi"]),
        },
        "IM": {
            "nominal": r4(Aa[None] / F[None]),
            "worst_low": r4(Aa["lo"] / F["hi"]),
            "worst_high": r4(Aa["hi"] / F["lo"]),
            "boundary_combo_low": {"Al2O3": "lower", "Fe2O3": "upper"},
            "boundary_combo_high": {"Al2O3": "upper", "Fe2O3": "lower"},
            "denominator_lower": float(F["lo"]),
            "denominator_upper": float(F["hi"]),
        },
        "KH": {
            "nominal": r4(num_kh(None, None, None) / (2.8 * S[None])),
            "worst_low": r4(num_kh("lo", "hi", "hi") / (2.8 * S["hi"])),
            "worst_high": r4(num_kh("hi", "lo", "lo") / (2.8 * S["lo"])),
            "boundary_combo_low": {"CaO": "lower", "Al2O3": "upper",
                                   "Fe2O3": "upper", "SiO2": "upper"},
            "boundary_combo_high": {"CaO": "upper", "Al2O3": "lower",
                                    "Fe2O3": "lower", "SiO2": "lower"},
            "denominator_lower": float(2.8 * S["lo"]),
            "denominator_upper": float(2.8 * S["hi"]),
        },
    }
    return out


def worst_hazards(rows, req, x) -> dict:
    """有害组分在检测上界下的合成值（碱当量两项同取上界）。"""
    out = {}
    for key in set(HAZARD_CALC) | set((req.hazard_limits_pct or {}).keys()):
        if key == "alkali_eq":
            out["alkali_eq"] = sum(_hazard_value(r, "alkali_eq", "hi") * xi
                                   for r, xi in zip(rows, x))
        elif any(key in r.composition_dry for r in rows):
            out[key] = sum(_hazard_value(r, key, "hi") * xi
                           for r, xi in zip(rows, x))
    return out


def _material_worst_combo(rows, kind: str) -> list[dict]:
    """某条约束最不利时，逐原料逐组分实际采用的边界（供报告与历史留痕）。"""
    combos = []
    sides = WORST_SIDES.get(kind, {})
    for i, r in enumerate(rows):
        if kind.startswith("hazard_"):
            key = kind[len("hazard_"):]
            if key == "alkali_eq":
                picks = {"Na2O": "hi", "K2O": "hi"}
            else:
                picks = {key: "hi"}
        else:
            picks = sides
        used = {}
        for cname, sd in picks.items():
            if cname in r.bounds and (
                r.bounds[cname]["lo"] != r.bounds[cname]["nominal_dry"]
                or r.bounds[cname]["hi"] != r.bounds[cname]["nominal_dry"]
            ):
                used[cname] = {
                    "side": "lower" if sd == "lo" else "upper",
                    "nominal_dry_pct": round(r.bounds[cname]["nominal_dry"], 6),
                    "used_dry_pct": round(r.bounds[cname]["lo" if sd == "lo" else "hi"], 6),
                }
        if used:
            combos.append({
                "material_index": i,
                "material_code": r.code,
                "material_name": r.name,
                "assay_version": r.version,
                "lab_report_no": r.lab_report_no,
                "components": used,
            })
    return combos


def _achieved(rows: list[Row], kind: str, x: np.ndarray, robust: bool = False):
    """按约束类型把违约解 x 换算回物理量（率值/百分比/掺量）。

    robust=True 时用该约束最不利的检测边界合成（与稳健 LP 系数一致），
    得到的突破量才是“最坏情况下仍突破”的量。
    """
    w = WORST_SIDES.get(kind, {}) if robust else {}
    S = sum(_comp(r, "SiO2", w.get("SiO2")) * xi for r, xi in zip(rows, x))
    Aa = sum(_comp(r, "Al2O3", w.get("Al2O3")) * xi for r, xi in zip(rows, x))
    F = sum(_comp(r, "Fe2O3", w.get("Fe2O3")) * xi for r, xi in zip(rows, x))
    Cc = sum(_comp(r, "CaO", w.get("CaO")) * xi for r, xi in zip(rows, x))
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
        return round(sum(_hazard_value(r, key, "hi" if robust else None) * xi
                         for r, xi in zip(rows, x)), 4)
    if kind == "avail":
        return None
    return None


def _diagnose(rows: list[Row], req, base_A, base_b, meta, robust: bool = False) -> dict:
    """最小违约模型：每条不等式加非负松弛 s_k，min Σ s_k/scale。

    松弛显著非零的约束即为冲突项；并用实际（稳健模式下为最坏边界）
    合成物的物理量描述超限方向与突破量。
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
            achieved = _achieved(rows, info["kind"], xpart, robust=robust)
            entry = {
                "constraint": info["label"],
                "kind": info["kind"],
                "limit": round(info["limit"], 4) if info["limit"] is not None else None,
                "normalized_gap": round(raw, 4),
                "boundary_combo": _material_worst_combo(rows, info["kind"]) if robust else [],
            }
            if achieved is not None:
                entry["achieved"] = achieved
                if info["limit"] is not None:
                    entry["breach"] = round(achieved - info["limit"], 4)
            conflicts.append(entry)
    mins = [r.min_share_pct / 100.0 for r in rows]
    if sum(mins) > 1.0 + 1e-9:
        conflicts.append({
            "constraint": "最低掺量之和",
            "kind": "min_share",
            "limit": 100.0,
            "achieved": round(sum(mins) * 100.0, 4),
            "breach": round((sum(mins) - 1.0) * 100.0, 4),
            "normalized_gap": round((sum(mins) - 1.0) * 100.0, 4),
            "boundary_combo": [],
        })
    reason = "ROBUST_INFEASIBLE_BOUNDARY" if robust else "INFEASIBLE_CONSTRAINT_SET"
    message = (
        "检测不确定度下无稳健可行解：名义化验值可能合格，但下列约束在“对该约束最不利”"
        "的检测边界组合上仍被突破（已给出报告/组分/边界与突破量）。"
        "请放宽窗口、收紧容差或更换原料；不能以某一个幸运样本替代全边界校验。"
        if robust else
        "当前约束组合无可行解（求解器状态 infeasible）。下列约束在最小违约解中仍被突破，"
        "即为冲突项，请放宽其中之一。"
    )
    return {
        "reason": reason,
        "message": message,
        "conflicts": conflicts,
        "min_violation_objective": round(float(res.fun), 6) if res.success else None,
    }


def _build_solution(mode, mode_label, rows, req, x, diagnostic=None,
                    include_uncertainty=False) -> dict:
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
        item = {
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
            "_material_id": r.material_id,
            "_assay_version_id": r.assay_version_id,
        }
        if include_uncertainty:
            item["uncertainty_trace"] = chemistry.build_uncertainty_trace(
                r.code, r.name, r.basis, r.moisture_pct,
                r.composition_raw, r.uncertainty_raw, r.bounds,
            )
            item["worst_case_snapshot"] = {
                c: {
                    "nominal_dry_pct": round(v["nominal_dry"], 6),
                    "lower_dry_pct": round(v["lo"], 6),
                    "upper_dry_pct": round(v["hi"], 6),
                } for c, v in r.bounds.items()
            }
        items.append(item)

    return {
        "mode": mode,
        "mode_label": mode_label,
        "success": True,
        "total_cost": round(float(total_cost), 2),
        "cost_per_t_dry": round(float(total_cost) / B, 2),
        "indicators": indicators,
        "composition_dry_pct": synth["dry_pct"],
        "composition_wet_pct": synth["wet_pct"],
        "water_pct_in_wet_mix": synth["water_pct_in_wet_mix"],
        "items": items,
        "diagnostic": diagnostic,
    }


MODE_LABELS = {
    "min_cost": "成本最优",
    "max_cheap": "廉价原料用量最大",
    "balanced": "率值居中平衡方案",
}


def _robust_margins(rows, req, x) -> dict:
    """稳健解 x 在检测边界上的最坏率值/有害组分与相对窗口的余量（正=安全）。"""
    wi = worst_indicators(rows, x)
    wh = worst_hazards(rows, req, x)
    t = req.targets

    def margin_row(key, iv):
        z = wi[key]
        row = {
            "nominal": round(z["nominal"], 4),
            "worst_low": round(z["worst_low"], 4),
            "worst_high": round(z["worst_high"], 4),
            "denominator_interval": [round(z["denominator_lower"], 6),
                                     round(z["denominator_upper"], 6)],
            "boundary_combo_low": z["boundary_combo_low"],
            "boundary_combo_high": z["boundary_combo_high"],
        }
        if iv.min is not None:
            row["limit_min"] = iv.min
            row["headroom_low"] = round(z["worst_low"] - iv.min, 4)
        if iv.max is not None:
            row["limit_max"] = iv.max
            row["headroom_high"] = round(iv.max - z["worst_high"], 4)
        return row

    margins = {
        "SM": margin_row("SM", t.SM),
        "IM": margin_row("IM", t.IM),
        "KH": margin_row("KH", t.KH),
        "hazards": {},
    }
    for key, limit in (req.hazard_limits_pct or {}).items():
        val = wh.get(key)
        if val is None:
            continue
        margins["hazards"][key] = {
            "worst_high": round(float(val), 4),
            "limit": float(limit),
            "headroom": round(float(limit) - float(val), 4),
            "boundary_combo_high": "各原料该组分（碱当量为 Na2O/K2O）同取检测上界",
        }
    return margins


def _raise_if_denominator_crosses_zero(rows, x):
    """稳健解必须对“分母区间跨零”明确拒绝（而非给出无定义率值）。

    未声明容差的组分按零容差点值（=干基名义值）处理。
    """
    denom_components = set(c for cs in chemistry.DENOMINATOR_PARTS.values() for c in cs)
    dry_bounds = []
    for r in rows:
        bd = {}
        for c in denom_components:
            if c in r.bounds:
                bd[c] = (r.bounds[c]["lo"], r.bounds[c]["hi"])
            elif c in r.composition_dry:
                v = float(r.composition_dry[c])
                bd[c] = (v, v)
        dry_bounds.append(bd)
    bad = chemistry.check_blend_denominators(list(x), dry_bounds)
    if not bad:
        return
    for z in bad:
        z["denominator_expr"] = z.pop("denominator")
        for mm in z["materials_with_zero_share_risk"]:
            i = mm.pop("material_index")
            r = rows[i]
            mm.update({
                "material_code": r.code, "material_name": r.name,
                "assay_version": r.version, "lab_report_no": r.lab_report_no,
            })
    raise chemistry.UncertainDenominatorError(bad)


def _solve_one_mode(rows: list[Row], req, mode: str, cost_c, robust: bool) -> dict:
    """单模式单次求解（名义或稳健），失败返回带诊断的失败方案。"""
    n = len(rows)
    tag = "（稳健·检测边界）" if robust else ""
    if mode == "min_cost":
        res, A, b, meta = _solve_lp(rows, req, cost_c, robust=robust)
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
        # 第一阶段：最大化廉价料份额（稳健：份额必须在最坏边界下也可行）
        c1 = np.zeros(n); c1[idx] = -1.0
        res1, A1, b1, meta1 = _solve_lp(rows, req, c1, robust=robust)
        if not res1.success:
            return _failed_solution(mode,
                                    _diagnose(rows, req, A1, b1, meta1, robust=robust),
                                    robust=robust)
        xbest = -res1.fun
        e = np.zeros(n); e[idx] = -1.0
        info = {"kind": "lock_cheap",
                "label": f"锁定廉价料最大份额 {r_name(rows, idx)}≥{xbest*100:.2f}%{tag}",
                "limit": None, "rhs": -(xbest - 1e-6)}
        res, A, b, meta = _solve_lp(
            rows, req, cost_c, extra_ub=[(e, -(xbest - 1e-6), info)], robust=robust)
    elif mode == "balanced":
        return _solve_balanced(rows, req, cost_c, robust=robust)
    else:
        raise chemistry.BlendError("BAD_MODE", f"未知求解模式: {mode}")

    if not res.success:
        return _failed_solution(mode, _diagnose(rows, req, A, b, meta, robust=robust),
                                robust=robust)
    x = np.clip(res.x[:n], 0, 1)
    sol = _build_solution(
        mode, MODE_LABELS[mode] + tag, rows, req, x,
        include_uncertainty=robust,
    )
    sol["robust"] = robust
    return sol


def solve(rows: list[Row], req) -> list[dict]:
    # 求解前缺测检查（四大率值组分 + 被约束有害组分必须实测）
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

    robust_on = getattr(req, "robust_mode", None) == "robust"

    # 最低掺量算术预检（与化验误差无关，名义/稳健共用）
    mins_sum = sum(r.min_share_pct for r in rows)
    if mins_sum > 100.0 + 1e-9:
        diag = {
            "reason": "MIN_SHARE_OVERFLOW",
            "message": f"最低掺量之和 {mins_sum:.2f}% 超过 100%，配比无解。",
            "conflicts": [{
                "constraint": "最低掺量之和",
                "violation_pct_or_t": round(mins_sum - 100.0, 4),
                "lhs": round(mins_sum, 4), "rhs": 100.0,
            }],
        }
        base = [_failed_solution(m, diag) for m in req.modes]
        if robust_on:
            for s, m in zip(base, req.modes):
                s["robust"] = _failed_solution(m, diag, robust=True)
        return base

    cost_c = _cost_coef(rows)
    results = []

    for mode in req.modes:
        # 1) 名义方案（现有求解路径，行为保持不变）
        nominal = _solve_one_mode(rows, req, mode, cost_c, robust=False)

        # 2) 稳健方案：全部检测边界上仍须满足约束（不抽幸运样本）
        if robust_on:
            robust_sol = _solve_one_mode(rows, req, mode, cost_c, robust=True)
            if robust_sol["success"]:
                x = np.array([
                    next((it["share_pct_dry"] / 100.0
                          for it in robust_sol["items"]
                          if it["material_code"] == r.code), 0.0)
                    for r in rows
                ])
                _raise_if_denominator_crosses_zero(rows, x)
                robust_sol["robust_report"] = {
                    "mode": mode,
                    "mode_label": MODE_LABELS[mode] + "（稳健）",
                    "margins": _robust_margins(rows, req, x),
                    "active_components": [
                        {
                            "material_code": r.code, "material_name": r.name,
                            "assay_version": r.version, "lab_report_no": r.lab_report_no,
                            "assay_basis": r.basis,
                            "components": sorted(r.bounds.keys()),
                        }
                        for r in rows if r.bounds
                    ],
                }
            nominal["robust"] = robust_sol
        results.append(nominal)
    return results


def r_name(rows, idx):
    return rows[idx].name


def _midpoint_rows(rows, req):
    S = _v(rows, "SiO2", None)
    Aa = _v(rows, "Al2O3", None)
    F = _v(rows, "Fe2O3", None)
    Cc = _v(rows, "CaO", None)
    cols, names = {}, {}
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
    return cols, names


def _solve_balanced(rows, req, cost_c, robust=False):
    n = len(rows)
    cols, names = _midpoint_rows(rows, req)
    k = len(cols)
    keys = list(cols.keys())
    G = np.array([cols[kk] for kk in keys])  # k×n，偏差行向量（名义中点，单位约为成分百分点）

    A_base, b_base, meta_base = _build_constraints(rows, req, robust=robust)
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
    mode = "balanced"
    if not res.success:
        return _failed_solution(mode,
                                _diagnose(rows, req, A_base, b_base, meta_base,
                                          robust=robust),
                                robust=robust)
    x = np.clip(res.x[:n], 0, 1)
    sol = _build_solution(mode, MODE_LABELS[mode]
                          + ("（稳健·检测边界）" if robust else ""),
                          rows, req, x, include_uncertainty=robust)
    sol["robust"] = robust
    return sol


def _failed_solution(mode, diagnostic, robust=False) -> dict:
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode)
                      + ("（稳健·检测边界）" if robust else ""),
        "success": False,
        "diagnostic": diagnostic,
        "items": [],
        "robust": robust,
    }
