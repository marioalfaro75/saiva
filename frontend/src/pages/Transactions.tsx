import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { usePeriod } from "../period/context";
import { FilterRow, FilterToggle } from "../table/FilterRow";
import { SortHeader } from "../table/SortHeader";
import type { ColumnSpec } from "../table/sorting";
import { useServerTable } from "../table/useServerTable";
import { useTable } from "../table/useTable";
import { CategoriseDialog } from "../components/CategoriseDialog";
import { RulesManager } from "../components/RulesManager";
import { TABLE_MIN, TableWrap } from "../table/TableWrap";
import { formatCents, formatDate } from "../format";
import type { Transaction, TxnGroup } from "../api/types";

const TXN_LABELS = {
  date: "Date",
  description: "Description",
  account: "Account",
  category: "Category",
  amount: "Amount",
};
const TXN_FILTER_KEYS = Object.keys(TXN_LABELS);

const GROUP_LABELS = { label: "Merchant", count: "Count", total: "Total" };

const GROUP_COLUMNS: ColumnSpec<TxnGroup>[] = [
  { key: "label", sort: (g) => g.sample_description || g.key },
  { key: "count", sort: (g) => g.count },
  { key: "total", sort: (g) => g.total_cents, text: (g) => formatCents(g.total_cents) },
];

type Tab = "transactions" | "groups" | "rules";
type GroupBy = "merchant" | "description";

