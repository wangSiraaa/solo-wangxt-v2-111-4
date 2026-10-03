"""数据库读写辅助。"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import chemistry, models
from .optimizer import Row, prepare_rows


def list_materials(db: Session, active_only: bool = False):
    stmt = select(models.Material).order_by(models.Material.id)
    if active_only:
        stmt = stmt.where(models.Material.is_active.is_(True))
    mats = list(db.scalars(stmt))
    for m in mats:
        m.assay_versions.sort(key=lambda a: (a.assayed_at, a.id), reverse=True)
    return mats


def resolve_candidates(db: Session, candidates) -> list[tuple]:
    """把 [{material_id, assay_version_id?}] 解析成 (Material, AssayVersion)。"""
    pairs = []
    for c in candidates:
        mat = db.get(models.Material, c.material_id)
        if mat is None:
            from .chemistry import BlendError
            raise BlendError("MATERIAL_NOT_FOUND",
                             f"原料 id={c.material_id} 不存在。",
                             {"material_id": c.material_id})
        if c.assay_version_id is not None:
            ass = db.get(models.AssayVersion, c.assay_version_id)
            if ass is None or ass.material_id != mat.id:
                from .chemistry import BlendError
                raise BlendError("ASSAY_NOT_FOUND",
                                 f"化验版 id={c.assay_version_id} 不属于原料 {mat.code}。")
        else:
            ass = max(mat.assay_versions, key=lambda a: (a.assayed_at, a.id))
        pairs.append((mat, ass))
    return pairs


def rows_from_candidates(db: Session, candidates) -> list[Row]:
    return prepare_rows(resolve_candidates(db, candidates))


def create_assay_version(db: Session, payload) -> models.AssayVersion:
    """登记新化验版（含同基准检测不确定度校验）。只新增，不改既有化验版。"""
    mat = db.get(models.Material, payload.material_id)
    if mat is None:
        from .chemistry import BlendError
        raise BlendError("MATERIAL_NOT_FOUND",
                         f"原料 id={payload.material_id} 不存在。",
                         {"material_id": payload.material_id})
    exists = db.scalars(
        select(models.AssayVersion).where(
            models.AssayVersion.material_id == mat.id,
            models.AssayVersion.version == payload.version,
        )
    ).first()
    if exists is not None:
        from .chemistry import BlendError
        raise BlendError("ASSAY_VERSION_EXISTS",
                         f"原料 {mat.code} 已存在化验版 {payload.version}，"
                         "检测不确定度修订请登记新版本，不得覆盖既有化验单。",
                         {"material_code": mat.code, "version": payload.version})

    # 不确定度合法性校验（未测组分不得声明、符号/越界检查）
    normalized = chemistry.normalize_uncertainties(
        payload.uncertainties or {}, payload.measured_oxides,
        material_code=mat.code, material_name=mat.name,
        assay_version=payload.version, lab_report_no=payload.lab_report_no,
    )
    # 湿基偏差换算到干基不得使组分下界为负
    comp_dry = chemistry.convert_composition(
        payload.composition, payload.basis, mat.moisture_pct
    )
    chemistry.bounds_dry(comp_dry, normalized, payload.basis, mat.moisture_pct)

    ass = models.AssayVersion(
        material_id=mat.id,
        version=payload.version,
        lab_report_no=payload.lab_report_no,
        assayed_at=payload.assayed_at or datetime.utcnow(),
        basis=payload.basis,
        composition=dict(payload.composition),
        measured_oxides=list(payload.measured_oxides),
        uncertainties=payload.uncertainties or None,
    )
    db.add(ass)
    db.commit()
    db.refresh(ass)
    return ass


def _run_uncertainty_snapshot(rows) -> dict:
    """本次试算的容差快照：名义值/干基上下界/零容差组分 + 湿→干换算参数。

    该快照随运行保存，此后即使化验单容差被修订，旧运行仍按此快照展示。
    """
    per_material = []
    for r in rows:
        per_material.append({
            "material_id": r.material_id,
            "material_code": r.code,
            "material_name": r.name,
            "assay_version_id": r.assay_version_id,
            "assay_version": r.version,
            "lab_report_no": r.lab_report_no,
            "basis": r.basis,
            "moisture_pct": r.moisture_pct,
            "dry_factor": round(
                1.0 if r.basis == "dry"
                else chemistry.dry_factor(r.moisture_pct), 6),
            "has_uncertainty": bool(r.uncertainties_native),
            "bounds_dry": {
                c: {
                    "nominal": round(b["nominal"], 4),
                    "lower": round(b["lower"], 4),
                    "upper": round(b["upper"], 4),
                    "zero_tolerance": b["zero_tolerance"],
                }
                for c, b in (r.bounds_dry or {}).items()
            },
        })
    return {
        "rule": "稳健模式在所有使 SM/IM/KH/有害组分最不利的组分边界组合上仍须满足约束；"
                "湿基化验单上下界先按 /（1−含水率）换到干基。",
        "materials": per_material,
    }


def save_run(db: Session, req, solutions: list[dict],
             robust_solutions: list[dict] | None = None, rows=None,
             scenario_name: str | None = None):
    robust_solutions = robust_solutions or []
    all_solutions = list(solutions) + list(robust_solutions)
    run = models.BlendRun(
        run_code=f"RUN-{uuid.uuid4().hex[:10].upper()}",
        scenario_name=scenario_name or getattr(req, "scenario_name", "试算"),
        batch_t_dry=getattr(req, "batch_t_dry", 1000.0),
        target=req.targets.model_dump(),
        constraint_set={
            "hazard_limits_pct": getattr(req, "hazard_limits_pct", {}),
            "cheap_material_id": getattr(req, "cheap_material_id", None),
            "modes": getattr(req, "modes", []),
            "robust": bool(getattr(req, "robust", False)),
        },
        uncertainty_snapshot=_run_uncertainty_snapshot(rows) if rows else None,
        status="feasible" if any(s["success"] for s in all_solutions) else "infeasible",
    )
    db.add(run)
    db.flush()

    for sol in all_solutions:
        srec = models.BlendSolution(
            run_id=run.id,
            mode=sol["mode"],
            success=sol["success"],
            robust=bool(sol.get("robust")),
            total_cost=sol.get("total_cost"),
            indicators={
                "indicators": sol.get("indicators"),
                "composition_dry_pct": sol.get("composition_dry_pct"),
                "composition_wet_pct": sol.get("composition_wet_pct"),
                "water_pct_in_wet_mix": sol.get("water_pct_in_wet_mix"),
                "cost_per_t_dry": sol.get("cost_per_t_dry"),
            },
            diagnostic=sol.get("diagnostic"),
            worst_case=sol.get("worst_case"),
        )
        db.add(srec)
        db.flush()
        for it in sol.get("items", []):
            db.add(models.BlendItem(
                run_id=run.id,
                solution_id=srec.id,
                material_id=it["_material_id"],
                assay_version_id=it["_assay_version_id"],
                share_pct_dry=it["share_pct_dry"],
                mass_t_dry=it["mass_t_dry"],
                mass_t_wet=it["mass_t_wet"],
                water_t=it["water_t"],
                cost=it["cost"],
                conversion_trace=it["conversion_trace"],
                assay_composition_snapshot=it["conversion_trace"]["steps"],
                uncertainty_snapshot=it.get("uncertainty_snapshot"),
            ))
    db.commit()
    db.refresh(run)
    return run


def get_run_detail(db: Session, run_id: int):
    run = db.get(models.BlendRun, run_id)
    if run is None:
        return None
    out = {
        "id": run.id,
        "run_code": run.run_code,
        "scenario_name": run.scenario_name,
        "batch_t_dry": run.batch_t_dry,
        "target": run.target,
        "constraint_set": run.constraint_set,
        "uncertainty_snapshot": run.uncertainty_snapshot,
        "status": run.status,
        "created_at": run.created_at.isoformat(timespec="seconds"),
        "solutions": [],
        "robust_solutions": [],
    }
    for s in run.solutions:
        items = []
        for it in s.items:
            mat = db.get(models.Material, it.material_id)
            ass = db.get(models.AssayVersion, it.assay_version_id)
            items.append({
                "material_code": mat.code,
                "material_name": mat.name,
                "assay_version": ass.version,
                "lab_report_no": ass.lab_report_no,
                "share_pct_dry": it.share_pct_dry,
                "mass_t_dry": it.mass_t_dry,
                "mass_t_wet": it.mass_t_wet,
                "water_t": it.water_t,
                "cost": it.cost,
                "conversion_trace": it.conversion_trace,
                "uncertainty_snapshot": it.uncertainty_snapshot,
                "raw_assay": {
                    "basis": ass.basis,
                    "composition": ass.composition,
                    "measured_oxides": ass.measured_oxides,
                    "uncertainties": ass.uncertainties,
                },
            })
        sol_out = {
            "mode": s.mode,
            "success": s.success,
            "robust": bool(s.robust),
            "total_cost": s.total_cost,
            "payload": s.indicators,
            "diagnostic": s.diagnostic,
            "worst_case": s.worst_case,
            "items": items,
        }
        (out["robust_solutions"] if s.robust else out["solutions"]).append(sol_out)
    return out


def list_runs(db: Session, limit: int = 50):
    runs = list(db.scalars(
        select(models.BlendRun).order_by(models.BlendRun.id.desc()).limit(limit)
    ))
    return [{
        "id": r.id,
        "run_code": r.run_code,
        "scenario_name": r.scenario_name,
        "status": r.status,
        "created_at": r.created_at.isoformat(timespec="seconds"),
        "modes": [s.mode for s in r.solutions],
        "robust": any(s.robust for s in r.solutions),
    } for r in runs]
