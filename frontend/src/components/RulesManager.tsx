import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { Category, MatchType } from "../api/types";

const MATCH_TYPES: MatchType[] = ["contains", "starts_with", "equals", "merchant", "regex"];

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

interface Props {
  categories: Category[];
  onFlash: (message: string) => void;
}

export function RulesManager({ categories, onFlash }: Props) {
  const qc = useQueryClient();
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules });
  const subcategories = useMemo(() => categories.filter((c) => c.parent_id), [categories]);

  const [matchType, setMatchType] = useState<MatchType>("contains");
  const [pattern, setPattern] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const debouncedPattern = useDebounced(pattern.trim(), 300);

  const preview = useQuery({
    queryKey: ["rule-preview", matchType, debouncedPattern],
    queryFn: () => api.previewRule({ match_type: matchType, pattern: debouncedPattern }),
    enabled: debouncedPattern.length > 0,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["rules"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["txn-groups"] });
    void qc.invalidateQueries({ queryKey: ["summary"] });
    void qc.invalidateQueries({ queryKey: ["breakdown"] });
  };

  const create = useMutation({
    mutationFn: () =>
      api.createRule({ match_type: matchType, pattern: pattern.trim(), category_id: categoryId }),
    onSuccess: () => {
      setPattern("");
      invalidate();
      onFlash("Rule created");
    },
  });
  const update = useMutation({
    mutationFn: (v: { id: string; patch: Parameters<typeof api.updateRule>[1] }) =>
      api.updateRule(v.id, v.patch),
    onSuccess: () => invalidate(),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteRule(id),
    onSuccess: () => {
      invalidate();
      onFlash("Rule deleted");
    },
  });
  const apply = useMutation({
    mutationFn: (id: string) => api.applyRule(id),
    onSuccess: (r) => {
      invalidate();
      onFlash(`Applied to ${r.updated} transaction${r.updated === 1 ? "" : "s"}`);
    },
  });

  const canCreate = pattern.trim().length > 0 && categoryId !== "" && !create.isPending;

  return (
    <div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>New rule</h2>
        <div className="row">
          <div>
            <label>When description…</label>
            <select value={matchType} onChange={(e) => setMatchType(e.target.value as MatchType)}>
              {MATCH_TYPES.map((m) => (
                <option key={m} value={m}>
                  {m.replace("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div style={{ flex: 2 }}>
            <label>Matches</label>
            <input
              placeholder="e.g. woolworths"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
            />
          </div>
          <div>
            <label>Category</label>
            <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)}>
              <option value="">Choose…</option>
              {subcategories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="spread" style={{ marginTop: 12 }}>
          <span className="muted">
            {debouncedPattern.length === 0
              ? "Enter text to preview matches"
              : preview.isLoading
                ? "Checking…"
                : preview.data
                  ? `Matches ${preview.data.matched} · would fill ${preview.data.fillable} uncategorised`
                  : ""}
          </span>
          <button className="btn btn-primary" disabled={!canCreate} onClick={() => create.mutate()}>
            Create rule
          </button>
        </div>
        {preview.data && preview.data.samples.length > 0 && debouncedPattern.length > 0 && (
          <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
            e.g. {preview.data.samples.slice(0, 3).join(" · ")}
          </p>
        )}
      </div>

      <div className="card">
        <h2>Your rules</h2>
        {rules.data && rules.data.length === 0 && <p className="muted">No rules yet.</p>}
        {rules.data && rules.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Active</th>
                <th>When</th>
                <th>Matches</th>
                <th>Category</th>
                <th>Source</th>
                <th className="actions"></th>
              </tr>
            </thead>
            <tbody>
              {rules.data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <input
                      type="checkbox"
                      style={{ width: "auto" }}
                      checked={r.is_active}
                      onChange={(e) =>
                        update.mutate({ id: r.id, patch: { is_active: e.target.checked } })
                      }
                    />
                  </td>
                  <td>
                    <select
                      className="pill-select"
                      value={r.match_type}
                      onChange={(e) =>
                        update.mutate({
                          id: r.id,
                          patch: { match_type: e.target.value as MatchType },
                        })
                      }
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
                      defaultValue={r.pattern}
                      style={{ minWidth: 140 }}
                      onBlur={(e) => {
                        const v = e.target.value.trim();
                        if (v && v !== r.pattern) update.mutate({ id: r.id, patch: { pattern: v } });
                      }}
                    />
                  </td>
                  <td>
                    <select
                      className="pill-select"
                      value={r.category_id}
                      onChange={(e) =>
                        update.mutate({ id: r.id, patch: { category_id: e.target.value } })
                      }
                    >
                      {subcategories.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="muted">{r.source}</td>
                  <td className="actions">
                    <button
                      className="btn btn-ghost"
                      disabled={apply.isPending}
                      onClick={() => apply.mutate(r.id)}
                    >
                      Apply now
                    </button>
                    <button
                      className="btn btn-ghost"
                      onClick={() => {
                        if (window.confirm("Delete this rule?")) remove.mutate(r.id);
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
