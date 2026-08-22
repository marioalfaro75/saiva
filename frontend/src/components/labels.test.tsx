import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CategoriseDialog } from "./CategoriseDialog";
import { Budgets } from "../pages/Budgets";
import { renderApp, stubApi } from "../testing/harness";
import { PeriodProvider } from "../period/PeriodProvider";
import type { Transaction } from "../api/types";

/**
 * Form labels across the app were text sitting beside a control with nothing
 * joining them. They are associated now, and the ids come from `useId` rather than
 * being written by hand — because several of these forms live inside a component
 * rendered once per row, where a hardcoded id would repeat down the page and every
 * label would point at the first one.
 */

const PERIOD = {
  "/periods/options": {
    default: "fy:2024",
    relative: [],
    financial_years: [
      { value: "fy:2024", label: "FY2024–25", start: "2024-07-01", end: "2025-06-30", quarters: [], months: [] },
    ],
  },
  "/periods/resolve": {
    start: "2024-07-01",
    end: "2025-06-30",
    label: "FY2024–25",
    is_current: true,
  },
};

const budget = (id: string, name: string) => ({
  id,
  category_id: `c-${id}`,
  category_name: name,
  period: "monthly",
  limit_cents: 80000,
  spent_cents: 40000,
  pct_used: 0.5,
  status: "ok",
  remaining_cents: 40000,
});

describe("labels are attached to their controls", () => {
  it("keeps them distinct when the same form is rendered for every row", async () => {
    stubApi({
      ...PERIOD,
      "/budgets": [budget("b1", "Groceries"), budget("b2", "Transport")],
      "/categories": [],
    });
    const { container } = renderApp(
      <PeriodProvider>
        <Budgets />
      </PeriodProvider>,
      { route: "/?period=fy:2024" },
    );
    await screen.findByText("Groceries");

    // Every budget card has an Edit button; open both so two copies of the same
    // form are in the document at once.
    const edits = screen.getAllByRole("button", { name: "Edit" });
    expect(edits).toHaveLength(2);
    for (const b of edits) fireEvent.click(b);

    const ids = [...container.querySelectorAll("label[for]")].map((l) => l.getAttribute("for"));
    // Two open cards with two fields each, plus the three-field add form below —
    // the two cards are the case a hardcoded id would collide on.
    expect(ids).toHaveLength(7);
    expect(new Set(ids).size).toBe(ids.length);

    // And each one points at a control that exists.
    for (const id of ids) expect(container.querySelector(`#${CSS.escape(id!)}`)).not.toBeNull();
  });

  it("names the categorise dialog's fields", () => {
    const txn = {
      id: "t1",
      txn_date: "2024-08-02",
      raw_description: "WOOLWORTHS",
      merchant: "Woolworths",
      amount_cents: -4250,
      account_id: "a1",
      account_name: "Everyday",
      category_id: null,
      category_name: null,
      category_locked: false,
      is_transfer: false,
    } as unknown as Transaction;
    render(
      <CategoriseDialog
        txn={txn}
        categories={[
          {
            id: "c1",
            name: "Supermarkets",
            parent_id: "p1",
            kind: "expense",
            icon: null,
            color: null,
            is_system: false,
            sort: 0,
          },
        ]}
        busy={false}
        onClose={() => {}}
        onSubmit={() => {}}
      />,
    );
    for (const name of ["Category", "Apply to"]) {
      expect(screen.getByLabelText(name)).toBeInTheDocument();
    }
  });
});
