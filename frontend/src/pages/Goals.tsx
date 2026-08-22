import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { SavingsGoal } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { dollarsToCents, formatCents, formatDate } from "../format";
import { usePeriod } from "../period/context";
import { PageHead } from "../components/PageHead";

type GoalPatch = {
  target_cents?: number;
  target_date?: string | null;
  current_cents?: number;
};

function GoalCard({
  goal,
  onSave,
  onRemove,
  busy,
}: {
  goal: SavingsGoal;
  onSave: (id: string, patch: GoalPatch) => void;
  onRemove: (id: string) => void;
  busy: boolean;
}) {
  const fid = useId();
  const [editing, setEditing] = useState(false);
  const [target, setTarget] = useState((goal.target_cents / 100).toString());
  const [date, setDate] = useState(goal.target_date ?? "");
  const [current, setCurrent] = useState((goal.current_cents / 100).toString());

  const save = () => {
    const patch: GoalPatch = { target_cents: dollarsToCents(target), target_date: date || null };
    if (!goal.account_id) patch.current_cents = dollarsToCents(current);
    onSave(goal.id, patch);
    setEditing(false);
  };

  return (
    <div className="card">
      <div className="spread">
        <strong>{goal.name}</strong>
        {goal.complete && <span className="status-pill ok">Complete</span>}
      </div>

      <div className="budget-bar">
        <span style={{ width: `${goal.pct_complete * 100}%` }} />
      </div>

      <div className="spread">
        <span>
          {formatCents(goal.current_cents)}{" "}
          <span className="muted">/ {formatCents(goal.target_cents)}</span>
        </span>
        <span className="muted">{(goal.pct_complete * 100).toFixed(0)}%</span>
      </div>
      <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        {goal.remaining_cents > 0 ? `${formatCents(goal.remaining_cents)} to go` : "Reached 🎉"}
        {goal.target_date && <> · by {formatDate(goal.target_date)}</>}
        {goal.account_id && <> · tracks {goal.account_name}</>}
      </div>
      {goal.suggested_per_period_cents > 0 && (
        <div style={{ marginTop: 6, fontSize: 14 }}>
          Suggested: <strong>{formatCents(goal.suggested_per_period_cents)}</strong>
          <span className="muted"> per {goal.period_label}</span>
        </div>
      )}

      {editing ? (
        <div style={{ marginTop: 12 }}>
          <div className="row">
            <div className="field">
              <label htmlFor={`${fid}-target`}>Target ($)</label>
              <input
                id={`${fid}-target`}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                inputMode="decimal"
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-target-date`}>Target date</label>
              <input
                id={`${fid}-target-date`}
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>
            {!goal.account_id && (
              <div className="field">
                <label htmlFor={`${fid}-saved-so-far`}>Saved so far ($)</label>
                <input id={`${fid}-saved-so-far`}
                  value={current}
                  onChange={(e) => setCurrent(e.target.value)}
                  inputMode="decimal"
                />
              </div>
            )}
          </div>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || dollarsToCents(target) <= 0}
              onClick={save}
            >
              Save
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="toolbar" style={{ marginTop: 12 }}>
          <button type="button" className="btn" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => onRemove(goal.id)}
          >
            Remove
          </button>
        </div>
      )}
    </div>
  );
}

export function Goals() {
  const fid = useId();
  const { period } = usePeriod();
  const qc = useQueryClient();
  const goals = useQuery({
    queryKey: ["goals", period],
    queryFn: () => api.goals(period),
  });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });

  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [date, setDate] = useState("");
  const [accountId, setAccountId] = useState("");
  const [current, setCurrent] = useState("");

  const invalidate = () => qc.invalidateQueries({ queryKey: ["goals"] });

  const create = useMutation({
    mutationFn: () =>
      api.createGoal({
        name,
        target_cents: dollarsToCents(target),
        target_date: date || null,
        account_id: accountId || null,
        current_cents: accountId ? 0 : dollarsToCents(current),
      }),
    onSuccess: () => {
      setName("");
      setTarget("");
      setDate("");
      setAccountId("");
      setCurrent("");
      return invalidate();
    },
  });
  const update = useMutation({
    mutationFn: (v: { id: string; patch: GoalPatch }) => api.updateGoal(v.id, v.patch),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteGoal(id),
    onSuccess: invalidate,
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (name && dollarsToCents(target) > 0) create.mutate();
  };

  const list = goals.data ?? [];
  const busy = update.isPending || remove.isPending;

  return (
    <div>
      <PageHead title="Savings goals" />

      {list.length > 0 ? (
        <div className="tiles">
          {list.map((g) => (
            <GoalCard
              key={g.id}
              goal={g}
              busy={busy}
              onSave={(id, patch) => update.mutate({ id, patch })}
              onRemove={(id) => remove.mutate(id)}
            />
          ))}
        </div>
      ) : (
        goals.data && (
          <EmptyState title="No savings goals yet">
            Add one below — link a savings account to track progress automatically, or enter
            an amount by hand. <Link to="/settings">Load demo data</Link> for examples.
          </EmptyState>
        )
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Add a goal</h2>
        <form onSubmit={onSubmit}>
          <div className="row">
            <div className="field">
              <label htmlFor={`${fid}-name`}>Name</label>
              <input id={`${fid}-name`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Emergency fund"
                required
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-target`}>Target ($)</label>
              <input id={`${fid}-target`}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                placeholder="e.g. 20000"
                inputMode="decimal"
                required
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-target-date-optional`}>Target date (optional)</label>
              <input
                id={`${fid}-target-date-optional`}
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>
          </div>
          <div className="row">
            <div className="field">
              <label htmlFor={`${fid}-linked-account-optional`}>Linked account (optional)</label>
              <select
                id={`${fid}-linked-account-optional`}
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
              >
                <option value="">Track manually</option>
                {(accounts.data ?? []).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
            {!accountId && (
              <div className="field">
                <label htmlFor={`${fid}-saved-so-far`}>Saved so far ($)</label>
                <input id={`${fid}-saved-so-far`}
                  value={current}
                  onChange={(e) => setCurrent(e.target.value)}
                  placeholder="0"
                  inputMode="decimal"
                />
              </div>
            )}
          </div>
          {create.isError && <div className="error">Couldn’t add that goal.</div>}
          <button
            className="btn btn-primary"
            disabled={create.isPending || !name || dollarsToCents(target) <= 0}
          >
            Add goal
          </button>
        </form>
      </div>

      {list.length > 0 ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
            gap: 16,
            marginTop: 16,
          }}
        >
          {list.map((g) => (
            <GoalCard
              key={g.id}
              goal={g}
              busy={busy}
              onSave={(id, patch) => update.mutate({ id, patch })}
              onRemove={(id) => remove.mutate(id)}
            />
          ))}
        </div>
      ) : (
        <div className="card" style={{ marginTop: 16 }}>
          <p className="muted">
            No savings goals yet. Add one above — link a savings account to track progress
            automatically, or enter an amount manually. Load demo data from Settings for examples.
          </p>
        </div>
      )}
    </div>
  );
}
