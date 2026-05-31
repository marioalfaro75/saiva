import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { formatCents, formatDate } from "../format";

export function Transactions() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [accountId, setAccountId] = useState("");
  const [uncategorised, setUncategorised] = useState(false);
  const [page, setPage] = useState(1);

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const txns = useQuery({
    queryKey: ["txns", q, accountId, uncategorised, page],
    queryFn: () =>
      api.transactions({
        q: q || undefined,
        account_id: accountId || undefined,
        uncategorised: uncategorised || undefined,
        page,
        page_size: 50,
      }),
  });

  const recategorise = useMutation({
    mutationFn: (v: { id: string; categoryId: string }) =>
      api.recategorise(v.id, v.categoryId || null, false, false),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["txns"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
      void qc.invalidateQueries({ queryKey: ["breakdown"] });
    },
  });

  const subcategories = (categories.data ?? []).filter((c) => c.parent_id);
  const totalPages = txns.data ? Math.max(1, Math.ceil(txns.data.total / txns.data.page_size)) : 1;

  return (
    <div>
      <div className="page-head">
        <h1>Transactions</h1>
      </div>

      <div className="toolbar">
        <input
          placeholder="Search description or merchant"
          value={q}
          onChange={(e) => {
            setPage(1);
            setQ(e.target.value);
          }}
          style={{ maxWidth: 280 }}
        />
        <select
          className="pill-select"
          value={accountId}
          onChange={(e) => {
            setPage(1);
            setAccountId(e.target.value);
          }}
        >
          <option value="">All accounts</option>
          {accounts.data?.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6, margin: 0 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={uncategorised}
            onChange={(e) => {
              setPage(1);
              setUncategorised(e.target.checked);
            }}
          />
          Needs review
        </label>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Description</th>
              <th>Account</th>
              <th>Category</th>
              <th className="num">Amount</th>
            </tr>
          </thead>
          <tbody>
            {txns.data?.items.map((t) => (
              <tr key={t.id}>
                <td className="muted">{formatDate(t.txn_date)}</td>
                <td>
                  {t.merchant ?? t.raw_description}
                  {t.is_transfer && <span className="tag transfer" style={{ marginLeft: 6 }}>transfer</span>}
                </td>
                <td className="muted">{t.account_name}</td>
                <td>
                  <select
                    className="pill-select"
                    value={t.category_id ?? ""}
                    onChange={(e) => recategorise.mutate({ id: t.id, categoryId: e.target.value })}
                  >
                    <option value="">Uncategorised</option>
                    {subcategories.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td className={`num ${t.amount_cents < 0 ? "negative" : "positive"}`}>
                  {formatCents(t.amount_cents)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {txns.data && txns.data.items.length === 0 && (
          <p className="muted">No transactions match your filters.</p>
        )}

        <div className="spread" style={{ marginTop: 12 }}>
          <span className="muted">{txns.data?.total ?? 0} total</span>
          <div className="toolbar" style={{ margin: 0 }}>
            <button className="btn" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              Prev
            </button>
            <span className="muted">
              Page {page} / {totalPages}
            </span>
            <button
              className="btn"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
