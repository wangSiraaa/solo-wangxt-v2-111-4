"""检测不确定度下的稳健配比测试：

① 零容差时稳健结果与名义求解一致；
② 名义合格但最坏边界使 KH 超限 → 稳健不可行，列出报告/组分/突破量；
③ 湿基化验单上下界换算到干基，不倒置、湿料成本不变；
④ 分母区间跨零明确拒绝；非法容差/未测组分明确报错。
"""
import pytest

from app import chemistry, optimizer
from app.chemistry import (
    BadUncertaintyError,
    DenominatorSpanZeroError,
    bounds_dry,
    calc_indicators,
    convert_composition,
    normalize_uncertainties,
)
from app.schemas import Interval
from types import SimpleNamespace


# ---------- 容差归一化 ----------
def test_normalize_sym_and_asym():
    out = normalize_uncertainties(
        {"SiO2": 0.3, "CaO": {"lower": -0.2, "upper": 0.4}},
        ["SiO2", "CaO"])
    assert out["SiO2"] == {"lower": -0.3, "upper": 0.3}
    assert out["CaO"] == {"lower": -0.2, "upper": 0.4}


def test_zero_tolerance_dropped():
    out = normalize_uncertainties(
        {"SiO2": {"lower": 0, "upper": 0}}, ["SiO2"])
    assert out == {}


def test_uncertainty_on_unmeasured_rejected():
    with pytest.raises(BadUncertaintyError) as e:
        normalize_uncertainties({"Fe2O3": 0.1}, ["CaO", "SiO2"],
                                material_code="SP01")
    assert e.value.code == "BAD_UNCERTAINTY"
    assert e.value.details["items"][0]["problem"] == "UNMEASURED_COMPONENT"


def test_uncertainty_bad_signs_rejected():
    with pytest.raises(BadUncertaintyError):
        normalize_uncertainties(
            {"SiO2": {"lower": 0.3, "upper": 0.1}}, ["SiO2"])


# ---------- 湿基区间换算 ----------
def test_wet_basis_bounds_converted_and_ordered():
    wet_comp = {"SiO2": 41.0}
    dry_nominal = convert_composition(wet_comp, "wet", 18.0)
    unc = normalize_uncertainties({"SiO2": 0.164}, ["SiO2"])
    b = bounds_dry(dry_nominal, unc, "wet", 18.0)
    # (41 ± 0.164)/0.82
    assert b["SiO2"]["nominal"] == pytest.approx(50.0)
    assert b["SiO2"]["lower"] == pytest.approx(49.8)
    assert b["SiO2"]["upper"] == pytest.approx(50.2)
    # 不倒置
    assert b["SiO2"]["lower"] <= b["SiO2"]["nominal"] <= b["SiO2"]["upper"]


# ---------- 构造带容差的求解行 ----------
def _row(mid, code, m, cost, mins, comp, unc=None, basis="dry",
         measured=None, avail=None):
    measured = measured or list(comp)
    unc_native = normalize_uncertainties(
        unc or {}, measured, material_code=code, material_name=code,
        assay_version="v1", lab_report_no=f"L{mid}")
    comp_dry = chemistry.convert_composition(comp, basis, m)
    bdry = bounds_dry(comp_dry, unc_native, basis, m)
    return optimizer.Row(
        mid, code, code, m, cost, avail, mins, mid, "v1", f"L{mid}",
        basis, dict(comp), comp_dry, set(measured), unc_native, bdry)


def _req(sm=(2.4, 2.8), im=(1.4, 1.8), kh=(0.88, 0.94), hazards=None,
         cheap=None, modes=("min_cost",)):
    iv = lambda t: Interval(min=t[0], max=t[1])
    return SimpleNamespace(
        targets=SimpleNamespace(SM=iv(sm), IM=iv(im), KH=iv(kh)),
        hazard_limits_pct=hazards or {}, batch_t_dry=1000.0,
        modes=list(modes), cheap_material_id=cheap, scenario_name="t")


