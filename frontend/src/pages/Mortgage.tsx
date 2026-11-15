import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api/client";
import type { Mortgage as MortgageData, MortgageProjection, MortgageYear } from "../api/types";
import { EVERY_LABELS, parseDollarsOrZero } from "../components/accountTypes";
import { EmptyState } from "../components/EmptyState";
import { MortgageSetup } from "../components/MortgageSetup";
import { monthsBetween, monthYear, span } from "../components/mortgageText";
import { PageHead } from "../components/PageHead";
import { formatCents, formatDate } from "../format";
import { TableWrap } from "../table/TableWrap";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

/** How much sooner `better` is paid off than `base`, and the interest it saves. */
function saving(base: { payoff: string | null; interest_cents: number }, better: typeof base) {
  if (!better.payoff) return null;
  if (!base.payoff) return { months: null, interest: null };
  return {
    months: monthsBetween(better.payoff, base.payoff),
    interest: base.interest_cents - better.interest_cents,
  };
}

const moneyTip = (value: unknown): string => formatCents(Math.round(Number(value) * 100));
const shortMoney = (v: number) =>
  v >= 1_000_000 ? `$${(v / 1_000_000).toFixed(1)}m` : v >= 1000 ? `$${Math.round(v / 1000)}k` : `$${v}`;

function PayoffChart({ p }: { p: MortgageProjection }) {
  const data = p.points.map((row) => ({
    month: row.month,
    "As you pay": row.as_you_pay !== undefined ? row.as_you_pay / 100 : undefined,
    "Regular repayments only": row.regular_only !== undefined ? row.regular_only / 100 : undefined,
    "What if": row.what_if !== undefined ? row.what_if / 100 : undefined,
  }));
  // A tick each January, thinned to about eight, so the years are evenly spaced.
  const januaries = data.filter((row) => row.month.endsWith("-01")).map((row) => row.month);
  const every = Math.max(1, Math.ceil(januaries.length / 8));
  const ticks = januaries.filter((_, i) => i % every === 0);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>What you'll owe</h2>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid stroke="#28344f" vertical={false} />
          <XAxis
            dataKey="month"
            stroke="#93a1bd"
            fontSize={12}
            ticks={ticks.length > 1 ? ticks : undefined}
            minTickGap={24}
            tickFormatter={(m: string) => m.slice(0, 4)}
          />
          <YAxis stroke="#93a1bd" fontSize={12} width={64} tickFormatter={shortMoney} />
          <Tooltip
            formatter={moneyTip}
            labelFormatter={(m) => monthYear(`${String(m)}-01`)}
            contentStyle={{ background: "#131c2e", border: "1px solid #28344f" }}
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="Regular repayments only"
            stroke="#93a1bd"
            strokeWidth={2}
            dot={false}
          />
          <Line type="monotone" dataKey="As you pay" stroke="#2dd4bf" strokeWidth={2} dot={false} />
          {p.what_if && (
            <Line
              type="monotone"
              dataKey="What if"
              stroke="#f59e0b"
              strokeWidth={2}
              strokeDasharray="5 4"
              dot={false}
            />
          )}
        </LineChart>
      </ResponsiveContainer>
      <ul className="mortgage-payoffs">
        <li>
          <strong>As you pay:</strong> paid off {p.as_you_pay.payoff ? monthYear(p.as_you_pay.payoff) : "never"}
        </li>
        <li>
          <strong>Regular repayments only:</strong> paid off{" "}
          {p.regular_only.payoff ? monthYear(p.regular_only.payoff) : "never"}
        </li>
        {p.what_if && (
          <li>
            <strong>What if:</strong> paid off {p.what_if.payoff ? monthYear(p.what_if.payoff) : "never"}
          </li>
        )}
      </ul>
    </div>
  );
}

interface WhatIfText {
  extra: string;
  lump: string;
  rate: string;
  offset: string;
}

