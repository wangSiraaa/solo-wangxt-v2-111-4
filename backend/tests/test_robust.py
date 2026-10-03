"""检测不确定度（稳健配比）核心行为测试。

覆盖：
- 同基准容差校验（倒置/负值/缺测项声明）；
- 湿基容差按 1/(1-w) 换干基，不倒置、不影响湿料成本口径；
- 零容差稳健解与名义解一致；
- 名义合格但 KH/碱当量边界超限时稳健不可行，并给报告/组分/突破量；
- 分母区间跨零明确拒绝；
- 缺测项不能以容差“补零”。
"""
import numpy as np
import pytest

from app import chemistry, optimizer
from app.chemistry import UncertaintyError, UncertainDenominatorError
from app.schemas import Interval
from types import SimpleNamespace


def _iv(lo=None, hi=None):
    return Interval(min=lo, max=hi)


def _targets(sm=(2.4, 2.8), im=(1.4, 1.8), kh=(0.88, 0.94)):
    return SimpleNamespace(SM=_iv(*sm), IM=_iv(*im), KH=_iv(*kh))


def _req(targets=None, hazards=None, batch=1000.0, modes=("min_cost",),
         cheap=None, robust="nominal"):
    return SimpleNamespace(
        targets=targets or _targets(), hazard_limits_pct=hazards or {},
        batch_t_dry=batch, modes=list(modes), cheap_material_id=cheap,
        robust_mode=robust, scenario_name="t",
    )


BASE = {
    "LS": dict(m=2, cost=65, mins=60,
               comp={"CaO": 49.5, "SiO2": 5.2, "Al2O3": 1.4, "Fe2O3": 0.7,
                     "MgO": 1.2, "SO3": 0.25, "K2O": 0.35, "Na2O": 0.08,
                     "Cl": 0.005, "LOI": 41.315}),
    "CL": dict(m=6, cost=42, mins=0,
               comp={"CaO": 8.0, "SiO2": 55.0, "Al2O3": 17.0, "Fe2O3": 7.5,
                     "MgO": 2.2, "SO3": 0.3, "K2O": 2.6, "Na2O": 0.7,
                     "Cl": 0.02, "LOI": 6.68}),
    "SS": dict(m=6, cost=42, mins=0,
               comp={"CaO": 1.5, "SiO2": 78.0, "Al2O3": 9.5, "Fe2O3": 3.8,
                     "MgO": 0.8, "SO3": 0.1, "K2O": 1.1, "Na2O": 0.25,
                     "Cl": 0.01, "LOI": 4.94}),
    "IR": dict(m=8, cost=320, mins=0,
               comp={"CaO": 4.0, "SiO2": 18.0, "Al2O3": 5.0, "Fe2O3": 62.0,
                     "MgO": 2.5, "SO3": 1.2, "K2O": 0.4, "Na2O": 0.1,
                     "Cl": 0.02, "LOI": 6.78}),
    "AS01": dict(m=18, cost=18, mins=0,
                 comp={"CaO": 3.69, "SiO2": 39.36, "Al2O3": 24.6,
                       "Fe2O3": 5.33, "LOI": 4.9077}),
    "QZ01": dict(m=0.5, cost=95, mins=0,
                 comp={"CaO": 0.05, "SiO2": 98.6, "Al2O3": 0.35,
                       "Fe2O3": 0.18, "LOI": 0.7}),
}


def _row(i, code, unc=None, basis="dry", moisture=None, comp_override=None):
    spec = dict(BASE[code])
    if comp_override:
        spec["comp"] = comp_override
    m = spec["m"] if moisture is None else moisture
    comp = spec["comp"]
    measured = list(comp)
    bounds = chemistry.normalize_uncertainty(
        unc, composition=dict(comp), measured=set(measured),
        basis=basis, moisture_pct=m, material_code=code,
        assay_version="v1", lab_report_no=f"L{i}")
    comp_dry = chemistry.convert_composition(comp, basis, m)
    return optimizer.Row(
        material_id=i, code=code, name=code, moisture_pct=m,
        cost_per_t_wet=spec["cost"], availability_t_wet=None,
        min_share_pct=spec["mins"], assay_version_id=i,
        version="v1", lab_report_no=f"L{i}", basis=basis,
        composition_raw=dict(comp), composition_dry=comp_dry,
        measured=set(measured), uncertainty_raw=unc, bounds=bounds,
    )


def _rows4(unc_map=None):
    unc_map = unc_map or {}
    return [_row(1, "LS", unc_map.get("LS")),
            _row(2, "CL", unc_map.get("CL")),
            _row(3, "SS", unc_map.get("SS")),
            _row(4, "IR", unc_map.get("IR"))]


# ---------- 容差声明校验 ----------
def test_inverted_interval_rejected():
    with pytest.raises(UncertaintyError) as e:
        chemistry.normalize_uncertainty(
            {"SiO2": {"lower": 0.2, "upper": 0.3}},
            composition={"SiO2": 5.2}, measured={"SiO2"},
            basis="dry", moisture_pct=0.0, material_code="X",
            assay_version="v", lab_report_no="L")
    assert e.value.code == "UNCERTAINTY_BAD_INTERVAL"


