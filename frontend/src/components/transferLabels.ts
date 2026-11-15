import type { Transaction, TransferKind } from "../api/types";

/** What each kind of linked pair is called, everywhere it is shown. */
export const TRANSFER_KIND_LABELS: Record<TransferKind, string> = {
  card_payment: "Card payment",
  own_accounts: "Between accounts",
  loan_repayment: "Loan repayment",
};

export const TRANSFER_KINDS = Object.keys(TRANSFER_KIND_LABELS) as TransferKind[];

/** "Card payment → Visa", "Between accounts ← Everyday": the kind, and where the money went
 * or came from. A transaction flagged as a transfer with no pair reads just "transfer". */
export function transferLabel(t: Transaction): string | null {
  if (t.transfer_kind) {
    const other = t.counterpart_account_name;
    const arrow = t.amount_cents < 0 ? "→" : "←";
    const kind = t.repayment === "extra" ? "Extra repayment" : TRANSFER_KIND_LABELS[t.transfer_kind];
    return other ? `${kind} ${arrow} ${other}` : kind;
  }
  return t.is_transfer ? "transfer" : null;
}

/** What a pair into an account of this type is: the server's kind_for_type. */
export function kindForType(type: string): TransferKind {
  if (type === "credit_card") return "card_payment";
  if (type === "home_loan" || type === "personal_loan") return "loan_repayment";
  return "own_accounts";
}
