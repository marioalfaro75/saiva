import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderApp, stubApi } from "../testing/harness";
import { PeriodProvider } from "./PeriodProvider";
import { useSearchParams } from "react-router-dom";
import { PeriodPicker } from "./PeriodPicker";

/**
 * The period picker is the app's most consequential global control, and the shell
 * rebuild moves it. These tests pin what it must keep doing wherever it lands.
 */

const OPTIONS = {
  default: "fy:2024",
  relative: [
    { value: "this_month", label: "This month" },
    { value: "last_30d", label: "Last 30 days" },
  ],
  financial_years: [
    {
      value: "fy:2024",
      label: "FY2024–25",
      start: "2024-07-01",
      end: "2025-06-30",
      quarters: [{ value: "q:2024-1", label: "Q1 (Jul–Sep 2024)" }],
      months: [{ value: "month:2024-07", label: "July 2024" }],
    },
    {
      value: "fy:2023",
      label: "FY2023–24",
      start: "2023-07-01",
      end: "2024-06-30",
      quarters: [],
      months: [],
    },
  ],
};

function stub() {
  stubApi({
    "/periods/options": OPTIONS,
    "/periods/resolve": {
      start: "2024-07-01",
      end: "2025-06-30",
      label: "FY2024–25",
      is_current: true,
    },
  });
}

/** Surfaces the router's own query state; MemoryRouter never touches window.location. */
function UrlProbe() {
  const [params] = useSearchParams();
  return <output data-testid="url">{params.toString()}</output>;
}

const picker = () =>
  renderApp(
    <PeriodProvider>
      <PeriodPicker />
      <UrlProbe />
    </PeriodProvider>,
  );

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("PeriodPicker", () => {
  it("is labelled, so it can be found however the shell is arranged", async () => {
    stub();
    picker();
    expect(await screen.findByLabelText("Period")).toBeInTheDocument();
  });

  it("offers financial years, their quarters and months, and relative ranges", async () => {
    stub();
    picker();
    await screen.findByLabelText("Period");
    for (const label of [
      "FY2024–25",
      "FY2023–24",
      "Q1 (Jul–Sep 2024)",
      "July 2024",
      "This month",
      "All time",
    ]) {
      expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    }
  });

  it("puts the chosen period in the URL so a view can be reloaded or shared", async () => {
    stub();
    picker();
    const select = await screen.findByLabelText("Period");
    fireEvent.change(select, { target: { value: "fy:2023" } });
    expect(screen.getByTestId("url")).toHaveTextContent("period=fy%3A2023");
  });

  it("turns the placeholder period into the server's default once it knows one", async () => {
    // Before the catalogue arrives there is nothing concrete to select, so the
    // provider starts on a placeholder. It must not stay there: a link copied out of
    // the address bar has to name a real period, and the picker needs a selection.
    localStorage.clear();
    stub();
    picker();
    const select = await screen.findByLabelText("Period");
    expect(select).toHaveValue("fy:2024");
    await waitFor(() => expect(screen.getByTestId("url")).toHaveTextContent("period=fy%3A2024"));
    expect(localStorage.getItem("saiva.period")).toBe("fy:2024");
  });

  it("opens on the period in the URL rather than the server's default", async () => {
    stub();
    renderApp(
      <PeriodProvider>
        <PeriodPicker />
        <UrlProbe />
      </PeriodProvider>,
      { route: "/?period=fy:2023" },
    );
    expect(await screen.findByLabelText("Period")).toHaveValue("fy:2023");
  });

  it("renders nothing until the catalogue arrives, rather than an empty control", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const { container } = picker();
    expect(container.querySelector("select")).toBeNull();
  });
});
