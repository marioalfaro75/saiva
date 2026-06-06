import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { BenchmarkItem } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { formatCents } from "../format";

function signed(cents: number): string {
  return `${cents > 0 ? "+" : "-"}${formatCents(Math.abs(cents))}`;
}

function Row({ item }: { item: BenchmarkItem }) {
  const has = item.your_weekly_cents > 0;
  const over = item.diff_cents > 0;
  const fill = Math.min(item.pct_of_typical, 2) * 50; // 100% of typical => half-full bar
  return (
    <tr>
      <td>{item.category}</td>
      <td className="num">
        {has ? formatCents(item.your_weekly_cents) : <span className="muted">—</span>}
      </td>
      <td className="num muted">{formatCents(item.typical_weekly_cents)}</td>
      <td style={{ minWidth: 120 }}>
        <div className="budget-bar" style={{ margin: "6px 0" }}>
          <span className={over ? "over" : ""} style={{ width: `${fill}%` }} />
        </div>
      </td>
      <td className={`num ${has ? (over ? "negative" : "positive") : "muted"}`}>
        {has ? signed(item.diff_cents) : "—"}
      </td>
    </tr>
  );
}

export function Benchmarks() {
  const qc = useQueryClient();
  const { me } = useAuth();
  const [adults, setAdults] = useState(me?.household.adults ?? 1);
  const [children, setChildren] = useState(me?.household.children ?? 0);

  const bm = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const save = useMutation({
    mutationFn: () => api.updateHousehold({ adults, children }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["benchmarks"] }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  const data = bm.data;
  const totalDiff = data ? data.your_total_weekly_cents - data.typical_total_weekly_cents : 0;

  return (
    <div>
      <div className="page-head">
        <h1>Benchmarks</h1>
        {data && <span className="muted">{data.basis}</span>}
      </div>

      <div className="card">
        <h2>Your household</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Typical figures are scaled to your household size.
        </p>
        <form onSubmit={onSubmit}>
          <div className="row">
            <div className="field">
              <label>Adults</label>
              <input
                type="number"
                min={1}
                value={adults}
                onChange={(e) => setAdults(Number(e.target.value))}
              />
            </div>
            <div className="field">
              <label>Children</label>
              <input
                type="number"
                min={0}
                value={children}
                onChange={(e) => setChildren(Number(e.target.value))}
              />
            </div>
          </div>
          <button className="btn" disabled={save.isPending}>
            {save.isPending ? "Updating…" : "Update household size"}
          </button>
        </form>
      </div>

      {data && (
        <div className="cards" style={{ marginTop: 16 }}>
          <div className="card">
            <div className="stat-label">Your weekly spend</div>
            <div className="stat-value">{formatCents(data.your_total_weekly_cents)}</div>
          </div>
          <div className="card">
            <div className="stat-label">Typical household</div>
            <div className="stat-value">{formatCents(data.typical_total_weekly_cents)}</div>
          </div>
          <div className="card">
            <div className="stat-label">Difference</div>
            <div className={`stat-value ${totalDiff > 0 ? "negative" : "positive"}`}>
              {signed(totalDiff)}
            </div>
          </div>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <table>
          <thead>
            <tr>
              <th>Category</th>
              <th className="num">You / wk</th>
              <th className="num">Typical / wk</th>
              <th>vs typical</th>
              <th className="num">Difference</th>
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((i) => (
              <Row key={i.category} item={i} />
            ))}
          </tbody>
        </table>
        {data && data.your_total_weekly_cents === 0 && (
          <p className="muted">
            No spending yet to compare. Import a few months of transactions (or load demo data from
            Settings) to see how you stack up.
          </p>
        )}
      </div>

      {data && (
        <p className="muted" style={{ marginTop: 12, fontSize: 13 }}>
          {data.note}
        </p>
      )}
    </div>
  );
}
