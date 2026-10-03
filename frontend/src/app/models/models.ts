export interface AssayUncertaintySpec {
  lower: number;  // 与化验单同基准的下偏置（百分点，≤0）
  upper: number;  // 与化验单同基准的上偏置（百分点，≥0）
}

export interface AssayVersion {
  id: number;
  version: string;
  lab_report_no: string;
  assayed_at: string;
  basis: 'dry' | 'wet';
  composition: Record<string, number>;
  measured_oxides: string[];
  uncertainty?: Record<string, AssayUncertaintySpec> | null;
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
  candidates: {
    material_id: number;
    assay_version_id?: number | null;
    uncertainty_override?: Record<string, AssayUncertaintySpec> | null;
  }[];
  targets: Targets;
  hazard_limits_pct: Record<string, number>;
  modes: string[];
  cheap_material_id?: number | null;
  robust_mode?: 'nominal' | 'robust';
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

export interface UncertaintyStep {
  component: string;
  basis_in: string;
  nominal_in: number | null;
  lower_in: number;
  upper_in: number;
  factor: number;
  nominal_dry: number | null;
  lower_dry: number | null;
  upper_dry: number | null;
  zero_tolerance: boolean;
}

export interface UncertaintyTrace {
  material_code: string;
  material_name: string;
  assay_basis: 'dry' | 'wet';
  moisture_pct: number;
  dry_factor: number;
  nontrivial_components: number;
  steps: UncertaintyStep[];
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
  uncertainty_trace?: UncertaintyTrace | null;
  worst_case_snapshot?: Record<string, {
    nominal_dry_pct: number; lower_dry_pct: number; upper_dry_pct: number;
  }> | null;
}

export interface BoundaryComponentInfo {
  material_index?: number;
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  assay_basis?: string;
  components: Record<string, {
    side: 'upper' | 'lower';
    nominal_dry_pct: number;
    used_dry_pct: number;
  }>;
}

export interface Conflict {
  constraint: string;
  kind?: string;
  limit?: number;
  achieved?: number;
  breach?: number;
  normalized_gap: number;
  boundary_combo?: BoundaryComponentInfo[];
}

export interface MarginRow {
  nominal: number;
  worst_low: number;
  worst_high: number;
  denominator_interval?: [number, number];
  boundary_combo_low: Record<string, string>;
  boundary_combo_high: Record<string, string>;
  limit_min?: number;
  limit_max?: number;
  headroom_low?: number;
  headroom_high?: number;
}

export interface RobustReport {
  mode: string;
  mode_label: string;
  margins: {
    SM: MarginRow; IM: MarginRow; KH: MarginRow;
    hazards: Record<string, {
      worst_high: number; limit: number; headroom: number;
      boundary_combo_high: string;
    }>;
  };
  active_components: {
    material_code: string; material_name: string;
    assay_version: string; lab_report_no: string; assay_basis: string;
    components: string[];
  }[];
}

export interface Diagnostic {
  reason: string; message: string;
  conflicts: Conflict[];
  min_violation_objective?: number;
}

export interface Solution {
  mode: string;
  mode_label: string;
  success: boolean;
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
  diagnostic?: Diagnostic;
  robust?: RobustSolution | null;
}

export interface RobustSolution {
  mode: string;
  mode_label: string;
  success: boolean;
  total_cost?: number;
  cost_per_t_dry?: number;
  indicators?: Solution['indicators'];
  composition_dry_pct?: Record<string, number>;
  items: SolutionItem[];
  diagnostic?: Diagnostic;
  robust_report?: RobustReport;
}

export interface BlendResponse {
  run_id: number | null;
  run_code: string;
  status: string;
  robust_status?: 'robust_feasible' | 'robust_infeasible' | null;
  solutions: Solution[];
}

export interface RunSummary {
  id: number; run_code: string; scenario_name: string;
  status: string; created_at: string; modes: string[];
  robust_mode?: string;
}

export interface RunDetail {
  id: number; run_code: string; scenario_name: string;
  batch_t_dry: number; target: Targets; constraint_set: any;
  status: string; created_at: string;
  solutions: any[];
}
