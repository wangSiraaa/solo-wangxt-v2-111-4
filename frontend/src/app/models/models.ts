export interface AssayVersion {
  id: number;
  version: string;
  lab_report_no: string;
  assayed_at: string;
  basis: 'dry' | 'wet';
  composition: Record<string, number>;
  measured_oxides: string[];
  // 检测不确定度（与本化验单同基准的绝对偏差，质量百分点）
  uncertainties?: Record<string, number | { lower: number; upper: number }> | null;
}

export interface UncertaintyBound {
  nominal: number;
  lower: number;
  upper: number;
  zero_tolerance: boolean;
  below_physical_zero?: boolean;
}

export interface AssayBounds {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  basis: 'dry' | 'wet';
  moisture_pct: number;
  bounds_dry: Record<string, UncertaintyBound>;
}

export interface Material {
  id: number;
  code: string;
  name: string;
  category: string;
  moisture_pct: number;
  cost_per_t_wet: number;
  availability_t_wet: number | null;
  min_share_pct: number;
  is_active: boolean;
  note?: string | null;
  assay_versions: AssayVersion[];
}

export interface Interval { min?: number | null; max?: number | null; }
export interface Targets { SM: Interval; IM: Interval; KH: Interval; }

export interface BlendRequest {
  scenario_name: string;
  batch_t_dry: number;
  candidates: { material_id: number; assay_version_id?: number | null }[];
  targets: Targets;
  hazard_limits_pct: Record<string, number>;
  modes: string[];
  cheap_material_id?: number | null;
  robust?: boolean;
  save?: boolean;
}

export interface ConversionStep {
  component: string;
  basis_in: string;
  value_in: number;
  formula: string;
  factor: number;
  basis_out: string;
  value_out: number;
}

export interface SolutionItem {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  share_pct_dry: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  cost: number;
  conversion_trace: {
    material_code: string;
    material_name: string;
    moisture_pct: number;
    assay_basis: string;
    dry_factor: number;
    steps: ConversionStep[];
    mass_balance?: any;
  };
  uncertainty_snapshot?: UncertaintyItemSnapshot | null;
}

export interface UncertaintyComponentSnapshot {
  component: string;
  basis: string;
  nominal_native: number;
  uncertainty_native: { lower: number; upper: number };
  nominal_dry: number;
  lower_dry: number;
  upper_dry: number;
  zero_tolerance: boolean;
}

export interface UncertaintyItemSnapshot {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  basis: 'dry' | 'wet';
  moisture_pct: number;
  components: UncertaintyComponentSnapshot[];
}

export interface WorstIndicator {
  indicator: string;
  limit_min?: number | null;
  limit_max?: number | null;
  nominal?: number | null;
  worst_min?: number;
  worst_max?: number;
  margin_min?: number | null;
  margin_max?: number | null;
  ok: boolean;
  reason?: string;
}

export interface WorstHazard {
  component: string;
  limit_max: number;
  worst_min: number;
  worst_max: number;
  margin_max: number;
  ok: boolean;
}

export interface WorstCase {
  indicators: WorstIndicator[];
  hazards: WorstHazard[];
  all_ok: boolean;
  note: string;
  hazard_nominal?: Record<string, number>;
}

export interface TriggerComponent {
  component: string;
  bound: 'upper' | 'lower';
  value_dry: number;
}

export interface TriggerReport {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  basis: 'dry' | 'wet';
  moisture_pct: number;
  components: TriggerComponent[];
}

export interface Conflict {
  constraint: string;
  kind?: string;
  limit?: number;
  achieved?: number;
  normalized_gap: number;
  breakthrough?: number;
  boundary?: string;
  trigger_reports?: TriggerReport[];
}

export interface Solution {
  mode: string;
  mode_label: string;
  success: boolean;
  robust?: boolean;
  total_cost?: number;
  cost_per_t_dry?: number;
  indicators?: {
    SM: number; IM: number; KH: number;
    CaO: number; SiO2: number; Al2O3: number; Fe2O3: number;
    warnings: string[];
  };
  composition_dry_pct?: Record<string, number>;
  composition_wet_pct?: Record<string, number>;
  water_pct_in_wet_mix?: number;
  items: SolutionItem[];
  diagnostic?: {
    reason: string; message: string;
    conflicts: Conflict[];
    min_violation_objective?: number;
  };
  worst_case?: WorstCase;
}

export interface BlendResponse {
  run_id: number | null;
  run_code: string;
  status: string;
  solutions: Solution[];
  robust_solutions?: Solution[];
}

export interface RunSummary {
  id: number; run_code: string; scenario_name: string;
  status: string; created_at: string; modes: string[];
  robust?: boolean;
}

export interface RunUncertaintySnapshot {
  rule: string;
  materials: {
    material_id: number;
    material_code: string;
    material_name: string;
    assay_version_id: number;
    assay_version: string;
    lab_report_no: string;
    basis: 'dry' | 'wet';
    moisture_pct: number;
    dry_factor: number;
    has_uncertainty: boolean;
    bounds_dry: Record<string, UncertaintyBound>;
  }[];
}

export interface RunDetail {
  id: number; run_code: string; scenario_name: string;
  batch_t_dry: number; target: Targets; constraint_set: any;
  uncertainty_snapshot?: RunUncertaintySnapshot | null;
  status: string; created_at: string;
  solutions: any[];
  robust_solutions?: any[];
}
