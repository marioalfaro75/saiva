import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { setViewport } from "../setupTests";
import { renderApp, stubApi } from "../testing/harness";
import { ImportPage } from "./Import";

/**
 * The evidence for a possible duplicate, in both render paths.
 *
 * It used to be a `title=""` on the status pill — nothing at all on a phone, nothing
 * to a keyboard. So every assertion here deliberately avoids hover and avoids
 * clicking: the text must simply be present.
 */

const SNIFF = {
  "/imports/sniff": {
    columns: ["Date", "Details", "Amount", "Acct"],
    sample_rows: [["01/06/2026", "WOOLWORTHS", "-45.50", "1234"]],
    delimiter: ",",
    has_header: true,
    suggested_account_col: null,
    suggested_mapping: {
      date_col: 0,
      description_col: 1,
      amount_mode: "single",
      amount_col: 2,
      debit_col: null,
      credit_col: null,
      account_col: null,
      date_format: null,
      skip_rows: 0,
      delimiter: ",",
    },
  },
};

/** One row that needs a decision, matched to a stored row worded differently. */
const reviewRow = (over: Record<string, unknown> = {}) => ({
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
  matched_neighbours: [
    {
      txn_date: "2026-06-08",
      amount_cents: -4550,
      description: "WOOLWORTHS METRO SURRY HILLS",
      same_amount: true,
      same_merchant: true,
    },
  ],
  matched_neighbours_total: 2,
  will_import: false,
  account_id: "a1",
  account_name: "Everyday",
  ...over,
});

const previewWith = (rows: Record<string, unknown>[]) => ({
  rows,
  duplicate_count: rows.length,
  probable_count: rows.length,
  unassigned_count: 0,
  accounts: [],
});

const ACCOUNTS = [
  { id: "a1", name: "Everyday", type: "everyday", institution: null, balance_cents: 0, txn_count: 0 },
];

const stubs = (rows: Record<string, unknown>[]) => ({
  ...SNIFF,
  "/accounts": ACCOUNTS,
  "/imports/preview": previewWith(rows),
});

/**
 * The evidence cell's span, beside the real header count of the table it sits in.
 *
 * Scoped to that table on purpose: the page has other tables, and a selector that
 * wanders into one of them would compare the span against the wrong header and pass
 * or fail for the wrong reason. Returned as a pair so a mismatch names both numbers.
 */
function spanVsHeader(container: HTMLElement) {
  const cell = container.querySelector(".match-tr > td")!;
  const table = cell.closest("table")!;
  return {
    span: Number(cell.getAttribute("colSpan")),
    headerCells: table.querySelectorAll("thead > tr:first-child > *").length,
  };
}

async function openPreview() {
  const view = renderApp(<ImportPage />);
  fireEvent.change(screen.getByLabelText("Choose a file"), {
    target: {
      files: [new File(["Date,Details,Amount,Acct\n"], "statement.csv", { type: "text/csv" })],
    },
  });
  await screen.findByText("What is in each column?");
  fireEvent.change(screen.getByLabelText("These transactions all belong to"), {
    target: { value: "a1" },
  });
  fireEvent.click(screen.getByRole("button", { name: /preview/i }));
  await screen.findByText("Preview", { selector: "h2" });
  return view;
}

