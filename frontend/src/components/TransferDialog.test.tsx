import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import type { Transaction, TransferGroup } from "../api/types";
import { TransferDialog } from "./TransferDialog";

/**
 * The ⇄ button on a row. Linked: say what with, change the kind, or unlink. Not linked:
 * offer the transactions in other accounts it could pair with, and link the one picked.
 */

const txn = (extra: Partial<Transaction>): Transaction =>
  ({
    id: "t1",
    account_id: "a1",
    account_name: "Savings",
    txn_date: "2026-10-23",
    amount_cents: -125492,
    raw_description: "By Authority To Westpac",
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

const LINKED = txn({
  transfer_group_id: "g1",
  transfer_kind: "loan_repayment",
  counterpart_id: "t9",
  counterpart_account_name: "Home loan",
});
const GROUP = (kind: TransferGroup["kind"]): TransferGroup => ({
  group_id: "g1",
  kind,
  outflow: null,
  inflow: null,
});

const onClose = vi.fn();
const onDone = vi.fn();
function show(t: Transaction) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <TransferDialog txn={t} onClose={onClose} onDone={onDone} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  onClose.mockReset();
  onDone.mockReset();
});

describe("a linked transaction", () => {
  it("says what it is linked with, and does not look for candidates", () => {
    const candidates = vi.spyOn(api, "linkCandidates");
    show(LINKED);
    expect(screen.getByText("Home loan")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Kind" })).toHaveValue("loan_repayment");
    expect(candidates).not.toHaveBeenCalled();
  });

  it("unlinks", async () => {
    const unlink = vi.spyOn(api, "unlinkTransfer").mockResolvedValue({ updated: 2 });
    show(LINKED);
    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith("Unlinked. Both are ordinary transactions again."),
    );
    expect(unlink).toHaveBeenCalledWith("g1");
    expect(onClose).toHaveBeenCalled();
  });

  it("changes the kind", async () => {
    const rekind = vi.spyOn(api, "changeTransferKind").mockResolvedValue(GROUP("own_accounts"));
    show(LINKED);
    fireEvent.change(screen.getByRole("combobox", { name: "Kind" }), {
      target: { value: "own_accounts" },
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Changed to Between accounts."));
    expect(rekind).toHaveBeenCalledWith("g1", "own_accounts");
  });

  it("says why an unlink failed, and stays open", async () => {
    vi.spyOn(api, "unlinkTransfer").mockRejectedValue(new ApiError(404, "Not found."));
    show(LINKED);
    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    expect(await screen.findByText("That didn't work. Not found.")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("a home loan repayment", () => {
  const REPAYMENT = { ...LINKED, repayment: "regular" } as Transaction;

  it("can be marked as an extra repayment", async () => {
    const set = vi.spyOn(api, "setRepayment").mockResolvedValue({
      ...GROUP("loan_repayment"),
      outflow: { ...REPAYMENT, repayment: "extra" } as Transaction,
    });
    show(REPAYMENT);
    const choice = screen.getByRole("combobox", { name: "Repayment" });
    expect(choice).toHaveValue("regular");
    fireEvent.change(choice, { target: { value: "extra" } });
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Marked as an extra repayment."));
    expect(set).toHaveBeenCalledWith("g1", true);
  });

  it("can be marked back as the regular one", async () => {
    const set = vi.spyOn(api, "setRepayment").mockResolvedValue({
      ...GROUP("loan_repayment"),
      outflow: REPAYMENT,
    });
    show({ ...REPAYMENT, repayment: "extra" } as Transaction);
    fireEvent.change(screen.getByRole("combobox", { name: "Repayment" }), {
      target: { value: "regular" },
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Marked as the regular repayment."));
    expect(set).toHaveBeenCalledWith("g1", false);
  });

  it("offers no such choice for other transfers", () => {
    show(LINKED);
    expect(screen.queryByRole("combobox", { name: "Repayment" })).toBeNull();
  });
});

describe("a transaction not linked", () => {
  const OTHER = txn({
    id: "t9",
    account_id: "a3",
    account_name: "Home loan",
    amount_cents: 125492,
    raw_description: "Repayment",
  });

  it("offers the other half and links the one picked, by the account it went into", async () => {
    const candidates = vi.spyOn(api, "linkCandidates").mockResolvedValue([OTHER]);
    const link = vi.spyOn(api, "linkTransfer").mockResolvedValue(GROUP("loan_repayment"));
    show(txn({}));
    const link_ = screen.getByRole("button", { name: "Link" });
    expect(link_).toBeDisabled();

    fireEvent.click(await screen.findByRole("radio", { name: /Home loan · Repayment/ }));
    fireEvent.click(link_);

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Linked as loan repayment."));
    expect(candidates).toHaveBeenCalledWith("t1");
    expect(link).toHaveBeenCalledWith({ first_id: "t1", second_id: "t9", kind: undefined });
  });

  it("links with the kind chosen", async () => {
    vi.spyOn(api, "linkCandidates").mockResolvedValue([OTHER]);
    const link = vi.spyOn(api, "linkTransfer").mockResolvedValue(GROUP("own_accounts"));
    show(txn({}));
    fireEvent.click(await screen.findByRole("radio", { name: /Home loan/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "Kind" }), {
      target: { value: "own_accounts" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Linked as between accounts."));
    expect(link).toHaveBeenCalledWith({ first_id: "t1", second_id: "t9", kind: "own_accounts" });
  });

  it("says when nothing could be the other half", async () => {
    vi.spyOn(api, "linkCandidates").mockResolvedValue([]);
    show(txn({}));
    expect(
      await screen.findByText("Nothing in your other accounts could be the other half."),
    ).toBeInTheDocument();
  });
});
