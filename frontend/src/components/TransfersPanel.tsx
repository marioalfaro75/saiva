import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { TransferGroup, TransferKind, TransferSuggestion } from "../api/types";
import { formatCents, formatDate } from "../format";
import { TRANSFER_KINDS, TRANSFER_KIND_LABELS } from "./transferLabels";

interface Props {
  onFlash: (message: string) => void;
  canEdit?: boolean;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function KindSelect({
  value,
  label,
  disabled,
  onChange,
}: {
  value: TransferKind;
  label: string;
  disabled?: boolean;
  onChange: (kind: TransferKind) => void;
}) {
  return (
    <select
      className="pill-select"
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as TransferKind)}
    >
      {TRANSFER_KINDS.map((k) => (
        <option key={k} value={k}>
          {TRANSFER_KIND_LABELS[k]}
        </option>
      ))}
    </select>
  );
}

const why = (error: unknown, fallback: string) =>
  error instanceof ApiError ? `${fallback} ${error.message}` : fallback;

/**
 * Transactions → Transfers. Pairs that agree on amount and date but that nothing else
 * links wait here for a person: accept (the wording is remembered, so the next one links
 * itself), or say it is not a transfer (never suggested again). Below, what has been
 * linked, newest first, to change its kind or undo it.
 */