# ---------- ① 零容差：稳健 = 名义 ----------
def _rows_with_unc(unc_map, wet_unc=None):
    return [
        _row(1, "LS", 2, 65, 60,
             {"CaO": 49.5, "SiO2": 5.2, "Al2O3": 1.4, "Fe2O3": 0.7,
              "K2O": 0.35, "Na2O": 0.08, "Cl": 0.005},
             unc_map.get("LS")),
        _row(2, "SS", 6, 42, 0,
             {"CaO": 1.5, "SiO2": 78.0, "Al2O3": 9.5, "Fe2O3": 3.8,
              "K2O": 1.1, "Na2O": 0.25, "Cl": 0.01},
             unc_map.get("SS")),
        _row(3, "SH", 14, 22, 0,
             {"CaO": 8.0, "SiO2": 55.0, "Al2O3": 17.0, "Fe2O3": 7.5,
              "K2O": 2.6, "Na2O": 0.7, "Cl": 0.02},
             unc_map.get("SH")),
        _row(4, "IR", 8, 320, 0,
             {"CaO": 4.0, "SiO2": 18.0, "Al2O3": 5.0, "Fe2O3": 62.0,
              "K2O": 0.4, "Na2O": 0.1, "Cl": 0.02},
             unc_map.get("IR")),
    ]


def test_robust_with_zero_tolerance_equals_nominal():
    rows = _rows_with_unc({})  # 全部零容差
    req = _req()
    n = optimizer.solve(rows, req)[0]
    r = optimizer.solve_robust(rows, req)[0]
    assert n["success"] and r["success"]
    assert r["robust"] is True
    assert r["cost_per_t_dry"] == n["cost_per_t_dry"]
    assert r["indicators"]["KH"] == n["indicators"]["KH"]
    assert r["indicators"]["SM"] == n["indicators"]["SM"]
    assert [(i["material_code"], i["share_pct_dry"]) for i in r["items"]] == \
           [(i["material_code"], i["share_pct_dry"]) for i in n["items"]]
    # 零容差时最坏边界应等于名义值
    wc = r["worst_case"]
    for e in wc["indicators"]:
        assert e["worst_min"] == pytest.approx(e["nominal"], abs=1e-6)
        assert e["worst_max"] == pytest.approx(e["nominal"], abs=1e-6)


# ---------- 小容差：稳健可行，余量非负，成本不低于名义 ----------
SMALL = {
    "LS": {"CaO": 0.15, "SiO2": 0.08, "Al2O3": 0.05, "Fe2O3": 0.04},
    "SS": {"SiO2": 0.3, "Al2O3": 0.12, "Fe2O3": 0.08},
    "SH": {"CaO": 0.1, "SiO2": 0.25, "Al2O3": 0.15, "Fe2O3": 0.1},
    "IR": {"Fe2O3": 0.6, "SiO2": 0.15, "Al2O3": 0.08},
}


def test_robust_feasible_with_margins():
    rows = _rows_with_unc(SMALL)
    req = _req(hazards={"Cl": 0.05})
    n = optimizer.solve(rows, req)[0]
    r = optimizer.solve_robust(rows, req)[0]
    assert n["success"] and r["success"]
    assert r["cost_per_t_dry"] >= n["cost_per_t_dry"] - 1e-9
    for e in r["worst_case"]["indicators"]:
        assert e["ok"]
        assert e["margin_min"] >= -1e-6 and e["margin_max"] >= -1e-6


# ---------- ② 名义合格、最坏边界 KH 超限 → 稳健不可行 ----------
QUICK = {
    "LS": {"CaO": 0.35, "SiO2": 0.18, "Al2O3": 0.10, "Fe2O3": 0.08},
    "SS": {"SiO2": 0.7, "Al2O3": 0.25, "Fe2O3": 0.18},
    "SH": {"CaO": 0.25, "SiO2": 0.6, "Al2O3": 0.35, "Fe2O3": 0.25},
    "IR": {"Fe2O3": 1.0, "SiO2": 0.3, "Al2O3": 0.15},
}


def test_nominal_pass_but_robust_kh_break():
    rows = _rows_with_unc(QUICK)
    # KH 窗口收紧到 [0.90, 0.95]：名义解贴 0.90/上界，误差把 KH 推出 0.95
    req = _req(sm=(2.3, 2.9), im=(1.3, 1.9), kh=(0.90, 0.95))
    n = optimizer.solve(rows, req)[0]
    r = optimizer.solve_robust(rows, req)[0]
    assert n["success"]
    assert 0.90 - 1e-9 <= n["indicators"]["KH"] <= 0.95 + 1e-9
    assert not r["success"]
    diag = r["diagnostic"]
    assert diag["reason"] == "ROBUST_INFEASIBLE_BOUNDARY"
    kh_conflicts = [c for c in diag["conflicts"]
                    if c["kind"] in ("KH_max", "KH_min")]
    assert kh_conflicts
    c0 = kh_conflicts[0]
    # 突破量为正
    assert c0["breakthrough"] > 0
    # 列出触发报告与组分
    reps = c0["trigger_reports"]
    assert reps and all({"material_code", "assay_version", "lab_report_no",
                         "components"} <= set(x) for x in reps)
    moved = {comp["component"] for x in reps for comp in x["components"]}
    assert {"CaO", "SiO2"} <= moved


