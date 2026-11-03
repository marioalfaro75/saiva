/**
 * Minimum widths, measured per table from realistic column content. Anything below
 * these squashes; anything above scrolls needlessly.
 *
 * Kept apart from TableWrap so that file exports nothing but its component: mixing
 * constants in defeats fast refresh for every page that renders a table.
 */
export const TABLE_MIN = {
  billsRecurring: 820,
  importPreview: 780,
  transactions: 700,
  rules: 650,
  accounts: 590,
  billsUpcoming: 560,
  benchmarks: 550,
  overviewBreakdown: 480,
  importAccounts: 470,
  netWorthItems: 440,
  txnGroups: 520,
} as const;
