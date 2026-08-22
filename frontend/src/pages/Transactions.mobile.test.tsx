import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PeriodProvider } from "../period/PeriodProvider";
import { setViewport } from "../setupTests";
import { renderApp, stubApi } from "../testing/harness";
import { Transactions } from "./Transactions";

/**
 * Below 640px the transactions list is a stack of cards rather than a seven-column
 * table. It is the same data and the same controls; these tests pin that the phone
 * layout keeps every affordance the table has — selection, bulk selection, the
 * category picker, the lock, the transfer tag — since a card list that quietly drops
 * one of them looks finished and is not.
 */

const PERIOD = {
  "/periods/options": {
    default: "fy:2024",
    relative: [],
    financial_years: [
      {
        value: "fy:2024",
        label: "FY2024–25",
        start: "2024-07-01",
        end: "2025-06-30",
        quarters: [],
        months: [],
      },
    ],
  },
  "/periods/resolve": {
    start: "2024-07-01",
    end: "2025-06-30",
    label: "FY2024–25",
    is_current: true,
  },
};

const TXNS = {
  "/transactions": {
    total: 2,
    page: 1,
    page_size: 25,
    items: [
      {
        id: "t1",
        txn_date: "2024-08-02",
        raw_description: "WOOLWORTHS 1234",
        merchant: "Woolworths",
        amount_cents: -4250,
        account_id: "a1",
        account_name: "Everyday",
        category_id: null,
        category_name: null,
        category_locked: false,
        is_transfer: false,
      },
      {
        id: "t2",
        txn_date: "2024-08-03",
        raw_description: "TRANSFER TO SAVINGS",
        merchant: null,
        amount_cents: -20000,
        account_id: "a1",
        account_name: "Everyday",
        category_id: null,
        category_name: null,
        category_locked: true,
        is_transfer: true,
      },
    ],
  },
  "/accounts": [{ id: "a1", name: "Everyday", type: "everyday", institution: null, balance_cents: 0, txn_count: 2 }],
  "/categories": [
    { id: "p1", name: "Food", parent_id: null, kind: "expense" },
    { id: "c1", name: "Supermarkets", parent_id: "p1", kind: "expense" },
  ],
};

/** Renders and waits for all four queries the page fires, so nothing lands after
 *  the assertions and updates state outside `act`. */
const showList = async () => {
  const result = renderApp(
    <PeriodProvider>
      <Transactions />
    </PeriodProvider>,
    { route: "/transactions?period=fy:2024" },
  );
  await screen.findByText("Woolworths");
  await screen.findAllByRole("option", { name: "Everyday" });
  await screen.findAllByRole("option", { name: "Supermarkets" });
  await waitFor(() => expect(urlsFetched()).toContain("/api/periods/resolve"));
  return result;
};

const urlsFetched = () =>
  vi.mocked(fetch).mock.calls.map((c) => String(c[0]).split("?")[0]);

afterEach(() => {
  vi.unstubAllGlobals();
  setViewport(800);
});

describe("Transactions on a phone", () => {
  beforeEach(() => {
    setViewport(390);
    stubApi({ ...PERIOD, ...TXNS });
  });

  it("shows cards instead of a table", async () => {
    const { container } = await showList();
    expect(container.querySelectorAll(".txn-card")).toHaveLength(2);
    expect(container.querySelector("table")).toBeNull();
  });

  it("keeps every per-row control the table row has", async () => {
    const { container } = await showList();
    const card = container.querySelectorAll(".txn-card")[0] as HTMLElement;

    expect(within(card).getByLabelText("Select Woolworths")).toBeInTheDocument();
    expect(within(card).getByLabelText("Category")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Lock category" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "More actions" })).toBeInTheDocument();
    // Date and account are the columns the card folds into a meta line.
    expect(within(card).getByText("Everyday")).toBeInTheDocument();
  });

  it("carries the transfer tag onto the card", async () => {
    const { container } = await showList();
    const cards = container.querySelectorAll(".txn-card");
    expect(within(cards[1] as HTMLElement).getByText("transfer")).toBeInTheDocument();
    expect(within(cards[0] as HTMLElement).queryByText("transfer")).toBeNull();
  });

  it("still offers select-all, which lives in the header on a wide screen", async () => {
    await showList();
    expect(screen.getByLabelText(/Select all 2 on this page/)).toBeInTheDocument();
  });

  it("replaces the column headers with sort chips and stacked filters", async () => {
    await showList();
    const chips = screen.getByRole("group", { name: "Sort by" });
    expect(within(chips).getAllByRole("button")).toHaveLength(5);

    // The filter row's fields are reachable from the same toggle.
    expect(screen.queryByLabelText("Filter Description")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Filter/ }));
    expect(await screen.findByLabelText("Filter Description")).toBeInTheDocument();
  });

  it("asks for a shorter page, since a card is taller than a row", async () => {
    await showList();
    const urls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]));
    expect(urls.find((u) => u.includes("/transactions?"))).toContain("page_size=25");
  });
});

describe("Transactions on a laptop", () => {
  beforeEach(() => {
    setViewport(1280);
    stubApi({ ...PERIOD, ...TXNS });
  });

  it("shows the table, not cards", async () => {
    const { container } = await showList();
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll(".txn-card")).toHaveLength(0);
    expect(screen.queryByRole("group", { name: "Sort by" })).toBeNull();
  });

  it("asks for the full page", async () => {
    await showList();
    const urls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]));
    expect(urls.find((u) => u.includes("/transactions?"))).toContain("page_size=50");
  });
});
