import { useMemo, useState } from "react";

import type { Category, RecategoriseScope, Transaction } from "../api/types";

interface Props {
  txn: Transaction;
  categories: Category[];
  busy?: boolean;
  onClose: () => void;
  onSubmit: (body: {
    category_id: string | null;
    scope: RecategoriseScope;
    pattern?: string | null;
    make_rule: boolean;
    lock: boolean;
  }) => void;
}

const SCOPE_LABELS: Record<RecategoriseScope, string> = {
  none: "Only this transaction",
  merchant: "All from this merchant",
  exact: "All with the exact same description",
  contains: "All containing text…",
};

export function CategoriseDialog({ txn, categories, busy, onClose, onSubmit }: Props) {
  const subcategories = useMemo(() => categories.filter((c) => c.parent_id), [categories]);
  const [categoryId, setCategoryId] = useState(txn.category_id ?? "");
  const [scope, setScope] = useState<RecategoriseScope>("none");
  const [pattern, setPattern] = useState(txn.merchant ?? txn.raw_description);
  const [makeRule, setMakeRule] = useState(false);
  const [lock, setLock] = useState(txn.category_locked);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Categorise</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {txn.merchant ?? txn.raw_description}
        </p>

        <div className="field">
          <label>Category</label>
          <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)}>
            <option value="">Uncategorised</option>
            {subcategories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Apply to</label>
          <select value={scope} onChange={(e) => setScope(e.target.value as RecategoriseScope)}>
            {(Object.keys(SCOPE_LABELS) as RecategoriseScope[]).map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s]}
              </option>
            ))}
          </select>
        </div>

        {scope === "contains" && (
          <div className="field">
            <label>Containing text</label>
            <input value={pattern} onChange={(e) => setPattern(e.target.value)} />
          </div>
        )}

        <div className="check-row">
          <input
            id="make-rule"
            type="checkbox"
            checked={makeRule}
            onChange={(e) => setMakeRule(e.target.checked)}
          />
          <label htmlFor="make-rule">Always do this for future imports (create a rule)</label>
        </div>
        <div className="check-row">
          <input
            id="lock-txn"
            type="checkbox"
            checked={lock}
            onChange={(e) => setLock(e.target.checked)}
          />
          <label htmlFor="lock-txn">Lock — exempt from auto-categorisation</label>
        </div>

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() =>
              onSubmit({
                category_id: categoryId || null,
                scope,
                pattern: scope === "contains" ? pattern : null,
                make_rule: makeRule,
                lock,
              })
            }
          >
            {busy ? "Saving…" : "Apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
