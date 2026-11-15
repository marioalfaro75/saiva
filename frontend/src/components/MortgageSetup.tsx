import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { Mortgage, MortgageRate, RepaymentEvery, RepaymentPeriod } from "../api/types";
import { formatCents, formatDate } from "../format";
import { EVERY_LABELS, parseDollars, parseDollarsOrZero, parseSignedDollars } from "./accountTypes";
import { localToday } from "./mortgageText";

interface Props {
  loan: Mortgage;
  onDone: (message: string) => void;
  onCancel?: () => void;
}

const dollars = (cents: number) => (cents / 100).toFixed(2);

interface RateRow {
  since: string;
  percent: string;
}

/** A rate as typed: "6.24" or "6.24%". null when it isn't one. */
function parseRate(text: string): number | null {
  const clean = text.trim().replace(/%$/, "").trim();
  if (!/^\d{1,2}(\.\d{1,3})?$/.test(clean)) return null;
  const n = Number(clean);
  return n <= 30 ? n : null;
}

/**
 * Set up a home loan for the Mortgage page: what was borrowed and when, the term, the
 * rate (and when it changed), the repayment, what is owed now, and the offset accounts.
 * Everything is on the loan statement.
 */
export function MortgageSetup({ loan, onDone, onCancel }: Props) {
  const fid = useId();
  const qc = useQueryClient();
  const today = localToday();
  const [borrowed, setBorrowed] = useState(
    loan.original_cents ? dollars(loan.original_cents) : "",
  );
  const [start, setStart] = useState(loan.start_date ?? "");
  const [years, setYears] = useState(
    loan.term_months
      ? String(Math.round((loan.term_months / 12) * 100) / 100)
      : loan.set_up
        ? "" // saved without one: left without one
        : "30",
  );
  const [rates, setRates] = useState<RateRow[]>(
    loan.rates.length
      ? loan.rates.map((r) => ({ since: r.since ?? "", percent: String(r.percent) }))
      : [{ since: "", percent: "" }],
  );

  // The repayment: earlier periods kept; the latest edited here (as on Accounts).
  const schedule = loan.repayment_schedule;
  const current = schedule.length ? schedule[schedule.length - 1] : null;
  const earlier = schedule.slice(0, -1);
  const [repay, setRepay] = useState(current ? dollars(current.cents) : "");
  const [every, setEvery] = useState<RepaymentEvery>(current?.every ?? "monthly");
  const [repaySince, setRepaySince] = useState(current?.since ?? "");
  const suggestion = loan.repayment_suggestion;

  const [owing, setOwing] = useState("");
  const [owingOn, setOwingOn] = useState(today);

  const chosen = new Set(loan.offsets.map((o) => o.account_id));
  const [offsets, setOffsets] = useState<Record<string, string>>(
    Object.fromEntries(
      loan.offset_choices
        .filter((o) => chosen.has(o.account_id))
        .map((o) => [o.account_id, dollars(o.balance_cents)]),
    ),
  );

  const borrowedCents = borrowed.trim() ? parseDollars(borrowed) : null;
  const termMonths = years.trim() ? Math.round(Number(years) * 12) : null;
  const repayCents = repay.trim() ? parseDollars(repay) : null;
  const owingCents = owing.trim() ? parseDollarsOrZero(owing) : null;
  const rateValues = rates.map((r) => (r.percent.trim() ? parseRate(r.percent) : null));

  const problems: string[] = [];
  if (borrowed.trim() && borrowedCents === null) problems.push("Enter the amount borrowed, e.g. 600,000.");
  if (years.trim() && (!termMonths || termMonths < 1 || termMonths > 600))
    problems.push("Enter the loan term in years, e.g. 30.");
  if (rates.some((r, i) => r.percent.trim() && rateValues[i] === null))
    problems.push("Enter the interest rate as a percentage, e.g. 6.24.");
  if (repay.trim() && repayCents === null) problems.push("Enter the repayment as an amount, e.g. 2,950.");
  if (owing.trim() && owingCents === null) problems.push("Enter what you owe as an amount, e.g. 452,000.");
  if (!loan.set_up && !owing.trim()) problems.push("Enter what you owe now.");
  if (owing.trim() && !owingOn) problems.push("Enter the date for what you owe.");
  if (owingOn > today) problems.push("The date for what you owe can't be in the future.");
  if (Object.values(offsets).some((v) => v.trim() && parseSignedDollars(v) === null))
    problems.push("Enter each offset balance as an amount, e.g. 25,000.");

  const repayment = (): RepaymentPeriod[] | null => {
    if (!repay.trim()) return schedule.length ? [] : null;
    if (repayCents === null) return null;
    const now: RepaymentPeriod = { since: repaySince || null, cents: repayCents, every };
    const keep =
      current && (current.since ?? "") !== (repaySince || "") ? [...earlier, current] : earlier;
    return [...keep.filter((p) => (p.since ?? "") !== (repaySince || "")), now];
  };

  const save = useMutation({
    mutationFn: () => {
      const cleanRates: MortgageRate[] = rates
        .map((r, i) => ({ since: r.since || null, percent: rateValues[i] }))
        .filter((r): r is MortgageRate => r.percent !== null);
      return api.saveMortgage(loan.account_id, {
        original_cents: borrowedCents,
        start_date: start || null,
        term_months: termMonths,
        rates: cleanRates,
        offsets: Object.entries(offsets).map(([account_id, text]) => {
          const before = loan.offset_choices.find((o) => o.account_id === account_id);
          const cents = text.trim() ? parseSignedDollars(text) : null;
          // Only a balance that was changed is sent: the account's own is left alone.
          return {
            account_id,
            balance_cents: cents !== null && cents !== before?.balance_cents ? cents : null,
          };
        }),
        owing_cents: owingCents,
        owing_on: owingCents !== null ? owingOn : null,
        repayment_schedule: repayment(),
      });
    },
    // The page shows the new figures before it says so.
    onSuccess: async () => {
      await qc.invalidateQueries();
      onDone("Saved.");
    },
  });

  const forget = useMutation({
    mutationFn: () => api.deleteMortgage(loan.account_id),
    onSuccess: async () => {
      await qc.invalidateQueries();
      onDone("Setup removed. Your accounts are unchanged.");
    },
  });

  const setRate = (i: number, patch: Partial<RateRow>) =>
    setRates(rates.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="card mortgage-setup">
      <h2>{loan.set_up ? "Edit your home loan" : "Set up your home loan"}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        You'll find these on your loan statement or in your banking app.
      </p>

      <div className="row">
        <div className="field">
          <label htmlFor={`${fid}-borrowed`}>Amount borrowed ($)</label>
          <input
            id={`${fid}-borrowed`}
            inputMode="decimal"
            value={borrowed}
            onChange={(e) => setBorrowed(e.target.value)}
            placeholder="e.g. 600,000"
          />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-start`}>Loan started</label>
          <input
            id={`${fid}-start`}
            type="date"
            value={start}
            max={today}
            onChange={(e) => setStart(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-years`}>Loan term (years)</label>
          <input
            id={`${fid}-years`}
            inputMode="decimal"
            value={years}
            onChange={(e) => setYears(e.target.value)}
            placeholder="e.g. 30"
          />
        </div>
      </div>

      <fieldset className="field">
        <legend>Interest rate</legend>
        {rates.map((r, i) => (
          <div className="row" key={i}>
            <div className="field">
              <label htmlFor={`${fid}-rate-${i}`}>{i === 0 ? "Rate (%)" : `New rate ${i} (%)`}</label>
              <input
                id={`${fid}-rate-${i}`}
                inputMode="decimal"
                value={r.percent}
                onChange={(e) => setRate(i, { percent: e.target.value })}
                placeholder="e.g. 6.24"
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-rate-since-${i}`}>{i === 0 ? "From" : `New rate ${i} from`}</label>
              <input
                id={`${fid}-rate-since-${i}`}
                type="date"
                value={r.since}
                onChange={(e) => setRate(i, { since: e.target.value })}
              />
            </div>
            {rates.length > 1 && (
              <div className="field" style={{ alignSelf: "end" }}>
                <button
                  type="button"
                  className="btn btn-ghost"
                  aria-label={i === 0 ? "Remove the first rate" : `Remove new rate ${i}`}
                  onClick={() => setRates(rates.filter((_, j) => j !== i))}
                >
                  Remove
                </button>
              </div>
            )}
          </div>
        ))}
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setRates([...rates, { since: today, percent: "" }])}
        >
          Add a rate change
        </button>
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
          When the rate changes, add the new rate and the date it started.
        </p>
      </fieldset>

      <fieldset className="field">
        <legend>Regular repayment</legend>
        {suggestion && (
          <p className="notice" role="status">
            Your repayments look like {formatCents(suggestion.cents)}{" "}
            {EVERY_LABELS[suggestion.every].toLowerCase()}.{" "}
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setRepay(dollars(suggestion.cents));
                setEvery(suggestion.every);
                setRepaySince(suggestion.since ?? "");
              }}
            >
              Use this
            </button>
          </p>
        )}
        <div className="row">
          <div className="field">
            <label htmlFor={`${fid}-repay`}>Repayment ($)</label>
            <input
              id={`${fid}-repay`}
              inputMode="decimal"
              value={repay}
              onChange={(e) => setRepay(e.target.value)}
              placeholder="e.g. 2,950"
            />
          </div>
          <div className="field">
            <label htmlFor={`${fid}-every`}>How often</label>
            <select
              id={`${fid}-every`}
              value={every}
              onChange={(e) => setEvery(e.target.value as RepaymentEvery)}
            >
              {(Object.keys(EVERY_LABELS) as RepaymentEvery[]).map((k) => (
                <option key={k} value={k}>
                  {EVERY_LABELS[k]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={`${fid}-repay-since`}>Repayment since</label>
            <input
              id={`${fid}-repay-since`}
              type="date"
              value={repaySince}
              onChange={(e) => setRepaySince(e.target.value)}
            />
          </div>
        </div>
        {earlier.length > 0 && (
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
            Before that:{" "}
            {earlier
              .map(
                (p) =>
                  `${formatCents(p.cents)} ${EVERY_LABELS[p.every].toLowerCase()}` +
                  (p.since ? ` from ${formatDate(p.since)}` : ""),
              )
              .join("; ")}
            .
          </p>
        )}
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
          Your usual repayment. Anything more counts as an extra repayment.
        </p>
      </fieldset>

      <fieldset className="field">
        <legend>What you owe</legend>
        <div className="row">
          <div className="field">
            <label htmlFor={`${fid}-owing`}>You owe ($)</label>
            <input
              id={`${fid}-owing`}
              inputMode="decimal"
              value={owing}
              onChange={(e) => setOwing(e.target.value)}
              placeholder={
                loan.projection ? `Now about ${formatCents(loan.projection.owing_cents)}` : "e.g. 452,000"
              }
            />
          </div>
          <div className="field">
            <label htmlFor={`${fid}-owing-on`}>On</label>
            <input
              id={`${fid}-owing-on`}
              type="date"
              value={owingOn}
              max={today}
              onChange={(e) => setOwingOn(e.target.value)}
            />
          </div>
        </div>
        {loan.set_up && (
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
            Leave it empty to keep what Saiva has.
          </p>
        )}
      </fieldset>

      <fieldset className="field">
        <legend>Offset accounts</legend>
        {loan.offset_choices.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            None yet. Add your offset account on Accounts.
          </p>
        ) : (
          loan.offset_choices.map((o) => {
            const on = o.account_id in offsets;
            return (
              <div className="row" key={o.account_id}>
                <label className="check-row" style={{ alignSelf: "center" }}>
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={(e) => {
                      const next = { ...offsets };
                      if (e.target.checked) next[o.account_id] = dollars(o.balance_cents);
                      else delete next[o.account_id];
                      setOffsets(next);
                    }}
                  />{" "}
                  {o.name}
                </label>
                {on && (
                  <div className="field">
                    <label htmlFor={`${fid}-offset-${o.account_id}`}>
                      {o.name} balance today ($)
                    </label>
                    <input
                      id={`${fid}-offset-${o.account_id}`}
                      inputMode="decimal"
                      value={offsets[o.account_id] ?? ""}
                      onChange={(e) => setOffsets({ ...offsets, [o.account_id]: e.target.value })}
                    />
                  </div>
                )}
              </div>
            );
          })
        )}
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
          Money in an offset account lowers the interest you pay. Fix the balance here if
          Saiva's is wrong.
        </p>
      </fieldset>

      <p className="muted" style={{ fontSize: 13 }}>
        Saving counts this loan and its offset in Net worth.
      </p>

      {problems.length > 0 && (
        <div role="status" aria-live="polite">
          {problems.map((p) => (
            <div key={p} className="error">
              {p}
            </div>
          ))}
        </div>
      )}
      {(save.isError || forget.isError) && (
        <div className="error">
          {(save.error ?? forget.error) instanceof ApiError
            ? `Not saved. ${(save.error ?? forget.error)?.message}`
            : "Not saved."}
        </div>
      )}
      <div className="modal-actions">
        {loan.set_up && (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={forget.isPending}
            onClick={() => {
              if (window.confirm("Remove the setup for this loan? Your accounts stay as they are.")) {
                forget.mutate();
              }
            }}
          >
            Remove setup
          </button>
        )}
        {onCancel && (
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary"
          disabled={save.isPending || problems.length > 0}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