const NO_WHAT_IF: WhatIfText = { extra: "", lump: "", rate: "", offset: "" };

/** The what-if as the API takes it; null while something typed isn't a number. */
function whatIfValues(w: WhatIfText) {
  const money = (t: string) => (t.trim() ? parseDollarsOrZero(t) : 0);
  const extra = money(w.extra);
  const lump = money(w.lump);
  // The offset can go down as well as up: "-5,000".
  const offset = money(w.offset.trim().replace(/^[-+]\s*/, ""));
  const rate = w.rate.trim() ? Number(w.rate.replace(/%$/, "")) : 0;
  if (extra === null || lump === null || offset === null || !Number.isFinite(rate)) return null;
  if (Math.abs(rate) > 10) return null;
  return {
    extra_cents: extra,
    lump_cents: lump,
    rate_change: rate,
    offset_change_cents: w.offset.trim().startsWith("-") ? -offset : offset,
  };
}

function WhatIfPanel({
  p,
  text,
  setText,
  invalid,
}: {
  p: MortgageProjection;
  text: WhatIfText;
  setText: (t: WhatIfText) => void;
  invalid: boolean;
}) {
  const fid = useId();
  const result = p.what_if ? saving(p.as_you_pay, p.what_if) : null;
  const field = (key: keyof WhatIfText, label: string, placeholder: string) => (
    <div className="field">
      <label htmlFor={`${fid}-${key}`}>{label}</label>
      <input
        id={`${fid}-${key}`}
        inputMode="decimal"
        value={text[key]}
        placeholder={placeholder}
        onChange={(e) => setText({ ...text, [key]: e.target.value })}
      />
    </div>
  );
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="spread">
        <h2>What if…</h2>
        {p.what_if && (
          <button type="button" className="btn btn-ghost" onClick={() => setText(NO_WHAT_IF)}>
            Clear
          </button>
        )}
      </div>
      <p className="muted" style={{ marginTop: 0 }}>Try a change. Nothing is saved.</p>
      <div className="row">
        {field("extra", `Extra each ${EVERY_LABELS[p.every].toLowerCase().replace("ly", "")} ($)`, "e.g. 200")}
        {field("lump", "One-off payment now ($)", "e.g. 10,000")}
        {field("rate", "Rate change (%)", "e.g. 0.5 or -0.25")}
        {field("offset", "Add to offset ($)", "e.g. 20,000")}
      </div>
      {invalid && <div className="error">Enter numbers only, e.g. 200.</div>}
      {p.what_if && (
        <p className="notice" role="status">
          {!p.what_if.payoff
            ? "With that, the repayments wouldn't cover the interest."
            : result?.months == null
              ? `With that, it's paid off in ${monthYear(p.what_if.payoff)}.`
              : result.months >= 0
                ? `Paid off ${monthYear(p.what_if.payoff)}: ${span(result.months)} sooner, saving ${formatCents(result.interest ?? 0)} in interest.`
                : `Paid off ${monthYear(p.what_if.payoff)}: ${span(result.months)} later, costing ${formatCents(-(result.interest ?? 0))} more in interest.`}
          {p.what_if_needed_cents !== null &&
            text.rate.trim() &&
            ` Your bank may change your repayment to about ${formatCents(p.what_if_needed_cents)}.`}
        </p>
      )}
    </div>
  );
}

