import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService,
} from '../services/api.service';
import {
  BlendResponse, Material, Solution, Targets,
} from '../models/models';
import { SolutionCardComponent } from './solution-card.component';

interface CandRow {
  material_id: number;
  assay_version_id: number | null;
  selected: boolean;
}

interface Preset {
  key: string;
  label: string;
  desc: string;
  ids: number[];
  targets: Targets;
  hazards: Record<string, number>;
  modes: string[];
  cheap_id?: number;
  batch?: number;
  robust?: boolean;
  /** 按原料 code 指定化验版（缺省取界面当前选择） */
  assayByCode?: Record<string, string>;
  demo?: boolean;
}

const BASE_T: Targets = { SM: { min: 2.4, max: 2.8 }, IM: { min: 1.4, max: 1.8 }, KH: { min: 0.88, max: 0.94 } };

@Component({
  selector: 'app-blend',
  standalone: true,
  imports: [CommonModule, FormsModule, SolutionCardComponent],
  templateUrl: './blend.component.html',
})
export class BlendComponent implements OnInit {
  materials: Material[] = [];
  cand: Record<number, CandRow> = {};
  targets: Targets = JSON.parse(JSON.stringify(BASE_T));
  hazardCl = 0.05;
  hazardAlkali = 1.5;
  batch = 1000;
  modes = { min_cost: true, max_cheap: true, balanced: true };
  robust = true;
  cheapId: number | null = 4;
  scenario = '检测不确定度稳健配比（虚构边界）';
  loading = false;
  result: BlendResponse | null = null;
  apiError: any = null;

  // 手工配比（错误演示）
  evalIds = [8];
  evalShares = [100];
  evalResult: any = null;
  evalError: any = null;
  evalBusy = false;

