import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import type { Mortgage as MortgageData, MortgageProjection, NetWorth as NetWorthData } from "../api/types";
import { nextSchedule } from "../components/accountTypes";
import { CLUSTERS } from "../components/navItems";
import { localToday, monthsApart, monthsBetween, monthYear, span } from "../components/mortgageText";
import { PeriodCtx } from "../period/context";
import { renderApp } from "../testing/harness";
import { Mortgage } from "./Mortgage";
import { NetWorth } from "./NetWorth";

/**
 * The Mortgage page (0.19.0): set up by the household -- the loan and its offset, what was
 * borrowed and when, the term, the rate over time, the repayment -- then when it is paid
 * off, as they pay and with regular repayments only, a what-if, the offset's effect, the
 * repayments made and a schedule.
 */

const SETUP_NEEDED: MortgageData = {
  account_id: "loan",
  name: "Home loan",
  set_up: false,
  original_cents: null,
  start_date: null,
  term_months: null,
  rates: [],
  offsets: [],
  balance_as_of: null,
  repayment_schedule: [],
  repayment_suggestion: { since: "2026-06-05", cents: 70000, every: "weekly" },
  offset_choices: [
    { account_id: "off", name: "Offset", balance_cents: 2_500_000 },
    { account_id: "sav", name: "Savings", balance_cents: 100_000 },
  ],
  missing: ["setup"],
  paid: [],
  projection: null,
};

const PROJECTION: MortgageProjection = {
  owing_cents: 45_000_000,
  estimated: false,
  known_on: "2026-11-15",
  known_cents: 45_000_000,
  offset_cents: 3_000_000,
  usual_extra_cents: 200_000,
  next_due: "2026-11-22",
  next_cents: 70000,
  every: "weekly",
  rate: 6.24,
  contract_end: "2050-03-15",
  needed_cents: 65000,
  as_you_pay: { payoff: "2041-03-14", interest_cents: 20_000_000 },
  regular_only: { payoff: "2046-09-11", interest_cents: 26_000_000 },
  without_offset: { payoff: "2043-01-09", interest_cents: 23_000_000 },
  what_if: null,
  what_if_needed_cents: null,
  points: [
    { month: "2026-11", as_you_pay: 44_900_000, regular_only: 44_950_000 },
    { month: "2026-12", as_you_pay: 44_700_000, regular_only: 44_850_000 },
  ],
  schedule: [
    { year: 2026, regular_cents: 420_000, extra_cents: 100_000, interest_cents: 300_000, owing_cents: 44_700_000 },
  ],
  what_if_schedule: null,
};

const SET_UP: MortgageData = {
  ...SETUP_NEEDED,
  set_up: true,
  original_cents: 60_000_000,
  start_date: "2020-03-15",
  term_months: 360,
  rates: [{ since: null, percent: 6.24 }],
  offsets: [{ account_id: "off", name: "Offset", balance_cents: 3_000_000 }],
  balance_as_of: "2026-11-15",
  repayment_schedule: [{ since: null, cents: 70000, every: "weekly" }],
  repayment_suggestion: null,
  missing: [],
  paid: [
    { month: "2026-11", regular_cents: 140000, extra_cents: 0 },
    { month: "2026-10", regular_cents: 280000, extra_cents: 500000 },
  ],
  projection: PROJECTION,
};

function show(loan: MortgageData | null) {
  vi.spyOn(api, "mortgages").mockResolvedValue(
    loan ? [{ account_id: loan.account_id, name: loan.name, set_up: loan.set_up }] : [],
  );
  const get = vi.spyOn(api, "mortgage");
  if (loan) get.mockResolvedValue(loan);
  renderApp(<Mortgage />);
  return get;
}

