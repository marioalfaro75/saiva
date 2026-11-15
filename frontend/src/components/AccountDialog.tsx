import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { Account } from "../api/types";
import { useDismissable } from "../hooks/useDismissable";
import { ACCOUNT_TYPES, accountTypeLabel } from "./accountTypes";

interface Props {
  account: Account;
  onClose: () => void;
  onDone: (message: string) => void;
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/**
 * Edit an account: its name and type, and the wording that marks money moving to or from
 * it in your other accounts' descriptions ("By Authority To Cc"). Changing the type or the
 * wording looks through your transactions again, so pairs it now explains are linked.
 */
export function AccountDialog({ account, onClose, onDone }: Props) {
  const fid = useId();
  const qc = useQueryClient();
  const panel = useDismissable<HTMLDivElement>(true, onClose);
  const [name, setName] = useState(account.name);
  const [type, setType] = useState(account.type);
  const [institution, setInstitution] = useState(account.institution ?? "");
  const [wording, setWording] = useState((account.transfer_wording ?? []).join("\n"));

  const phrases = wording
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const wordingChanged =
    phrases.join("\n") !== (account.transfer_wording ?? []).join("\n");
  const typeChanged = type !== account.type;

  const save = useMutation({
    mutationFn: async () => {
      await api.updateAccount(account.id, {
        name: name.trim(),
        type,
        institution: institution.trim() || null,
        transfer_wording: phrases,
      });
      // What the wording or type now explains is linked straight away, not at the next import.
      return wordingChanged || typeChanged ? api.detectTransfers() : null;
    },
    onSuccess: (scan) => {
      void qc.invalidateQueries();
      onDone(
        scan
          ? `Saved. ${plural(scan.linked / 2, "transfer")} linked, ${scan.to_review} to review.`
          : "Saved.",
      );
      onClose();
    },
  });

  return (
    <div className="modal-overlay">
      <div className="modal" ref={panel} role="dialog" aria-modal="true" aria-labelledby={`${fid}-title`}>
        <h2 id={`${fid}-title`}>Edit account</h2>
        <div className="field">
          <label htmlFor={`${fid}-name`}>Name</label>
          <input id={`${fid}-name`} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-type`}>Type</label>
          <select id={`${fid}-type`} value={type} onChange={(e) => setType(e.target.value)}>
            {ACCOUNT_TYPES.map((t) => (
              <option key={t} value={t}>
                {accountTypeLabel(t)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${fid}-institution`}>Institution</label>
          <input
            id={`${fid}-institution`}
            value={institution}
            onChange={(e) => setInstitution(e.target.value)}
            placeholder="optional"
          />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-wording`}>Transfer wording</label>
          <textarea
            id={`${fid}-wording`}
            rows={4}
            value={wording}
            onChange={(e) => setWording(e.target.value)}
            placeholder={"By Authority To Cc\nPayment to card"}
            aria-describedby={`${fid}-wording-help`}
          />
          <p id={`${fid}-wording-help`} className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
            One per line. When a description in another of your accounts contains one of these,
            Saiva knows the money went to or came from {account.name}.
          </p>
        </div>
        <p className="muted" style={{ fontSize: 13 }}>
          Account number:{" "}
          {account.bank_identifier ?? "none yet (it's recorded when you import a statement)"}
        </p>
        {save.isError && (
          <div className="error">
            {save.error instanceof ApiError
              ? `Not saved. ${save.error.message}`
              : "Not saved."}
          </div>
        )}
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={save.isPending || !name.trim()}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
