import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api/client";
import { formatCents, formatPct } from "../format";

const PERIODS = [
  { value: "this_month", label: "This month" },
  { value: "last_30d", label: "Last 30 days" },
  { value: "this_period", label: "Pay period" },
  { value: "this_fy", label: "Financial year" },
];

const COLORS = [
  "#2dd4bf", "#60a5fa", "#f59e0b", "#f472b6", "#a78bfa",
  "#34d399", "#fb7185", "#38bdf8", "#fbbf24", "#c084fc",
];

function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls ?? ""}`}>{value}</div>
    </div>
  );
}

export function Overview() {
  const [period, setPeriod] = useState("this_month");
  const summary = useQuery({ queryKey: ["summary", period], queryFn: () => api.summary({ period }) });
  const breakdown = useQuery({
    queryKey: ["breakdown", period],
    queryFn: () => api.breakdown({ period }),
  });
  const trends = useQuery({ queryKey: ["trends", period], queryFn: () => api.trends({ period }) });

  const pie = (breakdown.data?.items ?? [])
    .slice(0, 8)
    .map((i) => ({ name: i.category_name, value: i.amount_cents / 100 }));
  const bars = (trends.data?.points ?? []).map((p) => ({
    month: p.period_start.slice(0, 7),
    Income: p.income_cents / 100,
    Expenses: p.expense_cents / 100,
  }));
  const moneyTip = (value: unknown): string => formatCents(Math.round(Number(value) * 100));

  return (
    <div>
      <div className="page-head">
        <h1>Overview</h1>
        <select className="pill-select" value={period} onChange={(e) => setPeriod(e.target.value)}>
          {PERIODS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </div>

      {summary.data?.txn_count === 0 && (
        <div className="notice">
          No transactions in this period yet — import a statement, or load demo data from Settings.
        </div>
      )}

      <div className="cards">
        <Stat label="Income" value={formatCents(summary.data?.income_cents ?? 0)} cls="positive" />
        <Stat label="Expenses" value={formatCents(summary.data?.expense_cents ?? 0)} cls="negative" />
        <Stat label="Net" value={formatCents(summary.data?.net_cents ?? 0)} />
        <Stat
          label="Savings rate"
          value={summary.data ? formatPct(summary.data.savings_rate) : "—"}
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginTop: 16 }}>
        <div className="card">
          <h2>Where the money goes</h2>
          {pie.length ? (
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie
                  data={pie}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={60}
                  outerRadius={100}
                  paddingAngle={2}
                >
                  {pie.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={moneyTip} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="muted">No spending to show.</p>
          )}
        </div>

        <div className="card">
          <h2>Income vs expenses</h2>
          {bars.length ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={bars}>
                <XAxis dataKey="month" stroke="#93a1bd" fontSize={12} />
                <YAxis stroke="#93a1bd" fontSize={12} width={52} />
                <Tooltip formatter={moneyTip} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
                <Bar dataKey="Income" fill="#34d399" radius={[3, 3, 0, 0]} />
                <Bar dataKey="Expenses" fill="#f87171" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="muted">No trend to show.</p>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Top categories</h2>
        {breakdown.data && breakdown.data.items.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th>Parent</th>
                <th className="num">Spent</th>
                <th className="num">Share</th>
              </tr>
            </thead>
            <tbody>
              {breakdown.data.items.slice(0, 12).map((i) => (
                <tr key={i.category_id ?? "uncat"}>
                  <td>{i.category_name}</td>
                  <td className="muted">{i.parent_name ?? "—"}</td>
                  <td className="num">{formatCents(i.amount_cents)}</td>
                  <td className="num muted">{formatPct(i.pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">Nothing to show yet.</p>
        )}
      </div>
    </div>
  );
}
