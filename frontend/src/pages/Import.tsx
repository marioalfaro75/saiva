import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../api/client";
import type {
  AccountAssignment,
  AccountScanRow,
  CsvMapping,
  ImportPreview,
  PreviewRow,
  SniffResult,
} from "../api/types";
import { formatCents, formatDate } from "../format";

const ACCOUNT_TYPES = [
  "everyday",
  "savings",
  "credit_card",
  "home_loan",
  "offset",
  "personal_loan",
  "cash",
  "investment",
];

/** What the user chose to do with one distinct value of the account column. */
type Choice =
  | { mode: "" }
  | { mode: "account"; accountId: string }
  | { mode: "create"; name: string; type: string }
  | { mode: "skip" };

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
  const [scan, setScan] = useState<AccountScanRow[] | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setSniff(null);
    setMapping(null);
    setPreview(null);
    setDecisions({});
    setScan(null);
    setChoices({});
    setResult(null);
    setError(null);
  };

  const multiAccount = mapping?.account_col != null;

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

  /** Read the account column's distinct values so each can be pointed at an account. */
  const scanAccounts = async (accountCol: number) => {
    if (!file || !mapping) return;
    setBusy(true);
    setError(null);
    try {
      const found = await api.scanAccounts(file, { ...mapping, account_col: accountCol });
      setScan(found);
      setPreview(null);
      // Start from what the server matched; anything it could not place is left for
      // the user to choose, so no row is silently filed under the wrong account.
      setChoices(
        Object.fromEntries(
          found.map((r) => [
            r.value,
            r.suggested_account_id
              ? ({ mode: "account", accountId: r.suggested_account_id } as Choice)
              : ({ mode: "" } as Choice),
          ]),
        ),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not read the account column");
    } finally {
      setBusy(false);
    }
  };

  const setAccountCol = (col: number | null) => {
    setMap({ account_col: col });
    setScan(null);
    setChoices({});
    setPreview(null);
    if (col !== null) void scanAccounts(col);
  };

  const assignments = (): AccountAssignment[] =>
    (scan ?? []).map((r) => {
      const c = choices[r.value] ?? { mode: "" };
      if (c.mode === "account") return { value: r.value, account_id: c.accountId };
      if (c.mode === "create")
        return { value: r.value, create: { name: c.name || r.value, type: c.type } };
      if (c.mode === "skip") return { value: r.value, skip: true };
      return { value: r.value }; // unchosen -> the server reports it as unassigned
    });

  const unchosen = (scan ?? []).filter((r) => (choices[r.value]?.mode ?? "") === "").length;
  const readyToRun = !!file && (multiAccount ? !!scan : !!accountId) && !busy;

  const run = async (commit: boolean) => {
    if (!file || (!accountId && !multiAccount)) return;
    setBusy(true);
    setError(null);
    try {
      const csvMapping = format === "csv" ? mapping : null;
      const accountAssignments = multiAccount ? assignments() : undefined;
      if (commit) {
        const rows = preview?.rows ?? [];
        const r = await api.commit(
          file,
          multiAccount ? "" : accountId,
          format,
          csvMapping,
          {
            forceImport: rows
              .filter((row) => row.status === "duplicate_probable" && willImport(row))
              .map((row) => row.row_index),
            forceSkip: rows
              .filter((row) => row.status === "new" && !willImport(row))
              .map((row) => row.row_index),
          },
          accountAssignments,
        );
        setResult(
          `Imported ${r.added} transactions — ${r.skipped} skipped, ${r.transfers_linked} transfers linked.`,
        );
        setFile(null);
        reset();
      } else {
        setPreview(
          await api.preview(
            file,
            multiAccount ? "" : accountId,
            format,
            csvMapping,
            accountAssignments,
          ),
        );
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
            <select
              value={accountId}
              disabled={multiAccount}
              onChange={(e) => setAccountId(e.target.value)}
            >
              <option value="">
                {multiAccount ? "Taken from the file" : "Choose account…"}
              </option>
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

            <div className="field" style={{ marginTop: 4 }}>
              <label>
                <input
                  type="checkbox"
                  checked={multiAccount}
                  onChange={(e) =>
                    setAccountCol(
                      e.target.checked ? (sniff.suggested_account_col ?? 0) : null,
                    )
                  }
                />{" "}
                Rows in this file belong to more than one account
              </label>
              {!multiAccount && sniff.suggested_account_col !== null && (
                <div className="muted" style={{ fontSize: 12 }}>
                  The “{sniff.columns[sniff.suggested_account_col]}” column looks like it
                  names an account.
                </div>
              )}
            </div>

            {multiAccount && (
              <>
                <div className="field">
                  <label>Account column</label>
                  <ColSelect
                    cols={sniff.columns}
                    value={mapping.account_col ?? 0}
                    onChange={(v) => setAccountCol(v)}
                  />
                </div>

                {scan && (
                  <>
                    <h2 style={{ marginTop: 12 }}>Which account is which?</h2>
                    <p className="muted" style={{ marginTop: 0 }}>
                      Point each value from that column at an account. Anything left
                      unchosen is not imported.
                    </p>
                    <table>
                      <thead>
                        <tr>
                          <th>Value in file</th>
                          <th className="num">Rows</th>
                          <th>Import into</th>
                        </tr>
                      </thead>
                      <tbody>
                        {scan.map((s) => {
                          const c = choices[s.value] ?? { mode: "" };
                          return (
                            <tr key={s.value}>
                              <td>
                                {s.value}
                                {s.sample_description && (
                                  <div className="muted" style={{ fontSize: 12 }}>
                                    e.g. {s.sample_description}
                                  </div>
                                )}
                              </td>
                              <td className="num muted">{s.row_count}</td>
                              <td>
                                <select
                                  value={
                                    c.mode === "account"
                                      ? c.accountId
                                      : c.mode === "create"
                                        ? "__create__"
                                        : c.mode === "skip"
                                          ? "__skip__"
                                          : ""
                                  }
                                  onChange={(e) => {
                                    const v = e.target.value;
                                    const next: Choice =
                                      v === "__create__"
                                        ? { mode: "create", name: s.value, type: "everyday" }
                                        : v === "__skip__"
                                          ? { mode: "skip" }
                                          : v === ""
                                            ? { mode: "" }
                                            : { mode: "account", accountId: v };
                                    setChoices((prev) => ({ ...prev, [s.value]: next }));
                                  }}
                                >
                                  <option value="">Choose…</option>
                                  {accounts.data?.map((a) => (
                                    <option key={a.id} value={a.id}>
                                      {a.name}
                                    </option>
                                  ))}
                                  <option value="__create__">Create new account…</option>
                                  <option value="__skip__">Don’t import these</option>
                                </select>
                                {c.mode === "create" && (
                                  <div className="row" style={{ marginTop: 6 }}>
                                    <input
                                      value={c.name}
                                      placeholder="Account name"
                                      onChange={(e) =>
                                        setChoices((prev) => ({
                                          ...prev,
                                          [s.value]: { ...c, name: e.target.value },
                                        }))
                                      }
                                    />
                                    <select
                                      value={c.type}
                                      onChange={(e) =>
                                        setChoices((prev) => ({
                                          ...prev,
                                          [s.value]: { ...c, type: e.target.value },
                                        }))
                                      }
                                    >
                                      {ACCOUNT_TYPES.map((t) => (
                                        <option key={t} value={t}>
                                          {t.replace(/_/g, " ")}
                                        </option>
                                      ))}
                                    </select>
                                  </div>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    {unchosen > 0 && (
                      <div className="notice">
                        {unchosen} value{unchosen === 1 ? "" : "s"} still to be assigned —
                        those rows will be left out.
                      </div>
                    )}
                  </>
                )}
              </>
            )}
          </>
        )}

        <div className="toolbar" style={{ marginTop: 12 }}>
          <button className="btn" onClick={() => void run(false)} disabled={!readyToRun}>
            Preview
          </button>
          <button
            className="btn btn-primary"
            onClick={() => void run(true)}
            disabled={!readyToRun}
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
          {preview.accounts.length > 0 && (
            <p className="muted" style={{ marginTop: 0 }}>
              {preview.accounts.map((a) => (
                <span key={a.account_name} style={{ marginRight: 12 }}>
                  <strong>{a.account_name}</strong>
                  {a.account_id === null && " (new)"}: {a.new_count} to import
                  {a.duplicate_count > 0 && `, ${a.duplicate_count} duplicate`}
                </span>
              ))}
            </p>
          )}
          {preview.unassigned_count > 0 && (
            <div className="notice">
              {preview.unassigned_count} row
              {preview.unassigned_count === 1 ? "" : "s"} have an account value you haven’t
              assigned, and will not be imported.
            </div>
          )}
          <table>
            <thead>
              <tr>
                <th style={{ width: 32 }}>
                  <span className="sr-only">Import</span>
                </th>
                <th>Date</th>
                <th>Description</th>
                {multiAccount && <th>Account</th>}
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
                  {multiAccount && <td className="muted">{r.account_name ?? "—"}</td>}
                  <td className="muted">{r.suggested_category_name ?? "—"}</td>
                  <td className={`num ${r.amount_cents < 0 ? "negative" : "positive"}`}>
                    {formatCents(r.amount_cents)}
                  </td>
                  <td>
                    {r.status === "new" ? (
                      <span className="muted">New</span>
                    ) : r.status === "unassigned" ? (
                      <span className="tag">No account</span>
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