function ScheduleTable({ rows }: { rows: MortgageYear[] }) {
  return (
    <TableWrap min={520} label="Repayment schedule">
      <table>
        <thead>
          <tr>
            <th>Year</th>
            <th className="num">Regular</th>
            <th className="num">Extra</th>
            <th className="num">Interest</th>
            <th className="num">Owing at year end</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((y) => (
            <tr key={y.year}>
              <td>{y.year}</td>
              <td className="num">{formatCents(y.regular_cents)}</td>
              <td className="num">{y.extra_cents ? formatCents(y.extra_cents) : "–"}</td>
              <td className="num">{formatCents(y.interest_cents)}</td>
              <td className="num">{formatCents(y.owing_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

function Summary({ loan, p }: { loan: MortgageData; p: MortgageProjection }) {
  const extras = saving(p.regular_only, p.as_you_pay);
  const offset = saving(p.without_offset, p.as_you_pay);
  const early =
    p.contract_end && p.as_you_pay.payoff ? monthsBetween(p.as_you_pay.payoff, p.contract_end) : null;
  const paidOff =
    loan.original_cents && loan.original_cents > 0
      ? Math.max(0, Math.min(100, Math.round((1 - p.owing_cents / loan.original_cents) * 100)))
      : null;
  return (
    <ul className="mortgage-summary">
      {paidOff !== null && (
        <li>
          You've paid off {paidOff}% of the {formatCents(loan.original_cents ?? 0)} you borrowed.
        </li>
      )}
      {early !== null && p.contract_end && (
        <li>
          Your loan term ends {monthYear(p.contract_end)}.{" "}
          {early > 0
            ? `You're on track to finish ${span(early)} early.`
            : early < 0
              ? `At this rate you'll finish ${span(early)} late.`
              : "You're on track to finish on time."}
        </li>
      )}
      {p.usual_extra_cents > 0 && extras?.months != null && (
        <li>
          Your extra repayments (about {formatCents(p.usual_extra_cents)} a month) save{" "}
          {formatCents(extras.interest ?? 0)} in interest and {span(extras.months)}.
        </li>
      )}
      {p.offset_cents > 0 && offset?.months != null && (
        <li>
          Your offset ({formatCents(p.offset_cents)}) saves {formatCents(offset.interest ?? 0)} in
          interest and {span(offset.months)}.
        </li>
      )}
      {!p.as_you_pay.payoff && (
        <li className="negative">Your repayments don't cover the interest. Check the rate and repayment.</li>
      )}
    </ul>
  );
}

function LoanView({ accountId }: { accountId: string }) {
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [text, setText] = useState<WhatIfText>(NO_WHAT_IF);
  const [applied, setApplied] = useState<WhatIfText>(NO_WHAT_IF);
  const [showWhatIf, setShowWhatIf] = useState(false);
  // Ask once typing pauses, not on every key.
  useEffect(() => {
    const t = setTimeout(() => setApplied(text), 400);
    return () => clearTimeout(t);
  }, [text]);
  const values = whatIfValues(applied);
  const loan = useQuery({
    queryKey: ["mortgage", accountId, values],
    queryFn: () => api.mortgage(accountId, values ?? {}),
    placeholderData: keepPreviousData,
  });

  if (loan.isLoading) return <p className="muted">Loading…</p>;
  if (loan.isError || !loan.data) return <div className="error">Couldn't load your home loan.</div>;
  const data = loan.data;
  const p = data.projection;

  if (!data.set_up || editing) {
    return (
      <MortgageSetup
        key={`${data.account_id}-${editing}`}
        loan={data}
        onDone={(m) => {
          setMessage(m);
          setEditing(false);
        }}
        onCancel={data.set_up ? () => setEditing(false) : undefined}
      />
    );
  }

  return (
    <div>
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      <div className="spread" style={{ marginBottom: 12 }}>
        <span className="muted">
          {p ? `${p.rate}% · ${formatCents(p.next_cents)} ${EVERY_LABELS[p.every].toLowerCase()}` : ""}
        </span>
        <button type="button" className="btn" onClick={() => setEditing(true)}>
          Edit setup
        </button>
      </div>

      {!p ? (
        <EmptyState title="Almost there">
          {data.missing.includes("rate") && "Add your interest rate. "}
          {data.missing.includes("repayment") && "Add your regular repayment. "}
          <button type="button" className="btn btn-ghost" onClick={() => setEditing(true)}>
            Edit setup
          </button>
        </EmptyState>
      ) : (
        <>
          <div className="cards">
            <Stat
              label="You owe"
              value={formatCents(p.owing_cents)}
              sub={p.estimated ? `Estimated from ${formatDate(p.known_on)}` : undefined}
            />
            <Stat
              label="Paid off"
              value={p.as_you_pay.payoff ? monthYear(p.as_you_pay.payoff) : "Never"}
              sub={
                p.as_you_pay.payoff
                  ? `in ${span(monthsBetween(new Date().toISOString().slice(0, 10), p.as_you_pay.payoff))}`
                  : "at this repayment"
              }
            />
            <Stat
              label="Interest still to pay"
              value={p.as_you_pay.payoff ? formatCents(p.as_you_pay.interest_cents) : "–"}
            />
            <Stat
              label="Next repayment"
              value={formatCents(p.next_cents)}
              sub={formatDate(p.next_due)}
            />
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <Summary loan={data} p={p} />
          </div>

          <PayoffChart p={p} />

          <WhatIfPanel p={p} text={text} setText={setText} invalid={values === null} />

          <div className="card" style={{ marginTop: 16 }}>
            <div className="spread">
              <h2>Repayment schedule</h2>
              {p.what_if_schedule && (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={showWhatIf}
                    onChange={(e) => setShowWhatIf(e.target.checked)}
                  />{" "}
                  Show the what-if
                </label>
              )}
            </div>
            <p className="muted" style={{ marginTop: 0 }}>
              Each year from now, {showWhatIf && p.what_if_schedule ? "with your what-if" : "as you pay"}.
            </p>
            <ScheduleTable rows={showWhatIf && p.what_if_schedule ? p.what_if_schedule : p.schedule} />
          </div>
        </>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Repayments made</h2>
        <p className="muted" style={{ marginTop: 0 }}>The last 12 months, regular and extra.</p>
        {data.paid.some((m) => m.regular_cents || m.extra_cents) ? (
          <TableWrap min={360} label="Repayments made">
            <table>
              <thead>
                <tr>
                  <th>Month</th>
                  <th className="num">Regular</th>
                  <th className="num">Extra</th>
                </tr>
              </thead>
              <tbody>
                {data.paid.map((m) => (
                  <tr key={m.month}>
                    <td>{monthYear(`${m.month}-01`)}</td>
                    <td className="num">{m.regular_cents ? formatCents(m.regular_cents) : "–"}</td>
                    <td className="num">{m.extra_cents ? formatCents(m.extra_cents) : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        ) : (
          <p className="muted">
            None found yet. Import your home loan's statement, and set its wording on{" "}
            <Link to="/accounts">Accounts</Link>.
          </p>
        )}
      </div>
    </div>
  );
}

export function Mortgage() {
  const fid = useId();
  const loans = useQuery({ queryKey: ["mortgages"], queryFn: api.mortgages });
  const [chosen, setChosen] = useState<string | null>(null);
  const list = loans.data ?? [];
  const accountId = chosen && list.some((l) => l.account_id === chosen) ? chosen : list[0]?.account_id;

  return (
    <div>
      <PageHead title="Mortgage" sub="When your home loan will be paid off, and how to get there sooner.">
        {list.length > 1 && (
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor={`${fid}-loan`} className="sr-only">
              Home loan
            </label>
            <select id={`${fid}-loan`} value={accountId} onChange={(e) => setChosen(e.target.value)}>
              {list.map((l) => (
                <option key={l.account_id} value={l.account_id}>
                  {l.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </PageHead>
      {loans.isLoading ? (
        <p className="muted">Loading…</p>
      ) : loans.isError ? (
        <div className="error">Couldn't load your home loans.</div>
      ) : !accountId ? (
        <EmptyState title="No home loan yet">
          Add your home loan on <Link to="/accounts">Accounts</Link>, with the type “home loan”.
          Then come back here to set it up.
        </EmptyState>
      ) : (
        <LoanView key={accountId} accountId={accountId} />
      )}
    </div>
  );
}
