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

import { api, ApiError } from "../api/client";
import type {
  Mortgage as MortgageData,
  MortgageOutcome,
  MortgageProjection,
  MortgageWhatIf,
  MortgageYear,
} from "../api/types";
import { EVERY_LABELS, parseDollarsOrZero } from "../components/accountTypes";
import { EmptyState } from "../components/EmptyState";
import { MortgageSetup } from "../components/MortgageSetup";
import { localToday, monthsApart, monthYear, span } from "../components/mortgageText";
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

/** "this month", or "in 2 years 3 months". */
const fromNow = (months: number) => (months <= 0 ? "this month" : `in ${span(months)}`);

/** "paid off Mar 2041", or why it isn't. */
function paidOff(o: MortgageOutcome): string {
  if (o.payoff) return `paid off ${monthYear(o.payoff)}`;
  return o.stalls ? "never (the repayments don't cover the interest)" : "not within 50 years";
}

/** How much sooner `better` is paid off than `base` (in the months shown), and the
 *  interest it saves; null unless both are paid off. */
function saving(base: MortgageOutcome, better: MortgageOutcome) {
  if (!better.payoff || !base.payoff) return null;
  return {
    months: monthsApart(better.payoff, base.payoff),
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
  // A tick each January, thinned to about eight, so the years are evenly spaced. With
  // under two years to go, the months are named instead.
  const januaries = data.filter((row) => row.month.endsWith("-01")).map((row) => row.month);
  const yearly = januaries.length > 1;
  const step = Math.max(1, Math.ceil(januaries.length / 8));
  const ticks = januaries.filter((_, i) => i % step === 0);
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
            ticks={yearly ? ticks : undefined}
            minTickGap={24}
            tickFormatter={(m: string) => (yearly ? m.slice(0, 4) : monthYear(`${m}-01`))}
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
          <strong>As you pay:</strong> {paidOff(p.as_you_pay)}
        </li>
        <li>
          <strong>Regular repayments only:</strong> {paidOff(p.regular_only)}
        </li>
        {p.what_if && (
          <li>
            <strong>What if:</strong> {paidOff(p.what_if)}
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
  rateUp: boolean;
  offset: string;
  offsetIn: boolean;
}

const NO_WHAT_IF: WhatIfText = {
  extra: "",
  lump: "",
  rate: "",
  rateUp: true,
  offset: "",
  offsetIn: true,
};

/** The what-if as the API takes it, or what is wrong with what was typed. Every field
 *  is a plain number (phones' number pads have no minus key): a rate cut and money
 *  taken out of the offset are chosen beside them. */
function whatIfValues(w: WhatIfText): { values: MortgageWhatIf } | { problem: string } {
  const money = (t: string) => (t.trim() ? parseDollarsOrZero(t) : 0);
  const extra = money(w.extra);
  const lump = money(w.lump);
  const offset = money(w.offset);
  const rateText = w.rate.trim().replace(/%$/, "").trim();
  const rate = rateText ? Number(rateText) : 0;
  if (extra === null || lump === null || offset === null || !/^(\d+(\.\d*)?|\.\d+)?$/.test(rateText)) {
    return { problem: "Enter numbers only, e.g. 200." };
  }
  if (rate > 10) return { problem: "Try a rate change of up to 10%, e.g. 0.5." };
  return {
    values: {
      extra_cents: extra,
      lump_cents: lump,
      rate_change: w.rateUp ? rate : -rate,
      offset_change_cents: w.offsetIn ? offset : -offset,
    },
  };
}

const isEmpty = (w: WhatIfText) => !w.extra.trim() && !w.lump.trim() && !w.rate.trim() && !w.offset.trim();

function whatIfResult(p: MortgageProjection, tried: MortgageWhatIf): string | null {
  const w = p.what_if;
  if (!w) return null;
  const result = saving(p.as_you_pay, w);
  let text: string;
  if (!w.payoff) {
    text = w.stalls
      ? "With that, the repayments wouldn't cover the interest."
      : "With that, it isn't paid off within 50 years.";
  } else if (!result) {
    text = `With that, it's paid off ${monthYear(w.payoff)}.`;
  } else {
    const when =
      result.months > 0
        ? `${span(result.months)} sooner`
        : result.months < 0
          ? `${span(-result.months)} later`
          : "about the same time";
    const money =
      result.interest > 0
        ? `, saving ${formatCents(result.interest)} in interest`
        : result.interest < 0
          ? `, costing ${formatCents(-result.interest)} more in interest`
          : "";
    text = `Paid off ${monthYear(w.payoff)}: ${when}${money}.`;
  }
  if (tried.rate_change && p.what_if_needed_cents !== null) {
    text += ` At that rate, your bank may set your repayment to about ${formatCents(p.what_if_needed_cents)}.`;
  }
  return text;
}

function WhatIfPanel({
  p,
  text,
  setText,
  onClear,
  problem,
  tried,
  working,
  failed,
}: {
  p: MortgageProjection;
  text: WhatIfText;
  setText: (t: WhatIfText) => void;
  onClear: () => void;
  problem: string | null;
  tried: MortgageWhatIf;
  working: boolean;
  failed: string | null;
}) {
  const fid = useId();
  const field = (key: "extra" | "lump" | "rate" | "offset", label: string, placeholder: string) => (
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
  const result = whatIfResult(p, tried);
  const period = EVERY_LABELS[p.every].toLowerCase().replace("ly", "");
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="spread">
        <h2>What if…</h2>
        <button type="button" className="btn btn-ghost" disabled={isEmpty(text)} onClick={onClear}>
          Clear
        </button>
      </div>
      <p className="muted" style={{ marginTop: 0 }}>Try a change. Nothing is saved.</p>
      <div className="row">
        {field("extra", `Extra each ${period} ($)`, "e.g. 200")}
        {field("lump", "One-off payment now ($)", "e.g. 10,000")}
        <div className="field">
          <label htmlFor={`${fid}-rate-dir`}>Rate</label>
          <select
            id={`${fid}-rate-dir`}
            value={text.rateUp ? "up" : "down"}
            onChange={(e) => setText({ ...text, rateUp: e.target.value === "up" })}
          >
            <option value="up">Goes up</option>
            <option value="down">Goes down</option>
          </select>
        </div>
        {field("rate", "Rate change (%)", "e.g. 0.5")}
        <div className="field">
          <label htmlFor={`${fid}-offset-dir`}>Offset</label>
          <select
            id={`${fid}-offset-dir`}
            value={text.offsetIn ? "in" : "out"}
            onChange={(e) => setText({ ...text, offsetIn: e.target.value === "in" })}
          >
            <option value="in">Put in</option>
            <option value="out">Take out</option>
          </select>
        </div>
        {field("offset", "Offset amount ($)", "e.g. 20,000")}
      </div>
      <div role="status" aria-live="polite">
        {problem ? (
          <div className="error">{problem}</div>
        ) : failed ? (
          <div className="error">{failed}</div>
        ) : working ? (
          <p className="notice">Working it out…</p>
        ) : (
          result && <p className="notice">{result}</p>
        )}
      </div>
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
  const made = p.without_extras_made ? saving(p.without_extras_made, p.regular_only) : null;
  const offset = saving(p.without_offset, p.as_you_pay);
  const early =
    p.contract_end && p.as_you_pay.payoff ? monthsApart(p.as_you_pay.payoff, p.contract_end) : null;
  const ended = p.contract_end !== null && p.contract_end < localToday();
  /** "pays the loan off 1 year sooner and saves $X in interest", or the half of it
   *  that is true; null when neither is. */
  const effect = (
    s: { months: number; interest: number } | null,
    sooner: (time: string) => string,
    saves: string,
  ) => {
    if (!s || (s.months <= 0 && s.interest <= 0)) return null;
    const parts = [];
    if (s.months > 0) parts.push(sooner(span(s.months)));
    if (s.interest > 0) parts.push(`${saves} ${formatCents(s.interest)} in interest`);
    return parts.join(" and ");
  };
  const madeEffect = effect(made, (t) => `bring your pay-off forward ${t}`, "save");
  const usualEffect = effect(extras, (t) => `pay it off ${t} sooner`, "save");
  const offsetEffect = effect(offset, (t) => `pays it off ${t} sooner`, "saves");
  const paidOffShare =
    loan.original_cents && loan.original_cents > 0
      ? Math.max(0, Math.min(100, Math.round((1 - p.owing_cents / loan.original_cents) * 100)))
      : null;
  return (
    <ul className="mortgage-summary">
      {paidOffShare !== null && (
        <li>
          You've paid off {paidOffShare}% of the {formatCents(loan.original_cents ?? 0)} you borrowed.
        </li>
      )}
      {early !== null && p.contract_end && (
        <li>
          Your loan term {ended ? "ended" : "ends"} {monthYear(p.contract_end)}.{" "}
          {early > 0
            ? `You're on track to finish ${span(early)} early.`
            : early < 0
              ? `At this rate you'll finish ${span(-early)} late.`
              : "You're on track to finish on time."}
        </li>
      )}
      {madeEffect && (p.extras_made_cents ?? 0) > 0 && (
        <li>
          Your extra repayments in the last 12 months ({formatCents(p.extras_made_cents ?? 0)}){" "}
          {madeEffect}.
        </li>
      )}
      {usualEffect && p.usual_extra_cents > 0 && (
        <li>
          Your usual extra repayments (about {formatCents(p.usual_extra_cents)} a month){" "}
          {usualEffect}.
        </li>
      )}
      {offsetEffect && p.offset_cents > 0 && (
        <li>
          Your offset ({formatCents(p.offset_cents)}){" "}
          {offsetEffect}.
        </li>
      )}
      {!p.as_you_pay.payoff && (
        <li className="negative">
          {p.as_you_pay.stalls
            ? "Your repayments don't cover the interest. Check the rate and repayment."
            : "At these repayments it isn't paid off within 50 years. Check the rate and repayment."}
        </li>
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
  const parsed = whatIfValues(applied);
  const values = "values" in parsed ? parsed.values : null;
  const trying =
    values !== null &&
    Boolean(values.extra_cents || values.lump_cents || values.rate_change || values.offset_change_cents);
  // The loan as it is; and, apart, the what-if. A what-if that fails leaves the page.
  const base = useQuery({ queryKey: ["mortgage", accountId], queryFn: () => api.mortgage(accountId) });
  const whatIf = useQuery({
    queryKey: ["mortgage", accountId, values],
    queryFn: () => api.mortgage(accountId, values ?? {}),
    enabled: trying,
    // While the next answer comes, keep the last one only if there was one.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.state.data ? keepPreviousData(previous) : undefined,
  });
  const clear = () => {
    setText(NO_WHAT_IF);
    setApplied(NO_WHAT_IF);
  };
  const whatIfFailed = !trying || !whatIf.isError
    ? null
    : whatIf.error instanceof ApiError && whatIf.error.status < 500
      ? "That's too large to try."
      : "Couldn't try that. Please try again.";

  if (base.isLoading) return <p className="muted">Loading…</p>;
  if (!base.data) return <div className="error">Couldn't load your home loan.</div>;
  const data = (trying && whatIf.data) || base.data;
  const p = data.projection;
  const notice = message && (
    <p className="notice" role="status">
      {message}
    </p>
  );

  if (!data.set_up || editing) {
    return (
      <>
        {notice}
        <MortgageSetup
          key={`${data.account_id}-${editing}`}
          loan={base.data}
          onDone={(m) => {
            setMessage(m);
            setEditing(false);
          }}
          onCancel={data.set_up ? () => setEditing(false) : undefined}
        />
      </>
    );
  }

  const doubled = data.manual_liabilities ?? [];
  return (
    <div>
      {notice}
      {base.isError && (
        <p className="notice notice-warn" role="status">
          Couldn't refresh. Showing what Saiva had.
        </p>
      )}
      {doubled.length > 0 && (
        <p className="notice notice-warn" role="status">
          <Link to="/net-worth">Net worth</Link> also has {doubled.map((n) => `“${n}”`).join(", ")}{" "}
          entered by hand. If that's this loan, remove it there so it isn't counted twice.
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
              value={p.as_you_pay.payoff ? monthYear(p.as_you_pay.payoff) : p.as_you_pay.stalls ? "Never" : "50+ years"}
              sub={
                p.as_you_pay.payoff
                  ? fromNow(monthsApart(localToday(), p.as_you_pay.payoff))
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

          <WhatIfPanel
            p={p}
            text={text}
            setText={setText}
            onClear={clear}
            problem={"problem" in parsed ? parsed.problem : null}
            tried={values ?? {}}
            working={trying && (whatIf.isPlaceholderData || (whatIf.isFetching && !whatIf.data))}
            failed={whatIfFailed}
          />

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
