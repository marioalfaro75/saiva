import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import type { Account } from "../api/types";
import { AccountDialog } from "./AccountDialog";

/**
 * Accounts → Edit. The type decides how money into the account counts, and the wording is
 * what marks a payment to it in the other accounts ("By Authority To Cc"). Changing either
 * looks through the transactions again at once, and says what it linked.
 */

const ACCOUNT: Account = {
  id: "a2",
  name: "Credit card",
  type: "everyday",
  institution: "Westpac",
  currency: "AUD",
  opening_balance_cents: 0,
  owner_user_id: null,
  balance_cents: 0,
  txn_count: 12,
  bank_identifier: "4321",
  transfer_wording: ["By Authority To Cc"],
};

const onClose = vi.fn();
const onDone = vi.fn();
function show(account: Account = ACCOUNT) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AccountDialog account={account} onClose={onClose} onDone={onDone} />
    </QueryClientProvider>,
  );
}
const wording = () => screen.getByRole("textbox", { name: "Transfer wording" });
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));

beforeEach(() => {
  vi.restoreAllMocks();
  onClose.mockReset();
  onDone.mockReset();
});

describe("AccountDialog", () => {
  it("shows what is stored, one phrase per line, and the account number", () => {
    show();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Edit account");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Credit card");
    expect(screen.getByRole("combobox", { name: "Type" })).toHaveValue("everyday");
    expect(wording()).toHaveValue("By Authority To Cc");
    expect(screen.getByText(/Account number: 4321/)).toBeInTheDocument();
  });

  it("saves new wording, trimmed and without blank lines, and links what it now explains", async () => {
    const update = vi.spyOn(api, "updateAccount").mockResolvedValue(ACCOUNT);
    const detect = vi
      .spyOn(api, "detectTransfers")
      .mockResolvedValue({ linked: 6, to_review: 1, cash_deposits: 0 });
    show();
    fireEvent.change(wording(), {
      target: { value: "By Authority To Cc\n\n  Payment to card  \n" },
    });
    save();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Saved. 3 transfers linked, 1 to review."));
    expect(update).toHaveBeenCalledWith("a2", {
      name: "Credit card",
      type: "everyday",
      institution: "Westpac",
      transfer_wording: ["By Authority To Cc", "Payment to card"],
    });
    expect(detect).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
  });

  it("looks again when the type changes", async () => {
    const update = vi.spyOn(api, "updateAccount").mockResolvedValue(ACCOUNT);
    const detect = vi
      .spyOn(api, "detectTransfers")
      .mockResolvedValue({ linked: 2, to_review: 0, cash_deposits: 0 });
    show();
    fireEvent.change(screen.getByRole("combobox", { name: "Type" }), {
      target: { value: "credit_card" },
    });
    save();
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Saved. 1 transfer linked, 0 to review."));
    expect(update).toHaveBeenCalledWith("a2", expect.objectContaining({ type: "credit_card" }));
    expect(detect).toHaveBeenCalledTimes(1);
  });

  it("does not look again when only the name changes", async () => {
    const update = vi.spyOn(api, "updateAccount").mockResolvedValue(ACCOUNT);
    const detect = vi.spyOn(api, "detectTransfers");
    show();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "  Visa  " },
    });
    save();
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("Saved."));
    expect(update).toHaveBeenCalledWith("a2", expect.objectContaining({ name: "Visa" }));
    expect(detect).not.toHaveBeenCalled();
  });

  it("sends a cleared institution as nothing", async () => {
    const update = vi.spyOn(api, "updateAccount").mockResolvedValue(ACCOUNT);
    show();
    fireEvent.change(screen.getByRole("textbox", { name: "Institution" }), {
      target: { value: "   " },
    });
    save();
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(update).toHaveBeenCalledWith("a2", expect.objectContaining({ institution: null }));
  });

  it("will not save without a name", () => {
    show();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: " " } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("says why a save was refused, and stays open", async () => {
    vi.spyOn(api, "updateAccount").mockRejectedValue(new ApiError(422, "Too many phrases."));
    show();
    save();
    expect(await screen.findByText("Not saved. Too many phrases.")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("explains a missing account number", () => {
    show({ ...ACCOUNT, bank_identifier: null, transfer_wording: undefined });
    expect(screen.getByText(/none yet/)).toBeInTheDocument();
    expect(wording()).toHaveValue("");
  });

  it("says which line is too short before sending, and will not save it", () => {
    const update = vi.spyOn(api, "updateAccount");
    show();
    fireEvent.change(wording(), { target: { value: "By Authority To Cc\nCc" } });
    expect(screen.getByRole("status")).toHaveTextContent("“Cc” is too short to recognise a payment by");
    expect(wording()).toHaveAttribute("aria-invalid", "true");
    expect(wording()).toHaveAccessibleDescription(expect.stringContaining("too short"));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(update).not.toHaveBeenCalled();
  });

  it("says when a line is too long", () => {
    show();
    fireEvent.change(wording(), { target: { value: "x".repeat(81) } });
    expect(screen.getByRole("status")).toHaveTextContent("too long");
  });

  it("refuses a line with no words, the way the server does", () => {
    show();
    for (const line of ["0412", "-", "7"]) {
      fireEvent.change(wording(), { target: { value: `By Authority To Cc\n${line}` } });
      expect(screen.getByRole("status")).toHaveTextContent(`“${line}” has no words in it`);
      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    }
  });

  it("says nothing while the wording is fine", () => {
    show();
    fireEvent.change(wording(), { target: { value: "By Authority To Cc\nPayment to card" } });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(wording()).toHaveAttribute("aria-invalid", "false");
  });

  it("says how many links the new type recounted", async () => {
    vi.spyOn(api, "updateAccount").mockResolvedValue({ ...ACCOUNT, transfers_rekinded: 4 });
    vi.spyOn(api, "detectTransfers").mockResolvedValue({ linked: 0, to_review: 0, cash_deposits: 0 });
    show();
    fireEvent.change(screen.getByRole("combobox", { name: "Type" }), {
      target: { value: "home_loan" },
    });
    save();
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith(
        "Saved. 4 linked transfers changed to Loan repayment. 0 transfers linked, 0 to review.",
      ),
    );
  });

  it("is saved even when looking again fails, and says so", async () => {
    vi.spyOn(api, "updateAccount").mockResolvedValue(ACCOUNT);
    vi.spyOn(api, "detectTransfers").mockRejectedValue(new ApiError(500, "Server error"));
    show();
    fireEvent.change(wording(), { target: { value: "Payment to card" } });
    save();
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith(
        "Saved. Couldn't look through your transactions again: try Transactions → Transfers → Look again.",
      ),
    );
    expect(onClose).toHaveBeenCalled();
  });
});

