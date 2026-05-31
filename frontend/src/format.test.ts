import { describe, expect, it } from "vitest";

import { dollarsToCents, formatCents, formatDate, formatPct } from "./format";

describe("format helpers", () => {
  it("formats integer cents as AUD currency", () => {
    expect(formatCents(123456)).toBe("$1,234.56");
    expect(formatCents(-8540)).toBe("-$85.40");
  });

  it("formats fractions as percentages", () => {
    expect(formatPct(0.1234)).toBe("12.3%");
  });

  it("parses dollar strings to signed cents", () => {
    expect(dollarsToCents("$1,234.56")).toBe(123456);
    expect(dollarsToCents("-85.40")).toBe(-8540);
  });

  it("formats ISO dates in en-AU", () => {
    expect(formatDate("2025-06-01")).toMatch(/01\s+Jun\w*\s+2025/);
  });
});
