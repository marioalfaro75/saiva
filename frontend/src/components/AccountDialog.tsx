import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { Account, TransferScan } from "../api/types";
import { useDismissable } from "../hooks/useDismissable";
import { ACCOUNT_TYPES, accountTypeLabel, wordingProblem } from "./accountTypes";
import { kindForType, TRANSFER_KIND_LABELS } from "./transferLabels";

interface Props {
  account: Account;
  onClose: () => void;
  onDone: (message: string) => void;
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

interface Saved {
  scan: TransferScan | null;
  scanFailed: boolean;
  rekinded: number;
}

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

  const problem = wordingProblem(phrases);

  const save = useMutation({
    mutationFn: async (): Promise<Saved> => {
      const saved = await api.updateAccount(account.id, {
        name: name.trim(),
        type,
        institution: institution.trim() || null,
        transfer_wording: phrases,
      });
      const rekinded = saved.transfers_rekinded ?? 0;
      if (!wordingChanged && !typeChanged) return { scan: null, scanFailed: false, rekinded };
      // What the wording or type now explains is linked straight away, not at the next
      // import. The account is saved whether or not this works.
      try {
        return { scan: await api.detectTransfers(), scanFailed: false, rekinded };
      } catch {
        return { scan: null, scanFailed: true, rekinded };
      }
    },
    onSuccess: ({ scan, scanFailed, rekinded }) => {
      void qc.invalidateQueries();
      // Between two loan types the kind stays a loan repayment; what moves is where the
      // repayment is filed.
      const sameKind = kindForType(account.type) === kindForType(type);
      const recounted = !rekinded
        ? ""
        : sameKind && kindForType(type) === "loan_repayment"
          ? ` ${plural(rekinded, "loan repayment")} now filed as ${type === "home_loan" ? "Mortgage" : "Loan repayments"}.`
          : ` ${plural(rekinded, "linked transfer")} changed to ${TRANSFER_KIND_LABELS[kindForType(type)]}.`;
      onDone(
        scanFailed
          ? `Saved.${recounted} Couldn't look through your transactions again: try Transactions → Transfers → Look again.`
          : scan
            ? `Saved.${recounted} ${plural(scan.linked / 2, "transfer")} linked, ${scan.to_review} to review.`
            : `Saved.${recounted}`,
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
            aria-invalid={problem !== null}
            aria-describedby={`${fid}-wording-help${problem ? ` ${fid}-wording-error` : ""}`}
          />
          {/* Polite: it changes as the person types, and must not interrupt them. */}
          <div id={`${fid}-wording-error`} role="status" aria-live="polite">
            {problem && (
              <div className="error" style={{ marginTop: 6 }}>
                {problem}
              </div>
            )}
          </div>
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
            disabled={save.isPending || !name.trim() || problem !== null}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
