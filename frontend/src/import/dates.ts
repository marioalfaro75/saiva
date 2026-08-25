const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const render = (d: number, m: number, y: number): string | null =>
  d >= 1 && d <= 31 && m >= 1 && m <= 12 ? `${d} ${MONTHS[m - 1]} ${y}` : null;

/**
 * How a date in the file reads under the chosen interpretation.
 *
 * Only the numeric day/month/year shapes, which are the ambiguous ones: `01/07/2025`
 * is a valid date whether the 1 or the 7 is the month, so nothing fails to parse and
 * a wrong choice files a year into the wrong months in silence. Anything else returns
 * null and is not previewed — the server reads those unambiguously.
 */
export function readDate(raw: string, dayfirst: boolean): string | null {
  const parts = raw.trim().split(/[/-]/);
  if (parts.length !== 3 || parts.some((p) => !/^\d{1,4}$/.test(p))) return null;
  const [a, b, c] = parts.map(Number);

  // A four-digit leading year is unambiguous, so the setting does not apply.
  if (parts[0].length === 4) return render(c, b, a);

  const day = dayfirst ? a : b;
  const month = dayfirst ? b : a;
  return render(day, month, c < 100 ? c + 2000 : c);
}

/** True when the two readings disagree — the only time the choice matters. */
export function isAmbiguous(raw: string): boolean {
  const day = readDate(raw, true);
  const month = readDate(raw, false);
  return day !== null && month !== null && day !== month;
}
