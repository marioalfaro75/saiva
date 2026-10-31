import { describe, expect, it } from "vitest";

import type { PreviewRow } from "../api/types";
import { relativeDays } from "../format";
import { matchEvidenceOf, neighbourLine } from "./matchEvidence";

/**
 * The sentences a person reads before ticking the box.
 *
 * Tested without a DOM because the prose is the hard part: each of the matcher's four
 * tiers found the row for a different reason, and two of those sentences could be
 * confidently wrong in a way that would make someone double-count or lose a real
 * transaction.
 */

const row = (over: Partial<PreviewRow> = {}): PreviewRow =>
  ({
    row_index: 0,
    txn_date: "2026-06-03",
    amount_cents: -4550,
    raw_description: "EFTPOS WOOLWORTHS 4521 SYDNEY NSW",
    merchant: "Woolworths",
    suggested_category_id: null,
    suggested_category_name: "Supermarkets",
    confidence: null,
    is_duplicate: true,
    status: "duplicate_probable",
    duplicate_reason: "Same merchant and amount on 01 Jun 2026, worded differently",
    matched_txn_id: "t1",
    matched_date: "2026-06-01",
    matched_description: "WOOLWORTHS METRO",
    matched_amount_cents: -4550,
    matched_merchant: "Woolworths",
    matched_category_name: "Supermarkets",
    matched_source: "import",
    matched_imported_at: "2026-06-20T03:00:00",
    matched_imported_from: "cba-june.csv",
    matched_days_delta: -2,
    matched_wording: "differs",
    match_tier: "same_merchant",
    matched_neighbours: [],
    matched_neighbours_total: 0,
    will_import: false,
    account_id: "a1",
    account_name: "Everyday",
    ...over,
  }) as PreviewRow;

describe("when there is nothing to show", () => {
  it("returns null for a row that matched nothing", () => {
    expect(matchEvidenceOf(row({ matched_txn_id: null, status: "new" }))).toBeNull();
  });

  it("returns null when the field is absent entirely", () => {
    // An older cached response predates these fields, so the optionality is
    // load-bearing rather than defensive habit.
    const stale = { ...row() } as Record<string, unknown>;
    delete stale.matched_txn_id;
    expect(matchEvidenceOf(stale as unknown as PreviewRow)).toBeNull();
  });
});

describe("the headline names the tier's own reason", () => {
  it("says the wording differs on the merchant tier", () => {
    // The tier's whole premise. Saying "similar wording" here would contradict the
    // two descriptions printed underneath.
    expect(matchEvidenceOf(row())!.headline).toContain("worded differently");
  });

  it("gives the direction of the gap on a near match", () => {
    const e = matchEvidenceOf(
      row({ match_tier: "near_match", matched_days_delta: -2, matched_wording: "noise" }),
    )!;
    expect(e.headline).toContain("2 days earlier");
  });

  it("does not say 'same day, 0 days' on a same-day near match", () => {
    const e = matchEvidenceOf(
      row({ match_tier: "near_match", matched_days_delta: 0, matched_wording: null }),
    )!;
    expect(e.headline).toBe("Same amount and day, similar wording");
  });

  it("falls back to the server's reason for an unknown tier", () => {
    const e = matchEvidenceOf(row({ match_tier: null }))!;
    expect(e.headline).toContain("Same merchant and amount on 01 Jun 2026");
  });
});

describe("the wording verdict", () => {
  it("reassures only when the reference number is genuinely the same", () => {
    const e = matchEvidenceOf(row({ match_tier: "near_match", matched_wording: "noise" }))!;
    expect(e.headline).toContain("apart from a reference number");
  });

  it("does NOT reassure when the stripped numbers differ", () => {
    // fuzzy_norm strips any run of 4+ digits, so "TRANSFER TO 123456789" and
    // "TRANSFER TO 987654321" normalise equal while being payments to different
    // payees — which is what rent and a loan repayment look like. Calling that
    // "only a reference number" is the one sentence here that could cause a loss.
    const e = matchEvidenceOf(row({ match_tier: "near_match", matched_wording: "digits" }))!;
    expect(e.headline).toContain("the numbers in it differ");
    expect(e.headline).not.toContain("apart from a reference number");
  });

  it("says nothing extra when the wordings simply differ", () => {
    const e = matchEvidenceOf(row({ matched_wording: "differs" }))!;
    expect(e.headline).not.toContain("wording apart");
  });
});

