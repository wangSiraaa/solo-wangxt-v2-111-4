"""稳健配比 API 端到端（SQLite + 虚构演示数据）。

验收点：
① 零容差（nominal / 旧版化验）结果与原求解一致；
② 名义合格、检测边界致 KH 超限时稳健不可行，列出报告/组分/突破量；
③ 湿基容差换算到干基后参与计算，无倒置区间、湿料成本口径不变；
④ 缺测/分母跨零明确 422；历史回看保留原始化验快照，不被新容差改写。
"""
T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
     "KH": {"min": 0.88, "max": 0.94}}


def _blend(client, ids, modes=("min_cost",), hazards=None, cheap=None,
           save=False, batch=1000, robust="nominal", overrides=None):
    body = {
        "scenario_name": "api-robust", "batch_t_dry": batch,
        "candidates": [
            {"material_id": i,
             **({"uncertainty_override": overrides[i]}
                if overrides and i in overrides else {})}
            for i in ids
        ],
        "targets": T, "hazard_limits_pct": hazards or {},
        "modes": list(modes), "save": save, "robust_mode": robust,
    }
    if cheap:
        body["cheap_material_id"] = cheap
    return client.post("/api/blend", json=body)


# ① 零容差：稳健与名义一致（显式空容差覆盖，即使档案登记了演示不确定度）
def test_zero_tolerance_robust_equals_nominal(client):
    zero = {i: {} for i in [1, 2, 3, 4, 5]}
    r_nom = _blend(client, [1, 2, 3, 4, 5], overrides=zero)
    r_rob = _blend(client, [1, 2, 3, 4, 5], robust="robust", overrides=zero)
    assert r_nom.status_code == r_rob.status_code == 200
    nom = r_nom.json()["solutions"][0]
    rob = r_rob.json()["solutions"][0]["robust"]
    assert nom["success"] and rob["success"]
    assert nom["cost_per_t_dry"] == rob["cost_per_t_dry"]
    a = [(i["material_code"], i["share_pct_dry"]) for i in nom["items"]]
    b = [(i["material_code"], i["share_pct_dry"]) for i in rob["items"]]
    assert a == b
    # 零容差下最坏率值 == 名义率值
    m = rob["robust_report"]["margins"]
    for k in ("SM", "IM", "KH"):
        assert m[k]["worst_low"] == m[k]["nominal"] == m[k]["worst_high"]
    # 旧化验版 V2026-08（uncertainty 为 null）零容差
    mats = client.get("/api/materials").json()
    v08 = next(a for m in mats if m["id"] == 1 for a in m["assay_versions"]
               if a["version"] == "V2026-08")
    assert v08["uncertainty"] is None


def test_default_robust_mode_is_nominal(client):
    r = client.post("/api/blend", json={
        "scenario_name": "default", "batch_t_dry": 1000,
        "candidates": [{"material_id": i} for i in [1, 2, 3, 4, 5]],
        "targets": T, "hazard_limits_pct": {}, "modes": ["min_cost"],
        "save": False,
    })
    assert r.status_code == 200
    assert r.json()["robust_status"] is None
    assert r.json()["solutions"][0]["robust"] is None