describe("the evidence on a wide screen", () => {
  beforeEach(() => setViewport(1280));

  it("shows the stored transaction without a hover or a click", async () => {
    stubApi(stubs([reviewRow()]));
    await openPreview();
    // The whole point: present as text, not in a title attribute.
    // Exact: "WOOLWORTHS METRO" also prefixes the neighbour below it.
    const stored = await screen.findByText(/Already in Saiva:/);
    expect(stored.parentElement).toHaveTextContent("WOOLWORTHS METRO,");
    expect(screen.getByText(/Imported .* from cba-june\.csv/)).toBeInTheDocument();
  });

  it("shows the incoming wording, which the row itself replaces with the merchant", async () => {
    stubApi(stubs([reviewRow()]));
    await openPreview();
    expect(await screen.findByText(/EFTPOS WOOLWORTHS 4521 SYDNEY NSW/)).toBeInTheDocument();
  });

  it("counts what the account already holds nearby", async () => {
    stubApi(stubs([reviewRow()]));
    await openPreview();
    expect(await screen.findByText(/2 other rows at this amount or merchant/)).toBeInTheDocument();
    expect(screen.getByText(/WOOLWORTHS METRO SURRY HILLS/)).toBeInTheDocument();
  });

  it("spans exactly the header's cells, with no account column", async () => {
    // A wrong colSpan invents a phantom column and mis-renders in silence, so this
    // is asserted against the header rather than against a number.
    stubApi(stubs([reviewRow()]));
    const { container } = await openPreview();
    await waitFor(() => expect(container.querySelector(".match-tr")).not.toBeNull());
    expect(spanVsHeader(container)).toEqual({ span: 6, headerCells: 6 });
  });

  it("spans exactly the header's cells when the account column is present", async () => {
    // The account column shifts the count by one, which is the whole reason the span
    // is derived rather than written as a literal.
    stubApi({
      ...stubs([reviewRow()]),
      "/imports/sniff": { ...SNIFF["/imports/sniff"], suggested_account_col: 3 },
      "/imports/accounts/scan": [
        { value: "1234", row_count: 1, sample_description: "WOOLWORTHS", suggested_account_id: null },
      ],
      "/imports/preview": {
        ...previewWith([reviewRow()]),
        accounts: [{ value: "1234", account_id: "a1", account_name: "Everyday", rows: 1 }],
      },
    });
    // A recognised account column replaces the "all belong to" question with an
    // assignment step, so this flow is driven differently.
    const { container } = renderApp(<ImportPage />);
    fireEvent.change(screen.getByLabelText("Choose a file"), {
      target: {
        files: [new File(["Date,Details,Amount,Acct\n"], "s.csv", { type: "text/csv" })],
      },
    });
    await screen.findByText("What is in each column?");
    await screen.findByText("Which account is which?");
    fireEvent.change(await screen.findByLabelText("Import 1234 into"), {
      target: { value: "a1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByText("Preview", { selector: "h2" });
    await waitFor(() => expect(container.querySelector(".match-tr")).not.toBeNull());
    expect(spanVsHeader(container)).toEqual({ span: 7, headerCells: 7 });
  });

  it("shows no evidence row for a row that matched nothing", async () => {
    stubApi(
      stubs([
        reviewRow({
          status: "new",
          is_duplicate: false,
          will_import: true,
          matched_txn_id: null,
          matched_date: null,
          matched_description: null,
          matched_neighbours: [],
          matched_neighbours_total: 0,
        }),
      ]),
    );
    const { container } = await openPreview();
    await waitFor(() => expect(container.querySelector("tbody tr")).not.toBeNull());
    expect(container.querySelector(".match-tr")).toBeNull();
  });
});

describe("the evidence on a phone", () => {
  beforeEach(() => setViewport(390));

  it("is present as text, where a hover never existed at all", async () => {
    stubApi(stubs([reviewRow()]));
    const { container } = await openPreview();
    await waitFor(() => expect(container.querySelectorAll(".stack-card")).toHaveLength(1));
    // Still cards, still no table smuggled inside one.
    expect(container.querySelector(".stack-card table")).toBeNull();
    expect(container.querySelector(".match-evidence")).not.toBeNull();
    expect(screen.getByText(/Already in Saiva:/).parentElement)
      .toHaveTextContent("WOOLWORTHS METRO,");
  });

  it("is not dimmed with the row it belongs to", async () => {
    // Every probable duplicate defaults to not importing, so the row is dimmed —
    // and CSS opacity composites the whole subtree, so an inline 0.55 on the <li>
    // would render this evidence at 55% on the one layout where the hover text it
    // replaces was never available. No child declaration can escape that, which is
    // why the dimming has to sit on the head and meta instead.
    stubApi(stubs([reviewRow()]));
    const { container } = await openPreview();
    await waitFor(() => expect(container.querySelector(".match-evidence")).not.toBeNull());
    const card = container.querySelector<HTMLElement>(".stack-card")!;
    expect(card.style.opacity).toBe("");
    const head = container.querySelector<HTMLElement>(".stack-card-head")!;
    expect(head.style.opacity).toBe("0.55");
  });
});
