import { describe, expect, it } from "vitest";

import type { PreviewRow } from "../api/types";
import { openingSegment, READING_CAP, segmentsOf, visible } from "./segments";

/**
 * Re-importing a statement you already imported is thousands of rows that need
 * nothing and a handful that need an answer. Showing them as one list capped at the
 * first 200 put the answerable ones out of reach entirely — and sorting by status,
 * which was meant to be the way to them, does not help when the definite duplicates
 * that sort ahead of them number in the thousands.
 */

const row = (i: number, status: string): PreviewRow =>
  ({
    row_index: i,
    txn_date: "2026-05-29",
    amount_cents: -119954,
    raw_description: `ROW ${i}`,
    merchant: null,
    suggested_category_id: null,
    suggested_category_name: null,
    confidence: null,
    is_duplicate: status.startsWith("duplicate"),
    status,
    duplicate_reason: null,
    matched_txn_id: null,
    matched_date: null,
    matched_description: null,
    will_import: status === "new",
  }) as unknown as PreviewRow;

/** The shape of the reported case: 2,229 already imported, 6 needing a look. */
const reImport = (): PreviewRow[] => [
  ...Array.from({ length: 2229 }, (_, i) => row(i, "duplicate_exact")),
  ...Array.from({ length: 6 }, (_, i) => row(2229 + i, "duplicate_probable")),
];

describe("segmentsOf", () => {
  it("separates rows that want an answer from rows that do not", () => {
    const [review, importing, existing] = segmentsOf([
      row(0, "duplicate_probable"),
      row(1, "new"),
      row(2, "duplicate_exact"),
      row(3, "duplicate_provider"),
      row(4, "unassigned"),
    ]);
    expect(review.rows.map((r) => r.row_index)).toEqual([0]);
    // Unassigned sits with what will import, not with the duplicates: it is a row
    // whose account has not been chosen, and that is a decision.
    expect(importing.rows.map((r) => r.row_index)).toEqual([1, 4]);
    expect(existing.rows.map((r) => r.row_index)).toEqual([2, 3]);
  });
});

describe("openingSegment", () => {
  it("opens on the handful that need looking at, not the thousands that do not", () => {
    expect(openingSegment(segmentsOf(reImport()))).toBe("review");
  });

  it("opens on what will import when nothing needs review", () => {
    const rows = [row(0, "new"), row(1, "duplicate_exact")];
    expect(openingSegment(segmentsOf(rows))).toBe("importing");
  });

  it("falls back to the largest rather than opening empty", () => {
    const rows = Array.from({ length: 3 }, (_, i) => row(i, "duplicate_exact"));
    expect(openingSegment(segmentsOf(rows))).toBe("existing");
  });
});

describe("visible", () => {
  it("never holds back a row that needs an answer", () => {
    // Far more probable duplicates than the reading cap: every one is still shown,
    // because a cap may hide more of what you have seen, never the only rows asking
    // you something.
    const many = Array.from({ length: READING_CAP + 300 }, (_, i) =>
      row(i, "duplicate_probable"),
    );
    const [review] = segmentsOf(many);
    expect(visible(review)).toEqual({ rows: review.rows, hidden: 0 });
  });

  it("caps rows that are only being read, and says how many it held back", () => {
    const [, , existing] = segmentsOf(reImport());
    const { rows, hidden } = visible(existing);
    expect(rows).toHaveLength(READING_CAP);
    expect(hidden).toBe(2229 - READING_CAP);
  });

  it("does not cap a short list of read-only rows", () => {
    const [, , existing] = segmentsOf([row(0, "duplicate_exact")]);
    expect(visible(existing).hidden).toBe(0);
  });

  it("reaches a row that the old flat cap could not", () => {
    // The reported case: row 2,230 of 2,235. Under a single list capped at 200 it was
    // unreachable by any sequence of clicks — status sorts the 2,229 definite
    // duplicates ahead of it.
    const rows = reImport();
    const buried = rows[rows.length - 1];
    const [review] = segmentsOf(rows);
    expect(visible(review).rows).toContain(buried);
  });
});
