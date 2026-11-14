import { useEffect, useId, useMemo, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { TABLE_MIN, TableWrap } from "../table/TableWrap";
import { api, ApiError } from "../api/client";
import { FilterRow, FilterToggle } from "../table/FilterRow";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useTable } from "../table/useTable";
import { formatCents, formatDate } from "../format";
import type {
  Category,
  MatchType,
  Rule,
  RuleChange,
  RuleEffect,
  RulePreview,
  RulePreviewRow,
} from "../api/types";
import { CategoryPicker } from "./CategoryPicker";

const MATCH_TYPES: MatchType[] = ["contains", "starts_with", "equals", "merchant", "regex"];

type RulePatch = Parameters<typeof api.updateRule>[1];

/** The rule itself, from a save's answer that also says what the save did. */
const savedRule = (c: RuleChange): Rule => ({
  id: c.id,
  match_type: c.match_type,
  pattern: c.pattern,
  category_id: c.category_id,
  priority: c.priority,
  source: c.source,
  is_active: c.is_active,
});

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

const transactions = (n: number) => `${n} transaction${n === 1 ? "" : "s"}`;

/**
 * One sentence for what a rule change did. Saving and deleting a rule re-file the
 * transactions it reaches, so the person is told what moved -- never a bare "Saved".
 */
function describeEffect(
  effect: RuleEffect,
  { movedTo, takenOver }: { movedTo?: string; takenOver?: boolean } = {},
): string {
  const parts: string[] = [];
  if (effect.categorised > 0) {
    parts.push(
      takenOver
        ? `${transactions(effect.categorised)} now filed by another rule`
        : `${transactions(effect.categorised)} moved${movedTo ? ` to ${movedTo}` : ""}`,
    );
  }
  if (effect.uncategorised > 0) {
    parts.push(`${transactions(effect.uncategorised)} back to Uncategorised`);
  }
  return parts.length > 0 ? `${parts.join(", ")}.` : "No transactions needed changing.";
}

/** The preview's summary: how many it catches, then what happens to them. */
function previewSummary(p: RulePreview, categoryName: string | undefined): string {
  if (p.matched === 0) {
    return "No transactions match yet. The rule will still file future imports that do.";
  }
  const parts: string[] = [];
  if (p.will_change > 0) {
    parts.push(
      categoryName
        ? `${p.will_change} will move to ${categoryName}`
        : `${p.will_change} will be filed once you choose a category`,
    );
  }
  if (p.already > 0) parts.push(`${p.already} already in ${categoryName}`);
  if (p.kept > 0) parts.push(`${p.kept} kept: you chose their category`);
  if (p.locked > 0) parts.push(`${p.locked} kept: locked`);
  if (p.other_rule > 0) parts.push(`${p.other_rule} taken first by another rule`);
  return `Catches ${transactions(p.matched)}: ${parts.join(", ")}.`;
}

function outcomeText(row: RulePreviewRow, categoryName: string | undefined): string {
  switch (row.outcome) {
    case "change":
      return categoryName ? `→ ${categoryName}` : "Will be filed";
    case "same":
      return "Already here";
    case "kept":
      return "Kept: your choice";
    case "locked":
      return "Kept: locked";
    case "other_rule":
      return `Kept: rule “${row.other_rule_pattern}” (${row.other_rule_category_name}) comes first`;
  }
}

interface Props {
  categories: Category[];
  onFlash: (message: string) => void;
}

