import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api/client";
import { formatCents, formatDate } from "../format";

const HORIZONS = [
  { value: 30, label: "30 days" },
  { value: 60, label: "60 days" },
  { value: 90, label: "90 days" },
  { value: 180, label: "6 months" },
  { value: 365, label: "1 year" },
];
const CUTS = [10, 20, 50, 100];

function shortDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-AU", { day: "2-digit", month: "short" });
}

function Stat({ label, value, cls, sub }: { label: string; value: string; cls?: string; sub?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls ?? ""}`}>{value}</div>
      {sub && <span className="muted">{sub}</span>}
    </div>
  );
}

export function Forecast() {
  const [days, setDays] = useState(90);
  const [categoryId, setCategoryId] = useState("");
  const [cut, setCut] = useState(0);

  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const base = useQuery({
    queryKey: ["forecast", days],
    queryFn: () => api.forecast(days, []),
  });
  const scenarioOn = categoryId !== "" && cut > 0;
  const scenario = useQuery({
    queryKey: ["forecast-scenario", days, categoryId, cut],
    queryFn: () => api.forecast(days, [{ category_id: categoryId, pct: -cut }]),
    enabled: scenarioOn,
  });

  const expenseCats = (categories.data ?? []).filter((c) => c.parent_id && c.kind === "expense");
  const view = scenarioOn && scenario.data ? scenario.data : base.data;

  const chart = (base.data?.points ?? []).map((p, i) => ({
    label: shortDate(p.date),
    Balance: p.balance_cents / 100,
    ...(scenarioOn && scenario.data
      ? { Scenario: (scenario.data.points[i]?.balance_cents ?? 0) / 100 }
      : {}),
  }));
  const moneyTip = (value: unknown): string => formatCents(Math.round(Number(value) * 100));

  return (
    <div>
      <div className="page-head">
        <h1>Forecast</h1>
        <select
          className="pill-select"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          {HORIZONS.map((h) => (
            <option key={h.value} value={h.value}>
              {h.label}
            </option>
          ))}
        </select>
      </div>

      <div className="cards">
        <Stat label="Balance today" value={formatCents(base.data?.starting_balance_cents ?? 0)} />
        <Stat
          label="Projected balance"
          value={formatCents(view?.end_balance_cents ?? 0)}
          cls={(view?.end_balance_cents ?? 0) < 0 ? "negative" : "positive"}
          sub={`in ${days} days`}
        />
        <Stat
          label="Lowest point"
          value={formatCents(view?.low_balance_cents ?? 0)}
          cls={(view?.low_balance_cents ?? 0) < 0 ? "negative" : ""}
          sub={view ? formatDate(view.low_balance_date) : undefined}
        />
        <Stat
          label="Recurring income"
          value={formatCents(view?.monthly_income_cents ?? 0)}
          cls="positive"
          sub="per month"
        />
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="spread">
          <h2>Projected balance</h2>
          {(view?.low_balance_cents ?? 0) < 0 && (
            <span className="status-pill over">Dips below zero</span>
          )}
        </div>
        {chart.length > 1 ? (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="#28344f" vertical={false} />
              <XAxis dataKey="label" stroke="#93a1bd" fontSize={12} minTickGap={28} />
              <YAxis stroke="#93a1bd" fontSize={12} width={64} tickFormatter={(v) => `$${v}`} />
              <Tooltip formatter={moneyTip} contentStyle={{ background: "#131c2e", border: "1px solid #28344f" }} />
              <ReferenceLine y={0} stroke="#f87171" strokeDasharray="3 3" />
              <Line type="monotone" dataKey="Balance" stroke="#2dd4bf" strokeWidth={2} dot={false} />
              {scenarioOn && scenario.data && (
                <Line
                  type="monotone"
                  dataKey="Scenario"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  dot={false}
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <p className="muted">
            Not enough history to forecast yet. Import a few months of transactions (or load demo
            data from Settings) — paydays and your spending run-rate drive the projection.
          </p>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>What if…</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          See the effect of trimming a category's spending across the forecast.
        </p>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <select
            className="pill-select"
            value={categoryId}
            onChange={(e) => setCategoryId(e.target.value)}
          >
            <option value="">Choose a category…</option>
            {expenseCats.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <select
            className="pill-select"
            value={cut}
            onChange={(e) => setCut(Number(e.target.value))}
            disabled={categoryId === ""}
          >
            <option value={0}>No change</option>
            {CUTS.map((c) => (
              <option key={c} value={c}>
                Cut {c}%
              </option>
            ))}
          </select>
          {scenarioOn && scenario.data && base.data && (
            <span className="muted">
              {formatCents(
                scenario.data.end_balance_cents - base.data.end_balance_cents,
              )}{" "}
              better by day {days}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
