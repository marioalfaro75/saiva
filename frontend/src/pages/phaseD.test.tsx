import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PeriodProvider } from "../period/PeriodProvider";
import { renderApp, stubApi } from "../testing/harness";
import { Budgets } from "./Budgets";
import { Overview } from "./Overview";

/**
 * Three things the design review flagged as costing the page its meaning:
 * a form standing where the content should be, a loading state that reads as
 * data, and an empty state that names a page instead of linking to it.
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

const withPeriod = (ui: React.ReactElement) =>
  renderApp(<PeriodProvider>{ui}</PeriodProvider>, { route: "/?period=fy:2024" });

describe("Overview while it is still loading", () => {
  it("shows an em dash rather than claiming you earned and spent nothing", async () => {
    // A slow summary: the other queries resolve, this one never does.
    vi.stubGlobal(
      "fetch",
      vi.fn((input: unknown) => {
        const url = String(input);
        if (url.includes("/dashboard/summary")) return new Promise(() => {});
        const key = Object.keys(PERIOD).find((p) => url.startsWith(`/api${p}`));
        const body = key ? PERIOD[key as keyof typeof PERIOD] : { items: [], points: [] };
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: () => Promise.resolve(body),
          text: () => Promise.resolve(JSON.stringify(body)),
        });
      }),
    );
    const { container } = withPeriod(<Overview />);

    const stat = (label: string) =>
      [...container.querySelectorAll(".card")].find((c) => c.textContent?.startsWith(label));
    await waitFor(() => expect(stat("Income")).toBeDefined());
    for (const label of ["Income", "Expenses", "Net"]) {
      expect(stat(label)?.querySelector(".stat-value")?.textContent).toBe("—");
    }
  });
});

describe("Budgets", () => {
  const stub = (budgets: unknown[]) =>
    stubApi({ ...PERIOD, "/budgets": budgets, "/categories": [] });

  it("leads with the budgets, not with the form for making one", async () => {
    stub([
      {
        id: "b1",
        category_id: "c1",
        category_name: "Groceries",
        period: "monthly",
        limit_cents: 80000,
        spent_cents: 40000,
        pct_used: 0.5,
        status: "ok",
        remaining_cents: 40000,
      },
    ]);
    const { container } = withPeriod(<Budgets />);
    await screen.findByText("Groceries");

    const cards = [...container.querySelectorAll(".card")];
    const budget = cards.findIndex((c) => c.textContent?.includes("Groceries"));
    const form = cards.findIndex((c) => c.textContent?.includes("Add a budget"));
    expect(budget).toBeLessThan(form);
  });

  it("offers a way out of the empty state instead of naming a page", async () => {
    stub([]);
    withPeriod(<Budgets />);
    const empty = await screen.findByText("No budgets yet");
    const block = empty.closest(".empty") as HTMLElement;
    expect(within(block).getByRole("link", { name: /demo data/i })).toHaveAttribute(
      "href",
      "/settings",
    );
  });

  it("says nothing at all until it knows there is nothing", () => {
    stubApi({ ...PERIOD });
    withPeriod(<Budgets />);
    // An empty state shown while loading is a lie that corrects itself a beat later.
    expect(screen.queryByText("No budgets yet")).toBeNull();
  });
});
