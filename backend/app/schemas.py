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
    # 检测不确定度（与本化验单同基准的逐组分绝对偏置，百分点）；None=全部零容差
    uncertainty: dict | None = None


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
    # 请求级临时容差覆盖：{组分: {lower, upper}}，口径与所选化验单一致；
    # 不写库，仅本次试算有效（历史保存的是当次生效的快照）。
    uncertainty_override: dict | None = None


class BlendRequest(BaseModel):
    scenario_name: str = "未命名试算"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate]
    targets: Targets
    hazard_limits_pct: dict[str, float] = {}  # 干基 %，如 {"Cl": 0.03, "alkali_eq": 1.5}
    modes: list[Literal["min_cost", "max_cheap", "balanced"]] = ["min_cost"]
    cheap_material_id: int | None = None  # max_cheap 模式的“廉价原料”
    # nominal：化验值视为精确常量（现有行为，默认）；
    # robust：检测不确定度下的稳健配比，所有 SM/IM/KH/有害组分最坏边界同时满足。
    robust_mode: Literal["nominal", "robust"] = "nominal"
    save: bool = True


class EvaluateRequest(BaseModel):
    """手工配比试算：直接给干基份额，用于分母为零/缺测报错演示。"""

    scenario_name: str = "手工配比"
    picks: list[Candidate]
    shares_pct_dry: list[float]  # 与 picks 等长；和不强制 100，会归一化


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
    uncertainty_trace: dict | None = None
    worst_case_snapshot: dict | None = None


class RobustReport(BaseModel):
    mode: str
    mode_label: str
    margins: dict
    active_components: list = []


class SolutionOut(BaseModel):
    mode: str
    mode_label: str
    success: bool
    total_cost: float | None = None
    cost_per_t_dry: float | None = None
    indicators: dict | None = None
    composition_dry_pct: dict | None = None
    composition_wet_pct: dict | None = None
    water_pct_in_wet_mix: float | None = None
    items: list[SolutionItem] = []
    diagnostic: dict | None = None
    robust: dict | None = None  # 稳健方案（名义方案并列返回）


class BlendResponse(BaseModel):
    run_id: int | None
    run_code: str
    status: str
    robust_status: str | None = None  # nominal / robust_feasible / robust_infeasible
    solutions: list[SolutionOut]
