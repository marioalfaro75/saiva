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
import { PHONE, useMediaQuery } from "../hooks/useMediaQuery";
import { ColumnMapper } from "../import/ColumnMapper";
import { mappingFromRoles, rolesFromMapping, type Role, type Roles } from "../import/mapping";
import { SortChips } from "../table/MobileControls";
import { TABLE_MIN, TableWrap } from "../table/TableWrap";
import { formatCents, formatDate } from "../format";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useTable } from "../table/useTable";
import { PageHead } from "../components/PageHead";

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

// Import tables sort but are deliberately not remembered: a filter restored from a
// previous import would hide rows of a new file during the review step.
const SCAN_COLUMNS: ColumnSpec<AccountScanRow>[] = [
  { key: "value", sort: (r) => r.value },
  { key: "rows", sort: (r) => r.row_count },
];

const PREVIEW_COLUMNS: ColumnSpec<PreviewRow>[] = [
  { key: "date", sort: (r) => r.txn_date },
  { key: "description", sort: (r) => r.merchant ?? r.raw_description },
  { key: "account", sort: (r) => r.account_name },
  { key: "category", sort: (r) => r.suggested_category_name },
  { key: "amount", sort: (r) => r.amount_cents },
  { key: "status", sort: (r) => r.status },
];

const SCAN_LABELS = { value: "Value in file", rows: "Rows" };
const PREVIEW_LABELS = {
  date: "Date",
  description: "Description",
  account: "Account",
  category: "Category",
  amount: "Amount",
  status: "Status",
};

