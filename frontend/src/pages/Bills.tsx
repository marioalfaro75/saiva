import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { RecurringSeries } from "../api/types";
import { formatCents, formatDate } from "../format";

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function tagFor(s: RecurringSeries) {
  if (!s.active) return <span className="tag">inactive</span>;
  if (s.direction === "income") return <span className="tag transfer">income</span>;
  if (s.is_subscription) return <span className="tag transfer">subscription</span>;
  return <span className="tag">bill</span>;
}

export function Bills() {
  const recurring = useQuery({ queryKey: ["recurring"], queryFn: api.recurring });
  const upcoming = useQuery({ queryKey: ["upcoming-bills"], queryFn: () => api.upcomingBills(60) });

  const data = recurring.data;
  const series = data?.series ?? [];
  const bills = upcoming.data?.bills ?? [];

  return (
    <div>
      <div className="page-head">
        <h1>Bills &amp; recurring</h1>
      </div>

      <div className="cards" style={{ marginBottom: 16 }}>
        <div className="card">
          <div className="stat-label">Committed monthly</div>
          <div className="stat-value">{data ? formatCents(data.monthly_committed_cents) : "—"}</div>
          <span className="muted">recurring expenses, normalised</span>
        </div>
        <div className="card">
          <div className="stat-label">Subscriptions</div>
          <div className="stat-value">{data?.subscriptions_count ?? "—"}</div>
          <span className="muted">
            {data ? `${formatCents(data.subscriptions_monthly_cents)} / mo` : ""}
          </span>
        </div>
        <div className="card">
          <div className="stat-label">Recurring income</div>
          <div className="stat-value positive">
            {data ? formatCents(data.income_monthly_cents) : "—"}
          </div>
          <span className="muted">per month</span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="spread">
          <h2>Upcoming bills</h2>
          <span className="muted">
            next {upcoming.data?.horizon_days ?? 60} days · {formatCents(upcoming.data?.total_cents ?? 0)}
          </span>
        </div>
        {bills.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Due</th>
                <th>Merchant</th>
                <th>Category</th>
                <th>Cadence</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {bills.map((b, i) => (
                <tr key={`${b.merchant}-${b.due_date}-${i}`}>
                  <td>{formatDate(b.due_date)}</td>
                  <td>{b.merchant}</td>
                  <td className="muted">{b.category_name ?? "—"}</td>
                  <td className="muted">{cap(b.cadence)}</td>
                  <td className="num negative">{formatCents(-b.amount_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No upcoming bills detected yet.</p>
        )}
      </div>

      <div className="card">
        <h2>Recurring transactions</h2>
        {series.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Merchant</th>
                <th>Cadence</th>
                <th>Category</th>
                <th className="num">Typical</th>
                <th className="num">Monthly</th>
                <th>Last seen</th>
                <th>Next due</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {series.map((s) => (
                <tr key={`${s.merchant}-${s.cadence}`} style={{ opacity: s.active ? 1 : 0.55 }}>
                  <td>{s.merchant}</td>
                  <td className="muted">{cap(s.cadence)}</td>
                  <td className="muted">{s.category_name ?? "—"}</td>
                  <td className={`num ${s.direction === "income" ? "positive" : ""}`}>
                    {formatCents(
                      s.direction === "income" ? s.typical_amount_cents : -s.typical_amount_cents,
                    )}
                  </td>
                  <td className="num muted">{formatCents(s.monthly_amount_cents)}</td>
                  <td className="muted">{formatDate(s.last_date)}</td>
                  <td className="muted">{formatDate(s.next_due)}</td>
                  <td>{tagFor(s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">
            No recurring transactions detected yet. Once a few months of history are imported,
            subscriptions, bills and salary show up here.
          </p>
        )}
      </div>
    </div>
  );
}
