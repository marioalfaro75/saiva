import { describe, expect, it } from "vitest";

import {
  type ColumnSpec,
  columnText,
  compareValues,
  filterRows,
  nextSort,
  sortRows,
} from "./sorting";

interface Row {
  name: string;
  amount: number;
  date: string;
  category: string | null;
}

const rows: Row[] = [
  { name: "Woolworths", amount: -8540, date: "2025-06-01", category: "Supermarkets" },
  { name: "Item 10", amount: -900, date: "2025-01-15", category: null },
  { name: "item 2", amount: -100000, date: "2025-12-31", category: "Rent" },
];

const columns: ColumnSpec<Row>[] = [
  { key: "name", sort: (r) => r.name },
  { key: "amount", sort: (r) => r.amount, text: (r) => `$${(r.amount / 100).toFixed(2)}` },
  { key: "date", sort: (r) => r.date },
  { key: "category", sort: (r) => r.category },
];

const by = (key: string) => columns.find((c) => c.key === key);

describe("compareValues", () => {
  it("orders numbers numerically, not as text", () => {
    // The bug this prevents: "-8540" sorting before "-900" as a string.
    expect(compareValues(-8540, -900, "asc")).toBeLessThan(0);
    expect(compareValues(-8540, -900, "desc")).toBeGreaterThan(0);
  });

  it("keeps blanks last whichever way the column is sorted", () => {
    for (const dir of ["asc", "desc"] as const) {
      expect(compareValues(null, "Rent", dir)).toBeGreaterThan(0);
      expect(compareValues("Rent", null, dir)).toBeLessThan(0);
      expect(compareValues("", "Rent", dir)).toBeGreaterThan(0);
    }
    expect(compareValues(null, undefined, "asc")).toBe(0);
  });

  it("compares text case-insensitively and with embedded numbers in order", () => {
    expect(compareValues("item 2", "Item 10", "asc")).toBeLessThan(0);
    expect(compareValues("apple", "Apple", "asc")).toBe(0);
  });

  it("orders booleans false before true", () => {
    expect(compareValues(false, true, "asc")).toBeLessThan(0);
  });
});

describe("sortRows", () => {
  it("sorts by the underlying value rather than the rendered string", () => {
    const asc = sortRows(rows, by("amount"), "asc").map((r) => r.amount);
    expect(asc).toEqual([-100000, -8540, -900]);
  });

  it("sorts dates chronologically", () => {
    expect(sortRows(rows, by("date"), "asc").map((r) => r.date)).toEqual([
      "2025-01-15",
      "2025-06-01",
      "2025-12-31",
    ]);
  });

  it("puts rows with no value last, not first", () => {
    expect(sortRows(rows, by("category"), "asc").map((r) => r.category)).toEqual([
      "Rent",
      "Supermarkets",
      null,
    ]);
    expect(sortRows(rows, by("category"), "desc").map((r) => r.category)).toEqual([
      "Supermarkets",
      "Rent",
      null,
    ]);
  });

  it("is stable for equal values and leaves the input untouched", () => {
    const tied: Row[] = [
      { name: "b", amount: 100, date: "2025-01-01", category: null },
      { name: "a", amount: 100, date: "2025-01-01", category: null },
    ];
    const sorted = sortRows(tied, by("amount"), "asc");
    expect(sorted.map((r) => r.name)).toEqual(["b", "a"]);
    expect(tied.map((r) => r.name)).toEqual(["b", "a"]);
  });

  it("returns rows unchanged for a column that declares no sort value", () => {
    expect(sortRows(rows, { key: "actions" }, "asc")).toBe(rows);
  });
});

describe("filterRows", () => {
  it("matches a case-insensitive substring", () => {
    expect(filterRows(rows, columns, { name: "wool" })).toHaveLength(1);
    expect(filterRows(rows, columns, { name: "ITEM" })).toHaveLength(2);
  });

  it("matches the formatted text of a numeric column", () => {
    // "85" should find -$85.40 even though the stored value is -8540 cents.
    expect(filterRows(rows, columns, { amount: "85" })).toHaveLength(1);
  });

  it("combines several column filters with AND", () => {
    expect(filterRows(rows, columns, { name: "item", category: "rent" })).toHaveLength(1);
    expect(filterRows(rows, columns, { name: "wool", category: "rent" })).toHaveLength(0);
  });

  it("ignores blank and unknown filters", () => {
    expect(filterRows(rows, columns, { name: "  ", nope: "x" })).toHaveLength(3);
    expect(filterRows(rows, columns, {})).toBe(rows);
  });

  it("treats a row with no value as not matching", () => {
    // "e" appears in both real categories; the uncategorised row must drop out.
    expect(filterRows(rows, columns, { category: "e" }).map((r) => r.category)).toEqual([
      "Supermarkets",
      "Rent",
    ]);
  });
});

describe("columnText", () => {
  it("falls back to the sort value when no text accessor is given", () => {
    expect(columnText(by("name") as ColumnSpec<Row>, rows[0])).toBe("Woolworths");
    expect(columnText(by("category") as ColumnSpec<Row>, rows[1])).toBe("");
  });
});

describe("nextSort", () => {
  it("cycles ascending, descending, then back to the natural order", () => {
    const first = nextSort(null, "name");
    expect(first).toEqual({ key: "name", dir: "asc" });
    const second = nextSort(first, "name");
    expect(second).toEqual({ key: "name", dir: "desc" });
    expect(nextSort(second, "name")).toBeNull();
  });

  it("starts a different column ascending", () => {
    expect(nextSort({ key: "name", dir: "desc" }, "amount")).toEqual({
      key: "amount",
      dir: "asc",
    });
  });
});
