import { beforeEach, describe, expect, it } from "vitest";

import { EMPTY_STATE, loadTableState, saveTableState } from "./persist";

beforeEach(() => localStorage.clear());

describe("table state persistence", () => {
  it("round-trips how a table was left", () => {
    saveTableState("accounts", {
      sort: { key: "balance", dir: "desc" },
      filters: { name: "sav" },
      filtersOpen: true,
    });
    expect(loadTableState("accounts")).toEqual({
      sort: { key: "balance", dir: "desc" },
      filters: { name: "sav" },
      filtersOpen: true,
    });
  });

  it("keeps tables separate", () => {
    saveTableState("accounts", { sort: { key: "name", dir: "asc" }, filters: {}, filtersOpen: false });
    expect(loadTableState("bills")).toEqual(EMPTY_STATE);
  });

  it("returns empty state when nothing was stored", () => {
    expect(loadTableState("unseen")).toEqual(EMPTY_STATE);
  });

  it("discards malformed storage rather than throwing", () => {
    // A page must still render if storage was hand-edited or written by an old build.
    for (const bad of ["not json", "null", '"a string"', '{"sort":{"key":1,"dir":"sideways"}}']) {
      localStorage.setItem("saiva.table.accounts", bad);
      expect(() => loadTableState("accounts")).not.toThrow();
      expect(loadTableState("accounts").sort).toBeNull();
    }
  });

  it("drops non-string and empty filter values", () => {
    localStorage.setItem(
      "saiva.table.accounts",
      JSON.stringify({ sort: null, filters: { name: "ok", bad: 7, blank: "" }, filtersOpen: false }),
    );
    expect(loadTableState("accounts").filters).toEqual({ name: "ok" });
  });

  it("reopens the filter row when a restored filter is hiding rows", () => {
    // Otherwise the table would silently show a subset with no visible reason.
    saveTableState("accounts", {
      sort: null,
      filters: { name: "sav" },
      filtersOpen: false,
    });
    expect(loadTableState("accounts").filtersOpen).toBe(true);
  });
});
