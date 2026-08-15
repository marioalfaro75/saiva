import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../api/client";
import type { CsvMapping, ImportPreview, PreviewRow, SniffResult } from "../api/types";
import { formatCents, formatDate } from "../format";

function ColSelect({
  cols,
  value,
  onChange,
}: {
  cols: string[];
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <select value={value} onChange={(e) => onChange(Number(e.target.value))}>
      {cols.map((c, i) => (
        <option key={i} value={i}>
          {c}
        </option>
      ))}
    </select>
  );
}

function guessFormat(file: File): string {
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".qfx")) return "qfx";
  if (lower.endsWith(".ofx")) return "ofx";
  return "csv";
}

export function ImportPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [file, setFile] = useState<File | null>(null);
  const [accountId, setAccountId] = useState("");
  const [format, setFormat] = useState("csv");
  const [sniff, setSniff] = useState<SniffResult | null>(null);
  const [mapping, setMapping] = useState<CsvMapping | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  // Reviewer overrides of the preview's default verdict, keyed by row index.
  const [decisions, setDecisions] = useState<Record<number, boolean>>({});
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setSniff(null);
    setMapping(null);
    setPreview(null);
    setDecisions({});
    setResult(null);
    setError(null);
  };

  const onFile = async (f: File | null) => {
    setFile(f);
    reset();
    if (!f) return;
    const fmt = guessFormat(f);
    setFormat(fmt);
    if (fmt === "csv") {
      try {
        const s = await api.sniff(f);
        setSniff(s);
        setMapping(s.suggested_mapping);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Could not read file");
      }
    }
  };

  const setMap = (patch: Partial<CsvMapping>) => {
    if (mapping) setMapping({ ...mapping, ...patch });
  };

  // A row imports unless the reviewer said otherwise; definite duplicates never do.
  const willImport = (r: PreviewRow) => decisions[r.row_index] ?? r.will_import;
  const canDecide = (r: PreviewRow) => r.status === "new" || r.status === "duplicate_probable";

  const run = async (commit: boolean) => {
    if (!file || !accountId) return;
    setBusy(true);
    setError(null);
    try {
      const csvMapping = format === "csv" ? mapping : null;
      if (commit) {
        const rows = preview?.rows ?? [];
        const r = await api.commit(file, accountId, format, csvMapping, {
          forceImport: rows
            .filter((row) => row.status === "duplicate_probable" && willImport(row))
            .map((row) => row.row_index),
          forceSkip: rows
            .filter((row) => row.status === "new" && !willImport(row))
            .map((row) => row.row_index),
        });
        setResult(
          `Imported ${r.added} transactions — ${r.skipped} skipped, ${r.transfers_linked} transfers linked.`,
        );
        setFile(null);
        reset();
      } else {
        setPreview(await api.preview(file, accountId, format, csvMapping));
        setDecisions({});
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Import failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <h1>Import transactions</h1>
      </div>
      {error && <div className="error">{error}</div>}
      {result && <div className="notice">{result}</div>}

      <div className="card">
        <div className="row">
          <div className="field">
            <label>Account</label>
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              <option value="">Choose account…</option>
              {accounts.data?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>File (CSV, OFX or QFX)</label>
            <input
              type="file"
              accept=".csv,.ofx,.qfx"
              onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
            />
          </div>
        </div>

        {sniff && mapping && (
          <>
            <h2 style={{ marginTop: 12 }}>Column mapping</h2>
            <div className="row">
              <div className="field">
                <label>Date column</label>
                <ColSelect
                  cols={sniff.columns}
                  value={mapping.date_col}
                  onChange={(v) => setMap({ date_col: v })}
                />
              </div>
              <div className="field">
                <label>Description column</label>
                <ColSelect
                  cols={sniff.columns}
                  value={mapping.description_col}
                  onChange={(v) => setMap({ description_col: v })}
                />
              </div>
              <div className="field">
                <label>Amount format</label>
                <select
                  value={mapping.amount_mode}
                  onChange={(e) =>
                    setMap({ amount_mode: e.target.value as "single" | "debit_credit" })
                  }
                >
                  <option value="single">Single signed column</option>
                  <option value="debit_credit">Separate debit / credit</option>
                </select>
              </div>
            </div>
            <div className="row">
              {mapping.amount_mode === "single" ? (
                <div className="field">
                  <label>Amount column</label>
                  <ColSelect
                    cols={sniff.columns}
                    value={mapping.amount_col ?? 0}
                    onChange={(v) => setMap({ amount_col: v })}
                  />
                </div>
              ) : (
                <>
                  <div className="field">
                    <label>Debit column</label>
                    <ColSelect
                      cols={sniff.columns}
                      value={mapping.debit_col ?? 0}
                      onChange={(v) => setMap({ debit_col: v })}
                    />
                  </div>
                  <div className="field">
                    <label>Credit column</label>
                    <ColSelect
                      cols={sniff.columns}
                      value={mapping.credit_col ?? 0}
                      onChange={(v) => setMap({ credit_col: v })}
                    />
                  </div>
                </>
              )}
            </div>
          </>
        )}

        <div className="toolbar" style={{ marginTop: 12 }}>
          <button className="btn" onClick={() => void run(false)} disabled={!file || !accountId || busy}>
            Preview
          </button>
          <button
            className="btn btn-primary"
            onClick={() => void run(true)}
            disabled={!file || !accountId || busy}
          >
            Import
          </button>
        </div>
      </div>

      {preview && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="spread">
            <h2>Preview</h2>
            <span className="muted">
              {preview.rows.filter(willImport).length} to import · {preview.duplicate_count}{" "}
              duplicate{preview.duplicate_count === 1 ? "" : "s"}
              {preview.probable_count > 0 && ` · ${preview.probable_count} to review`}
            </span>
          </div>
          {preview.probable_count > 0 && (
            <p className="muted" style={{ marginTop: 0 }}>
              Rows marked <strong>Possible duplicate</strong> look like transactions you already
              have — same amount, around the same date. They are left out by default; tick one to
              import it anyway.
            </p>
          )}
          <table>
            <thead>
              <tr>
                <th style={{ width: 32 }}>
                  <span className="sr-only">Import</span>
                </th>
                <th>Date</th>
                <th>Description</th>
                <th>Suggested category</th>
                <th className="num">Amount</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {preview.rows.slice(0, 200).map((r) => (
                <tr key={r.row_index} style={{ opacity: willImport(r) ? 1 : 0.55 }}>
                  <td>
                    <input
                      type="checkbox"
                      checked={willImport(r)}
                      disabled={!canDecide(r)}
                      title={
                        canDecide(r)
                          ? "Import this row"
                          : "Already imported — importing it again would create a duplicate"
                      }
                      onChange={(e) =>
                        setDecisions((d) => ({ ...d, [r.row_index]: e.target.checked }))
                      }
                    />
                  </td>
                  <td className="muted">{formatDate(r.txn_date)}</td>
                  <td>{r.merchant ?? r.raw_description}</td>
                  <td className="muted">{r.suggested_category_name ?? "—"}</td>
                  <td className={`num ${r.amount_cents < 0 ? "negative" : "positive"}`}>
                    {formatCents(r.amount_cents)}
                  </td>
                  <td>
                    {r.status === "new" ? (
                      <span className="muted">New</span>
                    ) : r.status === "duplicate_probable" ? (
                      <span
                        className="status-pill warning"
                        title={
                          r.matched_date
                            ? `Matches ${formatDate(r.matched_date)} — ${r.matched_description ?? ""}`
                            : undefined
                        }
                      >
                        Possible duplicate
                      </span>
                    ) : (
                      <span className="tag" title={r.duplicate_reason ?? undefined}>
                        Already imported
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {preview.rows.length > 200 && (
            <p className="muted">Showing the first 200 of {preview.rows.length} rows.</p>
          )}
        </div>
      )}
    </div>
  );
}
