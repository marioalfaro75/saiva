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
  confidence: number | null;
  source: string;
  notes: string | null;
  tags: string[];
  split_parent_id: string | null;
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
