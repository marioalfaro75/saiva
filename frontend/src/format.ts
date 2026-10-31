export function formatCents(cents: number, currency = "AUD"): string {
  return new Intl.NumberFormat("en-AU", { style: "currency", currency }).format(cents / 100);
}

export function formatDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return new Intl.DateTimeFormat("en-AU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(d);
}

export function formatPct(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}

/** Parse a user-entered dollar string into signed integer cents. */
export function dollarsToCents(value: string): number {
  const n = Number.parseFloat(value.replace(/[$,\s]/g, ""));
  return Number.isFinite(n) ? Math.round(n * 100) : 0;
}

/**
 * A signed day offset as words: "same day", "1 day earlier", "2 days later".
 *
 * The matcher reports its reason with abs(), which loses the direction — and the
 * direction is evidence. A stored row a day or two *earlier* is what a bank re-dating
 * a pending authorisation looks like; one *later* is more likely a genuine repeat.
 */
export function relativeDays(delta: number): string {
  if (delta === 0) return "same day";
  const n = Math.abs(delta);
  return `${n} ${n === 1 ? "day" : "days"} ${delta < 0 ? "earlier" : "later"}`;
}
