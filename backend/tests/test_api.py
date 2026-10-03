"""API 端到端：依赖已播种的 PostgreSQL（虚构演示数据）。"""
from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)
T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
     "KH": {"min": 0.88, "max": 0.94}}


def _blend(ids, modes=("min_cost",), hazards=None, cheap=None, save=False, batch=1000):
    body = {
        "scenario_name": "api-test", "batch_t_dry": batch,
        "candidates": [{"material_id": i} for i in ids],
        "targets": T, "hazard_limits_pct": hazards or {},
        "modes": list(modes), "save": save,
    }
    if cheap:
        body["cheap_material_id"] = cheap
    return c.post("/api/blend", json=body)


def test_health_and_materials():
    assert c.get("/api/health").json()["mode"] == "fictional-boundary"
    mats = c.get("/api/materials", params={"active_only": True}).json()
    assert len(mats) == 5


def test_multi_mode_blend_and_trace_fields():
    r = _blend([1, 2, 3, 4, 5], ["min_cost", "max_cheap", "balanced"], cheap=4)
    assert r.status_code == 200
    sols = r.json()["solutions"]
    assert len(sols) == 3
    for s in sols:
        assert s["success"]
        assert s["indicators"]["SM"] and s["items"]
    costs = [s["cost_per_t_dry"] for s in sols]
    assert costs[0] <= costs[2]  # 成本最优不劣于平衡方案


def test_wet_basis_assay_converted():
    # AS01 粉煤灰为湿基化验单（SiO2 湿基 39.36，含水率 18%）
    r = _blend([1, 2, 3, 4, 5], ["max_cheap"], cheap=4)
    item = next(i for i in r.json()["solutions"][0]["items"]
                if i["material_code"] == "AS01")
    sio2 = next(st for st in item["conversion_trace"]["steps"]
                if st["component"] == "SiO2")
    assert sio2["basis_in"] == "wet" and sio2["basis_out"] == "dry"
    assert abs(sio2["value_out"] - 48.0) < 1e-6


def test_cheap_only_conflict_returns_conflicts():
    r = _blend([1, 3], ["min_cost"])
    sol = r.json()["solutions"][0]
    assert not sol["success"]
    assert sol["diagnostic"]["conflicts"]


def test_missing_assay_http_422():
    r = _blend([1, 2, 7], ["min_cost"])  # SP01 缺测 Fe2O3
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_ASSAY"


