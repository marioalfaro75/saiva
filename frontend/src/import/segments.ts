import type { PreviewRow } from "../api/types";

/**
 * The preview grouped by what it is asking of you.
 *
 * A flat list capped at the first 200 rows can hide an entire class of decision: a
 * re-import is thousands of already-imported rows with a handful of ambiguous ones
 * somewhere among them, and those few are the only rows that need a person. Sorting
 * by status was supposed to be the way to reach them, but with more than 200 definite
 * duplicates it is not, because they sort first.
 *
 * So the cap is applied per segment instead, and never to a segment that needs
 * decisions. A cap may hide more rows of a kind you have already seen; it must never
 * hide the only rows that want an answer.
 */
export type SegmentId = "review" | "importing" | "existing";

export interface Segment {
  id: SegmentId;
  label: string;
  rows: PreviewRow[];
  /** Whether these rows want an answer, which is what decides if they can be capped. */
  decides: boolean;
  /** What to say when this segment is showing and empty. */
  empty: string;
}

const isReview = (r: PreviewRow) => r.status === "duplicate_probable";
const isExisting = (r: PreviewRow) =>
  r.status === "duplicate_exact" || r.status === "duplicate_provider";

/** Rows you are only reading are capped; rows you must answer are not. */
export const READING_CAP = 200;

export function segmentsOf(rows: PreviewRow[]): Segment[] {
  return [
    {
      id: "review",
      label: "Needs review",
      rows: rows.filter(isReview),
      decides: true,
      empty: "Nothing in this file looks like a near-match of something you already have.",
    },
    {
      id: "importing",
      label: "Will import",
      // Unassigned rows sit here rather than with the duplicates: they are not
      // duplicates, they are rows whose account you have not chosen, and that is a
      // decision — leaving them out of sight is how they get imported nowhere.
      rows: rows.filter((r) => !isReview(r) && !isExisting(r)),
      decides: true,
      empty: "Nothing new in this file.",
    },
    {
      id: "existing",
      label: "Already imported",
      rows: rows.filter(isExisting),
      decides: false,
      empty: "None of these rows is already in Saiva.",
    },
  ];
}

/**
 * Which segment to open on: the first that wants a decision and has rows, so a
 * re-import lands on the handful worth looking at rather than on thousands that are
 * already filed. Falling back to the largest keeps the preview from opening empty.
 */
export function openingSegment(segments: Segment[]): SegmentId {
  const wanting = segments.find((s) => s.decides && s.rows.length > 0);
  if (wanting) return wanting.id;
  const biggest = [...segments].sort((a, b) => b.rows.length - a.rows.length)[0];
  return biggest?.id ?? "importing";
}

/** The rows to render, and how many were held back. */
export function visible(segment: Segment): { rows: PreviewRow[]; hidden: number } {
  if (segment.decides || segment.rows.length <= READING_CAP) {
    return { rows: segment.rows, hidden: 0 };
  }
  return {
    rows: segment.rows.slice(0, READING_CAP),
    hidden: segment.rows.length - READING_CAP,
  };
}
