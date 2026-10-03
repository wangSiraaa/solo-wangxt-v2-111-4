import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AssayBounds, Material } from '../models/models';
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
  /** 已请求过的干基不确定边界，按 assay_version_id 缓存 */
  boundsCache: Record<number, AssayBounds> = {};
  boundsLoading: Record<number, boolean> = {};

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

  hasUncertainty(m: Material): boolean {
    const u = this.chosen(m)?.uncertainties;
    return !!u && Object.keys(u).length > 0;
  }

  /** 组分的同基准原始偏差文本（如 ±0.30 或 −0.20/+0.40） */
  uncText(m: Material, ox: string): string {
    const u = this.chosen(m)?.uncertainties?.[ox];
    if (u == null) return '';
    if (typeof u === 'number') return `±${u}`;
    return `${u.lower}/${u.upper}`;
  }

  loadBounds(m: Material): void {
    const a = this.chosen(m);
    if (!a || this.boundsCache[a.id] || this.boundsLoading[a.id]) return;
    this.boundsLoading[a.id] = true;
    this.api.assayBounds(a.id).subscribe(b => {
      this.boundsCache[a.id] = b;
      this.boundsLoading[a.id] = false;
    });
  }
}