export function Transactions() {
  const qc = useQueryClient();
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>("transactions");
  // Group review is a third view of the same data, not a mode within the list.
  const view = tab === "groups" ? "groups" : "list";
  const [groupBy, setGroupBy] = useState<GroupBy>("merchant");
  const [q, setQ] = useState(searchParams.get("q") ?? "");
  const [accountId, setAccountId] = useState(searchParams.get("account_id") ?? "");
  const [categoryId, setCategoryId] = useState(searchParams.get("category_id") ?? "");
  const [uncategorised, setUncategorised] = useState(searchParams.get("uncategorised") === "true");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkCat, setBulkCat] = useState("");
  const [dialogTxn, setDialogTxn] = useState<Transaction | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const showFlash = (message: string) => {
    setFlash(message);
    window.setTimeout(() => setFlash((cur) => (cur === message ? null : cur)), 2500);
  };

  const { period } = usePeriod();
  // This list is paginated by the server, so ordering and filtering go with the
  // request — sorting the fetched page here would answer the wrong question.
  const server = useServerTable(TXN_FILTER_KEYS);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const txns = useQuery({
    queryKey: ["txns", q, accountId, categoryId, uncategorised, page, period, server.params],
    queryFn: () =>
      api.transactions({
        q: q || undefined,
        account_id: accountId || undefined,
        category_id: categoryId || undefined,
        uncategorised: uncategorised || undefined,
        period,
        page,
        page_size: 50,
        ...server.params,
      }),
    enabled: tab === "transactions",
  });
  const groups = useQuery({
    queryKey: ["txn-groups", groupBy],
    queryFn: () => api.transactionGroups(groupBy, true),
    enabled: tab === "groups",
  });
  // Group review is fully loaded, so it sorts and filters in the browser.
  const groupTable = useTable(groups.data?.groups ?? [], GROUP_COLUMNS, {
    id: "txn-groups",
    defaultSort: { key: "count", dir: "desc" },
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["txn-groups"] });
    void qc.invalidateQueries({ queryKey: ["summary"] });
    void qc.invalidateQueries({ queryKey: ["breakdown"] });
  };

  const quickCat = useMutation({
    mutationFn: (v: { id: string; categoryId: string }) =>
      api.recategorise(v.id, { category_id: v.categoryId || null, scope: "none" }),
    onSuccess: invalidate,
  });
  const dialogApply = useMutation({
    mutationFn: (v: { id: string; body: Parameters<typeof api.recategorise>[1] }) =>
      api.recategorise(v.id, v.body),
    onSuccess: (r) => {
      invalidate();
      setDialogTxn(null);
      showFlash(`Updated ${r.updated_count} transaction${r.updated_count === 1 ? "" : "s"}`);
    },
  });
  const lockToggle = useMutation({
    mutationFn: (v: { id: string; locked: boolean }) =>
      api.updateTransaction(v.id, { category_locked: v.locked }),
    onSuccess: (_d, v) => {
      invalidate();
      showFlash(v.locked ? "Locked" : "Unlocked");
    },
  });
  const bulkApply = useMutation({
    mutationFn: (v: { ids: string[]; categoryId: string | null }) =>
      api.bulkCategorise(v.ids, v.categoryId),
    onSuccess: (r) => {
      invalidate();
      setSelected(new Set());
      showFlash(`Updated ${r.updated} transaction${r.updated === 1 ? "" : "s"}`);
    },
  });
  const bulkLock = useMutation({
    mutationFn: (v: { ids: string[]; locked: boolean }) => api.bulkLock(v.ids, v.locked),
    onSuccess: (r, v) => {
      invalidate();
      setSelected(new Set());
      showFlash(`${v.locked ? "Locked" : "Unlocked"} ${r.updated}`);
    },
  });
  const groupApply = useMutation({
    mutationFn: (v: { sampleId: string; categoryId: string }) =>
      api.recategorise(v.sampleId, {
        category_id: v.categoryId,
        scope: groupBy === "merchant" ? "merchant" : "exact",
      }),
    onSuccess: (r) => {
      invalidate();
      showFlash(`Categorised ${r.updated_count} transaction${r.updated_count === 1 ? "" : "s"}`);
    },
  });

  const subcategories = (categories.data ?? []).filter((c) => c.parent_id);
  const totalPages = txns.data ? Math.max(1, Math.ceil(txns.data.total / txns.data.page_size)) : 1;
  const pageItems = txns.data?.items ?? [];
  const allOnPageSelected = pageItems.length > 0 && pageItems.every((t) => selected.has(t.id));

  const toggleSel = (id: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const toggleAllOnPage = () =>
    setSelected((s) => {
      const next = new Set(s);
      if (allOnPageSelected) pageItems.forEach((t) => next.delete(t.id));
      else pageItems.forEach((t) => next.add(t.id));
      return next;
    });

  const categoryOptions = subcategories.map((c) => (
    <option key={c.id} value={c.id}>
      {c.name}
    </option>
  ));

  return (
    <div>
      <div className="page-head">
        <h1>Transactions</h1>
      </div>

      <div className="tabs">
        <button
          className={`tab ${tab === "transactions" ? "active" : ""}`}
          onClick={() => setTab("transactions")}
        >
          Transactions
        </button>
        <button
          className={`tab ${tab === "groups" ? "active" : ""}`}
          onClick={() => {
            setTab("groups");
            setSelected(new Set());
          }}
        >
          Group review
        </button>
        <button className={`tab ${tab === "rules" ? "active" : ""}`} onClick={() => setTab("rules")}>
          Rules
        </button>
      </div>

      {flash && <div className="notice">{flash}</div>}

      {tab === "rules" ? (
        <RulesManager categories={categories.data ?? []} onFlash={showFlash} />
      ) : (
        <>
          <div className="toolbar">
            {view === "list" ? (
              <>
                <input
                  placeholder="Search description or merchant"
                  value={q}
                  onChange={(e) => {
                    setPage(1);
                    setQ(e.target.value);
                  }}
                  style={{ maxWidth: 240 }}
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
                <select
                  className="pill-select"
                  value={categoryId}
                  onChange={(e) => {
                    setPage(1);
                    setCategoryId(e.target.value);
                  }}
                >
                  <option value="">All categories</option>
                  {categoryOptions}
                </select>
                <label
                  className="muted"
                  style={{ display: "flex", alignItems: "center", gap: 6, margin: 0 }}
                >
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
              </>
            ) : (
              <>
                <select
                  className="pill-select"
                  value={groupBy}
                  onChange={(e) => setGroupBy(e.target.value as GroupBy)}
                >
                  <option value="merchant">Group by merchant</option>
                  <option value="description">Group by description</option>
                </select>
                <span className="muted">
                  Uncategorised transactions across all time, most common first — this list
                  ignores the selected period.
                </span>
              </>
            )}
          </div>

          {view === "list" ? (
            <div className="card">
              <div className="spread">
                <span />
                <FilterToggle table={server} count={txns.data?.total} />
              </div>
              <TableWrap min={TABLE_MIN.transactions} label="Transactions">
                <table>
                  <thead>
                    <tr>
                      <th className="pick">
                        <input
                          type="checkbox"
                          style={{ width: "auto" }}
                          checked={allOnPageSelected}
                          onChange={toggleAllOnPage}
                        />
                      </th>
                      <SortHeader table={server} col="date">
                        Date
                      </SortHeader>
                      <SortHeader table={server} col="description">
                        Description
                      </SortHeader>
                      <SortHeader table={server} col="account">
                        Account
                      </SortHeader>
                      <SortHeader table={server} col="category">
                        Category
                      </SortHeader>
                      <SortHeader table={server} col="amount" numeric>
                        Amount
                      </SortHeader>
                      <th className="actions"></th>
                    </tr>
                    <FilterRow
                      table={server}
                      labels={TXN_LABELS}
                      columns={[null, "date", "description", "account", "category", "amount", null]}
                    />
                  </thead>
                  <tbody>
                    {pageItems.map((t) => (
                      <tr key={t.id}>
                        <td className="pick">
                          <input
                            type="checkbox"
                            style={{ width: "auto" }}
                            checked={selected.has(t.id)}
                            onChange={() => toggleSel(t.id)}
                          />
                        </td>
                        <td className="muted">{formatDate(t.txn_date)}</td>
                        <td>
                          {t.merchant ?? t.raw_description}
                          {t.is_transfer && (
                            <span className="tag transfer" style={{ marginLeft: 6 }}>
                              transfer
                            </span>
                          )}
                        </td>
                        <td className="muted">{t.account_name}</td>
                        <td>
                          <select
                            className="pill-select"
                            value={t.category_id ?? ""}
                            onChange={(e) => quickCat.mutate({ id: t.id, categoryId: e.target.value })}
                          >
                            <option value="">Uncategorised</option>
                            {categoryOptions}
                          </select>
                        </td>
                        <td className={`num ${t.amount_cents < 0 ? "negative" : "positive"}`}>
                          {formatCents(t.amount_cents)}
                        </td>
                        <td className="actions">
                          <button
                            className={`icon-btn ${t.category_locked ? "on" : ""}`}
                            title={t.category_locked ? "Locked — click to unlock" : "Lock"}
                            onClick={() => lockToggle.mutate({ id: t.id, locked: !t.category_locked })}
                          >
                            {t.category_locked ? "🔒" : "🔓"}
                          </button>
                          <button
                            className="icon-btn"
                            title="Apply to similar, make a rule…"
                            onClick={() => setDialogTxn(t)}
                          >
                            ⋯
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
              {txns.data && pageItems.length === 0 && (
                <p className="muted">No transactions match your filters.</p>
              )}

              {selected.size > 0 && (
                <div className="bulk-bar">
                  <strong>{selected.size} selected</strong>
                  <select
                    className="pill-select"
                    value={bulkCat}
                    onChange={(e) => setBulkCat(e.target.value)}
                  >
                    <option value="">Set category…</option>
                    {categoryOptions}
                  </select>
                  <button
                    className="btn btn-primary"
                    disabled={!bulkCat || bulkApply.isPending}
                    onClick={() => bulkApply.mutate({ ids: [...selected], categoryId: bulkCat })}
                  >
                    Apply
                  </button>
                  <span className="grow" />
                  <button
                    className="btn btn-ghost"
                    onClick={() => bulkLock.mutate({ ids: [...selected], locked: true })}
                  >
                    Lock
                  </button>
                  <button
                    className="btn btn-ghost"
                    onClick={() => bulkLock.mutate({ ids: [...selected], locked: false })}
                  >
                    Unlock
                  </button>
                  <button className="btn btn-ghost" onClick={() => setSelected(new Set())}>
                    Clear
                  </button>
                </div>
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
          ) : (
            <div className="card">
              <div className="spread">
                <span />
                <FilterToggle table={groupTable} />
              </div>
              <TableWrap min={TABLE_MIN.txnGroups} label="Grouped transactions">
                <table>
                  <thead>
                    <tr>
                      <SortHeader table={groupTable} col="label">
                        {groupBy === "merchant" ? "Merchant" : "Description"}
                      </SortHeader>
                      <SortHeader table={groupTable} col="count" numeric>
                        Count
                      </SortHeader>
                      <SortHeader table={groupTable} col="total" numeric>
                        Total
                      </SortHeader>
                      <th>Categorise all as</th>
                    </tr>
                    <FilterRow
                      table={groupTable}
                      labels={GROUP_LABELS}
                      columns={["label", "count", "total", null]}
                    />
                  </thead>
                  <tbody>
                    {groupTable.rows.map((g) => (
                      <tr key={g.key}>
                        <td>{g.sample_description || g.key}</td>
                        <td className="num">{g.count}</td>
                        <td className="num">{formatCents(g.total_cents)}</td>
                        <td>
                          <select
                            className="pill-select"
                            value=""
                            disabled={groupApply.isPending}
                            onChange={(e) => {
                              if (e.target.value)
                                groupApply.mutate({ sampleId: g.sample_id, categoryId: e.target.value });
                            }}
                          >
                            <option value="">Choose…</option>
                            {categoryOptions}
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
              {groups.data && groups.data.groups.length === 0 && (
                <p className="muted">Nothing left to review — all transactions are categorised.</p>
              )}
            </div>
          )}
        </>
      )}

      {dialogTxn && (
        <CategoriseDialog
          txn={dialogTxn}
          categories={categories.data ?? []}
          busy={dialogApply.isPending}
          onClose={() => setDialogTxn(null)}
          onSubmit={(body) => dialogApply.mutate({ id: dialogTxn.id, body })}
        />
      )}
    </div>
  );
}
