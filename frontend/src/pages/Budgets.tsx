import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useMemo, useState } from "react";

import { api } from "../api/client";
import type { Budget, Category } from "../api/types";
import { dollarsToCents, formatCents, formatPct } from "../format";

const PERIODS = [
  { value: "monthly", label: "Monthly" },
  { value: "fortnightly", label: "Fortnightly" },
  { value: "annual", label: "Annual" },
];

interface CategoryOption {
  id: string;
  label: string;
}

/** Expense categories as a flat, parent-then-children list, minus ones already budgeted. */
function buildCategoryOptions(categories: Category[], taken: Set<string>): CategoryOption[] {
  const expense = categories.filter((c) => c.kind === "expense");
  const parents = expense.filter((c) => c.parent_id === null).sort((a, b) => a.sort - b.sort);
  const childrenOf = (id: string) =>
    expense.filter((c) => c.parent_id === id).sort((a, b) => a.sort - b.sort);

  const options: CategoryOption[] = [];
  for (const parent of parents) {
    if (!taken.has(parent.id)) options.push({ id: parent.id, label: parent.name });
    for (const child of childrenOf(parent.id)) {
      if (!taken.has(child.id)) options.push({ id: child.id, label: `— ${child.name}` });
    }
  }
  return options;
}

function BudgetCard({
  budget,
  onSave,
  onRemove,
  busy,
}: {
  budget: Budget;
  onSave: (id: string, patch: { period: string; limit_cents: number }) => void;
  onRemove: (id: string) => void;
  busy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [amount, setAmount] = useState((budget.limit_cents / 100).toString());
  const [period, setPeriod] = useState(budget.period);

  const fillWidth = Math.min(budget.pct_used, 1) * 100;
  const overBy = budget.remaining_cents < 0 ? -budget.remaining_cents : 0;

  return (
    <div className="card">
      <div className="spread">
        <div>
          <strong>{budget.category_name}</strong>
          {budget.parent_name && (
            <div className="muted" style={{ fontSize: 12 }}>
              {budget.parent_name}
            </div>
          )}
        </div>
        <span className={`status-pill ${budget.status}`}>{budget.status}</span>
      </div>

      <div className="budget-bar">
        <span className={budget.status} style={{ width: `${fillWidth}%` }} />
      </div>

      <div className="spread">
        <span>
          {formatCents(budget.actual_cents)}{" "}
          <span className="muted">/ {formatCents(budget.limit_cents)}</span>
        </span>
        <span className="muted">{formatPct(budget.pct_used)}</span>
      </div>
      <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        {budget.period_label} ·{" "}
        {overBy > 0 ? (
          <span className="negative">{formatCents(overBy)} over</span>
        ) : (
          `${formatCents(budget.remaining_cents)} left`
        )}
        {budget.status !== "ok" && <> · projected {formatCents(budget.projected_cents)}</>}
      </div>

      {editing ? (
        <div className="row" style={{ marginTop: 12 }}>
          <div className="field">
            <label>Limit ($)</label>
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
          </div>
          <div className="field">
            <label>Period</label>
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              {PERIODS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || dollarsToCents(amount) <= 0}
              onClick={() => {
                onSave(budget.id, { period, limit_cents: dollarsToCents(amount) });
                setEditing(false);
              }}
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
            onClick={() => onRemove(budget.id)}
          >
            Remove
          </button>
        </div>
      )}
    </div>
  );
}

export function Budgets() {
  const qc = useQueryClient();
  const budgets = useQuery({ queryKey: ["budgets"], queryFn: api.budgets });
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });

  const taken = useMemo(
    () => new Set((budgets.data ?? []).map((b) => b.category_id)),
    [budgets.data],
  );
  const options = useMemo(
    () => buildCategoryOptions(categories.data ?? [], taken),
    [categories.data, taken],
  );

  const [categoryId, setCategoryId] = useState("");
  const [period, setPeriod] = useState("monthly");
  const [amount, setAmount] = useState("");

  const invalidate = () => qc.invalidateQueries({ queryKey: ["budgets"] });

  const create = useMutation({
    mutationFn: () =>
      api.createBudget({ category_id: categoryId, period, limit_cents: dollarsToCents(amount) }),
    onSuccess: () => {
      setCategoryId("");
      setAmount("");
      return invalidate();
    },
  });
  const update = useMutation({
    mutationFn: (v: { id: string; patch: { period: string; limit_cents: number } }) =>
      api.updateBudget(v.id, v.patch),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBudget(id),
    onSuccess: invalidate,
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (categoryId && dollarsToCents(amount) > 0) create.mutate();
  };

  const list = budgets.data ?? [];
  const attention = list.filter((b) => b.status !== "ok").length;

  return (
    <div>
      <div className="page-head">
        <h1>Budgets</h1>
        {list.length > 0 && (
          <span className="muted">
            {list.length} budget{list.length === 1 ? "" : "s"}
            {attention > 0 ? ` · ${attention} need attention` : ""}
          </span>
        )}
      </div>

      <div className="card">
        <h2>Add a budget</h2>
        <form onSubmit={onSubmit}>
          <div className="row">
            <div className="field">
              <label>Category</label>
              <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)} required>
                <option value="">Choose a category…</option>
                {options.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Period</label>
              <select value={period} onChange={(e) => setPeriod(e.target.value)}>
                {PERIODS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Limit ($)</label>
              <input
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="e.g. 800"
                inputMode="decimal"
                required
              />
            </div>
          </div>
          {create.isError && (
            <div className="error">
              Couldn’t add that budget — a budget for this category may already exist.
            </div>
          )}
          <button
            className="btn btn-primary"
            disabled={create.isPending || !categoryId || dollarsToCents(amount) <= 0}
          >
            Add budget
          </button>
          {options.length === 0 && categories.data && categories.data.length > 0 && (
            <p className="muted" style={{ marginTop: 8 }}>
              Every expense category already has a budget — thorough!
            </p>
          )}
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
          {list.map((b) => (
            <BudgetCard
              key={b.id}
              budget={b}
              busy={update.isPending || remove.isPending}
              onSave={(id, patch) => update.mutate({ id, patch })}
              onRemove={(id) => remove.mutate(id)}
            />
          ))}
        </div>
      ) : (
        <div className="card" style={{ marginTop: 16 }}>
          <p className="muted">
            No budgets yet. Add one above to track spending against a limit — or load demo data from
            Settings to see it in action.
          </p>
        </div>
      )}
    </div>
  );
}
