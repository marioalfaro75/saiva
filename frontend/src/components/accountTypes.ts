/** The kinds of account Saiva knows; the type decides how transfers into it are counted. */
export const ACCOUNT_TYPES = [
  "everyday",
  "savings",
  "credit_card",
  "home_loan",
  "offset",
  "personal_loan",
  "cash",
  "investment",
];

export const accountTypeLabel = (type: string) => type.replace(/_/g, " ");

// The server's limits, checked as the person types rather than after a refusal.
const MIN_LETTERS = 4;
const MAX_LENGTH = 80;
const MAX_PHRASES = 30;

/** What is wrong with the wording, in a sentence -- or null when it can be saved. */
export function wordingProblem(phrases: string[]): string | null {
  if (phrases.length > MAX_PHRASES) return `At most ${MAX_PHRASES} lines.`;
  const long = phrases.find((p) => p.length > MAX_LENGTH);
  if (long) return `“${long.slice(0, 30)}…” is too long: at most ${MAX_LENGTH} characters a line.`;
  const short = phrases.find((p) => {
    const letters = p.toLowerCase().replace(/[^a-z]/g, "");
    return letters.length > 0 && letters.length < MIN_LETTERS;
  });
  if (short) {
    return `“${short}” is too short to recognise a payment by. Use more of your bank's wording, e.g. “By Authority To Cc”.`;
  }
  return null;
}
