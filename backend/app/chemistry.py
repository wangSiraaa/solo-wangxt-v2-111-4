"""化学口径核心：干湿基换算、质量守恒合成、硅率/铝率/石灰饱和指标。

硬性规则（工艺研发约定）：
1. “未测/缺测”绝不以 0 含量参与计算——缺测直接抛 MissingAssayError；
2. 率值分母为 0 抛 ZeroDenominatorError，不静默返回 inf/0；
3. 所有换算保留逐步 trace，供前端追溯；
4. 检测不确定度按“同基准逐组分上下偏置”声明，湿基区间必须先换成干基；
   区间倒置/负值、把容差声明在缺测项上、最坏情况下率值分母可能为零，
   一律显式报错（见 uncertainty 相关异常与函数）。
"""
import math
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


class UncertaintyError(BlendError):
    """检测不确定度声明不合法：倒置/负值区间、声明在缺测项上、基准非法等。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(code, message, details or {})


class UncertainDenominatorError(BlendError):
    """某率值分母的检测区间跨越零：真实值可能令分母为零，率值无定义，必须拒绝。"""

    def __init__(self, bad: list[dict]):
        super().__init__(
            "UNCERTAIN_ZERO_DENOMINATOR",
            "存在检测不确定区间跨越零的率值分母：真实化验值可能令 SM/IM/KH "
            "无定义，稳健试算必须拒绝（不得输出无定义的率值），请收窄容差或补测。",
            {"denominators": bad},
        )


# 率值分母的组成：分母表达式 → 所含组分
DENOMINATOR_PARTS = {
    "SM": ["Al2O3", "Fe2O3"],          # Al2O3 + Fe2O3
    "IM": ["Fe2O3"],                    # Fe2O3
    "KH": ["SiO2"],                     # 2.8 * SiO2
}


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


# ---------------------------------------------------------------------------
# 检测不确定度（uncertainty）
#
# 声明口径：与化验单同一基准（dry/wet），逐组分给“绝对偏置（百分点）”：
#   {"SiO2": {"lower": -0.30, "upper": 0.20}, ...}
#   lower <= 0 <= upper；缺省一侧按零容差；整项缺省 = 零容差。
# 湿基区间必须按现有含水率规则整体放大到干基（×1/(1-w)），
# 不允许先在湿基取最坏值再用别的口径折算。
# ---------------------------------------------------------------------------
def normalize_uncertainty(
    raw: dict | None,
    *,
    composition: dict,
    measured: set[str],
    basis: str,
    moisture_pct: float,
    material_code: str,
    assay_version: str,
    lab_report_no: str,
) -> dict:
    """校验并把同基准上下偏置换算为干基。

    返回 {component: {"lo": 干基下界(<=名义), "hi": 干基上界(>=名义),
                      "lo_in": 输入基准下偏置, "hi_in": 输入基准上偏置,
                      "nominal_dry": 干基名义值}}。
    零声明即零容差，缺省组分不进表（求解时按点值处理）。
    """
    src = {
        "material_code": material_code,
        "assay_version": assay_version,
        "lab_report_no": lab_report_no,
    }
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UncertaintyError(
            "UNCERTAINTY_BAD_FORMAT",
            "检测不确定度必须是 {组分: {lower, upper}} 形式的对象。",
            {"raw": raw, **src},
        )

    f = 1.0 if basis == "dry" else dry_factor(moisture_pct)
    out: dict[str, dict] = {}
    bad_fmt, bad_sign, negative, on_unmeasured = [], [], [], []

    for comp, spec in raw.items():
        if not isinstance(spec, dict):
            bad_fmt.append({"component": comp, "spec": spec, **src})
            continue
        try:
            lo_in = float(spec.get("lower", 0.0) or 0.0)
            hi_in = float(spec.get("upper", 0.0) or 0.0)
        except (TypeError, ValueError):
            bad_fmt.append({"component": comp, "spec": spec, **src})
            continue
        if not (math.isfinite(lo_in) and math.isfinite(hi_in)):
            bad_fmt.append({"component": comp, "spec": spec, **src})
            continue
        if lo_in > 0.0 or hi_in < 0.0 or lo_in > hi_in:
            bad_sign.append({
                "component": comp, "lower": lo_in, "upper": hi_in, **src,
                "rule": "必须 lower ≤ 0 ≤ upper（下界向下、上界向上）",
            })
            continue
        if comp not in measured or comp not in composition:
            on_unmeasured.append({"component": comp, **src})
            continue
        nominal_in = float(composition[comp])
        if nominal_in + lo_in < -EPS:
            negative.append({
                "component": comp, "nominal": nominal_in, "lower": lo_in,
                "min_value": round(nominal_in + lo_in, 6), **src,
            })
            continue
        nominal_dry = nominal_in * f
        out[comp] = {
            "lo": (nominal_in + lo_in) * f,
            "hi": (nominal_in + hi_in) * f,
            "lo_in": lo_in,
            "hi_in": hi_in,
            "nominal_dry": nominal_dry,
        }

    if bad_fmt:
        raise UncertaintyError(
            "UNCERTAINTY_BAD_FORMAT",
            "检测不确定度格式非法：每项必须给出数值型 lower/upper（百分点偏置）。",
            {"items": bad_fmt},
        )
    if bad_sign:
        raise UncertaintyError(
            "UNCERTAINTY_BAD_INTERVAL",
            "检测不确定度区间倒置或方向错误：必须满足 lower ≤ 0 ≤ upper，"
            "且下界不大于上界。",
            {"items": bad_sign},
        )
    if on_unmeasured:
        raise UncertaintyError(
            "UNCERTAINTY_ON_UNMEASURED",
            "不得对缺测组分声明检测容差：未测项目没有名义值，先补测或将该原料"
            "移出候选（系统绝不以零含量兜底）。",
            {"items": on_unmeasured},
        )
    if negative:
        raise UncertaintyError(
            "UNCERTAINTY_NEGATIVE_VALUE",
            "检测下界使组分含量为负：物理含量不得小于 0，请收窄容差。",
            {"items": negative},
        )
    return out


def uncertainty_dry_bounds(bounds: dict) -> dict:
    """从 normalize 结果取 {component: (lo_dry, hi_dry)}（均为干基质量百分数）。"""
    return {c: (v["lo"], v["hi"]) for c, v in bounds.items()}


def build_uncertainty_trace(
    material_code: str,
    material_name: str,
    basis: str,
    moisture_pct: float,
    composition: dict,
    raw: dict | None,
    bounds: dict,
) -> dict:
    """不确定度留痕：输入基准偏置、换算系数、干基上下界（随方案历史保存）。"""
    f = 1.0 if basis == "dry" else dry_factor(moisture_pct)
    steps = []
    for comp in sorted(set(composition) | set((raw or {}).keys())):
        b = bounds.get(comp)
        if b is None:
            nominal_in = composition.get(comp)
            nominal_dry = float(nominal_in) * f if nominal_in is not None else None
            steps.append({
                "component": comp,
                "basis_in": basis,
                "nominal_in": round(float(nominal_in), 6) if nominal_in is not None else None,
                "lower_in": 0.0, "upper_in": 0.0,
                "factor": round(f, 6),
                "nominal_dry": round(nominal_dry, 6) if nominal_dry is not None else None,
                "lower_dry": round(nominal_dry, 6) if nominal_dry is not None else None,
                "upper_dry": round(nominal_dry, 6) if nominal_dry is not None else None,
                "zero_tolerance": True,
            })
        else:
            steps.append({
                "component": comp,
                "basis_in": basis,
                "nominal_in": round(b["nominal_dry"] / f, 6),
                "lower_in": round(b["lo_in"], 6),
                "upper_in": round(b["hi_in"], 6),
                "factor": round(f, 6),
                "nominal_dry": round(b["nominal_dry"], 6),
                "lower_dry": round(b["lo"], 6),
                "upper_dry": round(b["hi"], 6),
                "zero_tolerance": b["lo_in"] == 0.0 and b["hi_in"] == 0.0,
            })
    n_nontrivial = sum(1 for s in steps if not s["zero_tolerance"])
    return {
        "material_code": material_code,
        "material_name": material_name,
        "assay_basis": basis,
        "moisture_pct": moisture_pct,
        "dry_factor": round(f, 6),
        "nontrivial_components": n_nontrivial,
        "steps": steps,
    }


# 率值分母的组成：指标 -> 分母表达式所含组分（KH 分母系数 2.8 另计）
DENOMINATOR_PARTS = {
    "SM": ["Al2O3", "Fe2O3"],
    "IM": ["Fe2O3"],
    "KH": ["SiO2"],
}
_DENOMINATOR_EXPR = {"SM": "Al2O3+Fe2O3", "IM": "Fe2O3", "KH": "2.8*SiO2"}


def denominator_interval(dry_bounds_materials: list[dict], indicator: str):
    """单原料层面某率值分母的干基区间列表 [(lo, hi), ...]（含量非负故 lo≥0）。"""
    parts = DENOMINATOR_PARTS[indicator]
    per = []
    for bd in dry_bounds_materials:
        lo = hi = 0.0
        for c in parts:
            if c in bd:
                lo += bd[c][0]
                hi += bd[c][1]
        if indicator == "KH":
            lo *= 2.8
            hi *= 2.8
        per.append((lo, hi))
    return per


def check_blend_denominators(x, dry_bounds_materials: list[dict]) -> list[dict]:
    """对给定干基份额 x，检查三个率值分母的最坏区间是否触及/跨越零。

    份额非负，混合分母下界 = Σ x_i·d_lo，上界 = Σ x_i·d_hi；
    下界 ≤ EPS 即存在使分母为零的检测边界组合，率值无定义，必须明确拒绝。
    """
    bad = []
    for ind, expr in _DENOMINATOR_EXPR.items():
        per = denominator_interval(dry_bounds_materials, ind)
        d_lo = sum(xi * p[0] for xi, p in zip(x, per))
        d_hi = sum(xi * p[1] for xi, p in zip(x, per))
        if d_lo <= EPS:
            contributors = sorted(
                ({
                    "material_index": i,
                    "denominator_lo_dry_pct": round(p[0], 6),
                    "denominator_hi_dry_pct": round(p[1], 6),
                 } for i, p in enumerate(per) if p[0] <= EPS and x[i] > 1e-12),
                key=lambda z: z["denominator_lo_dry_pct"],
            )
            bad.append({
                "indicator": ind,
                "denominator": expr,
                "blend_denominator_lower": round(d_lo, 6),
                "blend_denominator_upper": round(d_hi, 6),
                "materials_with_zero_share_risk": contributors,
            })
    return bad


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
