import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PeriodProvider } from "../period/PeriodProvider";
import { setViewport } from "../setupTests";
import { renderApp, stubApi } from "../testing/harness";
import { Transactions } from "./Transactions";

/**
 * Transfers on the Transactions page: a linked row says what kind it is and where the money
 * went; its note shows under the description; the Transfers tab counts what waits for review
 * and can be opened straight from a link; "Hide transfers" asks the server for the list
 * without them.
 */

const PERIOD = {
  "/periods/options": {
    default: "fy:2026",
    relative: [],
    financial_years: [
      {
        value: "fy:2026",
        label: "FY2026–27",
        start: "2026-07-01",
        end: "2027-06-30",
        quarters: [],
        months: [],
      },
    ],
  },
  "/periods/resolve": { start: "2026-07-01", end: "2027-06-30", label: "FY2026–27", is_current: true },
};

const row = (extra: Record<string, unknown>) => ({
  txn_date: "2026-10-29",
  merchant: null,
  account_id: "a1",
  account_name: "Savings",
  category_id: null,
  category_name: null,
  category_locked: false,
  is_transfer: false,
  notes: null,
  ...extra,
});

const STUBS = {
  ...PERIOD,
  "/transactions": {
    total: 3,
    page: 1,
    page_size: 50,
    items: [
      row({
        id: "t1",
        raw_description: "By Authority To Cc",
        amount_cents: -821370,
        is_transfer: true,
        transfer_group_id: "g1",
        transfer_kind: "card_payment",
        counterpart_id: "t9",
        counterpart_account_name: "Credit card",
      }),
      row({
        id: "t2",
        raw_description: "Deposit",
        amount_cents: 50000,
        notes: "Gift from Mum",
      }),
      row({ id: "t3", raw_description: "Woolworths", amount_cents: -4250 }),
    ],
  },
  "/transfers/suggestions": [
    { outflow: row({ id: "o" }), inflow: row({ id: "i" }), kind: "own_accounts", days_apart: 0 },
    { outflow: row({ id: "o2" }), inflow: row({ id: "i2" }), kind: "own_accounts", days_apart: 0 },
  ],
  "/transfers/recent": [],
  "/accounts": [{ id: "a1", name: "Savings", type: "savings", institution: null, balance_cents: 0, txn_count: 3 }],
  "/categories": [],
};

/** Renders, and waits for the period to settle: the list is asked for again once it has,
 *  and its rows go while that answer is on its way. */
const show = async (route = "/transactions?period=fy:2026") => {
  renderApp(
    <PeriodProvider>
      <Transactions />
    </PeriodProvider>,
    { route },
  );
  await waitFor(() =>
    expect(vi.mocked(fetch).mock.calls.map((c) => String(c[0]))).toContainEqual(
      expect.stringContaining("/api/periods/resolve"),
    ),
  );
};
const rowOf = (text: string) => {
  const found = screen.getAllByRole("row").find((r) => r.textContent?.includes(text));
  if (!found) throw new Error(`no row with ${text}`);
  return found;
};
const transactionUrls = () =>
  vi
    .mocked(fetch)
    .mock.calls.map((c) => String(c[0]))
    .filter((u) => u.startsWith("/api/transactions?"));

beforeEach(() => {
  setViewport(1280);
  stubApi(STUBS);
});
afterEach(() => {
  vi.unstubAllGlobals();
  setViewport(800);
});

describe("Transactions: transfers", () => {
  it("tags a linked row with its kind and the other account", async () => {
    await show();
    await waitFor(() => rowOf("By Authority To Cc"));
    expect(within(rowOf("By Authority To Cc")).getByText("Card payment → Credit card")).toHaveClass(
      "tag",
    );
    expect(within(rowOf("Woolworths")).queryByText(/→|←|transfer/)).toBeNull();
    expect(within(rowOf("By Authority To Cc")).getByRole("button", { name: "Transfer link" })).toBeInTheDocument();
    expect(within(rowOf("Woolworths")).getByRole("button", { name: "Link as a transfer" })).toBeInTheDocument();
  });

  it("shows a row's note under its description", async () => {
    await show();
    const deposit = await waitFor(() => rowOf("Deposit"));
    const note = within(deposit).getByText("Gift from Mum");
    expect(note).toHaveClass("txn-note");
  });

  it("counts the pairs waiting for review on the Transfers tab", async () => {
    await show();
    expect(await screen.findByLabelText("2 to review")).toHaveTextContent("2");
  });

  it("opens on the Transfers tab when the link asks for it", async () => {
    await show("/transactions?period=fy:2026&tab=transfers");
    expect(await screen.findByRole("heading", { name: "To review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Transfers/ })).toHaveClass("active");
    // The list itself is not fetched behind the tab.
    expect(transactionUrls()).toHaveLength(0);
  });

  it("ignores a tab it does not know", async () => {
    await show("/transactions?period=fy:2026&tab=nonsense");
    await screen.findByText("By Authority To Cc");
    expect(screen.queryByRole("heading", { name: "To review" })).toBeNull();
  });

  it("asks for the list without transfers when they are hidden", async () => {
    await show();
    await screen.findByText("By Authority To Cc");
    expect(transactionUrls().every((u) => !u.includes("include_transfers"))).toBe(true);

    fireEvent.click(screen.getByRole("checkbox", { name: "Hide transfers" }));
    await waitFor(() =>
      expect(transactionUrls().some((u) => u.includes("include_transfers=false"))).toBe(true),
    );
  });

  it("opens the link dialog from the ⇄ button", async () => {
    await show();
    await waitFor(() => rowOf("By Authority To Cc"));
    fireEvent.click(within(rowOf("By Authority To Cc")).getByRole("button", { name: "Transfer link" }));
    const dialog = await screen.findByRole("dialog", { name: "Transfer" });
    expect(within(dialog).getByText("Credit card")).toBeInTheDocument();
  });
});
