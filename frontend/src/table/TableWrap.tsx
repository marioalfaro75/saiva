import type { CSSProperties, ReactNode } from "react";

/**
 * Lets a wide table scroll sideways inside its card instead of squashing to
 * illegibility or pushing the whole page sideways.
 *
 * `min` is per table, measured from its own columns — a single blanket value would
 * leave an eight-column table both squashed *and* scrolling, which is two costs and
 * no benefit.
 *
 * The wrapper goes around the `<table>` only, never the card: the bulk-action bar is
 * `position: sticky; bottom: 0` and sits inside the card as a sibling of the table,
 * so wrapping the card would make it stick to a scroll container instead of the
 * viewport.
 *
 * A scrollable region has to be reachable by keyboard, hence `tabIndex` and a
 * required label (WCAG 2.1.1).
 */
export function TableWrap({
  min,
  label,
  children,
}: {
  min: number;
  label: string;
  children: ReactNode;
}) {
  return (
    <div
      className="table-wrap"
      style={{ "--tw-min": `${min}px` } as CSSProperties}
      tabIndex={0}
      role="region"
      aria-label={label}
    >
      {children}
    </div>
  );
}

/**
 * Minimum widths, measured per table from realistic column content. Anything below
 * these squashes; anything above scrolls needlessly.
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
