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

const chooseFile = () =>
  fireEvent.change(screen.getByLabelText("Choose a file"), { target: { files: [csv()] } });

/** Drops a file, then answers the account question the file could not. */
async function startImport() {
  const view = renderApp(<ImportPage />);
  chooseFile();
  await screen.findByText("What is in each column?");
  // Asked only now, and only because this file names no accounts of its own.
  fireEvent.change(screen.getByLabelText("These transactions all belong to"), {
    target: { value: "a1" },
  });
  return view;
}

/** A file whose account column is recognised, so the question never arises. */
async function startMultiAccountImport() {
  const view = renderApp(<ImportPage />);
  chooseFile();
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
    fireEvent.change(screen.getByLabelText("These transactions all belong to"), {
      target: { value: "a2" },
    });
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

/**
 * The page used to open with an account dropdown beside the file input, which reads
 * as a form to fill in and puts the account question first — at the one moment
 * nothing can answer it, since whether the file covers one account or names its own
 * is not yet known.
 */
describe("Import before a file is chosen", () => {
  beforeEach(() => stubApi(STUBS));

  it("asks for the file and nothing else", () => {
    renderApp(<ImportPage />);
    expect(screen.getByLabelText("Choose a file")).toBeInTheDocument();
    expect(screen.queryByLabelText(/belong to/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Preview/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Import/ })).toBeNull();
  });

  it("says the account question may not arise at all", () => {
    renderApp(<ImportPage />);
    expect(screen.getByText(/you will not be asked to choose one/i)).toBeInTheDocument();
  });

  it("takes a dropped file the same way as a chosen one", async () => {
    const { container } = renderApp(<ImportPage />);
    const zone = container.querySelector(".dropzone") as HTMLElement;
    fireEvent.drop(zone, { dataTransfer: { files: [csv()] } });
    expect(await screen.findByText("What is in each column?")).toBeInTheDocument();
  });
});

describe("Starting again", () => {
  beforeEach(() => stubApi(STUBS));

  it("puts the page back to asking for a file", async () => {
    await startImport();
    expect(screen.getByText("statement.csv")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start again" }));

    expect(screen.getByLabelText("Choose a file")).toBeInTheDocument();
    expect(screen.queryByText("What is in each column?")).toBeNull();
    expect(screen.queryByText("statement.csv")).toBeNull();
  });

  it("forgets the account that was chosen for the abandoned file", async () => {
    await startImport();
    fireEvent.click(screen.getByRole("button", { name: "Start again" }));
    chooseFile();
    await screen.findByText("What is in each column?");
    // Carried over, the old choice would silently apply to a different file.
    expect(screen.getByLabelText("These transactions all belong to")).toHaveValue("");
  });
});

/**
 * Re-importing a file you already imported: thousands of rows already filed, and a
 * handful that look close enough to something existing to want a decision. Those few
 * were unreachable — the preview showed the first 200 rows of one flat list, and the
 * definite duplicates that sort ahead of them numbered in the thousands.
 */
describe("Preview of a file that is almost all duplicates", () => {
  const row = (i: number, status: string) => ({
    row_index: i,
    txn_date: "2026-05-29",
    amount_cents: -119954,
    raw_description: status === "duplicate_probable" ? `NEEDS A LOOK ${i}` : `SEEN ${i}`,
    merchant: null,
    suggested_category_id: null,
    suggested_category_name: null,
    confidence: null,
    is_duplicate: true,
    status,
    duplicate_reason: null,
    matched_txn_id: null,
    matched_date: null,
    matched_description: null,
    will_import: false,
  });

  beforeEach(() =>
    stubApi({
      ...STUBS,
      "/imports/preview": {
        rows: [
          ...Array.from({ length: 2229 }, (_, i) => row(i, "duplicate_exact")),
          ...Array.from({ length: 6 }, (_, i) => row(2229 + i, "duplicate_probable")),
        ],
        duplicate_count: 2229,
        probable_count: 6,
        unassigned_count: 0,
        accounts: [],
      },
    }),
  );

  const preview = async () => {
    await startImport();
    fireEvent.click(screen.getByRole("button", { name: /^Preview/ }));
    await screen.findByText("Preview", { selector: "h2" });
  };

  it("opens on the rows that need a decision, not the ones that do not", async () => {
    await preview();
    expect(await screen.findByRole("tab", { name: "Needs review (6)" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Already imported (2229)" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("shows every row needing review — including the last of 2,235", async () => {
    await preview();
    // Row 2,234 was past the old 200-row cap with no sort order that reached it.
    expect(await screen.findByText("NEEDS A LOOK 2234")).toBeInTheDocument();
    for (let i = 2229; i < 2235; i += 1) {
      expect(screen.getByText(`NEEDS A LOOK ${i}`)).toBeInTheDocument();
    }
  });

  it("keeps the already-imported rows capped, and says what it held back", async () => {
    await preview();
    fireEvent.click(screen.getByRole("tab", { name: "Already imported (2229)" }));
    expect(await screen.findByText(/2029 need no decision from you/)).toBeInTheDocument();
  });

  it("says so plainly when a segment is empty rather than showing a bare table", async () => {
    await preview();
    fireEvent.click(screen.getByRole("tab", { name: "Will import (0)" }));
    expect(await screen.findByText("Nothing new in this file.")).toBeInTheDocument();
  });
});
