import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api } from "../api/client";
import type { NetWorth as NetWorthData, NetWorthItem } from "../api/types";
import { dollarsToCents, formatCents } from "../format";

type SavePatch = { name?: string; value_cents?: number };

function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls ?? ""}`}>{value}</div>
    </div>
  );
}

function ItemRow({
  item,
  onSave,
  onRemove,
  busy,
}: {
  item: NetWorthItem;
  onSave: (id: string, patch: SavePatch) => void;
  onRemove: (id: string) => void;
  busy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(item.name);
  const [amount, setAmount] = useState((item.value_cents / 100).toString());

  if (editing) {
    return (
      <tr>
        <td>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </td>
        <td className="num">
          <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
        </td>
        <td className="num">
          <button
            type="button"
            className="btn"
            disabled={busy || dollarsToCents(amount) < 0}
            onClick={() => {
              onSave(item.id, { name, value_cents: dollarsToCents(amount) });
              setEditing(false);
            }}
          >
            Save
          </button>{" "}
          <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td>{item.name}</td>
      <td className="num">{formatCents(item.value_cents)}</td>
      <td className="num">
        <button type="button" className="btn btn-ghost" onClick={() => setEditing(true)}>
          Edit
        </button>{" "}
        <button
          type="button"
          className="btn btn-ghost"
          disabled={busy}
          onClick={() => onRemove(item.id)}
        >
          Remove
        </button>
      </td>
    </tr>
  );
}

function ItemTable({
  title,
  items,
  total,
  onSave,
  onRemove,
  busy,
}: {
  title: string;
  items: NetWorthItem[];
  total: number;
  onSave: (id: string, patch: SavePatch) => void;
  onRemove: (id: string) => void;
  busy: boolean;
}) {
  return (
    <div className="card">
      <div className="spread">
        <h2>{title}</h2>
        <strong>{formatCents(total)}</strong>
      </div>
      {items.length ? (
        <table>
          <tbody>
            {items.map((i) => (
              <ItemRow key={i.id} item={i} onSave={onSave} onRemove={onRemove} busy={busy} />
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">None yet.</p>
      )}
    </div>
  );
}

export function NetWorth() {
  const qc = useQueryClient();
  const nw = useQuery({ queryKey: ["netWorth"], queryFn: api.netWorth });

  const [name, setName] = useState("");
  const [kind, setKind] = useState("asset");
  const [amount, setAmount] = useState("");

  const setData = (data: NetWorthData) => qc.setQueryData(["netWorth"], data);

  const create = useMutation({
    mutationFn: () => api.createNetWorthItem({ name, kind, value_cents: dollarsToCents(amount) }),
    onSuccess: (data) => {
      setName("");
      setAmount("");
      setData(data);
    },
  });
  const update = useMutation({
    mutationFn: (v: { id: string; patch: SavePatch }) => api.updateNetWorthItem(v.id, v.patch),
    onSuccess: setData,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteNetWorthItem(id),
    onSuccess: setData,
  });
  const snapshot = useMutation({
    mutationFn: () => api.recordNetWorthSnapshot(),
    onSuccess: setData,
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (name && dollarsToCents(amount) > 0) create.mutate();
  };

  const data = nw.data;
  const assets = (data?.items ?? []).filter((i) => i.kind === "asset");
  const liabilities = (data?.items ?? []).filter((i) => i.kind === "liability");
  const busy = update.isPending || remove.isPending;
  const moneyTip = (value: unknown): string => formatCents(Math.round(Number(value) * 100));
  const chart = (data?.history ?? []).map((p) => ({
    date: p.as_of.slice(0, 7),
    Net: p.net_cents / 100,
  }));

  const onSave = (id: string, patch: SavePatch) => update.mutate({ id, patch });
  const onRemove = (id: string) => remove.mutate(id);

  return (
    <div>
      <div className="page-head">
        <h1>Net worth</h1>
        <button
          type="button"
          className="btn"
          onClick={() => snapshot.mutate()}
          disabled={snapshot.isPending}
        >
          {snapshot.isPending ? "Saving…" : "Record snapshot"}
        </button>
      </div>

      <div className="cards">
        <Stat
          label="Net worth"
          value={formatCents(data?.net_cents ?? 0)}
          cls={(data?.net_cents ?? 0) < 0 ? "negative" : "positive"}
        />
        <Stat label="Assets" value={formatCents(data?.assets_cents ?? 0)} />
        <Stat label="Liabilities" value={formatCents(data?.liabilities_cents ?? 0)} />
      </div>

      {chart.length > 1 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2>Net worth over time</h2>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={chart}>
              <XAxis dataKey="date" stroke="#93a1bd" fontSize={12} />
              <YAxis stroke="#93a1bd" fontSize={12} width={64} />
              <Tooltip formatter={moneyTip} />
              <Line type="monotone" dataKey="Net" stroke="#2dd4bf" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginTop: 16 }}>
        <ItemTable
          title="Assets"
          items={assets}
          total={data?.assets_cents ?? 0}
          onSave={onSave}
          onRemove={onRemove}
          busy={busy}
        />
        <ItemTable
          title="Liabilities"
          items={liabilities}
          total={data?.liabilities_cents ?? 0}
          onSave={onSave}
          onRemove={onRemove}
          busy={busy}
        />
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Add an item</h2>
        <form onSubmit={onSubmit}>
          <div className="row">
            <div className="field">
              <label>Name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Family home"
                required
              />
            </div>
            <div className="field">
              <label>Type</label>
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="asset">Asset</option>
                <option value="liability">Liability</option>
              </select>
            </div>
            <div className="field">
              <label>Value ($)</label>
              <input
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="e.g. 850000"
                inputMode="decimal"
                required
              />
            </div>
          </div>
          {create.isError && <div className="error">Couldn’t add that item.</div>}
          <button
            className="btn btn-primary"
            disabled={create.isPending || !name || dollarsToCents(amount) <= 0}
          >
            Add item
          </button>
        </form>
      </div>

      {data && data.items.length === 0 && (
        <p className="muted" style={{ marginTop: 12 }}>
          Add your assets (home, super, savings) and liabilities (mortgage, loans) to track net
          worth over time — or load demo data from Settings.
        </p>
      )}
    </div>
  );
}