const input = (name: string | RegExp) => screen.getByRole("textbox", { name });
const type = (name: string | RegExp, value: string) =>
  fireEvent.change(input(name), { target: { value } });

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("Mortgage page", () => {
  it("is in the menu straight after Budgets", () => {
    const labels = CLUSTERS.flat().map((i) => i.label);
    expect(labels[labels.indexOf("Budgets") + 1]).toBe("Mortgage");
  });

  it("without a home loan, says to add one on Accounts", async () => {
    show(null);
    expect(await screen.findByText("No home loan yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Accounts" })).toHaveAttribute("href", "/accounts");
  });

  it("sets the loan up from what is on the statement", async () => {
    show(SETUP_NEEDED);
    const save = vi.spyOn(api, "saveMortgage").mockResolvedValue(SET_UP);
    await screen.findByText("Set up your home loan");

    type("Amount borrowed ($)", "600,000");
    fireEvent.change(screen.getByLabelText("Loan started"), { target: { value: "2020-03-15" } });
    type("Loan term (years)", "30");
    type("Rate (%)", "6.24");
    fireEvent.click(screen.getByRole("button", { name: "Use this" }));
    expect(input("Repayment ($)")).toHaveValue("700.00");
    type("You owe ($)", "452,000");
    fireEvent.click(screen.getByRole("checkbox", { name: "Offset" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Offset balance today ($)" }), {
      target: { value: "30,000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0]).toEqual([
      "loan",
      {
        original_cents: 60_000_000,
        start_date: "2020-03-15",
        term_months: 360,
        rates: [{ since: null, percent: 6.24 }],
        offsets: [{ account_id: "off", balance_cents: 3_000_000 }],
        owing_cents: 45_200_000,
        owing_on: localToday(),
        repayment_schedule: [{ since: "2026-06-05", cents: 70000, every: "weekly" }],
        today: localToday(),
      },
    ]);
  });

  it("sends an offset's balance only when it was changed", async () => {
    show({ ...SET_UP, projection: PROJECTION });
    const save = vi.spyOn(api, "saveMortgage").mockResolvedValue(SET_UP);
    fireEvent.click(await screen.findByRole("button", { name: "Edit setup" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(save).toHaveBeenCalled());
    const body = save.mock.calls[0][1];
    expect(body.offsets).toEqual([{ account_id: "off", balance_cents: null }]);
    expect(body.owing_cents).toBeNull(); // left empty: kept
    expect(body.repayment_schedule).toEqual([{ since: null, cents: 70000, every: "weekly" }]);
  });

  it("won't save a rate that isn't one, or a first setup without what is owed", async () => {
    show(SETUP_NEEDED);
    await screen.findByText("Set up your home loan");
    expect(screen.getByText("Enter what you owe now.")).toBeInTheDocument();
    type("Rate (%)", "six");
    expect(
      screen.getByText("Enter the interest rate as a percentage, e.g. 6.24."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("shows what is owed, when it is paid off, the next repayment, and what extras and the offset save", async () => {
    show(SET_UP);
    expect(await screen.findByText("$450,000.00")).toBeInTheDocument();
    expect(screen.getAllByText("Mar 2041").length).toBeGreaterThan(0);
    expect(screen.getByText("$700.00")).toBeInTheDocument();
    expect(screen.getByText(/You've paid off 25% of the \$600,000\.00 you borrowed/)).toBeInTheDocument();
    expect(screen.getByText(/finish 9 years early/)).toBeInTheDocument();
    expect(
      screen.getByText(/usual extra repayments \(about \$2,000\.00 a month\) pay it off 5 years 6 months sooner and save \$60,000\.00 in interest\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/offset \(\$30,000\.00\) pays it off 1 year 10 months sooner and saves \$30,000\.00 in interest\./)).toBeInTheDocument();
    expect(screen.getByText(/Regular repayments only:/)).toBeInTheDocument();
  });

  it("says what the extras made in the last year did", async () => {
    show({
      ...SET_UP,
      projection: {
        ...PROJECTION,
        extras_made_cents: 1_000_000,
        without_extras_made: { payoff: "2047-11-01", interest_cents: 27_500_000, stalls: false },
      },
    });
    expect(
      await screen.findByText(
        /extra repayments in the last 12 months \(\$10,000\.00\) bring your pay-off forward 1 year 2 months and save \$15,000\.00 in interest/,
      ),
    ).toBeInTheDocument();
  });

  it("lists the repayments made, extras apart, and the schedule by year", async () => {
    show(SET_UP);
    const made = await screen.findByRole("region", { name: "Repayments made" });
    expect(within(made).getByText("Oct 2026")).toBeInTheDocument();
    expect(within(made).getByText("$5,000.00")).toBeInTheDocument();
    const schedule = screen.getByRole("region", { name: "Repayment schedule" });
    expect(within(schedule).getByText("2026")).toBeInTheDocument();
    expect(within(schedule).getByText("$447,000.00")).toBeInTheDocument();
  });

  it("tries a what-if without saving, once typing pauses", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    get.mockResolvedValue({
      ...SET_UP,
      projection: {
        ...PROJECTION,
        what_if: { payoff: "2038-01-01", interest_cents: 17_000_000 },
        what_if_schedule: PROJECTION.schedule,
      },
    });

    type("Extra each week ($)", "200");

    await waitFor(() => expect(get).toHaveBeenLastCalledWith("loan", expect.objectContaining({ extra_cents: 20000 })));
    expect(
      await screen.findByText(/Paid off Jan 2038: 3 years 2 months sooner, saving \$30,000\.00 in interest/),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Show the what-if" })).toBeInTheDocument();
  });

  it("an offset can be taken down, and a rate cut, without a minus key", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    fireEvent.change(screen.getByRole("combobox", { name: "Offset" }), { target: { value: "out" } });
    type("Offset amount ($)", "5,000");
    fireEvent.change(screen.getByRole("combobox", { name: "Rate" }), { target: { value: "down" } });
    type("Rate change (%)", "0.25");
    await waitFor(() =>
      expect(get).toHaveBeenLastCalledWith(
        "loan",
        expect.objectContaining({ offset_change_cents: -500000, rate_change: -0.25 }),
      ),
    );
  });

  it("says a what-if that's later costs more, and a same-month one is about the same", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    const later = { payoff: "2041-04-04", interest_cents: 20_250_000, stalls: false };
    get.mockResolvedValue({ ...SET_UP, projection: { ...PROJECTION, what_if: later } });
    type("Offset amount ($)", "1,000");
    expect(
      await screen.findByText("Paid off Apr 2041: 1 month later, costing $2,500.00 more in interest."),
    ).toBeInTheDocument();

    const same = { payoff: "2041-03-28", interest_cents: 19_990_000, stalls: false };
    get.mockResolvedValue({ ...SET_UP, projection: { ...PROJECTION, what_if: same } });
    type("Offset amount ($)", "50");
    expect(
      await screen.findByText("Paid off Mar 2041: about the same time, saving $100.00 in interest."),
    ).toBeInTheDocument();
  });

  it("says why a what-if is never paid off, and gives the bank's repayment for a rate change", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    get.mockResolvedValue({
      ...SET_UP,
      projection: {
        ...PROJECTION,
        what_if: { payoff: null, interest_cents: 1, stalls: true },
        what_if_needed_cents: 81234,
      },
    });
    type("Rate change (%)", "3");
    expect(
      await screen.findByText(/repayments wouldn't cover the interest\. At that rate, your bank may set your repayment to about \$812\.34\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/What if:/).parentElement).toHaveTextContent(
      "never (the repayments don't cover the interest)",
    );
  });

  it("keeps the page when a what-if fails", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    get.mockRejectedValue(new Error("down"));
    type("Extra each week ($)", "200");
    expect(await screen.findByText("Couldn't try that. Please try again.")).toBeInTheDocument();
    expect(screen.getByText("$450,000.00")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Extra each week ($)" })).toHaveValue("200");
  });

  it("warns when net worth has a mortgage entered by hand", async () => {
    show({ ...SET_UP, manual_liabilities: ["Mortgage"] });
    const link = await screen.findByRole("link", { name: "Net worth" });
    expect(link).toHaveAttribute("href", "/net-worth");
    expect(link.parentElement).toHaveTextContent(/Net worth also has “Mortgage” entered by hand/);
  });

  it("keeps an earlier setup without a term without one, and takes a negative offset", async () => {
    show({
      ...SET_UP,
      term_months: null,
      offset_choices: [{ account_id: "off", name: "Offset", balance_cents: -12345 }],
    });
    const save = vi.spyOn(api, "saveMortgage").mockResolvedValue(SET_UP);
    fireEvent.click(await screen.findByRole("button", { name: "Edit setup" }));
    expect(input("Loan term (years)")).toHaveValue("");
    expect(input("Offset balance today ($)")).toHaveValue("-123.45");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][1].term_months).toBeNull();
  });

  it("asks for the date when what is owed is given without one", async () => {
    show(SETUP_NEEDED);
    await screen.findByText("Set up your home loan");
    type("You owe ($)", "452,000");
    fireEvent.change(screen.getByLabelText("On"), { target: { value: "" } });
    expect(screen.getByText("Enter the date for what you owe.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("leaves out a saving of no time, and a line with nothing saved", async () => {
    show({
      ...SET_UP,
      projection: {
        ...PROJECTION,
        offset_cents: 50000,
        without_offset: { payoff: "2041-03-30", interest_cents: 20_040_000, stalls: false },
        usual_extra_cents: 0,
        contract_end: "2025-03-15",
      },
    });
    expect(
      await screen.findByText("Your offset ($500.00) saves $400.00 in interest."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/usual extra repayments/)).not.toBeInTheDocument();
    expect(screen.getByText(/Your loan term ended Mar 2025/)).toBeInTheDocument();
  });

  it("clears the what-if at once, and Clear is off with nothing to clear", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    const clear = screen.getByRole("button", { name: "Clear" });
    expect(clear).toBeDisabled();
    get.mockResolvedValue({
      ...SET_UP,
      projection: { ...PROJECTION, what_if: { payoff: "2038-01-01", interest_cents: 1, stalls: false } },
    });
    type("Extra each week ($)", "200");
    await screen.findByText(/Paid off Jan 2038/);
    fireEvent.click(clear);
    expect(screen.queryByText(/Paid off Jan 2038/)).not.toBeInTheDocument();
    expect(input("Extra each week ($)")).toHaveValue("");

    // The next what-if says it is working, not the cleared one's answer.
    get.mockReturnValue(new Promise(() => {}));
    type("One-off payment now ($)", "50");
    expect(await screen.findByText("Working it out…")).toBeInTheDocument();
    expect(screen.queryByText(/Paid off Jan 2038/)).not.toBeInTheDocument();
    expect(screen.queryByText("What if:")).not.toBeInTheDocument(); // nor in the chart
  });

  it("says when a rate change is too big, or the amount too large to try", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    type("Rate change (%)", "12");
    expect(await screen.findByText("Try a rate change of up to 10%, e.g. 0.5.")).toBeInTheDocument();
    get.mockRejectedValue(new ApiError(422, "too big"));
    type("Rate change (%)", "");
    type("One-off payment now ($)", "99,999,999");
    expect(await screen.findByText("That's too large to try.")).toBeInTheDocument();
  });

  it("keeps the page when a refresh fails", async () => {
    const get = show(SET_UP);
    const save = vi.spyOn(api, "saveMortgage").mockResolvedValue(SET_UP);
    fireEvent.click(await screen.findByRole("button", { name: "Edit setup" }));
    get.mockRejectedValue(new Error("down"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(screen.getByText("Couldn't refresh. Showing what Saiva had.")).toBeInTheDocument();
    expect(screen.getByText("$450,000.00")).toBeInTheDocument();
  });

  it("shows a first setup as saved even when fetching it again fails", async () => {
    const get = show(SETUP_NEEDED);
    vi.spyOn(api, "saveMortgage").mockResolvedValue(SET_UP);
    await screen.findByText("Set up your home loan");
    type("Rate (%)", "6.24");
    type("You owe ($)", "450,000");
    get.mockRejectedValue(new Error("down"));
    vi.spyOn(api, "mortgages").mockRejectedValue(new Error("down"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(screen.getByText("Couldn't refresh. Showing what Saiva had.")).toBeInTheDocument();
    expect(screen.getByText("$450,000.00")).toBeInTheDocument();
    expect(screen.queryByText("Set up your home loan")).not.toBeInTheDocument();
  });

  it("says a what-if that isn't about size couldn't be tried, not that it's too large", async () => {
    const get = show(SET_UP);
    await screen.findByText("$450,000.00");
    get.mockRejectedValue(new ApiError(401, "Not signed in"));
    type("Extra each week ($)", "200");
    expect(await screen.findByText("Couldn't try that. Please try again.")).toBeInTheDocument();
  });

  it("won't save with the repayment emptied: it would lose the history", async () => {
    show(SET_UP);
    fireEvent.click(await screen.findByRole("button", { name: "Edit setup" }));
    type("Repayment ($)", "");
    expect(screen.getByText("Enter your regular repayment.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("asks for the rate when it is missing", async () => {
    show({ ...SET_UP, missing: ["rate"], projection: null });
    expect(await screen.findByText("Almost there")).toBeInTheDocument();
    expect(screen.getByText(/Add your interest rate/)).toBeInTheDocument();
  });
});

describe("how the page says time", () => {
  it("says months and years plainly", () => {
    expect(monthYear("2041-03-14")).toBe("Mar 2041");
    expect(span(30)).toBe("2 years 6 months");
    expect(span(12)).toBe("1 year");
    expect(span(1)).toBe("1 month");
    expect(span(0)).toBe("0 months");
    expect(monthsBetween("2026-01-15", "2026-03-14")).toBe(1);
    expect(monthsBetween("2026-03-15", "2026-01-10")).toBe(-2);
    // Pay-off months as shown: Jan 2041 to Feb 2041 is a month, whatever the days.
    expect(monthsApart("2041-01-31", "2041-02-01")).toBe(1);
    expect(monthsApart("2041-04-04", "2041-03-14")).toBe(-1);
    expect(localToday(new Date(2026, 10, 16, 7, 30))).toBe("2026-11-16");
  });
});

describe("editing the regular repayment", () => {
  const weekly = (since: string | null, cents: number) => ({ since, cents, every: "weekly" as const });

  it("starts a new repayment from a later date, keeping the one before", () => {
    expect(nextSchedule([weekly(null, 70000)], weekly("2026-09-01", 75000))).toEqual([
      weekly(null, 70000),
      weekly("2026-09-01", 75000),
    ]);
  });

  it("corrects the repayment on its own date or an earlier one", () => {
    const schedule = [weekly(null, 70000), weekly("2026-09-01", 80000)];
    expect(nextSchedule(schedule, weekly("2026-09-01", 75000))).toEqual([
      weekly(null, 70000),
      weekly("2026-09-01", 75000),
    ]);
    // Moved back to August: the August one is the current one, not an earlier one.
    expect(nextSchedule(schedule, weekly("2026-08-01", 75000))).toEqual([
      weekly(null, 70000),
      weekly("2026-08-01", 75000),
    ]);
    expect(nextSchedule(schedule, weekly(null, 75000))).toEqual([weekly(null, 75000)]);
  });
});

describe("Net worth accounts", () => {
  it("counts an account once it is ticked", async () => {
    const worth: NetWorthData = {
      assets_cents: 0,
      liabilities_cents: 0,
      net_cents: 0,
      items: [],
      history: [],
      accounts: [
        { id: "sav", name: "Savings", type: "savings", balance_cents: 250000, in_net_worth: false },
      ],
    };
    vi.spyOn(api, "netWorth").mockResolvedValue(worth);
    const update = vi.spyOn(api, "updateAccount").mockResolvedValue({} as never);
    renderApp(
      <PeriodCtx.Provider
        value={{ period: "month", setPeriod: () => {}, resolved: null, options: undefined, isPast: false }}
      >
        <NetWorth />
      </PeriodCtx.Provider>,
    );

    fireEvent.click(await screen.findByRole("checkbox", { name: "Count Savings" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith("sav", { in_net_worth: true }));
  });

  it("adds up the items apart from the counted accounts", async () => {
    const worth: NetWorthData = {
      assets_cents: 85_250_000,
      liabilities_cents: 0,
      net_cents: 85_250_000,
      items: [{ id: "h", name: "Family home", kind: "asset", value_cents: 85_000_000 }],
      history: [],
      accounts: [
        { id: "sav", name: "Savings", type: "savings", balance_cents: 250000, in_net_worth: true },
      ],
    };
    vi.spyOn(api, "netWorth").mockResolvedValue(worth);
    renderApp(
      <PeriodCtx.Provider
        value={{ period: "month", setPeriod: () => {}, resolved: null, options: undefined, isPast: false }}
      >
        <NetWorth />
      </PeriodCtx.Provider>,
    );
    await screen.findByText("Family home");
    const assets = screen.getByRole("heading", { name: "Assets you added" }).closest(".card");
    expect(assets).toHaveTextContent("$850,000.00");
    expect(assets).not.toHaveTextContent("$852,500.00");
    const liabilities = screen.getByRole("heading", { name: "Liabilities you added" }).closest(".card");
    expect(liabilities).toHaveTextContent("$0.00");
  });
});