def test_robust_hazard_breakthrough_lists_components():
    # Cl 名义 0.005 贴限 0.005，容差上界把它推过限
    rows = _rows_with_unc({
        "LS": {"Cl": 0.002}, "SS": {}, "SH": {"Cl": 0.004}, "IR": {"Cl": 0.004}})
    req = _req(hazards={"Cl": 0.005})
    r = optimizer.solve_robust(rows, req)[0]
    assert not r["success"]
    cl = [c for c in r["diagnostic"]["conflicts"]
          if c["kind"] == "hazard_Cl"]
    assert cl and cl[0]["breakthrough"] > 0
    assert cl[0]["trigger_reports"]


# ---------- ④ 分母跨零明确拒绝 ----------
def test_denominator_interval_spanning_zero_rejected():
    # Fe2O3 名义 0.30 ± 0.35 → 区间 [-0.05, 0.65]
    lz = _row(9, "LZ", 3, 48, 0,
              {"CaO": 2.0, "SiO2": 82.0, "Al2O3": 7.0, "Fe2O3": 0.30},
              {"Fe2O3": 0.35, "SiO2": 0.8})
    with pytest.raises(DenominatorSpanZeroError) as e:
        optimizer.solve_robust([lz], _req(
            sm=(0, 100), im=(0, 100), kh=(0, 100)))
    indicators = {p["indicator"] for p in e.value.details["problems"]}
    assert "IM" in indicators
    assert "KH" not in indicators  # SiO2 82±0.8 不跨零


def test_denominator_touching_zero_rejected():
    # 恰好触零（下界 = 0）也必须拒绝
    lz = _row(9, "LZ", 0, 48, 0,
              {"CaO": 1.0, "SiO2": 90.0, "Al2O3": 5.0, "Fe2O3": 0.1},
              {"Fe2O3": 0.1})
    with pytest.raises(DenominatorSpanZeroError):
        optimizer.solve_robust([lz], _req())


# ---------- ③ 湿基容差：干基区间参与稳健约束，湿料成本不变 ----------
def test_wet_uncertainty_bounds_enter_robust_and_cost_ok():
    dry_rows = _rows_with_unc(SMALL)
    # 用湿基粉煤灰替换页岩行
    wet_ash = _row(
        6, "AS", 18, 18, 0,
        {"CaO": 3.69, "SiO2": 39.36, "Al2O3": 24.6, "Fe2O3": 5.33,
         "K2O": 1.476, "Na2O": 0.738, "Cl": 0.0123},
        {"SiO2": 0.16, "Al2O3": 0.12, "Fe2O3": 0.04}, basis="wet")
    rows = [dry_rows[0], dry_rows[1], wet_ash, dry_rows[3]]
    req = _req(hazards={"Cl": 0.05})
    n = optimizer.solve(rows, req)[0]
    r = optimizer.solve_robust(rows, req)[0]
    assert n["success"] and r["success"]
    ash = next(x for x in r["items"] if x["material_code"] == "AS")
    # 湿料 = 干料/(1-0.18)；成本 = 湿料 × 18
    assert ash["mass_t_wet"] == pytest.approx(ash["mass_t_dry"] / 0.82)
    assert ash["cost"] == pytest.approx(ash["mass_t_wet"] * 18.0, abs=0.01)
    snap = ash["uncertainty_snapshot"]
    sio2 = next(x for x in snap["components"] if x["component"] == "SiO2")
    assert snap["basis"] == "wet"
    # 湿基 39.36 ± 0.16 → 干基 48.0，区间 [47.8049, 48.1951]
    assert sio2["lower_dry"] == pytest.approx(47.8049, abs=1e-3)
    assert sio2["upper_dry"] == pytest.approx(48.1951, abs=1e-3)
    assert sio2["lower_dry"] <= sio2["nominal_dry"] <= sio2["upper_dry"]


# ---------- 缺测仍按原规则拒绝 ----------
def test_missing_component_still_raises():
    a = _row(1, "A", 0, 10, 0,
             {"CaO": 50.0, "SiO2": 10.0, "Al2O3": 2.0},
             measured=["CaO", "SiO2", "Al2O3"])
    with pytest.raises(chemistry.MissingAssayError):
        optimizer.solve_robust([a], _req())
