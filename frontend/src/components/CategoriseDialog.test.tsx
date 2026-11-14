import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Category, Transaction } from "../api/types";
import { CategoriseDialog } from "./CategoriseDialog";

/**
 * The app's only overlay had no role, no Escape, no focus handling, and dismissed
 * on any click that landed outside — including the release of a drag that began
 * inside a field. The drawer reuses this behaviour, so it is pinned here first.
 */

const TXN = {
  id: "t1",
  account_id: "a1",
  account_name: "Everyday",
  txn_date: "2025-06-01",
  amount_cents: -8540,
  raw_description: "WOOLWORTHS METRO",
  merchant: "Woolworths Metro",
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
} as unknown as Transaction;

const CATEGORIES = [
  { id: "c1", name: "Supermarkets", parent_id: "p1", kind: "expense" },
] as unknown as Category[];

function open(onClose = vi.fn()) {
  render(
    <CategoriseDialog
      txn={TXN}
      categories={CATEGORIES}
      onClose={onClose}
      onSubmit={vi.fn()}
    />,
  );
  return onClose;
}

describe("CategoriseDialog", () => {
  it("applies the category typed in, when Apply is clicked straight after typing", () => {
    // Found in review. The dialog focuses the category field on open; typing "fuel"
    // highlights Fuel; the click on Apply blurs the field first. The first version of the
    // picker dropped the typed match on that blur, so Apply sent the OLD category (here,
    // none) -- with "All from this merchant", to every transaction from the merchant.
    const cats = [
      { id: "p2", name: "Transport", parent_id: null, kind: "expense", sort: 1 },
      { id: "c2", name: "Fuel", parent_id: "p2", kind: "expense", sort: 2 },
      { id: "c3", name: "Parking", parent_id: "p2", kind: "expense", sort: 3 },
    ] as unknown as Category[];
    const onSubmit = vi.fn();
    render(<CategoriseDialog txn={TXN} categories={cats} onClose={vi.fn()} onSubmit={onSubmit} />);
    const field = screen.getByRole("combobox", { name: "Category" });
    fireEvent.focus(field);
    fireEvent.change(field, { target: { value: "fuel" } });

    const apply = screen.getByRole("button", { name: "Apply" });
    fireEvent.mouseDown(apply);
    fireEvent.blur(field);
    fireEvent.click(apply);

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ category_id: "c2" }));
  });


  it("is announced as a modal dialog with a name", () => {
    open();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Categorise");
  });

  it("closes on Escape", () => {
    const onClose = open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("moves focus into the dialog when it opens", () => {
    open();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("closes when a click starts and ends outside", () => {
    const onClose = open();
    fireEvent.pointerDown(document.body);
    fireEvent.pointerUp(document.body);
    expect(onClose).toHaveBeenCalled();
  });

  it("does NOT close when a drag starts inside and ends outside", () => {
    // Selecting text in the pattern field and releasing past the edge used to
    // discard the edit.
    const onClose = open();
    fireEvent.pointerDown(screen.getByRole("dialog"));
    fireEvent.pointerUp(document.body);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not close on a click inside", () => {
    const onClose = open();
    const dialog = screen.getByRole("dialog");
    fireEvent.pointerDown(dialog);
    fireEvent.pointerUp(dialog);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps Tab inside the dialog", () => {
    open();
    const dialog = screen.getByRole("dialog");
    const focusable = [
      ...dialog.querySelectorAll<HTMLElement>("a[href], button, input, select, textarea"),
    ];
    const last = focusable[focusable.length - 1];
    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
