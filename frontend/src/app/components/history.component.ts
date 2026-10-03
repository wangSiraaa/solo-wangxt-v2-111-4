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

  objEntries(obj: any): [string, any][] {
    return Object.entries(obj || {});
  }

  nontrivial(steps: any[] | undefined | null): any[] {
    return (steps || []).filter(s => !s.zero_tolerance);
  }
}
