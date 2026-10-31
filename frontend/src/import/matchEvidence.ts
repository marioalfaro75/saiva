import type { MatchNeighbour, PreviewRow } from "../api/types";
import { formatCents, formatDate, relativeDays } from "../format";

/**
 * The evidence for a possible duplicate, as the two or three lines a person reads
 * before ticking the box.
 *
 * Pure and separate from rendering for two reasons. The sentences are the hard part
 * of this feature — each of the matcher's four tiers found the row for a different
 * reason and needs saying differently — and they are worth testing without a DOM.
 * And both render paths need the identical text, so there is one definition.
 *
 * What is deliberately NOT here: a field-by-field comparison grid. On a probable
 * duplicate the amount is identical by construction (dedup.py buckets candidates on
 * exact amount), so a diff would spend its most prominent row telling you the amounts
 * match, every single time. The amount is still shown — you verify rather than trust
 * — but as part of the stored row, not as a comparison.
 */

export interface Evidence {
  /** Why the matcher paired them, in the matcher's own terms. */
  headline: string;
  /** The stored transaction, as one readable line. */
  stored: string;
  /** Where the stored row came from. Absent if unknown. */
  provenance: string | null;
  /** The incoming wording, only when the row itself does not already show it. */
  yourFile: string | null;
  /** What else sits nearby, and the rows themselves. */
  nearby: { summary: string; rows: MatchNeighbour[]; more: number } | null;
}

/** The stored row's own line: date, amount, description, and its real category. */
function storedLine(row: PreviewRow): string {
  const parts = [
    row.matched_date ? formatDate(row.matched_date) : null,
    row.matched_amount_cents != null ? formatCents(row.matched_amount_cents) : null,
    row.matched_description || null,
    row.matched_category_name || null,
  ].filter(Boolean);
  return parts.join(", ");
}

/**
 * How the two wordings relate, as a sentence — or nothing when saying so would add
 * no information.
 *
 * "digits" never claims the difference is harmless. Two descriptions that normalise
 * equal because a long number was stripped may be transfers to different payees, so
 * the sentence points at the numbers rather than excusing them.
 */
function wordingLine(row: PreviewRow): string | null {
  switch (row.matched_wording) {
    case "same":
      return "Worded identically";
    case "noise":
      return "Same wording apart from a reference number";
    case "digits":
      return "Same wording, but the numbers in it differ — check they are the same payee";
    default:
      return null;
  }
}

function headlineFor(row: PreviewRow): string {
  const when = row.matched_days_delta != null ? relativeDays(row.matched_days_delta) : null;
  switch (row.match_tier) {
    case "same_merchant":
      // The tier's whole premise is that the wording differs, so say that and let the
      // two descriptions below carry the argument.
      return "Same merchant and amount, worded differently";
    case "near_match":
      return when && when !== "same day"
        ? `Same amount, ${when}, similar wording`
        : "Same amount and day, similar wording";
    case "provider_id":
      return "Your bank gave this the same transaction id";
    case "exact_hash":
      return "Same date, amount and description";
    default:
      return row.duplicate_reason ?? "Possible duplicate";
  }
}

/**
 * What else the account holds near this row.
 *
 * The copy says "at this amount or merchant" and never "nothing else nearby",
 * because that is all that was searched: dedup.py reaches these rows through its
 * amount and merchant indexes, so a quiet month of unrelated spending is invisible
 * to it. Claiming otherwise would be the one sentence here that is false.
 */
function nearbyFor(row: PreviewRow): Evidence["nearby"] {
  const total = row.matched_neighbours_total;
  if (total <= 0) {
    return {
      summary: "Nothing else at this amount or merchant within a fortnight",
      rows: [],
      more: 0,
    };
  }
  const noun = total === 1 ? "one other row" : `${total} other rows`;
  return {
    summary: `You already have ${noun} at this amount or merchant within a fortnight`,
    rows: row.matched_neighbours,
    more: Math.max(0, total - row.matched_neighbours.length),
  };
}

/**
 * The evidence for one preview row, or null when there is nothing to show.
 *
 * Null for a row that matched nothing, and null when `matched_txn_id` is absent —
 * which includes an older cached response that predates these fields, so the
 * optional-chaining is load-bearing rather than defensive habit.
 */
export function matchEvidenceOf(row: PreviewRow): Evidence | null {
  if (!row.matched_txn_id) return null;

  const stored = storedLine(row);
  if (!stored) return null;

  const wording = wordingLine(row);
  // Shown only when the row's own description is not already the incoming wording.
  // Both render paths print `merchant ?? raw_description`, so on the merchant tier
  // the one discriminating string is otherwise invisible — and when no merchant was
  // substituted, printing it here would just repeat the row above it.
  const substituted = Boolean(row.merchant) && row.merchant !== row.raw_description;

  return {
    headline: [headlineFor(row), wording].filter(Boolean).join(" · "),
    stored,
    provenance: provenanceFor(row),
    yourFile: substituted ? row.raw_description : null,
    // Only rows that ask for a decision are given a neighbourhood by the server.
    nearby: row.status === "duplicate_probable" ? nearbyFor(row) : null,
  };
}

/**
 * Where the stored row came from.
 *
 * A hand-entered match is the case that most deserves a second look — the household
 * typed it deliberately, and the app could not previously say so at all.
 */
function provenanceFor(row: PreviewRow): string | null {
  if (row.matched_source === "manual") {
    return row.matched_imported_at
      ? `You entered this one by hand on ${formatDate(dayOf(row.matched_imported_at))} — it did not come from a statement`
      : "You entered this one by hand — it did not come from a statement";
  }
  if (!row.matched_imported_from) return null;
  return row.matched_imported_at
    ? `Imported ${formatDate(dayOf(row.matched_imported_at))} from ${row.matched_imported_from}`
    : `Imported from ${row.matched_imported_from}`;
}

/**
 * The date part of a timestamp.
 *
 * `matched_imported_at` is a datetime while `matched_date` is a plain date, and
 * `formatDate` appends its own `T00:00:00` — so handing it a datetime produces
 * "2026-06-20T03:00:00T00:00:00" and an Invalid Date. Only ever the day is wanted
 * here anyway: when a row was imported matters, the minute does not.
 */
function dayOf(timestamp: string): string {
  return timestamp.slice(0, 10);
}

/** One neighbour as a line. Flags say why it is here, so the copy stays honest. */
export function neighbourLine(n: MatchNeighbour): string {
  const why = n.same_amount && n.same_merchant
    ? "same amount and merchant"
    : n.same_amount
      ? "same amount"
      : "same merchant";
  return `${formatDate(n.txn_date)}, ${formatCents(n.amount_cents)}, ${n.description} — ${why}`;
}
