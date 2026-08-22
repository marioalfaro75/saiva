import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Account } from "../api/types";
import { FilterRow, FilterToggle } from "../table/FilterRow";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useTable } from "../table/useTable";
import { formatCents } from "../format";

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

const ACCOUNT_LABELS = {
  name: "Name",
  type: "Type",
  institution: "Institution",
  balance: "Balance",
  txns: "Txns",
};

const ACCOUNT_COLUMNS: ColumnSpec<Account>[] = [
  { key: "name", sort: (a) => a.name },
  { key: "type", sort: (a) => a.type.replace(/_/g, " ") },
  { key: "institution", sort: (a) => a.institution },
  { key: "balance", sort: (a) => a.balance_cents, text: (a) => formatCents(a.balance_cents) },
  { key: "txns", sort: (a) => a.txn_count },
];

export function Accounts() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const table = useTable(accounts.data ?? [], ACCOUNT_COLUMNS, { id: "accounts" });
  const [name, setName] = useState("");
  const [type, setType] = useState("everyday");
  const [institution, setInstitution] = useState("");

  const create = useMutation({
    mutationFn: () => api.createAccount({ name, type, institution: institution || null }),
    onSuccess: () => {
      setName("");
      setInstitution("");
      return qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div>
      <div className="page-head">
        <h1>Accounts</h1>
      </div>

      <div className="grid">
        <div className="card">
          {accounts.data && accounts.data.length > 0 ? (
            <>
              <div className="spread">
                <span />
                <FilterToggle table={table} />
              </div>
              <table>
                <thead>
                  <tr>
                    <SortHeader table={table} col="name">
                      Name
                    </SortHeader>
                    <SortHeader table={table} col="type">
                      Type
                    </SortHeader>
                    <SortHeader table={table} col="institution">
                      Institution
                    </SortHeader>
                    <SortHeader table={table} col="balance" numeric>
                      Balance
                    </SortHeader>
                    <SortHeader table={table} col="txns" numeric>
                      Txns
                    </SortHeader>
                  </tr>
                  <FilterRow
                    table={table}
                    labels={ACCOUNT_LABELS}
                    columns={["name", "type", "institution", "balance", "txns"]}
                  />
                </thead>
                <tbody>
                  {table.rows.map((a) => (
                    <tr key={a.id}>
                      <td>
                        <Link to={`/transactions?account_id=${a.id}`}>{a.name}</Link>
                      </td>
                      <td>
                        <span className="tag">{a.type.replace(/_/g, " ")}</span>
                      </td>
                      <td className="muted">{a.institution ?? "—"}</td>
                      <td className={`num ${a.balance_cents < 0 ? "negative" : ""}`}>
                        {formatCents(a.balance_cents)}
                      </td>
                      <td className="num muted">{a.txn_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="muted">No accounts yet. Add one below, then import a statement.</p>
          )}
        </div>

        <div className="card">
          <h2>Add account</h2>
          <form onSubmit={onSubmit}>
            <div className="row">
              <div className="field">
                <label>Name</label>
                <input value={name} onChange={(e) => setName(e.target.value)} required />
              </div>
              <div className="field">
                <label>Type</label>
                <select value={type} onChange={(e) => setType(e.target.value)}>
                  {ACCOUNT_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Institution</label>
                <input
                  value={institution}
                  onChange={(e) => setInstitution(e.target.value)}
                  placeholder="optional"
                />
              </div>
            </div>
            {create.isError && <div className="error">Could not create account.</div>}
            <button className="btn btn-primary" disabled={create.isPending || !name}>
              Add account
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
