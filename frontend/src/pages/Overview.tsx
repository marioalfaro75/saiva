import { useQuery } from "@tanstack/react-query";
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

import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { CategoryBreakdownItem } from "../api/types";
import { usePeriod } from "../period/context";
import { FilterRow, FilterToggle } from "../table/FilterRow";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useTable } from "../table/useTable";
import { TABLE_MIN, TableWrap } from "../table/TableWrap";
import { formatCents, formatPct } from "../format";
import { PageHead } from "../components/PageHead";

// A categorical scale: each entry is one pie slice, not a meaning. Two of these
// hexes match --warning and --info, and sweeping them into those tokens would
// recolour chart series whenever a semantic colour is retuned.
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

const BREAKDOWN_LABELS = {
  category: "Category",
  parent: "Parent",
  spent: "Spent",
  share: "Share",
};

const BREAKDOWN_COLUMNS: ColumnSpec<CategoryBreakdownItem>[] = [
  { key: "category", sort: (i) => i.category_name },
  { key: "parent", sort: (i) => i.parent_name },
  { key: "spent", sort: (i) => i.amount_cents, text: (i) => formatCents(i.amount_cents) },
  { key: "share", sort: (i) => i.pct, text: (i) => formatPct(i.pct) },
];

/** A filtered Transactions view for one category in the period being viewed. */
function transactionsFor(categoryId: string | null, period: string): string {
  const params = new URLSearchParams({ period });
  if (categoryId) params.set("category_id", categoryId);
  else params.set("uncategorised", "true");
  return `/transactions?${params.toString()}`;
}

export function Overview() {
  const { period, resolved } = usePeriod();
  const summary = useQuery({ queryKey: ["summary", period], queryFn: () => api.summary({ period }) });
  const breakdown = useQuery({
    queryKey: ["breakdown", period],
    queryFn: () => api.breakdown({ period }),
  });
  const trends = useQuery({ queryKey: ["trends", period], queryFn: () => api.trends({ period }) });
  // Only the leading categories are charted, but the table sorts the whole breakdown.
  const table = useTable(breakdown.data?.items ?? [], BREAKDOWN_COLUMNS, {
    id: "overview-breakdown",
    defaultSort: { key: "spent", dir: "desc" },
  });

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
      <PageHead title="Overview">
        <span className="muted">{resolved?.label}</span>
      </PageHead>

      {summary.data?.txn_count === 0 && (
        <div className="notice">
          No transactions in this period yet — <Link to="/import">import a statement</Link>, or
          load demo data from <Link to="/settings">Settings</Link>.
        </div>
      )}

      <div className="cards">
        {/* An em dash while loading, not $0.00: a zero is a claim about the data,
            and reading "you spent nothing" for a second is worse than reading
            nothing at all. The savings rate already used this idiom. */}
        <Stat
          label="Income"
          value={summary.data ? formatCents(summary.data.income_cents) : "—"}
          cls="positive"
        />
        <Stat
          label="Expenses"
          value={summary.data ? formatCents(summary.data.expense_cents) : "—"}
          cls="negative"
        />
        <Stat label="Net" value={summary.data ? formatCents(summary.data.net_cents) : "—"} />
        <Stat
          label="Savings rate"
          value={summary.data ? formatPct(summary.data.savings_rate) : "—"}
        />
      </div>

      <div className="split" style={{ marginTop: 16 }}>
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
          <>
            <div className="spread">
              <span />
              <FilterToggle table={table} />
            </div>
            <TableWrap min={TABLE_MIN.overviewBreakdown} label="Spending by category">
              <table>
              <thead>
                <tr>
                  <SortHeader table={table} col="category">
                    Category
                  </SortHeader>
                  <SortHeader table={table} col="parent">
                    Parent
                  </SortHeader>
                  <SortHeader table={table} col="spent" numeric>
                    Spent
                  </SortHeader>
                  <SortHeader table={table} col="share" numeric>
                    Share
                  </SortHeader>
                </tr>
                <FilterRow
                  table={table}
                  labels={BREAKDOWN_LABELS}
                  columns={["category", "parent", "spent", "share"]}
                />
              </thead>
              <tbody>
                {table.rows.map((i) => (
                  <tr key={i.category_id ?? "uncat"}>
                    <td>
                      <Link to={transactionsFor(i.category_id, period)}>{i.category_name}</Link>
                    </td>
                    <td className="muted">{i.parent_name ?? "—"}</td>
                    <td className="num">{formatCents(i.amount_cents)}</td>
                    <td className="num muted">{formatPct(i.pct)}</td>
                  </tr>
                ))}
              </tbody>
              </table>
            </TableWrap>
          </>
        ) : (
          <p className="muted">Nothing to show yet.</p>
        )}
      </div>
    </div>
  );
}