def test_zero_denominator_http_422():
    r = c.post("/api/evaluate", json={
        "scenario_name": "零分母",
        "picks": [{"material_id": 8}],
        "shares_pct_dry": [100],
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "ZERO_DENOMINATOR"


def test_save_run_and_recall():
    r = _blend([1, 2, 3, 4, 5], ["min_cost"], save=True)
    rid = r.json()["run_id"]
    assert rid
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["run_code"].startswith("RUN-")
    assert detail["solutions"][0]["items"][0]["conversion_trace"]["steps"]
    assert detail["solutions"][0]["items"][0]["raw_assay"]


# ---------- 检测不确定度 / 稳健配比 ----------
def _version_id(material_id, version):
    mats = {m["id"]: m for m in c.get("/api/materials").json()}
    return next(a["id"] for a in mats[material_id]["assay_versions"]
                if a["version"] == version)


ROBUST_T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
            "KH": {"min": 0.88, "max": 0.94}}


def _precise_candidates():
    return [
        {"material_id": 1, "assay_version_id": _version_id(1, "V2026-10")},
        {"material_id": 2, "assay_version_id": _version_id(2, "V2026-10")},
        {"material_id": 3, "assay_version_id": _version_id(3, "V2026-10")},
        {"material_id": 4, "assay_version_id": _version_id(4, "V2026-10W")},
        {"material_id": 5, "assay_version_id": _version_id(5, "V2026-10")},
    ]


def _quick_candidates():
    return [
        {"material_id": 1, "assay_version_id": _version_id(1, "V2026-10Q")},
        {"material_id": 2, "assay_version_id": _version_id(2, "V2026-10Q")},
        {"material_id": 3, "assay_version_id": _version_id(3, "V2026-10Q")},
        {"material_id": 4, "assay_version_id": _version_id(4, "V2026-10WQ")},
        {"material_id": 5, "assay_version_id": _version_id(5, "V2026-10Q")},
    ]


def test_zero_tolerance_robust_matches_nominal():
    # 旧版 V2026-09 无 uncertainties（零容差）：稳健与名义结果一致
    cands = [
        {"material_id": 1, "assay_version_id": _version_id(1, "V2026-09")},
        {"material_id": 2, "assay_version_id": _version_id(2, "V2026-09")},
        {"material_id": 3, "assay_version_id": _version_id(3, "V2026-09")},
        {"material_id": 4, "assay_version_id": _version_id(4, "V2026-09W")},
        {"material_id": 5, "assay_version_id": _version_id(5, "V2026-09")},
    ]
    body = {
        "scenario_name": "零容差一致性",
        "candidates": cands,
        "targets": ROBUST_T,
        "hazard_limits_pct": {"Cl": 0.05, "alkali_eq": 1.5},
        "modes": ["min_cost"], "robust": True, "save": False,
    }
    j = c.post("/api/blend", json=body).json()
    n, r = j["solutions"][0], j["robust_solutions"][0]
    assert n["success"] and r["success"]
    assert n["cost_per_t_dry"] == r["cost_per_t_dry"]
    assert n["indicators"]["KH"] == r["indicators"]["KH"]


def test_robust_feasible_precise_versions():
    body = {
        "scenario_name": "精密容差稳健",
        "candidates": _precise_candidates(), "targets": ROBUST_T,
        "hazard_limits_pct": {"Cl": 0.05, "alkali_eq": 1.5},
        "modes": ["min_cost"], "robust": True, "save": False,
    }
    j = c.post("/api/blend", json=body).json()
    r = j["robust_solutions"][0]
    assert r["success"]
    assert r["worst_case"]["all_ok"]
    assert all(e["ok"] for e in r["worst_case"]["indicators"])


def test_nominal_pass_robust_kh_break_with_reports():
    t = {"SM": {"min": 2.3, "max": 2.9}, "IM": {"min": 1.3, "max": 1.9},
         "KH": {"min": 0.90, "max": 0.95}}
    body = {
        "scenario_name": "快检边界失效",
        "candidates": _quick_candidates(), "targets": t,
        "hazard_limits_pct": {"Cl": 0.05, "alkali_eq": 0.6},
        "modes": ["min_cost"], "robust": True, "save": False,
    }
    j = c.post("/api/blend", json=body).json()
    n, r = j["solutions"][0], j["robust_solutions"][0]
    assert n["success"]
    assert 0.90 <= n["indicators"]["KH"] <= 0.95
    assert not r["success"]
    assert r["diagnostic"]["reason"] == "ROBUST_INFEASIBLE_BOUNDARY"
    kh = [x for x in r["diagnostic"]["conflicts"]
          if x["kind"] in ("KH_max", "KH_min")]
    assert kh and kh[0]["breakthrough"] > 0
    assert kh[0]["trigger_reports"][0]["lab_report_no"].startswith("LAB-2610")


def test_wet_basis_uncertainty_bounds_endpoint():
    aid = _version_id(4, "V2026-10W")
    b = c.get(f"/api/assays/{aid}/bounds").json()
    sio2 = b["bounds_dry"]["SiO2"]
    assert b["basis"] == "wet"
    assert sio2["nominal"] == 48.0
    assert sio2["lower"] < sio2["nominal"] < sio2["upper"]
    assert sio2["lower"] == 47.8049
    assert sio2["upper"] == 48.1951


def test_denominator_span_zero_http_422():
    # LZ02（id=9）Fe2O3 0.30±0.35 → IM 分母区间跨零
    body = {
        "scenario_name": "跨零", "candidates": [{"material_id": 9},
                                                {"material_id": 1}],
        "targets": ROBUST_T, "hazard_limits_pct": {},
        "modes": ["min_cost"], "robust": True, "save": False,
    }
    r = c.post("/api/blend", json=body)
    assert r.status_code == 422
    assert r.json()["error_code"] == "DENOMINATOR_SPAN_ZERO"
    problems = r.json()["details"]["problems"]
    assert any(p["indicator"] == "IM" for p in problems)


def test_bad_uncertainty_on_unmeasured_rejected():
    r = c.post("/api/assays", json={
        "material_id": 7, "version": "V-BAD-1", "lab_report_no": "X1",
        "basis": "dry",
        "composition": {"CaO": 45.0, "SiO2": 6.0, "Al2O3": 2.0,
                        "MgO": 1.0, "LOI": 46.0},
        "measured_oxides": ["CaO", "SiO2", "Al2O3", "MgO", "LOI"],
        "uncertainties": {"Fe2O3": 0.1},
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "BAD_UNCERTAINTY"


def test_robust_run_snapshot_saved_and_old_run_untouched():
    body = {
        "scenario_name": "稳健快照入库", "candidates": _precise_candidates(),
        "targets": ROBUST_T,
        "hazard_limits_pct": {"Cl": 0.05, "alkali_eq": 1.5},
        "modes": ["min_cost"], "robust": True, "save": True,
    }
    rid = c.post("/api/blend", json=body).json()["run_id"]
    detail = c.get(f"/api/runs/{rid}").json()
    assert len(detail["robust_solutions"]) == 1
    assert detail["robust_solutions"][0]["robust"] is True
    assert detail["solutions"][0]["robust"] is False
    snap = detail["uncertainty_snapshot"]["materials"]
    ash = next(m for m in snap if m["material_code"] == "AS01")
    assert ash["basis"] == "wet" and ash["dry_factor"] > 1
    assert ash["bounds_dry"]["SiO2"]["lower"] < 48.0
    # 明细也带容差快照
    it = detail["robust_solutions"][0]["items"][0]
    assert it["uncertainty_snapshot"]["components"]
    # 旧格式运行（显式选零容差化验版）仍可回看，raw_assay 快照不变
    old = c.post("/api/blend", json={
        "scenario_name": "旧零容差运行", "candidates": [
            {"material_id": 1, "assay_version_id": _version_id(1, "V2026-09")},
            {"material_id": 2, "assay_version_id": _version_id(2, "V2026-09")},
            {"material_id": 3, "assay_version_id": _version_id(3, "V2026-09")},
            {"material_id": 4, "assay_version_id": _version_id(4, "V2026-09W")},
            {"material_id": 5, "assay_version_id": _version_id(5, "V2026-09")},
        ], "targets": ROBUST_T,
        "hazard_limits_pct": {"Cl": 0.05, "alkali_eq": 1.5},
        "modes": ["min_cost"], "save": True}).json()["run_id"]
    old_detail = c.get(f"/api/runs/{old}").json()
    assert old_detail["robust_solutions"] == []
    raw = old_detail["solutions"][0]["items"][0]["raw_assay"]
    assert raw["basis"] in ("dry", "wet") and raw["composition"]
