import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AiSettings, Household } from "../api/types";
import { renderApp } from "../testing/harness";
import { Settings } from "./Settings";

/**
 * Three forms on this page are seeded from a query: the household settings, the AI
 * connection, and the financial-year picker. They used to be effects copying fetched
 * data into state, which meant a refetch landing mid-edit overwrote what was in the
 * fields — and this page triggers refetches of its own (loading demo data invalidates
 * everything). They are derived now, with an edit winning over the fetched value, so
 * what is pinned here is: the server fills the fields, and stops being authoritative
 * for a field as soon as someone has typed in it.
 */

const HOUSEHOLD = {
  id: "h1",
  name: "Alfaro Household",
  country: "AU",
  currency: "AUD",
  locale: "en-AU",
  state: "NSW",
  timezone: "Australia/Sydney",
  fy_start_month: 7,
  fy_start_day: 1,
  period_basis: "calendar",
  pay_cycle_anchor: null,
  adults: 2,
  children: 2,
  income_band: null,
} satisfies Household;

const AI = {
  provider: "anthropic",
  base_url: null,
  model: null,
  privacy_mode: "aggregates",
  has_key: false,
  configured: true,
} satisfies AiSettings;

const YEARS = [
  { year: 2024, label: "FY2024–25", start: "2024-07-01", end: "2025-06-30" },
  { year: 2023, label: "FY2023–24", start: "2023-07-01", end: "2024-06-30" },
];

function json(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    clone: () => ({ json: () => Promise.resolve(body) }),
  });
}

type Server = { household: Household; ai: AiSettings; years: typeof YEARS };

/** Serves whatever `current` holds now, so a test can change it between fetches. */
function stubSettings(current: Server) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = String(input);
      if (url.startsWith("/api/household")) return json(current.household);
      if (url.startsWith("/api/ai/settings")) return json(current.ai);
      if (url.startsWith("/api/ai/models")) return json([]);
      if (url.startsWith("/api/reports/years")) return json(current.years);
      if (url.startsWith("/api/admin/update-check")) return json({ update_available: false });
      if (url.startsWith("/api/admin/seed-demo")) {
        return json({ message: "ok", transactions: 7 });
      }
      return json({});
    }),
  );
}

const stateSelect = () => screen.getByLabelText("State / territory");
const keyBox = () => screen.getByLabelText(/^API key/);
const reportLink = () => screen.getByRole("link", { name: "Download PDF" });
/** The year select carries no accessible name, so it is found by what it lists. */
const fySelect = () => screen.getByRole("option", { name: "FY2024–25" }).closest("select")!;

const server = (over: Partial<Server> = {}): Server => ({
  household: HOUSEHOLD,
  ai: AI,
  years: YEARS,
  ...over,
});

/** Loading demo data invalidates every query, which is the refetch under test. */
function loadDemoData() {
  fireEvent.click(screen.getByRole("button", { name: "Load demo data" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("Settings", () => {
  it("fills the household form from the server", async () => {
    stubSettings(server());
    renderApp(<Settings />);
    await waitFor(() => expect(stateSelect()).toHaveValue("NSW"));
    expect(screen.getByLabelText("Budget period basis")).toHaveValue("calendar");
    expect(screen.getByLabelText("Financial year starts (month)")).toHaveValue(7);
  });

  it("offers the newest financial year for the report without being asked", async () => {
    stubSettings(server());
    renderApp(<Settings />);
    await waitFor(() => expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2024"));
  });

  it("follows the server's newest financial year while nobody has picked one", async () => {
    // The select is seeded from the report-years query, so a year appearing after the
    // first fetch — the first import of a new financial year does exactly that — has
    // to become the default. Latching the first answer into state meant the button
    // kept offering last year's report until someone noticed and changed it by hand.
    const current = server();
    stubSettings(current);
    renderApp(<Settings />);
    await waitFor(() => expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2024"));

    current.years = [
      { year: 2025, label: "FY2025–26", start: "2025-07-01", end: "2026-06-30" },
      ...YEARS,
    ];
    loadDemoData();
    await waitFor(() => expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2025"));
  });

  it("keeps a picked financial year once one has been chosen", async () => {
    const current = server();
    stubSettings(current);
    renderApp(<Settings />);
    await waitFor(() => expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2024"));

    fireEvent.change(fySelect(), { target: { value: "2023" } });
    expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2023");

    current.years = [
      { year: 2025, label: "FY2025–26", start: "2025-07-01", end: "2026-06-30" },
      ...YEARS,
    ];
    current.ai = { ...AI, has_key: true };
    loadDemoData();
    await screen.findByLabelText(/leave blank to keep/);

    expect(reportLink()).toHaveAttribute("href", "/api/reports/fy?year=2023");
  });

  it("keeps an unsaved household edit when the household refetches underneath it", async () => {
    const current = server();
    stubSettings(current);
    renderApp(<Settings />);
    await waitFor(() => expect(stateSelect()).toHaveValue("NSW"));

    // Picked, not yet saved.
    fireEvent.change(stateSelect(), { target: { value: "VIC" } });

    // Meanwhile the stored household changes under them. `has_key` changes too, as a
    // signal from outside this form — it is read straight off its query rather than
    // through any field — so the invalidation has demonstrably landed and rendered
    // before the assertions below.
    current.household = { ...HOUSEHOLD, state: "QLD", fy_start_month: 4 };
    current.ai = { ...AI, has_key: true };
    loadDemoData();
    await screen.findByLabelText(/leave blank to keep/);

    expect(stateSelect()).toHaveValue("VIC");
    // The whole form is latched, not just the field that was touched: it is saved as
    // one patch, so what gets submitted should be what was on screen when the edit
    // began, not a mix of that and whatever arrived since.
    expect(screen.getByLabelText("Financial year starts (month)")).toHaveValue(7);
  });

  it("does not wipe an API key that is being typed when the AI settings refetch", async () => {
    const current = server();
    stubSettings(current);
    renderApp(<Settings />);
    await waitFor(() => expect(keyBox()).toBeInTheDocument());

    // A key is write-only — the server never sends it back — so losing it mid-entry
    // means fetching it again from wherever it came from.
    fireEvent.change(keyBox(), { target: { value: "sk-half-typed" } });

    // The stored settings now have a key. `has_key` is read straight from the query
    // rather than through the form, so the label changing is proof the refetch landed.
    current.ai = { ...AI, has_key: true };
    loadDemoData();
    await screen.findByLabelText(/leave blank to keep/);

    expect(keyBox()).toHaveValue("sk-half-typed");
  });
});
