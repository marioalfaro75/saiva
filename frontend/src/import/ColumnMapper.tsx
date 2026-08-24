import { useId } from "react";

import type { CsvMapping, SniffResult } from "../api/types";
import { ROLE_LABELS, ROLES, type Role, type Roles, whatsMissing } from "./mapping";

const DELIMITERS: { value: string; label: string }[] = [
  { value: ",", label: "Comma" },
  { value: "\t", label: "Tab" },
  { value: ";", label: "Semicolon" },
  { value: "|", label: "Pipe" },
];

/** The first few values of a column, for reading the data instead of the header. */
function samples(rows: string[][], col: number): string {
  const values = rows
    .map((r) => (col < r.length ? r[col].trim() : ""))
    .filter(Boolean)
    .slice(0, 3);
  if (!values.length) return "—";
  return values.join("  ·  ");
}

/**
 * Says what every column in the file is, and lets it be changed.
 *
 * Shown on every import rather than only when the guess looks shaky: a bank that
 * quietly adds or reorders a column would otherwise slip through, and one glance is
 * cheaper than finding out afterwards.
 */
export function ColumnMapper({
  sniff,
  mapping,
  roles,
  detected,
  onRole,
  onMapping,
}: {
  sniff: SniffResult;
  mapping: CsvMapping;
  roles: Roles;
  /** Roles that came from detection, so they can be marked as guesses. */
  detected: Roles;
  onRole: (col: number, role: Role) => void;
  onMapping: (patch: Partial<CsvMapping>) => void;
}) {
  const id = useId();
  const missing = whatsMissing(roles);
  const rows = sniff.sample_rows;

  return (
    <div className="mapper">
      <div className="spread" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>What is in each column?</h2>
        <span className="muted">{sniff.columns.length} columns</span>
      </div>

      <div className="row">
        <div className="field">
          <label htmlFor={`${id}-delim`}>Separated by</label>
          <select
            id={`${id}-delim`}
            value={mapping.delimiter ?? sniff.delimiter}
            onChange={(e) => onMapping({ delimiter: e.target.value })}
          >
            {DELIMITERS.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${id}-header`}>First row</label>
          <select
            id={`${id}-header`}
            value={mapping.has_header ? "header" : "data"}
            onChange={(e) => onMapping({ has_header: e.target.value === "header" })}
          >
            <option value="header">Column names</option>
            <option value="data">A transaction</option>
          </select>
        </div>
      </div>

      <table className="mapper-table">
        <thead>
          <tr>
            <th>Column</th>
            <th>First few values</th>
            <th>This column is…</th>
          </tr>
        </thead>
        <tbody>
          {sniff.columns.map((name, col) => {
            const role = roles[col] ?? "ignore";
            return (
              <tr key={col} className={role === "ignore" ? "ignored" : undefined}>
                <td>{name}</td>
                <td className="muted sample">{samples(rows, col)}</td>
                <td>
                  <select
                    aria-label={`Role for ${name}`}
                    value={role}
                    onChange={(e) => onRole(col, e.target.value as Role)}
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {ROLE_LABELS[r]}
                      </option>
                    ))}
                  </select>
                  {role !== "ignore" && detected[col] === role && (
                    <span className="muted detected"> detected</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {missing ? (
        <div className="notice warn">{missing}</div>
      ) : (
        <p className="muted" style={{ margin: "8px 0 0" }}>
          Everything needed is mapped. Anything marked <strong>Ignore</strong> is left
          out of the import.
        </p>
      )}
    </div>
  );
}
