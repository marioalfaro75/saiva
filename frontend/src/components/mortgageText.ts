/** How the Mortgage page says dates and lengths of time. */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2041-03-14" as "Mar 2041". */
export function monthYear(iso: string): string {
  return `${MONTHS[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}`;
}

/** Whole months from a to b (negative when b is first). */
export function monthsBetween(a: string, b: string): number {
  if (b < a) return -monthsBetween(b, a);
  const [ay, am, ad] = a.split("-").map(Number);
  const [by, bm, bd] = b.split("-").map(Number);
  return (by - ay) * 12 + (bm - am) - (bd < ad ? 1 : 0);
}

/** 30 as "2 years 6 months"; 12 as "1 year". */
export function span(months: number): string {
  const m = Math.abs(months);
  const years = Math.floor(m / 12);
  const rest = m % 12;
  const parts = [];
  if (years) parts.push(`${years} ${years === 1 ? "year" : "years"}`);
  if (rest || !years) parts.push(`${rest} ${rest === 1 ? "month" : "months"}`);
  return parts.join(" ");
}
