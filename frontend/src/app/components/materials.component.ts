import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AssayVersion, Material } from '../models/models';
import { ApiService } from '../services/api.service';

const OX = ['CaO', 'SiO2', 'Al2O3', 'Fe2O3', 'MgO', 'SO3', 'K2O', 'Na2O', 'Cl', 'LOI'];

@Component({
  selector: 'app-materials',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './materials.component.html',
})
export class MaterialsComponent implements OnInit {
  materials: Material[] = [];
  ox = OX;
  activeOnly = false;
  expanded: Record<number, boolean> = {};
  versionPick: Record<number, number> = {};

  constructor(private api: ApiService) {}

  ngOnInit(): void { this.load(); }

  load(): void {
    this.api.materials(this.activeOnly).subscribe(ms => {
      this.materials = ms;
      for (const m of ms) {
        if (m.assay_versions.length && this.versionPick[m.id] == null) {
          this.versionPick[m.id] = m.assay_versions[0].id;
        }
      }
    });
  }

  chosen(m: Material) {
    return m.assay_versions.find(a => a.id === this.versionPick[m.id]) ?? m.assay_versions[0];
  }

  isMeasured(m: Material, ox: string): boolean {
    const a = this.chosen(m);
    return a ? a.measured_oxides.includes(ox) : false;
  }

  /** 湿基化验单的干基换算预览：val / (1-m) */
  dryVal(m: Material, ox: string): string {
    const a = this.chosen(m);
    if (!a || !(ox in a.composition)) return '—';
    const v = a.composition[ox];
    if (a.basis === 'wet') {
      return (v / (1 - m.moisture_pct / 100)).toFixed(3);
    }
    return v.toFixed(3);
  }

  totalDry(m: Material): number {
    const a = this.chosen(m);
    if (!a) return 0;
    const f = a.basis === 'wet' ? 1 / (1 - m.moisture_pct / 100) : 1;
    return OX.reduce((s, o) => s + (o in a.composition ? a.composition[o] * f : 0), 0);
  }

  /** 该化验版声明了非零容差的组分 */
  uncertainEntries(a: AssayVersion | undefined): [string, { lower: number; upper: number }][] {
    if (!a?.uncertainty) return [];
    return Object.entries(a.uncertainty)
      .filter(([, v]) => (v.lower ?? 0) !== 0 || (v.upper ?? 0) !== 0)
      .map(([k, v]) => [k, v]);
  }

  /** 容差换算到干基后的半宽展示（湿基 ×1/(1-w)） */
  dryUnc(m: Material, a: AssayVersion | undefined, v: { lower: number; upper: number }): string {
    const f = a?.basis === 'wet' ? 1 / (1 - m.moisture_pct / 100) : 1;
    return `[${(v.lower * f).toFixed(3)}, +${(v.upper * f).toFixed(3)}]`;
  }
}
