/** The app's destinations, kept out of the component file so fast refresh works. */

export interface NavItem {
  to: string;
  label: string;
  end?: boolean;
}

/**
 * The thirteen destinations, in four clusters.
 *
 * The clusters are separated by space rather than headings. Every item here is a
 * page with that exact title, so group names like "Money" or "Plan" would be the
 * only words in the app naming concepts the product does not otherwise have.
 * Proximity does the grouping without asking anyone to learn new vocabulary.
 */
export const CLUSTERS: NavItem[][] = [
  // What the app tells you, and what you ask it.
  [
    { to: "/", label: "Overview", end: true },
    { to: "/alerts", label: "Alerts" },
    { to: "/insights", label: "Insights" },
    { to: "/advisor", label: "Advisor" },
  ],
  // The ledger and how money gets into it.
  [
    { to: "/transactions", label: "Transactions" },
    { to: "/accounts", label: "Accounts" },
    { to: "/import", label: "Import" },
  ],
  // Forward commitments.
  [
    { to: "/budgets", label: "Budgets" },
    { to: "/bills", label: "Bills" },
    { to: "/goals", label: "Savings goals" },
    { to: "/forecast", label: "Forecast" },
  ],
  // Where the household stands.
  [
    { to: "/net-worth", label: "Net worth" },
    { to: "/benchmarks", label: "Benchmarks" },
  ],
];

export const SETTINGS: NavItem = { to: "/settings", label: "Settings" };
