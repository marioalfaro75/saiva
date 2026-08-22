import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setViewport } from "../setupTests";
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

afterEach(() => {
  vi.unstubAllGlobals();
  setViewport(800);
});

/** Routes, not labels: the wording may differ between shells, reachability may not. */
const ROUTES = [
  "/",
  "/insights",
  "/advisor",
  "/alerts",
  "/transactions",
  "/accounts",
  "/budgets",
  "/bills",
  "/forecast",
  "/net-worth",
  "/goals",
  "/benchmarks",
  "/import",
  "/settings",
];

// Every guarantee below has to hold in both shells, so the suite runs twice: once
// narrow, where the original top bar renders, and once wide, where the sidebar does.
describe.each([
  ["top bar", 800, ".topbar"],
  ["sidebar", 1280, ".sidebar"],
])("app shell (%s)", (_name, width, shellSelector) => {
  it("renders the shell this viewport calls for, and only that one", async () => {
    setViewport(width);
    stubShell();
    const { container } = renderShell(<Layout>content</Layout>);
    await screen.findByLabelText("Period");
    expect(container.querySelector(shellSelector)).not.toBeNull();
    // Both shells at once would duplicate every link and announce two navigations.
    expect(container.querySelectorAll("nav")).toHaveLength(1);
  });

  it("offers every destination exactly once", async () => {
    setViewport(width);
    stubShell();
    const { container } = renderShell(<Layout>content</Layout>);
    await screen.findByLabelText("Period");
    for (const href of ROUTES) {
      expect(container.querySelectorAll(`a[href="${href}"]`)).toHaveLength(1);
    }
  });

  it("marks the destination you are on", async () => {
    setViewport(width);
    stubShell();
    const { container } = renderShell(<Layout>content</Layout>, { route: "/budgets" });
    await screen.findByLabelText("Period");
    expect(container.querySelector('a[href="/budgets"]')).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("keeps the global period control mounted", async () => {
    setViewport(width);
    stubShell();
    renderShell(<Layout>content</Layout>);
    expect(await screen.findByLabelText("Period")).toBeInTheDocument();
  });

  it("renders the page content and a way to sign out", async () => {
    setViewport(width);
    stubShell();
    renderShell(<Layout>the page</Layout>);
    expect(await screen.findByText("the page")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});

describe("status signals", () => {
  it("flags unread alerts, and stays quiet when there are none", async () => {
    stubShell({ "/notifications": { unread: 3, items: [] } });
    const { unmount } = renderShell(<Layout>c</Layout>);
    await screen.findByLabelText("Period");
    const alerts = document.querySelector('a[href="/alerts"]')!;
    await waitFor(() => expect(alerts.querySelector(".dot, .badge")).not.toBeNull());
    unmount();

    vi.unstubAllGlobals();
    stubShell();
    renderShell(<Layout>c</Layout>);
    await screen.findByLabelText("Period");
    const quiet = document.querySelector('a[href="/alerts"]')!;
    await waitFor(() => expect(quiet.querySelector(".dot, .badge")).toBeNull());
  });

  it("flags an available update on the Settings destination", async () => {
    stubShell({ "/admin/update-check": { update_available: true, latest: "v1.0.0" } });
    renderShell(<Layout>c</Layout>);
    await screen.findByLabelText("Period");
    const settings = document.querySelector('a[href="/settings"]')!;
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

  it("distinguishes a past period from a future one", async () => {
    // Colour alone cannot carry this: the two states mean different things, and a
    // wash fails for colour-vision deficiency and in sunlight.
    setViewport(1280);
    stubShell({
      "/periods/resolve": {
        start: "2020-07-01",
        end: "2021-06-30",
        label: "FY2020–21",
        is_current: false,
      },
    });
    const { unmount } = renderShell(<Layout>c</Layout>);
    expect(await screen.findByText(/Past period/)).toBeInTheDocument();
    unmount();

    vi.unstubAllGlobals();
    setViewport(1280);
    stubShell({
      "/periods/resolve": {
        start: "2090-07-01",
        end: "2091-06-30",
        label: "FY2090–91",
        is_current: false,
      },
    });
    renderShell(<Layout>c</Layout>);
    expect(await screen.findByText(/Future period/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing has happened yet/)).toBeInTheDocument();
  });
});
