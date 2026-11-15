import { useId, useState } from "react";

import { useDismissable } from "../hooks/useDismissable";

import type { Category, RecategoriseScope, Transaction } from "../api/types";
import { CategoryPicker } from "./CategoryPicker";

interface Props {
  txn: Transaction;
  categories: Category[];
  busy?: boolean;
  /** Why the last Apply failed, shown in the window. */
  error?: string | null;
  onClose: () => void;
  onSubmit: (body: {
    category_id: string | null;
    scope: RecategoriseScope;
    pattern?: string | null;
    make_rule: boolean;
    lock: boolean;
    /** What it was for; null clears it. Absent when unchanged. */
    notes?: string | null;
  }) => void;
}

const SCOPE_LABELS: Record<RecategoriseScope, string> = {
  none: "Only this transaction",
  merchant: "All from this merchant",
  exact: "All with the exact same description",
  contains: "All containing text…",
};

export function CategoriseDialog({ txn, categories, busy, error, onClose, onSubmit }: Props) {
  const fid = useId();
  const [categoryId, setCategoryId] = useState(txn.category_id ?? "");
  const [scope, setScope] = useState<RecategoriseScope>("none");
  const [pattern, setPattern] = useState(txn.merchant ?? txn.raw_description);
  const [makeRule, setMakeRule] = useState(false);
  const [lock, setLock] = useState(txn.category_locked);
  const [note, setNote] = useState(txn.notes ?? "");
  // Escape, a focus trap, focus return, and dismissal only when the gesture began
  // outside — so drag-selecting the pattern field no longer discards the edit.
  const panel = useDismissable<HTMLDivElement>(true, onClose);

  return (
    <div className="modal-overlay">
      <div
        className="modal"
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="categorise-title"
      >
        <h2 id="categorise-title">Categorise</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {txn.merchant ?? txn.raw_description}
        </p>

        <div className="field">
          <label htmlFor={`${fid}-category`}>Category</label>
          <CategoryPicker
            id={`${fid}-category`}
            categories={categories}
            value={categoryId}
            onChange={setCategoryId}
            emptyLabel="Uncategorised"
          />
        </div>

        <div className="field">
          <label htmlFor={`${fid}-apply-to`}>Apply to</label>
          <select
            id={`${fid}-apply-to`}
            value={scope}
            onChange={(e) => setScope(e.target.value as RecategoriseScope)}
          >
            {(Object.keys(SCOPE_LABELS) as RecategoriseScope[]).map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s]}
              </option>
            ))}
          </select>
        </div>

        {scope === "contains" && (
          <div className="field">
            <label htmlFor={`${fid}-containing-text`}>Containing text</label>
            <input
              id={`${fid}-containing-text`}
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
            />
          </div>
        )}

        <div className="field">
          <label htmlFor={`${fid}-note`}>Note</label>
          <input
            id={`${fid}-note`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What it was for (optional)"
            maxLength={500}
          />
          {!note && txn.note_suggestion && (
            <button
              type="button"
              className="btn btn-ghost note-suggestion"
              onClick={() => setNote(txn.note_suggestion ?? "")}
            >
              Use “{txn.note_suggestion}”
            </button>
          )}
        </div>

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

        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}

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
                ...(note.trim() !== (txn.notes ?? "") ? { notes: note.trim() || null } : {}),
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
