import { describe, expect, it } from "vitest";

import { isAmbiguous, readDate } from "./dates";

/**
 * The one import setting that can be wrong without anything failing. `01/07/2025`
 * parses either way round, so a wrong choice files a year of transactions into the
 * wrong months and nothing complains — which is why the wizard states the reading
 * against a real value from the file instead of leaving it to a default.
 */
describe("readDate", () => {
  it("reads the ambiguous case both ways", () => {
    expect(readDate("01/07/2025", true)).toBe("1 July 2025");
    expect(readDate("01/07/2025", false)).toBe("7 January 2025");
  });

  it("copes with a day that is not zero-padded, as Westpac writes it", () => {
    expect(readDate("7/08/2026", true)).toBe("7 August 2026");
  });

  it("accepts hyphens as well as slashes", () => {
    expect(readDate("01-07-2025", true)).toBe("1 July 2025");
  });

  it("expands a two-digit year", () => {
    expect(readDate("01/07/25", true)).toBe("1 July 2025");
  });

  it("ignores the setting when the year comes first", () => {
    // ISO order is unambiguous, so day-first has nothing to say about it.
    expect(readDate("2025-07-01", true)).toBe("1 July 2025");
    expect(readDate("2025-07-01", false)).toBe("1 July 2025");
  });

  it("declines anything it cannot read numerically", () => {
    expect(readDate("14 Aug 2026", true)).toBeNull();
    expect(readDate("not a date", true)).toBeNull();
    expect(readDate("", true)).toBeNull();
  });

  it("declines an impossible reading rather than inventing one", () => {
    // 14 cannot be a month, so month-first has no answer for it.
    expect(readDate("14/08/2026", false)).toBeNull();
    expect(readDate("14/08/2026", true)).toBe("14 August 2026");
  });
});

describe("isAmbiguous", () => {
  it("is true only when the two readings disagree", () => {
    expect(isAmbiguous("01/07/2025")).toBe(true);
    // Day 14 rules out month-first, so there is nothing to warn about.
    expect(isAmbiguous("14/08/2026")).toBe(false);
    expect(isAmbiguous("2025-07-01")).toBe(false);
  });
});
