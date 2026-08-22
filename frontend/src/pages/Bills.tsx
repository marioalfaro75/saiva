import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { RecurringSeries, UpcomingBill } from "../api/types";
import { TABLE_MIN, TableWrap } from "../table/TableWrap";
import { formatCents, formatDate } from "../format";
import { usePeriod } from "../period/context";
import { FilterRow, FilterToggle } from "../table/FilterRow";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useTable } from "../table/useTable";
import { PageHead } from "../components/PageHead";

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function tagFor(s: RecurringSeries) {
  if (!s.active) return <span className="tag">inactive</span>;
  if (s.direction === "income") return <span className="tag transfer">income</span>;
  if (s.is_subscription) return <span className="tag transfer">subscription</span>;
  return <span className="tag">bill</span>;
}

/** The label the row's tag shows, so "subscription" or "income" can be sorted and
 *  filtered like any other column. */
function kindOf(s: RecurringSeries): string {
  if (!s.active) return "inactive";
  if (s.direction === "income") return "income";
  return s.is_subscription ? "subscription" : "bill";
}

const UPCOMING_LABELS = {
  due: "Due",
  merchant: "Merchant",
  category: "Category",
  cadence: "Cadence",
  amount: "Amount",
};

const UPCOMING_COLUMNS: ColumnSpec<UpcomingBill>[] = [
  { key: "due", sort: (b) => b.due_date, text: (b) => formatDate(b.due_date) },
  { key: "merchant", sort: (b) => b.merchant },
  { key: "category", sort: (b) => b.category_name },
  { key: "cadence", sort: (b) => cap(b.cadence) },
  { key: "amount", sort: (b) => b.amount_cents, text: (b) => formatCents(-b.amount_cents) },
];

const SERIES_LABELS = {
  merchant: "Merchant",
  cadence: "Cadence",
  category: "Category",
  typical: "Typical",
  monthly: "Monthly",
  last: "Last seen",
  next: "Next due",
  kind: "Kind",
};

const SERIES_COLUMNS: ColumnSpec<RecurringSeries>[] = [
  { key: "merchant", sort: (s) => s.merchant },
  { key: "cadence", sort: (s) => cap(s.cadence) },
  { key: "category", sort: (s) => s.category_name },
  {
    key: "typical",
    sort: (s) => s.typical_amount_cents,
    text: (s) => formatCents(s.typical_amount_cents),
  },
  {
    key: "monthly",
    sort: (s) => s.monthly_amount_cents,
    text: (s) => formatCents(s.monthly_amount_cents),
  },
  { key: "last", sort: (s) => s.last_date, text: (s) => formatDate(s.last_date) },
  { key: "next", sort: (s) => s.next_due, text: (s) => formatDate(s.next_due) },
  { key: "kind", sort: kindOf },
];

export function Bills() {
  const { period } = usePeriod();
  const recurring = useQuery({
    queryKey: ["recurring", period],
    queryFn: () => api.recurring(period),
  });
  const upcoming = useQuery({
    queryKey: ["upcoming-bills", period],
    queryFn: () => api.upcomingBills(60, period),
  });

  const data = recurring.data;
  const series = data?.series ?? [];
  const bills = upcoming.data?.bills ?? [];
  const upcomingTable = useTable(bills, UPCOMING_COLUMNS, {
    id: "bills-upcoming",
    defaultSort: { key: "due", dir: "asc" },
  });
  const seriesTable = useTable(series, SERIES_COLUMNS, { id: "bills-recurring" });

  return (
    <div>
      <PageHead
        title="Bills & recurring"
        sub="Payments that repeat, and when the next ones fall due."
      />

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
          <>
          <div className="spread">
            <span />
            <FilterToggle table={upcomingTable} />
          </div>
          <TableWrap min={TABLE_MIN.billsUpcoming} label="Upcoming bills">
            <table>
              <thead>
                <tr>
                  <SortHeader table={upcomingTable} col="due">
                    Due
                  </SortHeader>
                  <SortHeader table={upcomingTable} col="merchant">
                    Merchant
                  </SortHeader>
                  <SortHeader table={upcomingTable} col="category">
                    Category
                  </SortHeader>
                  <SortHeader table={upcomingTable} col="cadence">
                    Cadence
                  </SortHeader>
                  <SortHeader table={upcomingTable} col="amount" numeric>
                    Amount
                  </SortHeader>
                </tr>
                <FilterRow
                  table={upcomingTable}
                  labels={UPCOMING_LABELS}
                  columns={["due", "merchant", "category", "cadence", "amount"]}
                />
              </thead>
              <tbody>
                {upcomingTable.rows.map((b, i) => (
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
          </TableWrap>
          </>
        ) : (
          <p className="muted">No upcoming bills detected yet.</p>
        )}
      </div>

      <div className="card">
        <h2>Recurring transactions</h2>
        {series.length > 0 ? (
          <>
          <div className="spread">
            <span />
            <FilterToggle table={seriesTable} />
          </div>
          <TableWrap min={TABLE_MIN.billsRecurring} label="Recurring transactions">
            <table>
              <thead>
                <tr>
                  <SortHeader table={seriesTable} col="merchant">
                    Merchant
                  </SortHeader>
                  <SortHeader table={seriesTable} col="cadence">
                    Cadence
                  </SortHeader>
                  <SortHeader table={seriesTable} col="category">
                    Category
                  </SortHeader>
                  <SortHeader table={seriesTable} col="typical" numeric>
                    Typical
                  </SortHeader>
                  <SortHeader table={seriesTable} col="monthly" numeric>
                    Monthly
                  </SortHeader>
                  <SortHeader table={seriesTable} col="last">
                    Last seen
                  </SortHeader>
                  <SortHeader table={seriesTable} col="next">
                    Next due
                  </SortHeader>
                  <SortHeader table={seriesTable} col="kind">
                    Kind
                  </SortHeader>
                </tr>
                <FilterRow
                  table={seriesTable}
                  labels={SERIES_LABELS}
                  columns={[
                    "merchant", "cadence", "category", "typical",
                    "monthly", "last", "next", "kind",
                  ]}
                />
              </thead>
              <tbody>
                {seriesTable.rows.map((s) => (
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
          </TableWrap>
          </>
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
