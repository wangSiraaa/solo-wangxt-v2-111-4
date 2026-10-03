"""化学口径核心：干湿基换算、质量守恒合成、硅率/铝率/石灰饱和指标。

硬性规则（工艺研发约定）：
1. “未测/缺测”绝不以 0 含量参与计算——缺测直接抛 MissingAssayError；
2. 率值分母为 0 抛 ZeroDenominatorError，不静默返回 inf/0；
3. 所有换算保留逐步 trace，供前端追溯。
"""
from dataclasses import dataclass

from .config import ALKALI_EQ_FACTOR_K2O

EPS = 1e-9


class BlendError(Exception):
    """带错误码的业务异常，API 层映射为 422。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class MissingAssayError(BlendError):
    def __init__(self, missing: list[dict]):
        super().__init__(
            "MISSING_ASSAY",
            "存在缺测组分，未测不允许按零含量处理，请补测或剔除该原料后重试。",
            {"missing": missing},
        )


class ZeroDenominatorError(BlendError):
    def __init__(self, indicator: str, denominator: str, value: float):
        super().__init__(
            "ZERO_DENOMINATOR",
            f"{indicator} 的分母（{denominator}）为零或接近零，指标无定义，"
            "不得用零含量兜底。",
            {"indicator": indicator, "denominator": denominator, "value": value},
        )


class BadUncertaintyError(BlendError):
    def __init__(self, message: str, details: list | dict):
        super().__init__("BAD_UNCERTAINTY", message, {"items": details}
                         if isinstance(details, list) else details)


class DenominatorSpanZeroError(BlendError):
    """检测不确定区间使率值分母可能为零——率值在边界上无定义，必须整体拒绝。"""

    def __init__(self, problems: list[dict]):
        super().__init__(
            "DENOMINATOR_SPAN_ZERO",
            "检测不确定区间使 SM/IM/KH 的分母在误差边界上可能为零（含触零），"
            "该边界下率值无定义；不允许给出无定义的率值，请收紧不确定度或剔除该原料。",
            {"problems": problems},
        )


def dry_factor(moisture_pct: float) -> float:
    """湿基质量 -> 干基质量的系数 1/(1-w)。"""
    w = moisture_pct / 100.0
    if not 0.0 <= w < 1.0:
        raise BlendError(
            "BAD_MOISTURE",
            f"含水率 {moisture_pct}% 越界，必须位于 [0,100)。",
            {"moisture_pct": moisture_pct},
        )
    return 1.0 / (1.0 - w)


def convert_composition(composition: dict, basis: str, moisture_pct: float) -> dict:
    """把化验单统一换算到干基（mass %，干基合计含 LOI）。

    dry: 原样返回；wet: 各组分除以 (1-w)，LOI 同样处理
    （自由水在烘干时脱除，不并入 LOI）。
    """
    if basis == "dry":
        return {k: float(v) for k, v in composition.items()}
    if basis == "wet":
        f = dry_factor(moisture_pct)
        return {k: float(v) * f for k, v in composition.items()}
    raise BlendError("BAD_BASIS", f"未知化验基准: {basis}", {"basis": basis})


def build_conversion_trace(
    material_code: str,
    material_name: str,
    composition: dict,
    basis: str,
    moisture_pct: float,
) -> dict:
    """逐组分留痕：输入值、基准、换算系数、输出干基值。"""
    f = dry_factor(moisture_pct)
    steps = []
    for ox, val in composition.items():
        if basis == "wet":
            steps.append(
                {
                    "component": ox,
                    "basis_in": "wet",
                    "value_in": round(float(val), 4),
                    "formula": f"{val} / (1 - {moisture_pct}/100)",
                    "factor": round(f, 6),
                    "basis_out": "dry",
                    "value_out": round(float(val) * f, 4),
                }
            )
        else:
            steps.append(
                {
                    "component": ox,
                    "basis_in": "dry",
                    "value_in": round(float(val), 4),
                    "formula": "干基化验单，无需水分换算",
                    "factor": 1.0,
                    "basis_out": "dry",
                    "value_out": round(float(val), 4),
                }
            )
    return {
        "material_code": material_code,
        "material_name": material_name,
        "moisture_pct": moisture_pct,
        "assay_basis": basis,
        "dry_factor": round(f, 6),
        "steps": steps,
    }


def require_measured(material_rows: list[dict], required: list[str]) -> None:
    """参与求解的原料必须实测所需组分；缺测即报错，绝不当零。"""
    missing = []
    for row in material_rows:
        measured = set(row.get("measured_oxides") or row["composition"].keys())
        for comp in required:
            if comp == "alkali_eq":
                ok = ("K2O" in measured and "Na2O" in measured)
            else:
                ok = comp in measured
            if not ok:
                missing.append(
                    {
                        "material_code": row["code"],
                        "material_name": row["name"],
                        "assay_version": row["version"],
                        "lab_report_no": row["lab_report_no"],
                        "component": comp,
                    }
                )
    if missing:
        raise MissingAssayError(missing)


@dataclass
class Indicators:
    SM: float          # 硅率 silica modulus
    IM: float          # 铝率 alumina modulus
    KH: float          # 石灰饱和系数 lime saturation factor
    C: float
    S: float
    A: float
    F: float

    def as_dict(self) -> dict:
        return {
            "SM": round(self.SM, 4),
            "IM": round(self.IM, 4),
            "KH": round(self.KH, 4),
            "CaO": round(self.C, 4),
            "SiO2": round(self.S, 4),
            "Al2O3": round(self.A, 4),
            "Fe2O3": round(self.F, 4),
            "warnings": self.warnings(),
        }

    def warnings(self) -> list[str]:
        w = []
        if self.KH < 0:
            w.append("KH 分子为负，CaO 不足以饱和酸性氧化物，实际生料不会出现该结果。")
        return w


def calc_indicators(comp_dry_pct: dict) -> Indicators:
    """硅率 SM=SiO2/(Al2O3+Fe2O3)，铝率 IM=Al2O3/Fe2O3，
    KH=(CaO-1.65Al2O3-0.35Fe2O3)/(2.8SiO2)。
    分母为零必须明确报错。缺测应在进入本函数前拦截。
    """
    try:
        C = float(comp_dry_pct["CaO"])
        S = float(comp_dry_pct["SiO2"])
        A = float(comp_dry_pct["Al2O3"])
        F = float(comp_dry_pct["Fe2O3"])
    except KeyError as e:
        raise MissingAssayError(
            [{"component": e.args[0], "where": "calc_indicators"}]
        )

    if abs(A + F) < EPS:
        raise ZeroDenominatorError("SM", "Al2O3+Fe2O3", A + F)
    if abs(F) < EPS:
        raise ZeroDenominatorError("IM", "Fe2O3", F)
    if abs(S) < EPS:
        raise ZeroDenominatorError("KH", "2.8*SiO2", 2.8 * S)

    return Indicators(
        SM=S / (A + F),
        IM=A / F,
        KH=(C - 1.65 * A - 0.35 * F) / (2.8 * S),
        C=C,
        S=S,
        A=A,
        F=F,
    )


def synthesize(
    picks: list[dict], shares_dry: list[float], components: list[str]
) -> dict:
    """质量守恒合成。

    picks: [{code, name, moisture_pct, composition_dry:% , composition_wet:% (可选)}]
    shares_dry: 干基份额（小数，和为 1）
    返回干基/湿基合成成分（%）、水量平衡，以及逐原料贡献明细。
    """
    dry_pct = {c: 0.0 for c in components}
    wet_pct = {c: 0.0 for c in components}
    contributions = []
    wet_share_sum = 0.0

    for pick, x in zip(picks, shares_dry):
        m = pick["moisture_pct"] / 100.0
        cdry = pick["composition_dry"]
        # 该原料干基 -> 湿基组分：wet% = dry% * (1-m)；湿料中水占 m
        cwet = {c: cdry.get(c, 0.0) * (1.0 - m) for c in components}
        row = {"material_code": pick["code"], "share_dry": x, "by_component": {}}
        for c in components:
            dry_pct[c] += x * cdry.get(c, 0.0)
            row["by_component"][c] = round(x * cdry.get(c, 0.0), 4)
        contributions.append(row)

    # 湿基：以湿料总质量为 100% 重新归一。干基份额 x 对应湿基份额
    # x_wet = x/(1-m) / sum_j x_j/(1-m_j)
    wet_mass_rel = [x / (1.0 - p["moisture_pct"] / 100.0) for x, p in zip(shares_dry, picks)]
    tot_wet_rel = sum(wet_mass_rel)
    water_pct_total = 0.0
    for pick, rel in zip(picks, wet_mass_rel):
        x_wet = rel / tot_wet_rel
        m = pick["moisture_pct"] / 100.0
        wet_share_sum += x_wet
        for c in components:
            wet_pct[c] += x_wet * pick["composition_dry"].get(c, 0.0) * (1.0 - m)
        water_pct_total += x_wet * m
        for row in contributions:
            if row["material_code"] == pick["code"]:
                row["share_wet"] = round(x_wet, 6)

    return {
        "dry_pct": {c: round(v, 4) for c, v in dry_pct.items()},
        "wet_pct": {c: round(v, 4) for c, v in wet_pct.items()},
        "water_pct_in_wet_mix": round(water_pct_total * 100.0, 4),
        "contributions": contributions,
        "dry_share_sum": round(sum(shares_dry), 8),
        "wet_share_sum": round(wet_share_sum, 8),
    }


def alkali_equivalent(comp: dict) -> float | None:
    """Na2O 当量 = Na2O + 0.658*K2O；任一缺测返回 None（调用方负责报错策略）。"""
    if "Na2O" not in comp or "K2O" not in comp:
        return None
    return float(comp["Na2O"]) + ALKALI_EQ_FACTOR_K2O * float(comp["K2O"])


# --------------------------------------------------------------------------
# 检测不确定度：同基准上下界、湿基→干基区间换算、零容差、分母跨零拒绝
# --------------------------------------------------------------------------
UNCERTAINTY_COMPONENTS = [
    "CaO", "SiO2", "Al2O3", "Fe2O3",
    "MgO", "SO3", "K2O", "Na2O", "Cl", "LOI",
]


def normalize_uncertainties(
    uncertainties: dict | None,
    measured: list[str] | set[str],
    *,
    material_code: str = "",
    material_name: str = "",
    assay_version: str = "",
    lab_report_no: str = "",
) -> dict:
    """把化验单登记的不确定度统一为 {comp: {"lower": -a, "upper": b}}（与化验同基准）。

    允许写法：
      - {"SiO2": 0.3}                         对称偏差 ±0.3（质量百分点，同基准）
      - {"SiO2": {"lower": -0.2, "upper": 0.4}}  非对称偏差
      - {"SiO2": {"lower": 0, "upper": 0}}    零容差（等价于缺省）
    校验：键必须为已测组分；lower<=0<=upper；偏差不得使组分下界为负。
    """
    measured = set(measured)
    normalized: dict[str, dict] = {}
    errors: list[dict] = []
    base = {
        "material_code": material_code, "material_name": material_name,
        "assay_version": assay_version, "lab_report_no": lab_report_no,
    }
    for comp, spec in (uncertainties or {}).items():
        ctx = {**base, "component": comp}
        if comp not in measured:
            errors.append({**ctx, "problem": "UNMEASURED_COMPONENT",
                           "message": "只能为已实测组分声明不确定度；未测项必须先补测。"})
            continue
        try:
            if isinstance(spec, (int, float)):
                lower, upper = -abs(float(spec)), abs(float(spec))
            elif isinstance(spec, dict) and "lower" in spec and "upper" in spec:
                lower, upper = float(spec["lower"]), float(spec["upper"])
            else:
                raise ValueError
        except (TypeError, ValueError):
            errors.append({**ctx, "problem": "BAD_SHAPE",
                           "message": "不确定度须为非负数，或 {lower<=0, upper>=0}。"})
            continue
        if lower > 0 or upper < 0:
            errors.append({**ctx, "problem": "BAD_SIGNS",
                           "message": f"偏差符号非法：lower={lower} 必须 ≤0，upper={upper} 必须 ≥0。"})
            continue
        if abs(lower) < EPS and abs(upper) < EPS:
            continue  # 零容差不落入区间表（按精确常量处理）
        normalized[comp] = {"lower": lower, "upper": upper}
    if errors:
        raise BadUncertaintyError(
            "化验不确定度登记不合法：未测组分不得声明容差，偏差须满足 lower≤0≤upper。",
            errors,
        )
    return normalized


def bounds_dry(
    composition_dry: dict,
    uncertainties_native: dict,
    basis: str,
    moisture_pct: float,
) -> dict:
    """名义干基值 + 不确定区间（全部换到干基，单位：干基质量百分点）。

    dry: 直接加减偏差；wet: 偏差先随名义值一起除以 (1-w)（线性换算，区间不倒置）。
    返回 {comp: {"nominal", "lower", "upper", "zero_tolerance"}}。
    """
    f = 1.0 if basis == "dry" else dry_factor(moisture_pct)
    out = {}
    for comp, nominal in composition_dry.items():
        spec = uncertainties_native.get(comp)
        if spec is None:
            out[comp] = {
                "nominal": float(nominal),
                "lower": float(nominal),
                "upper": float(nominal),
                "zero_tolerance": True,
            }
        else:
            lo = float(nominal) + spec["lower"] * f
            hi = float(nominal) + spec["upper"] * f
            # 注意：下界允许为负——率值分母跨零由 check_denominator_intervals
            # 以 DENOMINATOR_SPAN_ZERO 明确拒绝；此处不静默截断为零，
            # 以免出现倒置/错误边界，只用标记位提示越过物理零点。
            out[comp] = {
                "nominal": float(nominal),
                "lower": lo,
                "upper": hi,
                "zero_tolerance": False,
                "below_physical_zero": lo < -EPS,
            }
    return out


def check_denominator_intervals(rows: list) -> None:
    """率值分母的不确定区间不得触及/跨越零。

    rows: 含 code/name/version/lab_report_no 与 bounds_dry 的求解行。
    对每个候选原料独立检查其各组分都可能被单独配入（其余份额可趋零），
    因此任一候选的分母区间下界 ≤ 0 即整体拒绝，不输出无定义的率值。
    """
    problems = []
    denoms = [
        ("SM", "Al2O3+Fe2O3", ["Al2O3", "Fe2O3"]),
        ("IM", "Fe2O3", ["Fe2O3"]),
        ("KH", "2.8*SiO2", ["SiO2"]),
    ]
    for r in rows:
        bd = getattr(r, "bounds_dry", None) or {}
        for indicator, den_name, comps in denoms:
            lo = sum(bd[c]["lower"] for c in comps if c in bd)
            hi = sum(bd[c]["upper"] for c in comps if c in bd)
            # 名义为正但误差下界把分母带到零（或负）→ 跨零拒绝
            if lo <= EPS:
                detail = {c: {k: round(bd[c][k], 4) for k in ("lower", "upper")}
                          for c in comps if c in bd}
                problems.append({
                    "material_code": r.code, "material_name": r.name,
                    "assay_version": r.version, "lab_report_no": r.lab_report_no,
                    "indicator": indicator, "denominator": den_name,
                    "denominator_interval_dry": [round(lo, 4), round(hi, 4)],
                    "component_bounds_dry": detail,
                    "message": (f"{r.code} {r.name} 的 {indicator} 分母不确定区间"
                                f" [{lo:.4f}, {hi:.4f}] 触及零，边界上 {indicator} 无定义。"),
                })
    if problems:
        raise DenominatorSpanZeroError(problems)


def worst_case_vector(rows: list, shares_dry, hazard_keys=()) -> dict:
    """给定固定干基份额 x，计算 SM/IM/KH 与有害组分在检测误差下的最坏边界。

    返回各指标的 nominal / worst_min / worst_max（率值为单调分式，
    分子取低、分母取高→指标最低；反之为最高），以及触发组分方向。
    """
    def col(comp, pick):
        vals = []
        for r in rows:
            b = (getattr(r, "bounds_dry", None) or {}).get(comp)
            if b is None:
                v = r.composition_dry.get(comp, 0.0)
                vals.append(v)
            else:
                vals.append(b["lower"] if pick == "lo" else b["upper"])
        return sum(v * x for v, x in zip(vals, shares_dry))

    S_lo = col("SiO2", "lo"); S_hi = col("SiO2", "hi")
    A_lo = col("Al2O3", "lo"); A_hi = col("Al2O3", "hi")
    F_lo = col("Fe2O3", "lo"); F_hi = col("Fe2O3", "hi")
    C_lo = col("CaO", "lo"); C_hi = col("CaO", "hi")

    out = {}
    if A_lo + F_lo > EPS:
        out["SM"] = {
            "worst_min": round(S_lo / (A_hi + F_hi), 4),
            "worst_max": round(S_hi / (A_lo + F_lo), 4),
        }
    else:
        out["SM"] = {"undefined": True}
    if F_lo > EPS:
        out["IM"] = {
            "worst_min": round(A_lo / F_hi, 4),
            "worst_max": round(A_hi / F_lo, 4),
        }
    else:
        out["IM"] = {"undefined": True}
    if S_lo > EPS:
        num_lo = C_lo - 1.65 * A_hi - 0.35 * F_hi
        num_hi = C_hi - 1.65 * A_lo - 0.35 * F_lo
        out["KH"] = {
            "worst_min": round(num_lo / (2.8 * S_hi), 4),
            "worst_max": round(num_hi / (2.8 * S_lo), 4),
        }
    else:
        out["KH"] = {"undefined": True}

    for key in hazard_keys:
        if key == "alkali_eq":
            na_lo = col("Na2O", "lo"); na_hi = col("Na2O", "hi")
            k_lo = col("K2O", "lo"); k_hi = col("K2O", "hi")
            out["alkali_eq"] = {
                "worst_min": round(na_lo + ALKALI_EQ_FACTOR_K2O * k_lo, 4),
                "worst_max": round(na_hi + ALKALI_EQ_FACTOR_K2O * k_hi, 4),
            }
        else:
            out[key] = {
                "worst_min": round(col(key, "lo"), 4),
                "worst_max": round(col(key, "hi"), 4),
            }
    return out