def test_tolerance_on_unmeasured_rejected():
    with pytest.raises(UncertaintyError) as e:
        chemistry.normalize_uncertainty(
            {"Fe2O3": {"lower": -0.1, "upper": 0.1}},
            composition={"CaO": 45.0}, measured={"CaO"},
            basis="dry", moisture_pct=0.0, material_code="SP01",
            assay_version="v", lab_report_no="L")
    assert e.value.code == "UNCERTAINTY_ON_UNMEASURED"


def test_negative_content_rejected():
    with pytest.raises(UncertaintyError) as e:
        chemistry.normalize_uncertainty(
            {"Fe2O3": {"lower": -0.5, "upper": 0.1}},
            composition={"Fe2O3": 0.18}, measured={"Fe2O3"},
            basis="dry", moisture_pct=0.0, material_code="QZ01",
            assay_version="v", lab_report_no="L")
    assert e.value.code == "UNCERTAINTY_NEGATIVE_VALUE"


# ---------- 湿基容差换算 ----------
def test_wet_basis_uncertainty_scaled_to_dry():
    # 粉煤灰湿基 SiO2 名义 39.36，湿基容差 ±0.328，含水率 18%
    bounds = chemistry.normalize_uncertainty(
        {"SiO2": {"lower": -0.328, "upper": 0.328}},
        composition={"SiO2": 39.36}, measured={"SiO2"},
        basis="wet", moisture_pct=18.0, material_code="AS01",
        assay_version="vW", lab_report_no="W")
    f = 1.0 / 0.82
    assert bounds["SiO2"]["nominal_dry"] == pytest.approx(39.36 * f)
    assert bounds["SiO2"]["lo"] == pytest.approx((39.36 - 0.328) * f)
    assert bounds["SiO2"]["hi"] == pytest.approx((39.36 + 0.328) * f)
    # 不出现倒置区间
    assert bounds["SiO2"]["lo"] <= bounds["SiO2"]["nominal_dry"] <= bounds["SiO2"]["hi"]


def test_wet_uncertainty_does_not_change_wet_mass_cost():
    # 湿料成本只依赖档案含水率与湿吨价，容差不改变质量/成本系数
    unc = {"SiO2": {"lower": -0.328, "upper": 0.328}}
    r = _row(5, "AS01", unc, basis="wet", moisture=18.0)
    c = optimizer._cost_coef([r])[0]
    assert c == pytest.approx(18.0 / 0.82)


# ---------- ① 零容差 == 名义 ----------
def test_zero_tolerance_robust_matches_nominal():
    rows = _rows4()
    s_nom = optimizer.solve(rows, _req(robust="nominal"))[0]
    s_rob = optimizer.solve(_rows4(), _req(robust="robust"))[0]
    assert s_nom["success"] and s_rob["robust"]["success"]
    a = [(it["material_code"], it["share_pct_dry"]) for it in s_nom["items"]]
    b = [(it["material_code"], it["share_pct_dry"]) for it in s_rob["robust"]["items"]]
    assert a == b
    # 名义方案与稳健方案成本一致
    assert s_nom["cost_per_t_dry"] == s_rob["robust"]["cost_per_t_dry"]


# ---------- ② 名义合格、边界 KH 超限 → 稳健不可行 ----------
def test_nominal_ok_but_robust_kh_boundary_infeasible():
    # KH 窗口收紧到 0.90~0.92：名义解可落进窗口，但 CaO 的正偏置把 KH_max 顶破
    unc = {
        "LS": {"CaO": {"lower": -0.6, "upper": 0.6}},
        "CL": {"CaO": {"lower": -0.3, "upper": 0.3}},
        "SS": {"CaO": {"lower": -0.2, "upper": 0.2}},
        "IR": {"CaO": {"lower": -0.2, "upper": 0.2}},
    }
    rows = _rows4(unc)
    req = _req(targets=_targets(kh=(0.895, 0.92)), robust="robust")
    s = optimizer.solve(rows, req)[0]
    assert s["success"]               # 名义可行
    assert not s["robust"]["success"]  # 稳健不可行
    diag = s["robust"]["diagnostic"]
    assert diag["reason"] == "ROBUST_INFEASIBLE_BOUNDARY"
    kh_conf = next(c for c in diag["conflicts"] if c["kind"] == "KH_max")
    assert kh_conf["achieved"] > kh_conf["limit"]
    assert kh_conf["breach"] > 0
    # 列出报告/组分/边界
    mats = {m["material_code"] for m in kh_conf["boundary_combo"]}
    assert "LS" in mats
    ls = next(m for m in kh_conf["boundary_combo"] if m["material_code"] == "LS")
    assert ls["lab_report_no"] == "L1"
    assert ls["components"]["CaO"]["side"] == "upper"


