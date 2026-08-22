import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PeriodProvider } from "../period/PeriodProvider";
import { renderApp, stubApi } from "../testing/harness";
import { Accounts } from "./Accounts";
import { Overview } from "./Overview";

/**
 * Overview and Accounts used to be dead ends: nothing on them was clickable, even
 * though the Transactions page already reads these filters from the URL. These
 * tests pin the links, since a wrong query string fails silently — the page loads
 * and quietly shows the wrong rows.
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

afterEach(() => vi.unstubAllGlobals());

describe("Overview", () => {
  it("links each category to its transactions, carrying the period", async () => {
    stubApi({
      ...PERIOD,
      "/dashboard/summary": { income_cents: 0, expense_cents: 0, net_cents: 0, savings_rate: 0, txn_count: 4 },
      "/dashboard/trends": { interval: "month", points: [] },
      "/dashboard/categories": {
        items: [
          { category_id: "c1", category_name: "Supermarkets", parent_name: "Food", amount_cents: -8540, pct: 0.4 },
        ],
      },
    });
    renderApp(
      <PeriodProvider>
        <Overview />
      </PeriodProvider>,
      { route: "/?period=fy:2024" },
    );
    const link = await screen.findByRole("link", { name: "Supermarkets" });
    expect(link).toHaveAttribute("href", expect.stringContaining("/transactions?"));
    expect(link.getAttribute("href")).toContain("category_id=c1");
    expect(link.getAttribute("href")).toContain("period=fy%3A2024");
  });

  it("sends an uncategorised row to the uncategorised filter, not a null id", async () => {
    stubApi({
      ...PERIOD,
      "/dashboard/summary": { income_cents: 0, expense_cents: 0, net_cents: 0, savings_rate: 0, txn_count: 1 },
      "/dashboard/trends": { interval: "month", points: [] },
      "/dashboard/categories": {
        items: [
          { category_id: null, category_name: "Uncategorised", parent_name: null, amount_cents: -100, pct: 1 },
        ],
      },
    });
    renderApp(
      <PeriodProvider>
        <Overview />
      </PeriodProvider>,
    );
    const link = await screen.findByRole("link", { name: "Uncategorised" });
    expect(link.getAttribute("href")).toContain("uncategorised=true");
    expect(link.getAttribute("href")).not.toContain("category_id");
  });

  it("offers a way out of the empty state instead of naming a page", async () => {
    stubApi({
      ...PERIOD,
      "/dashboard/summary": { income_cents: 0, expense_cents: 0, net_cents: 0, savings_rate: 0, txn_count: 0 },
      "/dashboard/trends": { interval: "month", points: [] },
      "/dashboard/categories": { items: [] },
    });
    renderApp(
      <PeriodProvider>
        <Overview />
      </PeriodProvider>,
    );
    expect(await screen.findByRole("link", { name: /import a statement/i })).toHaveAttribute(
      "href",
      "/import",
    );
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });
});

describe("Accounts", () => {
  it("links each account to its transactions", async () => {
    stubApi({
      "/accounts": [
        {
          id: "a1",
          name: "Everyday",
          type: "everyday",
          institution: null,
          balance_cents: 1000,
          txn_count: 12,
        },
      ],
    });
    renderApp(<Accounts />);
    const link = await screen.findByRole("link", { name: "Everyday" });
    expect(link).toHaveAttribute("href", "/transactions?account_id=a1");
  });
});
