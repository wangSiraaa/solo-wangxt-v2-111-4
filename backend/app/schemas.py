"""Pydantic 入参/出参模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AssayVersionOut(BaseModel):
    id: int
    version: str
    lab_report_no: str
    assayed_at: datetime
    basis: str
    composition: dict
    measured_oxides: list
    # 检测不确定度（与本化验单同基准）：{"SiO2": 0.3} 或
    # {"SiO2": {"lower": -0.2, "upper": 0.4}}；缺省/零 = 零容差精确常量
    uncertainties: dict | None = None


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    moisture_pct: float
    cost_per_t_wet: float
    availability_t_wet: float | None
    min_share_pct: float
    is_active: bool
    note: str | None = None
    assay_versions: list[AssayVersionOut] = []


class Interval(BaseModel):
    min: float | None = None
    max: float | None = None


class Targets(BaseModel):
    SM: Interval
    IM: Interval
    KH: Interval


class Candidate(BaseModel):
    material_id: int
    assay_version_id: int | None = None  # 默认取最新版


class BlendRequest(BaseModel):
    scenario_name: str = "未命名试算"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate]
    targets: Targets
    hazard_limits_pct: dict[str, float] = {}  # 干基 %，如 {"Cl": 0.03, "alkali_eq": 1.5}
    modes: list[Literal["min_cost", "max_cheap", "balanced"]] = ["min_cost"]
    cheap_material_id: int | None = None  # max_cheap 模式的“廉价原料”
    robust: bool = False  # 同时求解检测不确定度最坏边界下的稳健方案
    save: bool = True


class EvaluateRequest(BaseModel):
    """手工配比试算：直接给干基份额，用于分母为零/缺测报错演示。"""

    scenario_name: str = "手工配比"
    picks: list[Candidate]
    shares_pct_dry: list[float]  # 与 picks 等长；和不强制 100，会归一化
    robust: bool = False  # 同时按检测不确定最坏边界评估并拒绝跨零分母


class SolutionItem(BaseModel):
    material_code: str
    material_name: str
    assay_version: str
    lab_report_no: str
    share_pct_dry: float
    mass_t_dry: float
    mass_t_wet: float
    water_t: float
    cost: float
    conversion_trace: dict
    uncertainty_snapshot: dict | None = None


class SolutionOut(BaseModel):
    mode: str
    mode_label: str
    success: bool
    robust: bool = False
    total_cost: float | None = None
    cost_per_t_dry: float | None = None
    indicators: dict | None = None
    composition_dry_pct: dict | None = None
    composition_wet_pct: dict | None = None
    water_pct_in_wet_mix: float | None = None
    items: list[SolutionItem] = []
    diagnostic: dict | None = None
    worst_case: dict | None = None


class AssayCreate(BaseModel):
    """登记新化验版（可声明同基准检测不确定度）。"""
    material_id: int
    version: str = Field(min_length=1, max_length=32)
    lab_report_no: str = Field(min_length=1, max_length=64)
    assayed_at: datetime | None = None
    basis: Literal["dry", "wet"] = "dry"
    composition: dict[str, float]
    measured_oxides: list[str]
    # {"SiO2": 0.3} 对称偏差；或 {"SiO2": {"lower": -0.2, "upper": 0.4}}
    uncertainties: dict | None = None


class BlendResponse(BaseModel):
    run_id: int | None
    run_code: str
    status: str
    solutions: list[SolutionOut]
    robust_solutions: list[SolutionOut] = []
