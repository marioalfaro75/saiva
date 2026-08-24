import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setViewport } from "../setupTests";
import { renderApp, stubApi } from "../testing/harness";
import { ImportPage } from "./Import";

/**
 * The import wizard's preview is derived from the file, the account, six mapping
 * fields and the per-value account assignments. Committing sends *row indices* taken
 * from that preview, so a preview left on screen after one of those changed is not
 * merely misleading — it can import or skip the wrong rows.
 *
 * These tests drive the controls that used to leave a preview standing.
 */

const SNIFF = {
  "/imports/sniff": {
    columns: ["Date", "Details", "Amount", "Acct"],
    sample_rows: [
      ["01/08/2024", "COLES 0001", "-42.50", "1234"],
      ["02/08/2024", "COLES 0002", "-11.00", "1234"],
    ],
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

/** The same file, but its fourth column is recognised as naming the account. */
const SNIFF_MULTI = {
  "/imports/sniff": {
    ...SNIFF["/imports/sniff"],
    suggested_account_col: 3,
  },
};

const previewBody = (n: number) => ({
  rows: Array.from({ length: n }, (_, i) => ({
    row_index: i,
    txn_date: "2024-08-0" + (i + 1),
    raw_description: "COLES " + i,
    merchant: "Coles",
    amount_cents: -1000 - i,
    account_name: "Everyday",
    suggested_category_name: "Supermarkets",
    status: "new",
    will_import: true,
    duplicate_reason: null,
    matched_date: null,
    matched_description: null,
  })),
  duplicate_count: 0,
  probable_count: 0,
  unassigned_count: 0,
  accounts: [],
});

const STUBS = {
  ...SNIFF,
  "/accounts": [
    { id: "a1", name: "Everyday", type: "everyday", institution: null, balance_cents: 0, txn_count: 0 },
    { id: "a2", name: "Savings", type: "savings", institution: null, balance_cents: 0, txn_count: 0 },
  ],
  "/imports/preview": previewBody(3),
};

const csv = () => new File(["Date,Details,Amount,Acct\n"], "statement.csv", { type: "text/csv" });

/** Picks an account and a file, then waits for the sniffed mapping to appear. */
async function startImport() {
  const view = renderApp(<ImportPage />);
  await screen.findByRole("option", { name: "Everyday" });
  fireEvent.change(screen.getByLabelText("Account"), { target: { value: "a1" } });
  fireEvent.change(screen.getByLabelText(/^File/), { target: { files: [csv()] } });
  await screen.findByText("What is in each column?");
  return view;
}

/** Picks a file whose account column is recognised, so no account is chosen first. */
async function startMultiAccountImport() {
  const view = renderApp(<ImportPage />);
  await screen.findByRole("option", { name: "Everyday" });
  fireEvent.change(screen.getByLabelText(/^File/), { target: { files: [csv()] } });
  await screen.findByText("What is in each column?");
  return view;
}

const importButton = () => screen.getByRole("button", { name: /^Import/ });
const previewButton = () => screen.getByRole("button", { name: /^Preview/ });

afterEach(() => {
  vi.unstubAllGlobals();
  setViewport(800);
});

describe("Import: preview before commit", () => {
  beforeEach(() => stubApi(STUBS));

  it("will not import until a preview says what that means", async () => {
    await startImport();
    expect(importButton()).toBeDisabled();
    expect(previewButton()).toBeEnabled();
    // The primary action is the reversible one.
    expect(previewButton()).toHaveClass("btn-primary");
    expect(importButton()).not.toHaveClass("btn-primary");
  });

  it("states its own consequence once there is a preview", async () => {
    await startImport();
    fireEvent.click(previewButton());
    await screen.findByText("Preview", { selector: "h2" });
    await waitFor(() => expect(importButton()).toBeEnabled());
    expect(importButton()).toHaveTextContent("Import 3 transactions");
  });
});

describe("Import: a preview stops being an answer when the question changes", () => {
  beforeEach(() => stubApi(STUBS));

  const preview = async () => {
    await startImport();
    fireEvent.click(previewButton());
    await screen.findByText("Preview", { selector: "h2" });
    await waitFor(() => expect(importButton()).toBeEnabled());
  };

  const expectStale = async () => {
    await waitFor(() => expect(importButton()).toBeDisabled());
    expect(screen.getByText(/this preview is out of date/)).toBeInTheDocument();
    expect(previewButton()).toHaveTextContent("Preview again");
  };

  it("notices when the destination account changes", async () => {
    await preview();
    fireEvent.change(screen.getByLabelText("Account"), { target: { value: "a2" } });
    await expectStale();
  });

  it("notices when a mapped column changes", async () => {
    await preview();
    fireEvent.change(screen.getByLabelText("Role for Acct"), { target: { value: "balance" } });
    await expectStale();
  });

  it("notices when the amount format changes", async () => {
    await preview();
    fireEvent.change(screen.getByLabelText("Role for Amount"), { target: { value: "money_out" } });
    await expectStale();
  });

  it("goes fresh again on a second preview", async () => {
    await preview();
    fireEvent.change(screen.getByLabelText("Role for Acct"), { target: { value: "balance" } });
    await expectStale();
    fireEvent.click(previewButton());
    await waitFor(() => expect(importButton()).toBeEnabled());
    expect(screen.queryByText(/this preview is out of date/)).toBeNull();
  });
});

describe("Import: multi-account assignments", () => {
  beforeEach(() =>
    stubApi({
      ...STUBS,
      ...SNIFF_MULTI,
      "/imports/accounts/scan": [
        { value: "1234", row_count: 12, sample_description: "COLES", suggested_account_id: null },
      ],
    }),
  );

  const toAssignment = async () => {
    await startMultiAccountImport();
    // No opt-in: the file names its accounts, so the step appears by itself.
    return screen.findByText("Which account is which?");
  };

  it("marks a preview stale when a value is pointed somewhere else", async () => {
    await toAssignment();
    fireEvent.change(await screen.findByLabelText("Import 1234 into"), {
      target: { value: "a1" },
    });
    fireEvent.click(previewButton());
    await screen.findByText("Preview", { selector: "h2" });
    await waitFor(() => expect(importButton()).toBeEnabled());

    fireEvent.change(screen.getByLabelText("Import 1234 into"), { target: { value: "a2" } });
    await waitFor(() => expect(importButton()).toBeDisabled());
    expect(screen.getByText(/this preview is out of date/)).toBeInTheDocument();
  });

  it("marks a preview stale when a new account is renamed", async () => {
    await toAssignment();
    fireEvent.change(await screen.findByLabelText("Import 1234 into"), {
      target: { value: "__create__" },
    });
    fireEvent.click(previewButton());
    await waitFor(() => expect(importButton()).toBeEnabled());

    fireEvent.change(screen.getByLabelText("New account name for 1234"), {
      target: { value: "Offset" },
    });
    await waitFor(() => expect(importButton()).toBeDisabled());
  });
});

describe("Import on a phone", () => {
  beforeEach(() => {
    setViewport(390);
    stubApi(STUBS);
  });

  it("reviews the preview as cards, with the same per-row decision", async () => {
    const { container } = await startImport();
    fireEvent.click(previewButton());
    await screen.findByText("Preview", { selector: "h2" });

    await waitFor(() => expect(container.querySelectorAll(".stack-card")).toHaveLength(3));
    expect(container.querySelector(".stack-card table")).toBeNull();
    const first = container.querySelectorAll(".stack-card")[0] as HTMLElement;
    expect(within(first).getByLabelText("Import Coles")).toBeChecked();
    expect(screen.getByRole("group", { name: "Sort by" })).toBeInTheDocument();
  });
});
