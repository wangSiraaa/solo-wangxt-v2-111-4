import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  Conflict, MarginRow, RobustSolution, Solution, Targets,
} from '../models/models';
import { StackBarComponent, Seg, colorOf } from './stack-bar.component';

interface Row { code: string; name: string; share: number; }

@Component({
  selector: 'app-solution-card',
  standalone: true,
  imports: [CommonModule, FormsModule, StackBarComponent],
  templateUrl: './solution-card.component.html',
})
export class SolutionCardComponent {
  @Input() sol!: Solution;
  @Input() targets?: Targets | null;
  @Input() index = 0;

  openTrace: Record<string, boolean> = {};
  private paletteUsed: string[] = [];

  inRange(v: number, lo: number | null | undefined, hi: number | null | undefined): boolean {
    return (lo == null || v >= lo - 1e-9) && (hi == null || v <= hi + 1e-9);
  }

  ratioCls(v: number, lo: any, hi: any): string {
    return this.inRange(v, lo, hi) ? 'badge ok' : 'badge err';
  }

  /** 稳健余量着色：>=0 安全（绿），<0 被边界突破（红） */
  headCls(v: number | undefined): string {
    if (v == null) return 'badge';
    return v >= -1e-9 ? 'badge ok' : 'badge err';
  }

  robust(): RobustSolution | null | undefined {
    return this.sol.robust;
  }

  marginEntries(): { key: string; row: MarginRow; lo: any; hi: any }[] {
    const r = this.robust()?.robust_report?.margins;
    if (!r) return [];
    return [
      { key: 'SM', row: r.SM, lo: this.targets?.SM?.min, hi: this.targets?.SM?.max },
      { key: 'IM', row: r.IM, lo: this.targets?.IM?.min, hi: this.targets?.IM?.max },
      { key: 'KH', row: r.KH, lo: this.targets?.KH?.min, hi: this.targets?.KH?.max },
    ];
  }

  hazardEntries(): { key: string; worst: number; limit: number; head: number }[] {
    const h = this.robust()?.robust_report?.margins?.hazards || {};
    return Object.entries(h).map(([key, v]) => ({
      key, worst: v.worst_high, limit: v.limit, head: v.headroom,
    }));
  }

  robustConflicts(): Conflict[] {
    return this.robust()?.diagnostic?.conflicts || [];
  }

  shareSegs(): Seg[] {
    this.paletteUsed = [];
    return this.sol.items.map(it => ({
      label: `${it.material_code} ${it.material_name}`,
      value: it.share_pct_dry,
      color: colorOf(it.material_code, this.paletteUsed),
    }));
  }

  /** 四大氧化物按原料来源拆分（百分点 → 占四氧化物总量的相对比例） */
  oxideSourceSegs(): { oxide: string; segs: Seg[]; total: number }[] {
    const oxides = ['CaO', 'SiO2', 'Al2O3', 'Fe2O3'];
    this.paletteUsed = [];
    return oxides.map(oxide => {
      const segs = this.sol.items.map(it => {
        const v = it.conversion_trace.steps.find(s => s.component === oxide);
        const dry = v ? v.value_out : 0;
        return {
          label: `${it.material_code} ${it.material_name}`,
          value: dry * it.share_pct_dry / 100,
          color: colorOf(it.material_code, this.paletteUsed),
        };
      });
      const total = segs.reduce((a, s) => a + s.value, 0);
      return { oxide, segs, total };
    });
  }

  compositionEntries(basis: 'dry' | 'wet'): [string, number][] {
    const comp = basis === 'dry'
      ? this.sol.composition_dry_pct
      : this.sol.composition_wet_pct;
    if (!comp) return [];
    return Object.entries(comp)
      .filter(([k]) => k !== 'LOI')
      .sort((a, b) => b[1] - a[1]);
  }

  loi(): number | null {
    return this.sol.composition_dry_pct?.['LOI'] ?? null;
  }

  // ---------- 稳健子方案展示辅助 ----------
  keyVal(obj: Record<string, any>): [string, any][] {
    return Object.entries(obj || {});
  }

  comboText(combo: Record<string, string> | undefined): string {
    if (!combo) return '';
    return Object.entries(combo)
      .map(([c, side]) => `${c}${side === 'upper' ? '↑' : '↓'}`).join(' ');
  }

  nontrivialSteps(it: any): any[] {
    return (it?.uncertainty_trace?.steps || []).filter((s: any) => !s.zero_tolerance);
  }

  robustSegs(): Seg[] {
    const rb = this.robust();
    this.paletteUsed = [];
    return (rb?.items || []).map(it => ({
      label: `${it.material_code} ${it.material_name}`,
      value: it.share_pct_dry,
      color: colorOf(it.material_code, this.paletteUsed),
    }));
  }
}