describe("the incoming wording", () => {
  it("is shown when the row displays a normalised merchant instead", () => {
    // Both render paths print `merchant ?? raw_description`, so on this tier the one
    // discriminating string would otherwise be invisible on the incoming side.
    expect(matchEvidenceOf(row())!.yourFile).toBe("EFTPOS WOOLWORTHS 4521 SYDNEY NSW");
  });

  it("is not repeated when the row already shows the raw description", () => {
    expect(matchEvidenceOf(row({ merchant: null }))!.yourFile).toBeNull();
    expect(matchEvidenceOf(row({ merchant: "COFFEE", raw_description: "COFFEE" }))!.yourFile)
      .toBeNull();
  });
});

describe("provenance", () => {
  it("names the file and date an imported match came from", () => {
    // Asserted as a shape rather than an exact string: the month abbreviation comes
    // from Intl and "June" does not shorten, so pinning it would be pinning the
    // locale data rather than this module's behaviour.
    expect(matchEvidenceOf(row())!.provenance).toMatch(/^Imported 20 Jun\w* 2026 from cba-june\.csv$/);
  });

  it("says plainly when the match was entered by hand", () => {
    // The case that most deserves a second look, and the one the app could not
    // previously express at all.
    const e = matchEvidenceOf(row({ matched_source: "manual", matched_imported_from: null }))!;
    expect(e.provenance).toContain("by hand");
    expect(e.provenance).toContain("did not come from a statement");
  });

  it("is absent rather than invented when the batch is unknown", () => {
    expect(matchEvidenceOf(row({ matched_imported_from: null }))!.provenance).toBeNull();
  });
});

describe("the neighbourhood", () => {
  it("claims only what was searched when nothing is nearby", () => {
    // The search only ever looked at rows matching this amount or merchant, so a
    // quiet month of unrelated spending is invisible to it. "Nothing else in this
    // account" would be false.
    const e = matchEvidenceOf(row({ matched_neighbours_total: 0 }))!;
    expect(e.nearby!.summary).toContain("at this amount or merchant");
    expect(e.nearby!.summary).not.toContain("Nothing else in this account");
  });

  it("counts what the account already holds, and says how many are not listed", () => {
    const e = matchEvidenceOf(
      row({
        matched_neighbours_total: 5,
        matched_neighbours: [
          { txn_date: "2026-06-04", amount_cents: -4550, description: "A", same_amount: true, same_merchant: false },
          { txn_date: "2026-06-05", amount_cents: -4550, description: "B", same_amount: true, same_merchant: true },
        ],
      }),
    )!;
    expect(e.nearby!.summary).toContain("5 other rows");
    expect(e.nearby!.more).toBe(3);
  });

  it("uses the singular for one", () => {
    const e = matchEvidenceOf(row({ matched_neighbours_total: 1 }))!;
    expect(e.nearby!.summary).toContain("one other row");
  });

  it("is absent on a row that is not asking for a decision", () => {
    // Only probable duplicates get a neighbourhood from the server; claiming
    // "nothing nearby" for the others would be asserting an unasked question.
    expect(matchEvidenceOf(row({ status: "duplicate_exact" }))!.nearby).toBeNull();
  });

  it("says why each neighbour is listed", () => {
    expect(
      neighbourLine({ txn_date: "2026-06-04", amount_cents: -4550, description: "COFFEE", same_amount: true, same_merchant: false }),
    ).toContain("same amount");
    expect(
      neighbourLine({ txn_date: "2026-06-04", amount_cents: -4550, description: "COFFEE", same_amount: true, same_merchant: true }),
    ).toContain("same amount and merchant");
    expect(
      neighbourLine({ txn_date: "2026-06-04", amount_cents: -900, description: "COFFEE", same_amount: false, same_merchant: true }),
    ).toContain("same merchant");
  });
});

describe("relativeDays keeps the direction", () => {
  it("distinguishes earlier from later, which abs() in the matcher does not", () => {
    expect(relativeDays(0)).toBe("same day");
    expect(relativeDays(-1)).toBe("1 day earlier");
    expect(relativeDays(-2)).toBe("2 days earlier");
    expect(relativeDays(2)).toBe("2 days later");
  });
});
