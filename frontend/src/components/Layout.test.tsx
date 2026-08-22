import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderShell, stubApi } from "../testing/harness";
import { SPA_VERSION } from "../version";
import { Layout } from "./Layout";

/**
 * The app shell is about to be rebuilt from a top bar into a side menu. These tests
 * pin the things that must survive that change: every destination reachable, the
 * global period control mounted, and the three status signals still firing.
 */

const ME = {
  user: { id: "u1", email: "a@b.c", name: "Owner", role: "owner", is_active: true },
  household: { id: "h1", name: "Alfaro Household", state: "NSW", adults: 2, children: 2 },
  csrf_token: "t",
};

const PERIOD_OPTIONS = {
  default: "fy:2024",
  relative: [{ value: "this_month", label: "This month" }],
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
};

const CURRENT = { start: "2024-07-01", end: "2025-06-30", label: "FY2024–25", is_current: true };

function stubShell(over: Record<string, unknown> = {}) {
  stubApi({
    "/auth/csrf": { csrf_token: "t" },
    "/auth/status": { initialised: true },
    "/auth/me": ME,
    "/periods/options": PERIOD_OPTIONS,
    "/periods/resolve": CURRENT,
    "/meta": { version: SPA_VERSION },
    "/admin/update-check": { update_available: false },
    "/notifications": { unread: 0, items: [] },
    ...over,
  });
}

afterEach(() => vi.unstubAllGlobals());

const DESTINATIONS = [
  ["Overview", "/"],
  ["Insights", "/insights"],
  ["Advisor", "/advisor"],
  ["Alerts", "/alerts"],
  ["Transactions", "/transactions"],
  ["Accounts", "/accounts"],
  ["Budgets", "/budgets"],
  ["Bills", "/bills"],
  ["Forecast", "/forecast"],
  ["Net worth", "/net-worth"],
  ["Goals", "/goals"],
  ["Benchmarks", "/benchmarks"],
  ["Import", "/import"],
  ["Settings", "/settings"],
] as const;

describe("app shell", () => {
  it("offers every destination, whatever shape the navigation takes", async () => {
    stubShell();
    renderShell(<Layout>content</Layout>);
    for (const [label, href] of DESTINATIONS) {
      const link = await screen.findByRole("link", { name: new RegExp(`^${label}`) });
      expect(link).toHaveAttribute("href", href);
    }
  });

  it("marks the destination you are on", async () => {
    stubShell();
    renderShell(<Layout>content</Layout>, { route: "/budgets" });
    const link = await screen.findByRole("link", { name: /^Budgets/ });
    expect(link).toHaveAttribute("aria-current", "page");
  });

  it("keeps the global period control mounted", async () => {
    stubShell();
    renderShell(<Layout>content</Layout>);
    // Whether it lives in a top bar or an app bar, it must always be reachable.
    expect(await screen.findByLabelText("Period")).toBeInTheDocument();
  });

  it("renders the page content", async () => {
    stubShell();
    renderShell(<Layout>the page</Layout>);
    expect(await screen.findByText("the page")).toBeInTheDocument();
  });
});

describe("status signals", () => {
  it("flags unread alerts, and stays quiet when there are none", async () => {
    stubShell({ "/notifications": { unread: 3, items: [] } });
    const { unmount } = renderShell(<Layout>c</Layout>);
    const alerts = await screen.findByRole("link", { name: /^Alerts/ });
    await waitFor(() => expect(alerts.querySelector(".dot, .badge")).not.toBeNull());
    unmount();

    vi.unstubAllGlobals();
    stubShell();
    renderShell(<Layout>c</Layout>);
    const quiet = await screen.findByRole("link", { name: /^Alerts/ });
    await waitFor(() => expect(quiet.querySelector(".dot, .badge")).toBeNull());
  });

  it("flags an available update on the Settings destination", async () => {
    stubShell({ "/admin/update-check": { update_available: true, latest: "v1.0.0" } });
    renderShell(<Layout>c</Layout>);
    const settings = await screen.findByRole("link", { name: /^Settings/ });
    await waitFor(() => expect(settings.querySelector(".dot, .badge")).not.toBeNull());
  });

  it("offers a reload when the server is running a different build", async () => {
    stubShell({ "/meta": { version: `${SPA_VERSION}-newer` } });
    renderShell(<Layout>c</Layout>);
    expect(await screen.findByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("says so when the period being viewed is not the current one", async () => {
    stubShell({
      "/periods/resolve": { ...CURRENT, label: "FY2023–24", is_current: false },
    });
    renderShell(<Layout>c</Layout>);
    // The wording may move into an app bar, but it must remain on screen.
    expect(await screen.findByText(/not the current period/i)).toBeInTheDocument();
  });
});
