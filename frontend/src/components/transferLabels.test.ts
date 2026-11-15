import { describe, expect, it } from "vitest";

import type { Transaction } from "../api/types";
import { transferLabel } from "./transferLabels";

const t = (extra: Partial<Transaction>) =>
  ({ amount_cents: -100, is_transfer: false, ...extra }) as Transaction;

describe("transferLabel", () => {
  it("names the kind and where the money went", () => {
    expect(
      transferLabel(t({ transfer_kind: "card_payment", counterpart_account_name: "Visa" })),
    ).toBe("Card payment → Visa");
  });

  it("points the other way for money coming in", () => {
    expect(
      transferLabel(
        t({ amount_cents: 500, transfer_kind: "own_accounts", counterpart_account_name: "Everyday" }),
      ),
    ).toBe("Between accounts ← Everyday");
  });

  it("names just the kind when the other side is not known", () => {
    expect(transferLabel(t({ transfer_kind: "loan_repayment" }))).toBe("Loan repayment");
  });

  it("reads 'transfer' for one flagged by hand, and nothing otherwise", () => {
    expect(transferLabel(t({ is_transfer: true }))).toBe("transfer");
    expect(transferLabel(t({}))).toBeNull();
  });

  it("names an extra home loan repayment as one", () => {
    expect(
      transferLabel(
        t({ transfer_kind: "loan_repayment", repayment: "extra", counterpart_account_name: "Westpac" }),
      ),
    ).toBe("Extra repayment → Westpac");
    expect(
      transferLabel(
        t({ transfer_kind: "loan_repayment", repayment: "regular", counterpart_account_name: "Westpac" }),
      ),
    ).toBe("Loan repayment → Westpac");
    // Seen from the loan's side.
    expect(
      transferLabel(
        t({
          amount_cents: 500000,
          transfer_kind: "loan_repayment",
          repayment: "extra",
          counterpart_account_name: "Savings",
        }),
      ),
    ).toBe("Extra repayment ← Savings");
  });
});