def test_nominal_ok_but_robust_alkali_boundary_infeasible():
    # 宽松率值窗口 + 页岩最低掺量 5%：名义碱当量达标，K2O/Na2O 取上界即超限
    unc_cl = {"K2O": {"lower": -0.25, "upper": 0.25},
              "Na2O": {"lower": -0.06, "upper": 0.06}}
    ls = _row(1, "LS", None)
    cl = _row(2, "CL", unc_cl)
    cl.min_share_pct = 5.0
    ss = _row(3, "SS", None)
    ir = _row(4, "IR", None)
    wide = _targets(sm=(0, 10), im=(0, 10), kh=(0, 2))
    req = _req(targets=wide, hazards={"alkali_eq": 0.42}, robust="robust")
    s = optimizer.solve([ls, cl, ss, ir], req)[0]
    assert s["success"]
    assert not s["robust"]["success"]
    conf = next(c for c in s["robust"]["diagnostic"]["conflicts"]
                if c["kind"] == "hazard_alkali_eq")
    assert conf["achieved"] > conf["limit"]
    assert conf["breach"] > 0
    # 碱当量边界必须列出 Na2O 与 K2O，且定位到页岩的化验单
    clc = next(m for m in conf["boundary_combo"] if m["material_code"] == "CL")
    assert set(clc["components"]) == {"Na2O", "K2O"}
    assert clc["lab_report_no"] == "L2"


# ---------- 稳健可行时报告余量 ----------
def test_robust_feasible_reports_margins():
    unc = {k: {"SiO2": {"lower": -0.05, "upper": 0.05}} for k in BASE}
    s = optimizer.solve(_rows4(unc), _req(robust="robust"))[0]
    assert s["success"] and s["robust"]["success"]
    m = s["robust"]["robust_report"]["margins"]
    assert m["KH"]["headroom_low"] >= -1e-9
    assert m["KH"]["headroom_high"] >= -1e-9
    assert m["KH"]["worst_low"] <= m["KH"]["nominal"] <= m["KH"]["worst_high"]
    # 分母区间均为正
    assert m["IM"]["denominator_interval"][0] > 0
    assert m["KH"]["denominator_interval"][0] > 0


# ---------- ③/④ 分母跨零 ----------
def test_denominator_interval_crossing_zero_rejected():
    # 高纯石英：Fe2O3 名义 0.18，下界 0.0
    qz = _row(6, "QZ01", {"Fe2O3": {"lower": -0.18, "upper": 0.05},
                          "SiO2": {"lower": -0.4, "upper": 0.4},
                          "CaO": {"lower": -0.02, "upper": 0.02},
                          "Al2O3": {"lower": -0.05, "upper": 0.05}})
    # 配入少量铁料使名义 IM 有定义；但稳健下 QZ01 的 Fe2O3 可取 0，必须拒绝。
    # 率值只设非负性约束，避免 KH 下限先判名义不可行。
    ir = _row(4, "IR", None)
    rows = [qz, ir]
    req = _req(targets=_targets(sm=(0, None), im=(0, None), kh=(None, None)),
               robust="robust")
    with pytest.raises(UncertainDenominatorError) as e:
        optimizer.solve(rows, req)
    assert e.value.code == "UNCERTAIN_ZERO_DENOMINATOR"
    inds = {d["indicator"] for d in e.value.details["denominators"]}
    assert "IM" in inds


def test_missing_component_still_raises_under_robust():
    # 缺测 Fe2O3 的矿样：即使不开容差也必须 MISSING_ASSAY
    spec = {"CaO": 45.0, "SiO2": 6.0, "Al2O3": 2.0, "LOI": 46.0}
    bounds = {}
    sp = optimizer.Row(
        material_id=7, code="SP01", name="缺测", moisture_pct=3.0,
        cost_per_t_wet=30.0, availability_t_wet=None, min_share_pct=0.0,
        assay_version_id=7, version="v1", lab_report_no="LM", basis="dry",
        composition_raw=dict(spec), composition_dry=dict(spec),
        measured={"CaO", "SiO2", "Al2O3", "LOI"}, uncertainty_raw=None,
        bounds=bounds)
    with pytest.raises(chemistry.MissingAssayError):
        optimizer.solve([sp], _req(robust="robust"))


# ---------- 最坏边界方向数学核对 ----------
def test_worst_indicator_directions():
    r = _row(1, "LS", {"SiO2": {"lower": -0.2, "upper": 0.2},
                       "Al2O3": {"lower": -0.1, "upper": 0.1},
                       "Fe2O3": {"lower": -0.05, "upper": 0.05},
                       "CaO": {"lower": -0.3, "upper": 0.3}})
    x = np.array([1.0])
    w = optimizer.worst_indicators([r], x)
    # SM = SiO2/(A+F)：SiO2 下、A/F 上 → 最小；反之最大
    assert w["SM"]["worst_low"] < w["SM"]["nominal"] < w["SM"]["worst_high"]
    assert w["IM"]["worst_low"] < w["IM"]["nominal"] < w["IM"]["worst_high"]
    assert w["KH"]["worst_low"] < w["KH"]["nominal"] < w["KH"]["worst_high"]