# ② 名义合格、KH 边界超限
def test_nominal_ok_robust_kh_breach_lists_report_and_components(client):
    overrides = {
        1: {"CaO": {"lower": -0.6, "upper": 0.6}},
        2: {"CaO": {"lower": -0.3, "upper": 0.3}},
        3: {"CaO": {"lower": -0.2, "upper": 0.2}},
        4: {"CaO": {"lower": -0.2, "upper": 0.2}},
        5: {"CaO": {"lower": -0.25, "upper": 0.25}},
    }
    body_targets = {"SM": T["SM"], "IM": T["IM"], "KH": {"min": 0.895, "max": 0.92}}
    body = {
        "scenario_name": "kh-edge", "batch_t_dry": 1000,
        "candidates": [{"material_id": i, "uncertainty_override": overrides[i]}
                       for i in [1, 2, 3, 4, 5]],
        "targets": body_targets, "hazard_limits_pct": {},
        "modes": ["min_cost"], "save": False, "robust_mode": "robust",
    }
    r = client.post("/api/blend", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["robust_status"] == "robust_infeasible"
    sol = data["solutions"][0]
    assert sol["success"] and not sol["robust"]["success"]
    conf = next(c for c in sol["robust"]["diagnostic"]["conflicts"]
                if c["kind"] == "KH_max")
    assert conf["achieved"] > conf["limit"]
    assert conf["breach"] > 0
    # 报告号 + 组分 + 边界方向
    ls = next(m for m in conf["boundary_combo"] if m["material_code"] == "LS01")
    assert ls["lab_report_no"].startswith("LAB-")
    assert ls["components"]["CaO"]["side"] == "upper"


# ② 非法容差声明 → 422 明确错误码
def test_inverted_interval_http_422(client):
    r = _blend(client, [1, 2, 3, 4, 5], robust="robust",
               overrides={1: {"SiO2": {"lower": 0.5, "upper": 0.2}}})
    assert r.status_code == 422
    assert r.json()["error_code"] == "UNCERTAINTY_BAD_INTERVAL"


def test_tolerance_on_unmeasured_http_422(client):
    # SP01(id=7) 缺测 Fe2O3，却声明其容差
    r = _blend(client, [1, 7], robust="robust",
               overrides={7: {"Fe2O3": {"lower": -0.1, "upper": 0.1}}})
    assert r.status_code == 422
    assert r.json()["error_code"] == "UNCERTAINTY_ON_UNMEASURED"


# ③ 湿基化验单容差先换干基；湿料成本不变
def test_wet_uncertainty_converted_to_dry(client):
    # max_cheap（廉价料=粉煤灰 id=4）保证 AS01 有掺量；
    # 湿基 SiO2 ±0.082 百分点 → 干基 ±0.10（=0.082/0.82）
    overrides = {4: {"SiO2": {"lower": -0.082, "upper": 0.082},
                     "Al2O3": {"lower": -0.041, "upper": 0.041},
                     "Fe2O3": {"lower": -0.016, "upper": 0.016},
                     "CaO": {"lower": -0.025, "upper": 0.025}}}
    r_nom = _blend(client, [1, 2, 3, 4, 5], modes=("max_cheap",), cheap=4)
    r = _blend(client, [1, 2, 3, 4, 5], modes=("max_cheap",), cheap=4,
               robust="robust", overrides=overrides, save=True)
    assert r.status_code == 200
    assert r.json()["robust_status"] == "robust_feasible"
    rid = r.json()["run_id"]
    detail = client.get(f"/api/runs/{rid}").json()
    rob_items = detail["solutions"][0]["robust"]["items"]
    as_item = next(i for i in rob_items if i["material_code"] == "AS01")
    sio2 = next(s for s in as_item["uncertainty_trace"]["steps"]
                if s["component"] == "SiO2")
    # 湿基 39.36±0.082 → 干基 48.0 ±0.10，区间不倒置
    assert sio2["basis_in"] == "wet"
    assert abs(sio2["nominal_dry"] - 48.0) < 1e-6
    assert abs(sio2["lower_dry"] - (39.36 - 0.082) / 0.82) < 1e-6
    assert abs(sio2["upper_dry"] - (39.36 + 0.082) / 0.82) < 1e-6
    assert sio2["lower_dry"] <= sio2["nominal_dry"] <= sio2["upper_dry"]
    # 湿料成本/质量换算只依赖档案含水率：湿 = 干 / 0.82，不随容差改变系数
    assert abs(as_item["mass_t_wet"] - as_item["mass_t_dry"] / 0.82) < 1e-4
    nom_as = next(i for i in r_nom.json()["solutions"][0]["items"]
                  if i["material_code"] == "AS01")
    # 湿基单价 18 元/t：成本 = 湿料吨数 × 18
    assert abs(as_item["cost"] - as_item["mass_t_wet"] * 18.0) < 0.05
    # 最坏率值区间包住名义值
    m = detail["solutions"][0]["robust"]["robust_report"]["margins"]["SM"]
    assert m["worst_low"] <= m["nominal"] <= m["worst_high"]
    assert nom_as["material_code"] == "AS01"


# ④ 分母跨零 → 422（QZ01 id=6，Fe2O3 下界 0）
def test_uncertain_zero_denominator_http_422(client):
    # 配少量铁粉让名义 IM 有定义；稳健下石英 Fe2O3 可取 0
    body = {
        "scenario_name": "denom-zero", "batch_t_dry": 10.0,
        "candidates": [{"material_id": 6}, {"material_id": 5}],
        "targets": {"SM": {"min": 0}, "IM": {"min": 0}, "KH": {}},
        "hazard_limits_pct": {}, "modes": ["min_cost"],
        "save": False, "robust_mode": "robust",
    }
    r = client.post("/api/blend", json=body)
    assert r.status_code == 422
    assert r.json()["error_code"] == "UNCERTAIN_ZERO_DENOMINATOR"
    inds = {d["indicator"] for d in r.json()["details"]["denominators"]}
    assert "IM" in inds


# ④ 缺测仍报 MISSING_ASSAY（稳健模式不改变既有硬性规则）
def test_missing_assay_still_422(client):
    r = _blend(client, [1, 2, 7], robust="robust")
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_ASSAY"


# ④ 历史快照：旧运行保存的化验单不被后续容差改写
def test_history_keeps_original_assay_snapshot(client):
    # 先保存一个“名义”运行
    r1 = _blend(client, [1, 2, 3, 4, 5], save=True)
    rid1 = r1.json()["run_id"]
    before = client.get(f"/api/runs/{rid1}").json()
    raw_before = before["solutions"][0]["items"][0]["raw_assay"]
    assert "uncertainty" in raw_before  # 字段存在（可能为 null 或对象）

    # 再发一个对 LS01 的临时容差覆盖（不写库）
    r2 = _blend(client, [1, 2, 3, 4, 5], robust="robust", save=True,
                overrides={1: {"SiO2": {"lower": -0.05, "upper": 0.05}}})
    assert r2.status_code == 200

    # 旧运行回看：原始化验快照仍为档案值
    after = client.get(f"/api/runs/{rid1}").json()
    raw_after = after["solutions"][0]["items"][0]["raw_assay"]
    assert raw_after["composition"] == raw_before["composition"]
    assert raw_after["uncertainty"] == raw_before["uncertainty"]

    # 新运行的 robust 段带有边界留痕
    rid2 = r2.json()["run_id"]
    new = client.get(f"/api/runs/{rid2}").json()
    assert new["constraint_set"]["robust_mode"] == "robust"
    assert new["solutions"][0]["robust"]["success"] is True
    assert new["solutions"][0]["robust"]["robust_report"]["margins"]["SM"]