/** What the user chose to do with one distinct value of the account column. */
type Choice =
  | { mode: "" }
  | { mode: "account"; accountId: string }
  | { mode: "create"; name: string; type: string }
  | { mode: "skip" };

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
  // What each column is for. The mapping the API needs is derived from this, so the
  // screen and the request can never disagree about which column is which.
  const [roles, setRoles] = useState<Roles>({});
  // What detection said, kept so a role can be shown as a guess rather than a choice.
  const [detected, setDetected] = useState<Roles>({});
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  // The configuration the preview on screen was computed from — compared against
  // the current one below to tell whether it still answers the question being asked.
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  // Reviewer overrides of the preview's default verdict, keyed by row index.
  const [decisions, setDecisions] = useState<Record<number, boolean>>({});
  const [scan, setScan] = useState<AccountScanRow[] | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped to remount the file input: clearing `file` leaves the native control
  // still showing the name of the file that was just imported.
  const [fileInputKey, setFileInputKey] = useState(0);
  const narrow = !useMediaQuery(PHONE);

  const reset = () => {
    setSniff(null);
    setMapping(null);
    setPreview(null);
    setPreviewKey(null);
    setDecisions({});
    setScan(null);
    setChoices({});
    setResult(null);
    setError(null);
  };

  // The file answers the account question whenever it carries one, rather than the
  // question being asked first and the answer hidden behind an opt-in.
  const multiAccount = Object.values(roles).includes("account") || format !== "csv";
  // What the API is sent: the roles on screen, resolved into the shape it expects.
  const effectiveMapping =
    mapping && format === "csv" ? mappingFromRoles(roles, mapping) : null;

  const onFile = async (f: File | null) => {
    setFile(f);
    reset();
    if (!f) return;
    const fmt = guessFormat(f);
    setFormat(fmt);
    try {
      if (fmt !== "csv") {
        // OFX names its accounts per statement, so there is nothing to map — go
        // straight to matching them against yours.
        await runScan(f, fmt, null);
        return;
      }
      const s = await api.sniff(f);
      setSniff(s);
      if (!s.suggested_mapping) return;
      const seeded = { ...s.suggested_mapping, account_col: s.suggested_account_col };
      setMapping(seeded);
      const initial = rolesFromMapping(seeded, s.columns.length);
      setRoles(initial);
      setDetected(initial);
      if (s.suggested_account_col !== null) {
        await runScan(f, "csv", mappingFromRoles(initial, seeded));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not read file");
    }
  };

  const setMap = (patch: Partial<CsvMapping>) => {
    if (mapping) setMapping({ ...mapping, ...patch });
  };

  /** Re-read the file under a changed mapping: the account values depend on it. */
  const setRole = (col: number, role: Role) => {
    const next = { ...roles, [col]: role };
    setRoles(next);
    setScan(null);
    setChoices({});
    if (!file || !mapping) return;
    if (Object.values(next).includes("account")) {
      void runScan(file, "csv", mappingFromRoles(next, mapping));
    }
  };

  // A row imports unless the reviewer said otherwise; definite duplicates never do.
  const willImport = (r: PreviewRow) => decisions[r.row_index] ?? r.will_import;
  const canDecide = (r: PreviewRow) => r.status === "new" || r.status === "duplicate_probable";

  /** List the accounts a file covers, so each can be pointed at one of yours. */
  const runScan = async (f: File, fmt: string, csvMapping: CsvMapping | null) => {
    setBusy(true);
    setError(null);
    try {
      const found = await api.scanAccounts(f, csvMapping, fmt);
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
      setError(e instanceof ApiError ? e.message : "Could not read the accounts in this file");
    } finally {
      setBusy(false);
    }
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

  const scanTable = useTable(scan ?? [], SCAN_COLUMNS);
  // Sorting by Status groups every possible duplicate together for review.
  const previewTable = useTable(preview?.rows ?? [], PREVIEW_COLUMNS);

  const unchosen = (scan ?? []).filter((r) => (choices[r.value]?.mode ?? "") === "").length;
  const readyToRun = !!file && (multiAccount ? !!scan : !!accountId) && !busy;

  /**
   * Everything the preview is computed from, as one comparable value.
   *
   * Ten controls can change one of these — the account, six mapping fields, and
   * three per-value assignment controls — and none of them used to clear the
   * preview. That left the last preview on screen describing a file that was no
   * longer being imported, and, far worse, `run(true)` sends *row indices* taken
   * from it: a stale index means importing or skipping the wrong row. Deriving
   * staleness from the inputs is the version that cannot rot, since a control added
   * later is covered the moment its value is part of the request.
   */
  const configKey = JSON.stringify({
    file: file && [file.name, file.size, file.lastModified],
    accountId: multiAccount ? "" : accountId,
    format,
    // The derived mapping, not the base one: what a column is for lives in `roles`,
    // so hashing the base would miss every change made in the mapping step.
    mapping: effectiveMapping,
    assignments: multiAccount ? assignments() : null,
  });
  const stale = preview !== null && previewKey !== configKey;
  const toImport = preview && !stale ? preview.rows.filter(willImport).length : 0;
  const canImport = readyToRun && preview !== null && !stale && toImport > 0;

  /**
   * What an account value actually is.
   *
   * The value itself is the bank's, not yours — "7.34364E+11" identifies nothing to a
   * person. The period it covers and the balance it ends on do: -819,480.37 over 74
   * rows is unmistakably the mortgage.
   */
  const accountIdentity = (s: AccountScanRow) => (
    <div className="acct-identity muted">
      <span>{s.row_count} rows</span>
      {s.first_date && s.last_date && (
        <>
          <span aria-hidden="true">·</span>
          <span>
            {formatDate(s.first_date)} – {formatDate(s.last_date)}
          </span>
        </>
      )}
      {s.latest_balance_cents !== null && (
        <>
          <span aria-hidden="true">·</span>
          <span className={s.latest_balance_cents < 0 ? "negative" : ""}>
            balance {formatCents(s.latest_balance_cents)}
          </span>
        </>
      )}
      {s.sample_description && (
        <div className="acct-sample">e.g. {s.sample_description}</div>
      )}
      {s.looks_mangled && (
        <div className="acct-warn">
          Excel has turned this account number into scientific notation, so its digits
          are gone. It imports correctly either way — exporting again without opening
          the file in Excel lets it be recognised automatically next time.
        </div>
      )}
    </div>
  );

  /** One definition of the assignment control, laid out as a table cell on a wide
   *  screen and inside a card on a phone. */
  const accountPicker = (s: AccountScanRow) => {
    const c = choices[s.value] ?? { mode: "" };
    return (
      <>
        <select
          aria-label={`Import ${s.value} into`}
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
              aria-label={`New account name for ${s.value}`}
              onChange={(e) =>
                setChoices((prev) => ({ ...prev, [s.value]: { ...c, name: e.target.value } }))
              }
            />
            <select
              value={c.type}
              aria-label={`New account type for ${s.value}`}
              onChange={(e) =>
                setChoices((prev) => ({ ...prev, [s.value]: { ...c, type: e.target.value } }))
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
      </>
    );
  };

  const importBox = (r: PreviewRow) => (
    <input
      type="checkbox"
      checked={willImport(r)}
      disabled={!canDecide(r)}
      aria-label={`Import ${r.merchant ?? r.raw_description}`}
      title={
        canDecide(r)
          ? "Import this row"
          : "Already imported — importing it again would create a duplicate"
      }
      onChange={(e) => setDecisions((d) => ({ ...d, [r.row_index]: e.target.checked }))}
    />
  );

  const statusCell = (r: PreviewRow) =>
    r.status === "new" ? (
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
    );

  const run = async (commit: boolean) => {
    if (!file || (!accountId && !multiAccount)) return;
    setBusy(true);
    setError(null);
    // Captured before awaiting: this is the configuration the request describes,
    // even if something changes while it is in flight.
    const ranWith = configKey;
    try {
      const csvMapping = effectiveMapping;
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
        setFileInputKey((n) => n + 1);
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
        setPreviewKey(ranWith);
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
      <PageHead title="Import transactions" />
      {error && <div className="error">{error}</div>}
      {result && <div className="notice">{result}</div>}

      <div className="card">
        <div className="row">
          {/* Only asked when the file cannot answer it. A statement that names its
              own accounts is the common case, and being made to pick one first was
              what hid multi-account import entirely. */}
          {!multiAccount && (
            <div className="field">
              <label htmlFor="imp-account">Account</label>
              <select
                id="imp-account"
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
              >
                <option value="">Choose account…</option>
                {accounts.data?.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="field">
            <label htmlFor="imp-file">File (CSV, OFX or QFX)</label>
            <input
              key={fileInputKey}
              id="imp-file"
              type="file"
              accept=".csv,.ofx,.qfx"
              onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
            />
          </div>
        </div>

        {sniff && mapping && (
          <>
            <ColumnMapper
              sniff={sniff}
              mapping={mapping}
              roles={roles}
              detected={detected}
              onRole={setRole}
              onMapping={(patch) => {
                setMap(patch);
                // Re-reading the file with a different separator or header row changes
                // what the columns even are, so nothing derived from them survives.
                setScan(null);
                setPreview(null);
              }}
            />
          </>
        )}

        {multiAccount && (
          <>
                {scan && (
                  <>
                    <h2 style={{ marginTop: 12 }}>Which account is which?</h2>
                    <p className="muted" style={{ marginTop: 0 }}>
                      Point each value from that column at an account. Anything left
                      unchosen is not imported.
                    </p>
                    {narrow ? (
                      <>
                        <SortChips
                          table={scanTable}
                          labels={SCAN_LABELS}
                          columns={["value", "rows"]}
                        />
                        <ul className="stack-cards">
                          {scanTable.rows.map((s) => (
                            <li className="stack-card" key={s.value}>
                              <div className="stack-card-head">
                                <strong>{s.value}</strong>
                              </div>
                              {accountIdentity(s)}
                              {accountPicker(s)}
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : (
                      <TableWrap
                        min={TABLE_MIN.importAccounts}
                        label="Accounts found in the file"
                      >
                        <table>
                          <thead>
                            <tr>
                              <SortHeader table={scanTable} col="value">
                                Value in file
                              </SortHeader>
                              <SortHeader table={scanTable} col="rows" numeric>
                                Rows
                              </SortHeader>
                              <th>Import into</th>
                            </tr>
                          </thead>
                          <tbody>
                            {scanTable.rows.map((s) => (
                              <tr key={s.value}>
                                <td>
                                  <div>{s.value}</div>
                                  {accountIdentity(s)}
                                </td>
                                <td className="num muted">{s.row_count}</td>
                                <td>{accountPicker(s)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </TableWrap>
                    )}
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

        {/* Preview is the primary action: importing is the irreversible one, and it
            is only offered once there is a preview describing exactly what it will
            do. The label carries the count so the button states its own consequence. */}
        <div className="toolbar" style={{ marginTop: 12 }}>
          <button
            className="btn btn-primary"
            onClick={() => void run(false)}
            disabled={!readyToRun}
          >
            {stale ? "Preview again" : "Preview"}
          </button>
          <button
            className="btn"
            onClick={() => void run(true)}
            disabled={!canImport}
            title={canImport ? undefined : "Preview the file first to see what will be imported"}
          >
            {canImport
              ? `Import ${toImport} transaction${toImport === 1 ? "" : "s"}`
              : "Import"}
          </button>
        </div>
      </div>

      {preview && (
        <div className={`card${stale ? " stale" : ""}`} style={{ marginTop: 16 }}>
          {stale && (
            <div className="notice">
              You changed the import settings, so this preview is out of date. Preview
              again to see what will actually be imported.
            </div>
          )}
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
          {narrow ? (
            <>
              <SortChips
                table={previewTable}
                labels={PREVIEW_LABELS}
                columns={
                  multiAccount
                    ? ["date", "description", "account", "amount", "status"]
                    : ["date", "description", "amount", "status"]
                }
              />
              <ul className="stack-cards">
                {previewTable.rows.slice(0, 200).map((r) => (
                  <li
                    className="stack-card"
                    key={r.row_index}
                    style={{ opacity: willImport(r) ? 1 : 0.55 }}
                  >
                    <div className="stack-card-head">
                      {importBox(r)}
                      <span className="grow">{r.merchant ?? r.raw_description}</span>
                      <span className={`num ${r.amount_cents < 0 ? "negative" : "positive"}`}>
                        {formatCents(r.amount_cents)}
                      </span>
                    </div>
                    <div className="stack-card-meta muted">
                      <span>{formatDate(r.txn_date)}</span>
                      {multiAccount && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span>{r.account_name ?? "No account"}</span>
                        </>
                      )}
                      {r.suggested_category_name && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span>{r.suggested_category_name}</span>
                        </>
                      )}
                    </div>
                    <div>{statusCell(r)}</div>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <TableWrap min={TABLE_MIN.importPreview} label="Import preview">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 32 }}>
                      <span className="sr-only">Import</span>
                    </th>
                    <SortHeader table={previewTable} col="date">
                      Date
                    </SortHeader>
                    <SortHeader table={previewTable} col="description">
                      Description
                    </SortHeader>
                    {multiAccount && (
                      <SortHeader table={previewTable} col="account">
                        Account
                      </SortHeader>
                    )}
                    <SortHeader table={previewTable} col="category">
                      Suggested category
                    </SortHeader>
                    <SortHeader table={previewTable} col="amount" numeric>
                      Amount
                    </SortHeader>
                    <SortHeader table={previewTable} col="status">
                      Status
                    </SortHeader>
                  </tr>
                </thead>
                <tbody>
                  {previewTable.rows.slice(0, 200).map((r) => (
                    <tr key={r.row_index} style={{ opacity: willImport(r) ? 1 : 0.55 }}>
                      <td>{importBox(r)}</td>
                      <td className="muted">{formatDate(r.txn_date)}</td>
                      <td>{r.merchant ?? r.raw_description}</td>
                      {multiAccount && <td className="muted">{r.account_name ?? "—"}</td>}
                      <td className="muted">{r.suggested_category_name ?? "—"}</td>
                      <td className={`num ${r.amount_cents < 0 ? "negative" : "positive"}`}>
                        {formatCents(r.amount_cents)}
                      </td>
                      <td>{statusCell(r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
          {preview.rows.length > 200 && (
            <p className="muted">Showing the first 200 of {preview.rows.length} rows.</p>
          )}
        </div>
      )}
    </div>
  );
}
