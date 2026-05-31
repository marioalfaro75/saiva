import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { api } from "../api/client";
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

export function Accounts() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
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
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Institution</th>
                  <th className="num">Balance</th>
                  <th className="num">Txns</th>
                </tr>
              </thead>
              <tbody>
                {accounts.data.map((a) => (
                  <tr key={a.id}>
                    <td>{a.name}</td>
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
