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