export function TransfersPanel({ onFlash, canEdit = true }: Props) {
  const qc = useQueryClient();
  const suggestions = useQuery({
    queryKey: ["transfer-suggestions"],
    queryFn: api.transferSuggestions,
  });
  const recent = useQuery({ queryKey: ["transfer-recent"], queryFn: () => api.recentTransfers(30) });
  const [kinds, setKinds] = useState<Record<string, TransferKind>>({});
  const [learn, setLearn] = useState(true);
  // Linking moves amounts in and out of every total, budget and list.
  const refresh = () => void qc.invalidateQueries();
  const key = (s: TransferSuggestion) => `${s.outflow.id}:${s.inflow.id}`;

  const accept = useMutation({
    mutationFn: (s: TransferSuggestion) =>
      api.acceptTransfer({
        outflow_id: s.outflow.id,
        inflow_id: s.inflow.id,
        kind: kinds[key(s)] ?? s.kind,
        learn,
      }),
    onSuccess: (r) => {
      refresh();
      onFlash(
        r.learned
          ? `Linked. From now on “${r.learned}” links payments to ${r.learned_for} by itself.`
          : "Linked.",
      );
    },
    onError: (e) => onFlash(why(e, "Couldn't link those.")),
  });
  const dismiss = useMutation({
    mutationFn: (s: TransferSuggestion) =>
      api.dismissTransfer({ outflow_id: s.outflow.id, inflow_id: s.inflow.id }),
    onSuccess: () => {
      refresh();
      onFlash("Noted: not a transfer. It won't be suggested again.");
    },
    onError: (e) => onFlash(why(e, "Couldn't save that.")),
  });
  const unlink = useMutation({
    mutationFn: (g: TransferGroup) => api.unlinkTransfer(g.group_id),
    onSuccess: () => {
      refresh();
      onFlash("Unlinked. Both are ordinary transactions again.");
    },
    onError: (e) => onFlash(why(e, "Couldn't unlink that.")),
  });
  const rekind = useMutation({
    mutationFn: (v: { group: TransferGroup; kind: TransferKind }) =>
      api.changeTransferKind(v.group.group_id, v.kind),
    onSuccess: (g) => {
      refresh();
      if (g.kind) onFlash(`Changed to ${TRANSFER_KIND_LABELS[g.kind]}.`);
    },
    onError: (e) => onFlash(why(e, "Couldn't change that.")),
  });
  const scan = useMutation({
    mutationFn: api.detectTransfers,
    onSuccess: (r) => {
      refresh();
      onFlash(
        `${plural(r.linked / 2, "transfer")} linked, ${r.to_review} to review, ` +
          `${plural(r.cash_deposits, "cash deposit")}.`,
      );
    },
    onError: (e) => onFlash(why(e, "Couldn't look through your transactions.")),
  });
  const busy = accept.isPending || dismiss.isPending;

  return (
    <div>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="spread">
          <h2 style={{ margin: 0 }}>To review</h2>
          {canEdit && (
            <button className="btn" disabled={scan.isPending} onClick={() => scan.mutate()}>
              {scan.isPending ? "Looking…" : "Look again"}
            </button>
          )}
        </div>
        <p className="muted">
          These match on amount and date, but nothing else links them. Accept the ones that
          are money moving between your accounts.
        </p>
        {canEdit && (
          <div className="check-row">
            <input
              id="transfer-learn"
              type="checkbox"
              checked={learn}
              onChange={(e) => setLearn(e.target.checked)}
            />
            <label htmlFor="transfer-learn">
              Remember the wording, so the next one links by itself
            </label>
          </div>
        )}
        {suggestions.data && suggestions.data.length === 0 && (
          <p className="muted">Nothing to review.</p>
        )}
        <ul className="transfer-list">
          {(suggestions.data ?? []).map((s) => (
            <li key={key(s)} className="transfer-item">
              <div className="transfer-sides">
                <div>
                  <span className="muted">{formatDate(s.outflow.txn_date)}</span>{" "}
                  <strong>{s.outflow.account_name}</strong> → <strong>{s.inflow.account_name}</strong>
                  <span className="num negative"> {formatCents(s.outflow.amount_cents)}</span>
                </div>
                <div className="muted">
                  Out: {s.outflow.raw_description} · In: {s.inflow.raw_description}
                  {s.days_apart > 0 && ` · ${plural(s.days_apart, "day")} apart`}
                </div>
              </div>
              {canEdit && (
                <div className="transfer-actions">
                  <KindSelect
                    value={kinds[key(s)] ?? s.kind}
                    label={`Kind of transfer for ${s.outflow.raw_description}`}
                    onChange={(k) => setKinds((all) => ({ ...all, [key(s)]: k }))}
                  />
                  <button className="btn btn-primary" disabled={busy} onClick={() => accept.mutate(s)}>
                    Accept
                  </button>
                  <button className="btn btn-ghost" disabled={busy} onClick={() => dismiss.mutate(s)}>
                    Not a transfer
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h2>Linked</h2>
        <p className="muted">
          Card payments and money between your accounts are left out of spending and income.
          A loan repayment counts as spending once, on the side that paid it.
        </p>
        {recent.data && recent.data.length === 0 && (
          <p className="muted">No transfers linked yet.</p>
        )}
        <ul className="transfer-list">
          {(recent.data ?? []).map((g) =>
            g.outflow && g.inflow ? (
              <li key={g.group_id} className="transfer-item">
                <div className="transfer-sides">
                  <div>
                    <span className="muted">{formatDate(g.outflow.txn_date)}</span>{" "}
                    <strong>{g.outflow.account_name}</strong> →{" "}
                    <strong>{g.inflow.account_name}</strong>
                    <span className="num"> {formatCents(Math.abs(g.outflow.amount_cents))}</span>
                  </div>
                  <div className="muted">{g.outflow.raw_description}</div>
                </div>
                {canEdit && (
                  <div className="transfer-actions">
                    <KindSelect
                      value={g.kind ?? "own_accounts"}
                      label={`Kind of transfer for ${g.outflow.raw_description}`}
                      disabled={rekind.isPending}
                      onChange={(kind) => rekind.mutate({ group: g, kind })}
                    />
                    <button
                      className="btn btn-ghost"
                      disabled={unlink.isPending}
                      onClick={() => unlink.mutate(g)}
                    >
                      Unlink
                    </button>
                  </div>
                )}
              </li>
            ) : null,
          )}
        </ul>
      </div>
    </div>
  );
}
