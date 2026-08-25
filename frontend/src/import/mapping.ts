import type { CsvMapping } from "../api/types";

/**
 * What a column in a statement is for.
 *
 * The wizard used to ask the other way round — "which column is the date?" — which
 * cannot describe a file it does not already understand: columns nobody asked about
 * are invisible, so you cannot tell whether one that was skipped mattered. Asking
 * what each column is instead accounts for every one of them, and an unfamiliar
 * header stops being a problem because you read the values rather than the name.
 */
export type Role =
  | "ignore"
  | "date"
  | "description"
  | "amount"
  | "money_in"
  | "money_out"
  | "account"
  | "balance";

export const ROLE_LABELS: Record<Role, string> = {
  ignore: "Ignore",
  date: "Date",
  description: "Description",
  amount: "Amount (signed)",
  money_in: "Money in",
  money_out: "Money out",
  account: "Account",
  balance: "Balance",
};

/** Offered in this order: the ones every file needs, then the optional ones. */
export const ROLES: Role[] = [
  "ignore",
  "date",
  "description",
  "amount",
  "money_out",
  "money_in",
  "account",
  "balance",
];

/** Every role except Ignore names exactly one column. */
const SINGLE_USE: Role[] = ROLES.filter((r) => r !== "ignore");

export type Roles = Record<number, Role>;

/** The roles implied by a mapping, so the table opens on what was detected. */
export function rolesFromMapping(mapping: CsvMapping, columnCount: number): Roles {
  const roles: Roles = {};
  // Anything not named below is ignored, and shown as such rather than left blank:
  // an absent state and a chosen one look alike, and only one of them is a decision.
  for (let i = 0; i < columnCount; i += 1) roles[i] = "ignore";

  const set = (col: number | null, role: Role) => {
    if (col !== null && col >= 0 && col < columnCount) roles[col] = role;
  };
  set(mapping.date_col, "date");
  set(mapping.description_col, "description");
  set(mapping.account_col, "account");
  set(mapping.balance_col, "balance");
  if (mapping.amount_mode === "debit_credit") {
    set(mapping.debit_col, "money_out");
    set(mapping.credit_col, "money_in");
  } else {
    set(mapping.amount_col, "amount");
  }
  return roles;
}

const columnFor = (roles: Roles, role: Role): number | null => {
  const hit = Object.entries(roles).find(([, r]) => r === role);
  return hit ? Number(hit[0]) : null;
};

/** A mapping the API understands, built from the roles on screen. */
export function mappingFromRoles(roles: Roles, base: CsvMapping): CsvMapping {
  const amount = columnFor(roles, "amount");
  const debit = columnFor(roles, "money_out");
  const credit = columnFor(roles, "money_in");
  // Separate in/out columns win when present: a file offering both those and a signed
  // column describes the same money twice, and the pair is the more explicit of the
  // two. `whatsMissing` refuses that combination anyway; this keeps the derived
  // mapping coherent while the user is still mid-edit.
  const split = debit !== null || credit !== null;
  return {
    ...base,
    date_col: columnFor(roles, "date") ?? 0,
    description_col: columnFor(roles, "description") ?? 0,
    account_col: columnFor(roles, "account"),
    balance_col: columnFor(roles, "balance"),
    amount_mode: split ? "debit_credit" : "single",
    amount_col: split ? null : amount,
    debit_col: split ? debit : null,
    credit_col: split ? credit : null,
  };
}

/**
 * What is stopping this file from being read, in one sentence, or null when nothing
 * is. Named rather than left to a disabled button: "why can't I continue" is the
 * question a disabled control never answers.
 */
export function whatsMissing(roles: Roles): string | null {
  const used = Object.values(roles);
  const count = (role: Role) => used.filter((r) => r === role).length;

  for (const role of SINGLE_USE) {
    if (count(role) > 1) {
      return `Two columns are both marked ${ROLE_LABELS[role]} — only one can be.`;
    }
  }
  if (!count("date")) return "Tell me which column holds the date.";
  if (!count("description")) return "Tell me which column describes each transaction.";
  if (count("amount") && (count("money_in") || count("money_out"))) {
    return "Use either a single signed Amount, or separate Money in and Money out — not both.";
  }
  if (!count("amount") && !count("money_in") && !count("money_out")) {
    return "Tell me which column holds the amount.";
  }
  return null;
}
