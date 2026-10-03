"""FastAPI 入口：原料/化验查询、配比试算、手工评估、历史追溯。

注意：本服务为离线工艺研发试算工具，采用虚构工艺边界与演示数据，
不向任何真实生产设备下发指令。
"""
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import chemistry, crud, models, optimizer
from .database import Base, engine, ensure_new_columns, get_db
from .schemas import (
    AssayCreate,
    AssayVersionOut,
    BlendRequest,
    BlendResponse,
    EvaluateRequest,
    MaterialOut,
    SolutionItem,
    SolutionOut,
)

Base.metadata.create_all(bind=engine)
ensure_new_columns()

app = FastAPI(
    title="离线原料配比试算（虚构工艺边界 · 研发用）",
    version="1.0.0",
    description="质量守恒合成 + 率值计算 + SciPy LP 优化；不连接任何生产控制系统。",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(chemistry.BlendError)
def blend_error_handler(request, exc: chemistry.BlendError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=422,
        content={
            "error_code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "rawmix-offline", "mode": "fictional-boundary"}


@app.get("/api/materials", response_model=list[MaterialOut])
def materials(active_only: bool = False, db: Session = Depends(get_db)):
    return crud.list_materials(db, active_only=active_only)


@app.post("/api/assays", response_model=AssayVersionOut, status_code=201)
def create_assay(payload: AssayCreate, db: Session = Depends(get_db)):
    """登记新化验版并声明同基准检测不确定度（零容差可不填）。只增不改。"""
    return crud.create_assay_version(db, payload)


@app.get("/api/assays/{assay_id}/bounds")
def assay_bounds(assay_id: int, db: Session = Depends(get_db)):
    """干基不确定边界预览：名义值 / 下界 / 上界 / 零容差，湿基先做湿→干换算。"""
    ass = db.get(models.AssayVersion, assay_id)
    if ass is None:
        raise HTTPException(404, "化验版不存在。")
    mat = db.get(models.Material, ass.material_id)
    comp_dry = chemistry.convert_composition(ass.composition, ass.basis, mat.moisture_pct)
    unc_native = chemistry.normalize_uncertainties(
        ass.uncertainties or {}, ass.measured_oxides,
        material_code=mat.code, material_name=mat.name,
        assay_version=ass.version, lab_report_no=ass.lab_report_no,
    )
    bdry = chemistry.bounds_dry(comp_dry, unc_native, ass.basis, mat.moisture_pct)
    return {
        "material_code": mat.code, "material_name": mat.name,
        "assay_version": ass.version, "lab_report_no": ass.lab_report_no,
        "basis": ass.basis, "moisture_pct": mat.moisture_pct,
        "bounds_dry": {c: {
            "nominal": round(v["nominal"], 4),
            "lower": round(v["lower"], 4),
            "upper": round(v["upper"], 4),
            "zero_tolerance": v["zero_tolerance"],
        } for c, v in bdry.items()},
    }


def _serialize_solution(sol: dict) -> SolutionOut:
    return SolutionOut(
        mode=sol["mode"],
        mode_label=sol["mode_label"],
        success=sol["success"],
        robust=bool(sol.get("robust")),
        total_cost=sol.get("total_cost"),
        cost_per_t_dry=sol.get("cost_per_t_dry"),
        indicators=sol.get("indicators"),
        composition_dry_pct=sol.get("composition_dry_pct"),
        composition_wet_pct=sol.get("composition_wet_pct"),
        water_pct_in_wet_mix=sol.get("water_pct_in_wet_mix"),
        diagnostic=sol.get("diagnostic"),
        worst_case=sol.get("worst_case"),
        items=[SolutionItem(**{k: v for k, v in it.items()
                               if not k.startswith("_")}) for it in sol.get("items", [])],
    )


@app.post("/api/blend", response_model=BlendResponse)
def blend(req: BlendRequest, db: Session = Depends(get_db)):
    pairs = crud.resolve_candidates(db, req.candidates)
    rows = optimizer.prepare_rows(pairs)
    if not rows:
        raise HTTPException(400, "候选原料为空。")
    solutions = optimizer.solve(rows, req)
    robust_solutions = optimizer.solve_robust(rows, req) if req.robust else []
    run_id, run_code = None, ""
    if req.save:
        run = crud.save_run(db, req, solutions,
                            robust_solutions=robust_solutions, rows=rows)
        run_id, run_code = run.id, run.run_code
    all_sols = solutions + robust_solutions
    return BlendResponse(
        run_id=run_id,
        run_code=run_code,
        status="feasible" if any(s["success"] for s in all_sols) else "infeasible",
        solutions=[_serialize_solution(s) for s in solutions],
        robust_solutions=[_serialize_solution(s) for s in robust_solutions],
    )


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest, db: Session = Depends(get_db)):
    """手工给定干基份额做质量守恒合成与率值计算。

    用于显式演示：缺测报错、分母为零报错（不以零含量兜底）。
    """
    if len(req.picks) != len(req.shares_pct_dry):
        raise HTTPException(400, "picks 与 shares_pct_dry 长度必须一致。")
    pairs = crud.resolve_candidates(db, req.picks)
    rows = optimizer.prepare_rows(pairs)

    total = sum(req.shares_pct_dry)
    if total <= 0:
        raise HTTPException(400, "配比份额之和必须为正。")
    x = np.array([v / total for v in req.shares_pct_dry])

    material_rows = [{
        "code": r.code, "name": r.name, "version": r.version,
        "lab_report_no": r.lab_report_no,
        "composition": r.composition_dry, "measured_oxides": list(r.measured),
    } for r in rows]
    chemistry.require_measured(material_rows, ["CaO", "SiO2", "Al2O3", "Fe2O3"])

    # 稳健评估：按给定份额加权后，率值分母的不确定区间触及零也必须明确拒绝
    worst_case = None
    if getattr(req, "robust", False):
        problems = []
        blends = chemistry.worst_case_vector(rows, x)
        for key, den_name in (("SM", "Al2O3+Fe2O3"), ("IM", "Fe2O3"),
                              ("KH", "2.8*SiO2")):
            if blends[key].get("undefined"):
                problems.append({
                    "indicator": key, "denominator": den_name,
                    "message": f"该手工份额下 {key} 的分母不确定区间触及零，"
                               f"边界上 {key} 无定义。",
                })
        if problems:
            raise chemistry.DenominatorSpanZeroError(problems)
        worst_case = {
            "indicators": blends,
            "note": "按给定手工份额、各组分独立取最不利检测误差端点。",
        }

    components = [c for c in optimizer.COMPONENT_ORDER if any(
        c in r.composition_dry for r in rows
    )]
    pick_dicts = [{
        "code": r.code, "name": r.name, "moisture_pct": r.moisture_pct,
        "composition_dry": {k: r.composition_dry.get(k, 0.0) for k in components},
    } for r in rows]
    synth = chemistry.synthesize(pick_dicts, list(x), components)

    # 率值：分母为零必须由 ZeroDenominatorError 显式抛出
    indicators = chemistry.calc_indicators(synth["dry_pct"]).as_dict()

    items = []
    for r, xi in zip(rows, x):
        if xi < 1e-10:
            continue
        trace = chemistry.build_conversion_trace(
            r.code, r.name, r.composition_raw, r.basis, r.moisture_pct
        )
        items.append({
            "material_code": r.code,
            "material_name": r.name,
            "assay_version": r.version,
            "lab_report_no": r.lab_report_no,
            "share_pct_dry": round(xi * 100.0, 4),
            "conversion_trace": trace,
        })
    return {
        "scenario_name": req.scenario_name,
        "indicators": indicators,
        "composition_dry_pct": synth["dry_pct"],
        "composition_wet_pct": synth["wet_pct"],
        "water_pct_in_wet_mix": synth["water_pct_in_wet_mix"],
        "contributions": synth["contributions"],
        "worst_case": worst_case,
        "items": items,
    }


@app.get("/api/runs")
def runs(limit: int = 50, db: Session = Depends(get_db)):
    return crud.list_runs(db, limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db)):
    detail = crud.get_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(404, "试算记录不存在。")
    return detail


# ---- 生产构建后的静态前端（ng build 产物） ----
_dist = Path(__file__).resolve().parent.parent / "static" / "browser"
if _dist.exists():
    _assets = _dist / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        index = _dist / "index.html"
        if full_path and (candidate := _dist / full_path).is_file():
            return FileResponse(candidate)
        return FileResponse(index)