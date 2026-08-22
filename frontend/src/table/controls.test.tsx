import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FilterRow, FilterToggle } from "./FilterRow";
import { FilterFields, SortChips } from "./MobileControls";
import { SortHeader } from "./SortHeader";
import type { TableControls } from "./useTable";

/**
 * `SortHeader` and `FilterRow` are the table-shaped presentations of `TableControls`.
 * The phone work adds sibling presentations (sort chips, stacked filter fields) that
 * consume the same interface, so these tests pin the contract both sides rely on:
 * what gets announced, and which callbacks fire with which arguments.
 */

type Callbacks = "toggleSort" | "setFilter" | "clearFilters" | "toggleFilters";
/** The callbacks stay spies — only the state around them is worth overriding. */
type MockedControls = Omit<TableControls, Callbacks> & Record<Callbacks, ReturnType<typeof vi.fn>>;

function controls(over: Partial<Omit<TableControls, Callbacks>> = {}): MockedControls {
  return {
    sort: null,
    toggleSort: vi.fn(),
    filters: {},
    setFilter: vi.fn(),
    clearFilters: vi.fn(),
    filtersOpen: true,
    toggleFilters: vi.fn(),
    activeFilterCount: 0,
    matched: 0,
    total: 0,
    ...over,
  };
}

const inTable = (node: React.ReactNode) => (
  <table>
    <thead>
      <tr>{node}</tr>
    </thead>
  </table>
);

describe("SortHeader", () => {
  it("announces the sort state to assistive tech", () => {
    const { rerender } = render(inTable(<SortHeader table={controls()} col="date">Date</SortHeader>));
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "none");

    rerender(
      inTable(
        <SortHeader table={controls({ sort: { key: "date", dir: "asc" } })} col="date">
          Date
        </SortHeader>,
      ),
    );
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "ascending");

    rerender(
      inTable(
        <SortHeader table={controls({ sort: { key: "date", dir: "desc" } })} col="date">
          Date
        </SortHeader>,
      ),
    );
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "descending");
  });

  it("does not claim a sort that belongs to another column", () => {
    render(
      inTable(
        <SortHeader table={controls({ sort: { key: "amount", dir: "asc" } })} col="date">
          Date
        </SortHeader>,
      ),
    );
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "none");
  });

  it("sorts from the keyboard, because the header is a real button", () => {
    const table = controls();
    render(inTable(<SortHeader table={table} col="amount">Amount</SortHeader>));
    const button = screen.getByRole("button", { name: /Amount/ });
    button.focus();
    expect(button).toHaveFocus();
    fireEvent.click(button);
    expect(table.toggleSort).toHaveBeenCalledWith("amount");
  });
});

describe("FilterRow", () => {
  const labels = { date: "Date", amount: "Amount" };

  it("labels each filter by its column", () => {
    render(
      <table>
        <thead>
          <FilterRow table={controls()} labels={labels} columns={["date", "amount"]} />
        </thead>
      </table>,
    );
    expect(screen.getByLabelText("Filter Date")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter Amount")).toBeInTheDocument();
  });

  it("reports edits against the column key", () => {
    const table = controls();
    render(
      <table>
        <thead>
          <FilterRow table={table} labels={labels} columns={["date", "amount"]} />
        </thead>
      </table>,
    );
    fireEvent.change(screen.getByLabelText("Filter Amount"), { target: { value: "45" } });
    expect(table.setFilter).toHaveBeenCalledWith("amount", "45");
  });

  it("renders nothing while the filter row is closed", () => {
    const { container } = render(
      <table>
        <thead>
          <FilterRow
            table={controls({ filtersOpen: false })}
            labels={labels}
            columns={["date"]}
          />
        </thead>
      </table>,
    );
    expect(container.querySelector("input")).toBeNull();
  });

  it("leaves a cell empty for columns that cannot be filtered", () => {
    render(
      <table>
        <thead>
          <FilterRow table={controls()} labels={labels} columns={[null, "date"]} />
        </thead>
      </table>,
    );
    // One input for one filterable column; the blank cell keeps the row aligned.
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });
});

describe("FilterToggle", () => {
  it("shows how much of the table is hidden, and offers a way back", () => {
    const table = controls({ activeFilterCount: 2, matched: 12, total: 340 });
    render(<FilterToggle table={table} />);
    expect(screen.getByText("12 of 340")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Clear/ }));
    expect(table.clearFilters).toHaveBeenCalled();
  });

  it("reports the match count alone when the total is not known", () => {
    // Server-paginated tables know how many rows matched, not the unfiltered total.
    render(<FilterToggle table={controls({ activeFilterCount: 1 })} count={87} />);
    expect(screen.getByText("87 matching")).toBeInTheDocument();
  });

  it("says whether the filter row is open", () => {
    const table = controls({ filtersOpen: false });
    render(<FilterToggle table={table} />);
    const button = screen.getByRole("button", { name: /Filter/ });
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(table.toggleFilters).toHaveBeenCalled();
  });
});

/**
 * The phone presentations. What matters here is that they are *presentations*: a
 * chip must reach `toggleSort` with the same argument a column header sends, so the
 * ascending → descending → off cycle and the URL it writes stay in one place.
 */
describe("SortChips", () => {
  const labels = { date: "Date", amount: "Amount" };

  it("calls the same toggleSort a column header calls", () => {
    const chip = controls();
    const chips = render(<SortChips table={chip} labels={labels} columns={["date", "amount"]} />);
    fireEvent.click(chips.getByRole("button", { name: /Amount/ }));
    chips.unmount();

    // Same control, same call — only the shape of the thing you press differs.
    const header = controls();
    const head = render(inTable(<SortHeader table={header} col="amount">Amount</SortHeader>));
    fireEvent.click(head.getByRole("button", { name: /Amount/ }));

    expect(chip.toggleSort.mock.calls).toEqual(header.toggleSort.mock.calls);
    expect(chip.toggleSort).toHaveBeenCalledWith("amount");
  });

  it("marks the sorted column pressed, and says which way", () => {
    render(
      <SortChips
        table={controls({ sort: { key: "date", dir: "desc" } })}
        labels={labels}
        columns={["date", "amount"]}
      />,
    );
    expect(screen.getByRole("button", { name: /Date/ })).toHaveAttribute("aria-pressed", "true");
    // The arrow is decorative; the direction has to be readable without it.
    expect(screen.getByRole("button", { name: /Date/ })).toHaveAccessibleName(
      /sorted descending/,
    );
    expect(screen.getByRole("button", { name: /Amount/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("groups the chips so they are announced as one control", () => {
    render(<SortChips table={controls()} labels={labels} columns={["date"]} />);
    expect(screen.getByRole("group", { name: "Sort by" })).toBeInTheDocument();
  });
});

describe("FilterFields", () => {
  const labels = { date: "Date", amount: "Amount" };

  it("labels and reports edits exactly as the filter row does", () => {
    const table = controls();
    render(<FilterFields table={table} labels={labels} columns={["date", "amount"]} />);
    fireEvent.change(screen.getByLabelText("Filter Date"), { target: { value: "Feb" } });
    expect(table.setFilter).toHaveBeenCalledWith("date", "Feb");
  });

  it("stays closed with the filter row", () => {
    const { container } = render(
      <FilterFields table={controls({ filtersOpen: false })} labels={labels} columns={["date"]} />,
    );
    expect(container.querySelector("input")).toBeNull();
  });
});
