export interface User {
  id: string;
  email: string;
  name: string;
  role: string;
  household_id: string;
}

export interface Household {
  id: string;
  name: string;
  country: string;
  currency: string;
  locale: string;
  state: string | null;
  timezone: string;
  fy_start_month: number;
  fy_start_day: number;
  period_basis: string;
  pay_cycle_anchor: string | null;
  adults: number;
  children: number;
  income_band: string | null;
}

export interface Me {
  user: User;
  household: Household;
  csrf_token: string;
}

export interface Account {
  id: string;
  name: string;
  type: string;
  institution: string | null;
  currency: string;
  opening_balance_cents: number;
  owner_user_id: string | null;
  balance_cents: number;
  txn_count: number;
}

export interface Category {
  id: string;
  name: string;
  parent_id: string | null;
  kind: string;
  icon: string | null;
  color: string | null;
  is_system: boolean;
  sort: number;
}

export interface Transaction {
  id: string;
  account_id: string;
  account_name: string | null;
  txn_date: string;
  amount_cents: number;
  raw_description: string;
  merchant: string | null;
  category_id: string | null;
  category_name: string | null;
  is_transfer: boolean;
  is_recurring: boolean;
  category_locked: boolean;
  confidence: number | null;
  source: string;
  notes: string | null;
  tags: string[];
  split_parent_id: string | null;
}

export type MatchType = "contains" | "starts_with" | "regex" | "merchant" | "equals";
export type RecategoriseScope = "none" | "merchant" | "exact" | "contains";

export interface RecategoriseResult {
  transaction: Transaction;
  updated_count: number;
}

export interface Rule {
  id: string;
  match_type: string;
  pattern: string;
  category_id: string;
  priority: number;
  source: string;
  is_active: boolean;
}

export interface RulePreview {
  matched: number;
  fillable: number;
  samples: string[];
}

export interface TxnGroup {
  key: string;
  sample_id: string;
  sample_description: string;
  count: number;
  total_cents: number;
}

export interface TxnGroups {
  by: string;
  groups: TxnGroup[];
}

export interface TransactionList {
  items: Transaction[];
  total: number;
  page: number;
  page_size: number;
}

export interface Summary {
  period_label: string;
  start: string;
  end: string;
  income_cents: number;
  expense_cents: number;
  net_cents: number;
  savings_rate: number;
  txn_count: number;
}

export interface CategoryBreakdownItem {
  category_id: string | null;
  category_name: string;
  parent_name: string | null;
  kind: string;
  amount_cents: number;
  pct: number;
}

export interface CategoryBreakdown {
  start: string;
  end: string;
  total_cents: number;
  items: CategoryBreakdownItem[];
}

export interface TrendPoint {
  period_start: string;
  income_cents: number;
  expense_cents: number;
  net_cents: number;
}

export interface Trend {
  interval: string;
  points: TrendPoint[];
}

export interface Budget {
  id: string;
  category_id: string;
  category_name: string;
  parent_name: string | null;
  period: string;
  period_label: string;
  period_start: string;
  period_end: string;
  limit_cents: number;
  actual_cents: number;
  remaining_cents: number;
  pct_used: number;
  projected_cents: number;
  status: string;
}

export interface NetWorthItem {
  id: string;
  name: string;
  kind: string;
  value_cents: number;
}

export interface NetWorthPoint {
  as_of: string;
  assets_cents: number;
  liabilities_cents: number;
  net_cents: number;
}

export interface NetWorth {
  assets_cents: number;
  liabilities_cents: number;
  net_cents: number;
  items: NetWorthItem[];
  history: NetWorthPoint[];
}

export interface BenchmarkItem {
  category: string;
  your_weekly_cents: number;
  typical_weekly_cents: number;
  diff_cents: number;
  pct_of_typical: number;
}

export interface Benchmark {
  basis: string;
  adults: number;
  children: number;
  note: string;
  your_total_weekly_cents: number;
  typical_total_weekly_cents: number;
  items: BenchmarkItem[];
}

export interface Insight {
  key: string;
  type: string;
  severity: string;
  title: string;
  body: string;
  action: string | null;
  amount_cents: number | null;
  link: string | null;
}

export interface Insights {
  generated_for: string;
  insights: Insight[];
}

export interface RecurringSeries {
  merchant: string;
  category_id: string | null;
  category_name: string | null;
  direction: "expense" | "income";
  cadence: string;
  interval_days: number;
  typical_amount_cents: number;
  monthly_amount_cents: number;
  occurrences: number;
  first_date: string;
  last_date: string;
  next_due: string;
  active: boolean;
  is_subscription: boolean;
}

export interface RecurringOut {
  series: RecurringSeries[];
  monthly_committed_cents: number;
  subscriptions_count: number;
  subscriptions_monthly_cents: number;
  income_monthly_cents: number;
}

export interface UpcomingBill {
  due_date: string;
  merchant: string;
  amount_cents: number;
  category_id: string | null;
  category_name: string | null;
  cadence: string;
}

export interface UpcomingBills {
  horizon_days: number;
  total_cents: number;
  bills: UpcomingBill[];
}

export interface SavingsGoal {
  id: string;
  name: string;
  target_cents: number;
  target_date: string | null;
  account_id: string | null;
  account_name: string | null;
  current_cents: number;
  remaining_cents: number;
  pct_complete: number;
  suggested_per_period_cents: number;
  period_label: string;
  complete: boolean;
}

export interface CsvMapping {
  has_header: boolean;
  date_col: number;
  description_col: number;
  amount_mode: "single" | "debit_credit";
  amount_col: number | null;
  debit_col: number | null;
  credit_col: number | null;
  balance_col: number | null;
  date_format: string | null;
  decimal: string;
  invert_amount: boolean;
  skip_rows: number;
}

export interface SniffResult {
  detected_format: string;
  has_header: boolean;
  columns: string[];
  sample_rows: string[][];
  suggested_mapping: CsvMapping | null;
}

export interface PreviewRow {
  txn_date: string;
  amount_cents: number;
  raw_description: string;
  merchant: string | null;
  suggested_category_id: string | null;
  suggested_category_name: string | null;
  confidence: number | null;
  is_duplicate: boolean;
}

export interface ImportPreview {
  account_id: string;
  file_format: string;
  total_rows: number;
  new_rows: PreviewRow[];
  duplicate_count: number;
}

export interface ImportCommit {
  batch_id: string;
  added: number;
  skipped: number;
  transfers_linked: number;
}

export interface SetupBody {
  household_name: string;
  name: string;
  email: string;
  password: string;
  state?: string | null;
  period_basis?: "calendar" | "weekly" | "fortnightly" | "monthly";
}

export interface UpdateStatus {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  apply_available: boolean;
  check_enabled: boolean;
  release_url: string | null;
  release_notes: string | null;
  published_at: string | null;
}
