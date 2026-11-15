import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { Transaction, TransferKind } from "../api/types";
import { formatCents, formatDate } from "../format";
import { useDismissable } from "../hooks/useDismissable";
import { TRANSFER_KINDS, TRANSFER_KIND_LABELS } from "./transferLabels";

interface Props {
  txn: Transaction;
  onClose: () => void;
  onDone: (message: string) => void;
}

const why = (error: unknown, fallback: string) =>
  error instanceof ApiError ? `${fallback} ${error.message}` : fallback;

/**
 * One transaction's link: what it is linked to, to change its kind or undo it -- or,
 * when it isn't linked, the transactions in your other accounts it could be the other
 * half of, to link it by hand.
 */
export function TransferDialog({ txn, onClose, onDone }: Props) {
  const fid = useId();
  const qc = useQueryClient();
  const panel = useDismissable<HTMLDivElement>(true, onClose);
  const linked = Boolean(txn.transfer_group_id);
  const candidates = useQuery({
    queryKey: ["link-candidates", txn.id],
    queryFn: () => api.linkCandidates(txn.id),
    enabled: !linked,
    gcTime: 0,
  });
  const [chosen, setChosen] = useState<string | null>(null);
  const [kind, setKind] = useState<TransferKind | "">("");
  const finish = (message: string) => {
    void qc.invalidateQueries();
    onDone(message);
    onClose();
  };

  const link = useMutation({
    mutationFn: () =>
      api.linkTransfer({ first_id: txn.id, second_id: chosen ?? "", kind: kind || undefined }),
    onSuccess: (g) =>
      finish(g.kind ? `Linked as ${TRANSFER_KIND_LABELS[g.kind].toLowerCase()}.` : "Linked."),
  });
  const unlink = useMutation({
    mutationFn: () => api.unlinkTransfer(txn.transfer_group_id ?? ""),
    onSuccess: () => finish("Unlinked. Both are ordinary transactions again."),
  });
  const rekind = useMutation({
    mutationFn: (k: TransferKind) => api.changeTransferKind(txn.transfer_group_id ?? "", k),
    onSuccess: (g) => finish(g.kind ? `Changed to ${TRANSFER_KIND_LABELS[g.kind]}.` : "Changed."),
  });
  const failed = link.error ?? unlink.error ?? rekind.error;

  return (
    <div className="modal-overlay">
      <div className="modal" ref={panel} role="dialog" aria-modal="true" aria-labelledby={`${fid}-title`}>
        <h2 id={`${fid}-title`}>Transfer</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {formatDate(txn.txn_date)} · {txn.account_name} · {txn.raw_description} ·{" "}
          {formatCents(txn.amount_cents)}
        </p>

        {linked ? (
          <>
            <p>
              Linked with <strong>{txn.counterpart_account_name ?? "another account"}</strong>.
            </p>
            <div className="field">
              <label htmlFor={`${fid}-kind`}>Kind</label>
              <select
                id={`${fid}-kind`}
                value={txn.transfer_kind ?? "own_accounts"}
                disabled={rekind.isPending}
                onChange={(e) => rekind.mutate(e.target.value as TransferKind)}
              >
                {TRANSFER_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {TRANSFER_KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </div>
          </>
        ) : (
          <>
            <p className="muted">
              Pick the other half of this transfer: it is in another of your accounts, within
              two weeks.
            </p>
            {candidates.data && candidates.data.length === 0 && (
              <p className="muted">Nothing in your other accounts could be the other half.</p>
            )}
            <div role="radiogroup" aria-label="Other half" className="link-candidates">
              {(candidates.data ?? []).map((c) => (
                <label key={c.id} className="check-row">
                  <input
                    type="radio"
                    name={`${fid}-other`}
                    checked={chosen === c.id}
                    onChange={() => setChosen(c.id)}
                  />
                  <span>
                    {formatDate(c.txn_date)} · <strong>{c.account_name}</strong> · {c.raw_description}{" "}
                    · {formatCents(c.amount_cents)}
                  </span>
                </label>
              ))}
            </div>
            <div className="field">
              <label htmlFor={`${fid}-link-kind`}>Kind</label>
              <select
                id={`${fid}-link-kind`}
                value={kind}
                onChange={(e) => setKind(e.target.value as TransferKind | "")}
              >
                <option value="">By the account it went into</option>
                {TRANSFER_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {TRANSFER_KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}

        {failed && <div className="error">{why(failed, "That didn't work.")}</div>}

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
          {linked ? (
            <button className="btn btn-danger" disabled={unlink.isPending} onClick={() => unlink.mutate()}>
              {unlink.isPending ? "Unlinking…" : "Unlink"}
            </button>
          ) : (
            <button
              className="btn btn-primary"
              disabled={!chosen || link.isPending}
              onClick={() => link.mutate()}
            >
              {link.isPending ? "Linking…" : "Link"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