export function RulesManager({ categories, onFlash }: Props) {
  const fid = useId();
  const qc = useQueryClient();
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules });
  const categoryNames = useMemo(
    () => new Map(categories.map((c) => [c.id, c.name])),
    [categories],
  );

  const [matchType, setMatchType] = useState<MatchType>("contains");
  const [pattern, setPattern] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const debouncedPattern = useDebounced(pattern.trim(), 300);
  const categoryName = categoryId ? categoryNames.get(categoryId) : undefined;

  const preview = useQuery({
    queryKey: ["rule-preview", matchType, debouncedPattern, categoryId],
    queryFn: () =>
      api.previewRule({
        match_type: matchType,
        pattern: debouncedPattern,
        category_id: categoryId || undefined,
      }),
    enabled: debouncedPattern.length > 0,
    // While the next answer is on its way the last one stays up, dimmed, rather than
    // the list collapsing and reappearing under the cursor at every keystroke.
    placeholderData: keepPreviousData,
  });
  const typed = pattern.trim();
  const previewCurrent =
    typed.length > 0 && typed === debouncedPattern && !preview.isPlaceholderData;

  // The rule just made, picked out in the list for a moment so it can be seen arriving.
  const [newRuleId, setNewRuleId] = useState<string | null>(null);
  useEffect(() => {
    if (!newRuleId) return;
    const id = setTimeout(() => setNewRuleId(null), 4000);
    return () => clearTimeout(id);
  }, [newRuleId]);

  // A rule change re-files transactions, which every total, budget and list in the app
  // is built from: everything is refetched, not a guessed list of keys.
  const refresh = () => void qc.invalidateQueries();

  const create = useMutation({
    mutationFn: () =>
      api.createRule({ match_type: matchType, pattern: pattern.trim(), category_id: categoryId }),
    onSuccess: (rule) => {
      setPattern("");
      setNewRuleId(rule.id);
      // In the list at once, not after the refetch: that is where the person looks next.
      qc.setQueryData<Rule[]>(["rules"], (list) => (list ? [...list, savedRule(rule)] : list));
      refresh();
      onFlash(
        `Rule created. ${describeEffect(rule, { movedTo: categoryNames.get(rule.category_id) })}`,
      );
    },
    onError: (error) => {
      onFlash(
        error instanceof ApiError ? `Rule not created. ${error.message}` : "Rule not created.",
      );
    },
  });
  // Edits not yet confirmed by the server, shown in the row meanwhile: a checkbox that
  // does not tick until the server answers reads as one that did not take. Local state,
  // not the query cache, because the cache tells its readers a tick later and the box
  // would visibly spring back first.
  const [unsaved, setUnsaved] = useState<Record<string, RulePatch>>({});
  const settleEdit = (id: string, patch: RulePatch) =>
    setUnsaved((all) => {
      const mine = { ...all[id] };
      for (const key of Object.keys(patch) as (keyof RulePatch)[]) {
        // A later edit to the same field, still on its way, keeps showing.
        if (mine[key] === patch[key]) delete mine[key];
      }
      const next = { ...all };
      if (Object.keys(mine).length > 0) next[id] = mine;
      else delete next[id];
      return next;
    });
  const update = useMutation({
    mutationFn: (v: { id: string; patch: RulePatch }) => api.updateRule(v.id, v.patch),
    onSuccess: (change, { id, patch }) => {
      const rule = savedRule(change);
      qc.setQueryData<Rule[]>(["rules"], (list) => list?.map((r) => (r.id === id ? rule : r)));
      settleEdit(id, patch);
      refresh();
      if (change.categorised > 0 || change.uncategorised > 0) {
        onFlash(`Rule updated. ${describeEffect(change)}`);
      }
    },
    onError: (error, { id, patch }) => {
      settleEdit(id, patch);
      onFlash(
        error instanceof ApiError && error.status === 422
          ? `That change to the rule wasn't saved. ${error.message}`
          : "That change to the rule wasn't saved.",
      );
    },
  });
  const remove = useMutation({
    mutationFn: (rule: Rule) => api.deleteRule(rule.id),
    onError: () => onFlash("The rule wasn't deleted."),
    onSuccess: (effect, rule) => {
      qc.setQueryData<Rule[]>(["rules"], (list) => list?.filter((r) => r.id !== rule.id));
      refresh();
      onFlash(`Rule deleted. ${describeEffect(effect, { takenOver: true })}`);
    },
  });

  const canCreate = typed.length > 0 && categoryId !== "" && !create.isPending;
  const shown = (rules.data ?? []).map((r) => (unsaved[r.id] ? { ...r, ...unsaved[r.id] } : r));
  const own = shown.filter((r) => r.source === "user");
  const builtIn = shown.filter((r) => r.source !== "user");
  const tableProps = {
    categories,
    categoryNames,
    onUpdate: (id: string, patch: RulePatch) => {
      setUnsaved((all) => ({ ...all, [id]: { ...all[id], ...patch } }));
      update.mutate({ id, patch });
    },
    onDelete: (rule: Rule) => {
      if (
        window.confirm(
          `Delete the rule “${rule.pattern}”? Transactions it filed go back to ` +
            "Uncategorised, unless another rule catches them.",
        )
      ) {
        remove.mutate(rule);
      }
    },
  };

  return (
    <div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>New rule</h2>
        <div className="row">
          <div>
            <label htmlFor={`${fid}-when-description`}>When description…</label>
            <select
              id={`${fid}-when-description`}
              value={matchType}
              onChange={(e) => setMatchType(e.target.value as MatchType)}
            >
              {MATCH_TYPES.map((m) => (
                <option key={m} value={m}>
                  {m.replace("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div style={{ flex: 2 }}>
            <label htmlFor={`${fid}-matches`}>Matches</label>
            <input
              id={`${fid}-matches`}
              placeholder="e.g. woolworths"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor={`${fid}-category`}>Category</label>
            <CategoryPicker
              id={`${fid}-category`}
              categories={categories}
              value={categoryId}
              onChange={setCategoryId}
              placeholder="Choose…"
            />
          </div>
        </div>
        <div className="spread" style={{ marginTop: 12 }}>
          <span className="muted" role="status">
            {typed.length === 0
              ? "Type what to match to see the transactions it catches."
              : !previewCurrent
                ? "Checking…"
                : preview.data
                  ? previewSummary(preview.data, categoryName)
                  : preview.isError
                    ? preview.error instanceof ApiError && preview.error.status === 422
                      ? preview.error.message
                      : "Couldn't check that pattern."
                    : ""}
          </span>
          <button className="btn btn-primary" disabled={!canCreate} onClick={() => create.mutate()}>
            Create rule
          </button>
        </div>
        {typed.length > 0 && preview.data && preview.data.rows.length > 0 && (
          <RulePreviewList
            preview={preview.data}
            categoryName={categoryName}
            stale={!previewCurrent}
          />
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Your rules</h2>
        {rules.data && own.length === 0 && (
          <p className="muted">No rules of your own yet. Make one above.</p>
        )}
        {own.length > 0 && (
          <RuleTable
            rules={own}
            tableId="rules"
            label="Your rules"
            highlightId={newRuleId}
            {...tableProps}
          />
        )}
      </div>

      {builtIn.length > 0 && (
        <details className="card rules-builtin">
          <summary>
            <strong>Built-in rules</strong>{" "}
            <span className="muted">· {builtIn.length}</span>
          </summary>
          <p className="muted">
            The rules the app starts with. Where one of yours matches the same transaction,
            yours wins.
          </p>
          <RuleTable
            rules={builtIn}
            tableId="rules-built-in"
            label="Built-in rules"
            highlightId={null}
            {...tableProps}
          />
        </details>
      )}
    </div>
  );
}

/** The transactions a prospective rule catches, and what it would do to each. */
function RulePreviewList({
  preview,
  categoryName,
  stale,
}: {
  preview: RulePreview;
  categoryName: string | undefined;
  stale: boolean;
}) {
  return (
    <div className={stale ? "rule-preview is-stale" : "rule-preview"} aria-busy={stale}>
      <div
        className="rule-preview-scroll"
        tabIndex={0}
        role="region"
        aria-label="Transactions this rule catches"
      >
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Description</th>
              <th className="num">Amount</th>
              <th>Category now</th>
              <th>With this rule</th>
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row) => (
              <tr key={row.id} className={`rule-outcome-${row.outcome}`}>
                <td className="muted">{formatDate(row.txn_date)}</td>
                <td>{row.description}</td>
                <td className={`num ${row.amount_cents < 0 ? "negative" : "positive"}`}>
                  {formatCents(row.amount_cents)}
                </td>
                <td className="muted">{row.category_name ?? "Uncategorised"}</td>
                <td className="rule-outcome">{outcomeText(row, categoryName)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview.matched > preview.rows.length && (
        <p className="muted" style={{ margin: "6px 0 0", fontSize: 13 }}>
          Showing the latest {preview.rows.length} of {preview.matched}.
        </p>
      )}
    </div>
  );
}

const RULE_LABELS = {
  active: "Active",
  when: "When",
  pattern: "Matches",
  category: "Category",
};

function RuleTable({
  rules,
  tableId,
  label,
  highlightId,
  categories,
  categoryNames,
  onUpdate,
  onDelete,
}: {
  rules: Rule[];
  tableId: string;
  label: string;
  highlightId: string | null;
  categories: Category[];
  categoryNames: Map<string, string>;
  onUpdate: (id: string, patch: RulePatch) => void;
  onDelete: (rule: Rule) => void;
}) {
  // Category sorts by name rather than id, which is what the row shows.
  const columns = useMemo<ColumnSpec<Rule>[]>(
    () => [
      { key: "active", sort: (r) => r.is_active, text: (r) => (r.is_active ? "active" : "off") },
      { key: "when", sort: (r) => r.match_type.replace("_", " ") },
      { key: "pattern", sort: (r) => r.pattern },
      { key: "category", sort: (r) => categoryNames.get(r.category_id) ?? "" },
    ],
    [categoryNames],
  );
  const table = useTable(rules, columns, { id: tableId });

  return (
    // One group per list, so its Filter button and filter fields are announced as
    // belonging to it: the tab shows two of each.
    <div role="group" aria-label={label}>
      <div className="spread">
        <span />
        <FilterToggle table={table} />
      </div>
      <TableWrap min={TABLE_MIN.rules} label={`${label}, table`}>
        <table>
          <thead>
            <tr>
              <SortHeader table={table} col="active">
                Active
              </SortHeader>
              <SortHeader table={table} col="when">
                When
              </SortHeader>
              <SortHeader table={table} col="pattern">
                Matches
              </SortHeader>
              <SortHeader table={table} col="category">
                Category
              </SortHeader>
              <th className="actions"></th>
            </tr>
            <FilterRow
              table={table}
              labels={RULE_LABELS}
              columns={["active", "when", "pattern", "category", null]}
            />
          </thead>
          <tbody>
            {table.rows.map((r) => (
              <tr key={r.id} className={r.id === highlightId ? "rule-new" : undefined}>
                <td>
                  <input
                    type="checkbox"
                    style={{ width: "auto" }}
                    aria-label={`Rule “${r.pattern}” is active`}
                    checked={r.is_active}
                    onChange={(e) => onUpdate(r.id, { is_active: e.target.checked })}
                  />
                </td>
                <td>
                  <select
                    className="pill-select"
                    aria-label={`How “${r.pattern}” matches`}
                    value={r.match_type}
                    onChange={(e) => onUpdate(r.id, { match_type: e.target.value as MatchType })}
                  >
                    {MATCH_TYPES.map((m) => (
                      <option key={m} value={m}>
                        {m.replace("_", " ")}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    // Keyed on the saved pattern, so a save from elsewhere replaces what
                    // is shown instead of leaving the old text in an uncontrolled field.
                    key={r.pattern}
                    aria-label={`Text the rule “${r.pattern}” matches`}
                    defaultValue={r.pattern}
                    style={{ minWidth: 140 }}
                    onBlur={(e) => {
                      const v = e.target.value.trim();
                      if (v && v !== r.pattern) onUpdate(r.id, { pattern: v });
                    }}
                  />
                </td>
                <td>
                  <CategoryPicker
                    className="pill-select"
                    aria-label={`Category for “${r.pattern}”`}
                    categories={categories}
                    value={r.category_id}
                    onChange={(id) => onUpdate(r.id, { category_id: id })}
                  />
                </td>
                <td className="actions">
                  <button className="btn btn-ghost" onClick={() => onDelete(r)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>
    </div>
  );
}
