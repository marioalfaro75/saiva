import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import type { Transaction, TransferGroup, TransferSuggestion } from "../api/types";
import { TransfersPanel } from "./TransfersPanel";

/**
 * Transactions → Transfers. Pairs nothing links for sure wait here: accepting links them
 * (and says what wording it learned), "Not a transfer" drops them for good; below, what
 * is linked can change kind or be undone; "Look again" scans and says what it found.
 */

const txn = (extra: Partial<Transaction>): Transaction =>
  ({
    id: "t",
    account_id: "a1",
    account_name: "Savings",
    txn_date: "2026-10-29",
    amount_cents: -821370,
    raw_description: "By Authority To Cc",
    merchant: null,
    category_id: null,
    category_name: null,
    is_transfer: false,
    is_recurring: false,
    category_locked: false,
    confidence: null,
    source: "import",
    notes: null,
    tags: [],
    split_parent_id: null,
    ...extra,
  }) as unknown as Transaction;

const OUT = txn({ id: "out1" });
const IN = txn({
  id: "in1",
  account_id: "a2",
  account_name: "Credit card",
  amount_cents: 821370,
  raw_description: "Payment received",
});
const SUGGESTION: TransferSuggestion = { outflow: OUT, inflow: IN, kind: "card_payment", days_apart: 1 };
const GROUP: TransferGroup = {
  group_id: "g1",
  kind: "own_accounts",
  outflow: txn({ id: "o2", raw_description: "Transfer to Everyday", amount_cents: -5000 }),
  inflow: txn({ id: "i2", account_name: "Everyday", amount_cents: 5000 }),
};

const flash = vi.fn();
function show(canEdit = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <TransfersPanel onFlash={flash} canEdit={canEdit} />
    </QueryClientProvider>,
  );
}
const toReview = () =>
  screen.getByRole("heading", { name: "To review" }).closest(".card") as HTMLElement;
const linked = () => screen.getByRole("heading", { name: "Linked" }).closest(".card") as HTMLElement;

beforeEach(() => {
  vi.restoreAllMocks();
  flash.mockReset();
});

describe("to review", () => {
  it("lists each pair with both sides and how far apart they are", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    show();
    const item = await within(toReview()).findByRole("listitem");
    expect(item).toHaveTextContent("Savings → Credit card");
    expect(item).toHaveTextContent("Out: By Authority To Cc · In: Payment received · 1 day apart");
    expect(
      within(item).getByRole("combobox", { name: "Kind of transfer for By Authority To Cc" }),
    ).toHaveValue("card_payment");
  });

  it("says when there is nothing to review", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    show();
    expect(await within(toReview()).findByText("Nothing to review.")).toBeInTheDocument();
    expect(within(linked()).getByText("No transfers linked yet.")).toBeInTheDocument();
  });

  it("accepts with the kind chosen and remembers the wording, then says so", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    const accept = vi.spyOn(api, "acceptTransfer").mockResolvedValue({
      group: GROUP,
      learned: "by authority to cc",
      learned_for: "Credit card",
    });
    show();
    const item = await within(toReview()).findByRole("listitem");
    fireEvent.change(within(item).getByRole("combobox"), { target: { value: "own_accounts" } });
    fireEvent.click(within(item).getByRole("button", { name: "Accept" }));

    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith(
        "Linked. From now on “by authority to cc” links payments to Credit card by itself.",
      ),
    );
    expect(accept).toHaveBeenCalledWith({
      outflow_id: "out1",
      inflow_id: "in1",
      kind: "own_accounts",
      learn: true,
    });
  });

  it("does not ask to learn the wording once the box is unticked", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    const accept = vi
      .spyOn(api, "acceptTransfer")
      .mockResolvedValue({ group: GROUP, learned: null, learned_for: null });
    show();
    const item = await within(toReview()).findByRole("listitem");
    fireEvent.click(screen.getByRole("checkbox", { name: /Remember the wording/ }));
    fireEvent.click(within(item).getByRole("button", { name: "Accept" }));

    await waitFor(() => expect(flash).toHaveBeenCalledWith("Linked."));
    expect(accept).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "card_payment", learn: false }),
    );
  });

  it("says why an accept failed", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    vi.spyOn(api, "acceptTransfer").mockRejectedValue(new ApiError(409, "Already linked."));
    show();
    const item = await within(toReview()).findByRole("listitem");
    fireEvent.click(within(item).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(flash).toHaveBeenCalledWith("Couldn't link those. Already linked."));
  });

  it("drops a pair that is not a transfer", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    const dismiss = vi.spyOn(api, "dismissTransfer").mockResolvedValue(undefined);
    show();
    const item = await within(toReview()).findByRole("listitem");
    fireEvent.click(within(item).getByRole("button", { name: "Not a transfer" }));
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("Noted: not a transfer. It won't be suggested again."),
    );
    expect(dismiss).toHaveBeenCalledWith({ outflow_id: "out1", inflow_id: "in1" });
  });

  it("looks again and says what it found", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([]);
    vi.spyOn(api, "detectTransfers").mockResolvedValue({ linked: 2, to_review: 3, cash_deposits: 0 });
    show();
    fireEvent.click(screen.getByRole("button", { name: "Look again" }));
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("1 transfer linked, 3 to review, 0 cash deposits."),
    );
  });

  it("offers no buttons to someone who can only look", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([SUGGESTION]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([GROUP]);
    show(false);
    await within(toReview()).findByRole("listitem");
    await within(linked()).findByRole("listitem");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("linked", () => {
  it("lists what is linked, newest first, with its kind", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([GROUP]);
    show();
    const item = await within(linked()).findByRole("listitem");
    expect(item).toHaveTextContent("Savings → Everyday");
    expect(item).toHaveTextContent("$50.00");
    expect(within(item).getByRole("combobox")).toHaveValue("own_accounts");
  });

  it("changes the kind straight away", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([GROUP]);
    const rekind = vi
      .spyOn(api, "changeTransferKind")
      .mockResolvedValue({ ...GROUP, kind: "card_payment" });
    show();
    const item = await within(linked()).findByRole("listitem");
    fireEvent.change(within(item).getByRole("combobox"), { target: { value: "card_payment" } });
    await waitFor(() => expect(flash).toHaveBeenCalledWith("Changed to Card payment."));
    expect(rekind).toHaveBeenCalledWith("g1", "card_payment");
  });

  it("unlinks", async () => {
    vi.spyOn(api, "transferSuggestions").mockResolvedValue([]);
    vi.spyOn(api, "recentTransfers").mockResolvedValue([GROUP]);
    const unlink = vi.spyOn(api, "unlinkTransfer").mockResolvedValue({ updated: 2 });
    show();
    const item = await within(linked()).findByRole("listitem");
    fireEvent.click(within(item).getByRole("button", { name: "Unlink" }));
    await waitFor(() =>
      expect(flash).toHaveBeenCalledWith("Unlinked. Both are ordinary transactions again."),
    );
    expect(unlink).toHaveBeenCalledWith("g1");
  });
});