  presets: Preset[] = [
    {
      key: 'base', label: '基准：三方案对比',
      desc: '5 种常规原料，成本最优 / 粉煤灰用量最大 / 率值居中，对比含水率对湿料采购量的影响。',
      ids: [1, 2, 3, 4, 5], targets: JSON.parse(JSON.stringify(BASE_T)),
      hazards: { Cl: 0.05, alkali_eq: 1.5 },
      modes: ['min_cost', 'max_cheap', 'balanced'], cheap_id: 4,
      robust: false,
    },
    {
      key: 'precise', label: '稳健：10月精密复测版',
      desc: '全部选用 V2026-10 精密化验单（小幅检测不确定度；粉煤灰为湿基容差），'
        + '并列展示名义方案与最坏边界下仍可行的稳健方案及余量。',
      ids: [1, 2, 3, 4, 5], targets: JSON.parse(JSON.stringify(BASE_T)),
      hazards: { Cl: 0.05, alkali_eq: 1.5 },
      modes: ['min_cost', 'max_cheap', 'balanced'], cheap_id: 4,
      robust: true,
      assayByCode: {
        LS01: 'V2026-10', SS01: 'V2026-10', SH01: 'V2026-10',
        AS01: 'V2026-10W', IR01: 'V2026-10',
      },
    },
    {
      key: 'boundary', label: '稳健失效：快检版误差边界',
      desc: '选用 V2026-10Q 现场快检版（误差较大）并收紧 KH 窗口到 [0.90,0.95]、'
        + '碱当量 0.60%：名义值合格，但最坏边界上 KH 被突破——稳健方案不可行，'
        + '列出触发报告、组分取界与突破量。',
      ids: [1, 2, 3, 4, 5],
      targets: { SM: { min: 2.3, max: 2.9 }, IM: { min: 1.3, max: 1.9 }, KH: { min: 0.90, max: 0.95 } },
      hazards: { Cl: 0.05, alkali_eq: 0.6 },
      modes: ['min_cost'], robust: true,
      assayByCode: {
        LS01: 'V2026-10Q', SS01: 'V2026-10Q', SH01: 'V2026-10Q',
        AS01: 'V2026-10WQ', IR01: 'V2026-10Q',
      },
    },
    {
      key: 'cheap', label: '廉价原料致指标超限',
      desc: '仅石灰石+高碱页岩且无铁质校正：廉价料拉低成本但 IM/KH 超限，展示失败与冲突项。',
      ids: [1, 3], targets: JSON.parse(JSON.stringify(BASE_T)),
      hazards: { Cl: 0.05, alkali_eq: 1.5 },
      modes: ['min_cost'], robust: false,
    },
    {
      key: 'alkali', label: '有害组分（碱当量）上限冲突',
      desc: '碱当量收紧到 0.40%（干基），廉价页岩/粉煤灰无法同时满足，诊断指明突破量。',
      ids: [1, 2, 3, 4, 5], targets: JSON.parse(JSON.stringify(BASE_T)),
      hazards: { Cl: 0.05, alkali_eq: 0.4 },
      modes: ['min_cost'], robust: false,
    },
    {
      key: 'avail', label: '大批量可用量约束',
      desc: '5000 t 干生料超过石灰石 3000 t 湿基可用量，KH 与可用量同时冲突。',
      ids: [1, 2, 3, 4, 5], targets: JSON.parse(JSON.stringify(BASE_T)),
      hazards: { Cl: 0.05, alkali_eq: 1.5 },
      modes: ['min_cost'], batch: 5000, robust: false,
    },
  ];

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.api.materials(false).subscribe(ms => {
      this.materials = ms;
      for (const m of ms) {
        this.cand[m.id] = {
          material_id: m.id,
          assay_version_id: m.assay_versions[0]?.id ?? null,
          selected: [1, 2, 3, 4, 5].includes(m.id),
        };
      }
    });
  }

  mat(id: number): Material | undefined { return this.materials.find(m => m.id === id); }

  chosenVersions(id: number) { return this.mat(id)?.assay_versions ?? []; }

  applyPreset(p: Preset): void {
    this.scenario = p.label;
    for (const m of this.materials) {
      this.cand[m.id].selected = p.ids.includes(m.id);
      const ver = p.assayByCode?.[m.code];
      if (ver) {
        const av = m.assay_versions.find(a => a.version === ver);
        if (av) this.cand[m.id].assay_version_id = av.id;
      }
    }
    this.targets = JSON.parse(JSON.stringify(p.targets));
    this.hazardCl = p.hazards['Cl'] ?? 0.05;
    this.hazardAlkali = p.hazards['alkali_eq'] ?? 1.5;
    this.batch = p.batch ?? 1000;
    this.robust = p.robust ?? false;
    this.modes = {
      min_cost: p.modes.includes('min_cost'),
      max_cheap: p.modes.includes('max_cheap'),
      balanced: p.modes.includes('balanced'),
    };
    if (p.cheap_id) this.cheapId = p.cheap_id;
  }

  selectedCandidates() {
    return Object.values(this.cand).filter(c => c.selected);
  }

  selectedModes(): string[] {
    return (['min_cost', 'max_cheap', 'balanced'] as const)
      .filter(k => this.modes[k]);
  }

  solve(): void {
    this.apiError = null;
    const cands = this.selectedCandidates();
    if (!cands.length) { this.apiError = { message: '请至少勾选一种候选原料。' }; return; }
    if (!this.selectedModes().length) { this.apiError = { message: '请至少选择一种求解模式。' }; return; }
    this.loading = true;
    this.result = null;
    this.api.blend({
      scenario_name: this.scenario,
      batch_t_dry: this.batch,
      candidates: cands.map(c => ({
        material_id: c.material_id,
        assay_version_id: c.assay_version_id,
      })),
      targets: this.targets,
      hazard_limits_pct: { Cl: this.hazardCl, alkali_eq: this.hazardAlkali },
      modes: this.selectedModes(),
      cheap_material_id: this.modes.max_cheap ? this.cheapId : null,
      robust: this.robust,
      save: true,
    }).subscribe({
      next: r => { this.result = r; this.loading = false; },
      error: e => {
        this.apiError = e.error ?? { message: '请求失败：' + e.message };
        this.loading = false;
      },
    });
  }

  // ---------- 手工配比错误演示 ----------
  setEvalDemo(kind: 'zero' | 'missing'): void {
    if (kind === 'zero') { this.evalIds = [8]; this.evalShares = [100]; }
    else { this.evalIds = [7, 1]; this.evalShares = [20, 80]; }
    this.evalResult = null; this.evalError = null;
  }

  onEvalIdsChange(): void {
    this.evalShares = this.evalIds.map(() => Math.round(100 / this.evalIds.length));
    this.evalResult = null; this.evalError = null;
  }

  evaluate(): void {
    this.evalError = null; this.evalResult = null; this.evalBusy = true;
    this.api.evaluate(
      this.evalIds.map(id => ({ material_id: id })),
      this.evalShares,
      '手工配比错误演示',
    ).subscribe({
      next: r => { this.evalResult = r; this.evalBusy = false; },
      error: e => { this.evalError = e.error ?? { message: e.message }; this.evalBusy = false; },
    });
  }
}
