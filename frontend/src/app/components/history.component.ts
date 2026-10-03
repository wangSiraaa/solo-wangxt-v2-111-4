import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api.service';
import { RunDetail, RunSummary } from '../models/models';

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './history.component.html',
})
export class HistoryComponent implements OnInit {
  runs: RunSummary[] = [];
  detail: RunDetail | null = null;
  loading = false;

  constructor(private api: ApiService) {}

  ngOnInit(): void { this.reload(); }

  reload(): void {
    this.api.runs().subscribe(rs => { this.runs = rs; });
  }

  open(id: number): void {
    this.loading = true;
    this.api.run(id).subscribe(d => { this.detail = d; this.loading = false; });
  }

  ind(s: any, key: string): string {
    return s.payload?.indicators?.[key] != null
      ? Number(s.payload.indicators[key]).toFixed(3) : '—';
  }

  margin(s: any, key: string, side: 'min' | 'max'): string {
    const e = s.worst_case?.indicators?.find((x: any) => x.indicator === key);
    if (!e) return '—';
    const v = side === 'min' ? e.margin_min : e.margin_max;
    return v == null ? '—' : Number(v).toFixed(3);
  }

  allZero(us: any): boolean {
    return !!us && us.components.every((c: any) => c.zero_tolerance);
  }

  tolerated(us: any): number {
    return us ? us.components.filter((c: any) => !c.zero_tolerance).length : 0;
  }

  toleratedComps(us: any): any[] {
    return us ? us.components.filter((c: any) => !c.zero_tolerance) : [];
  }
}
