import type {
  Account,
  AccountAssignment,
  AccountScanRow,
  AiModel,
  AiSettings,
  Benchmark,
  Budget,
  Category,
  CategoryBreakdown,
  ChatMessage,
  Forecast,
  ForecastAdjustment,
  FYReportOption,
  ImportCommit,
  ImportPreview,
  Insights,
  MatchType,
  Me,
  NetWorth,
  Notification,
  NotificationList,
  NotificationSettings,
  PeriodOptions,
  RecategoriseResult,
  RecategoriseScope,
  RecurringOut,
  ResolvedPeriod,
  Rule,
  RulePreview,
  UpcomingBills,
  SavingsGoal,
  SetupBody,
  SniffResult,
  Summary,
  Transaction,
  TransactionList,
  TxnGroups,
  Trend,
  UpdateStatus,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

const CSRF_COOKIE = "saiva_csrf";

let csrfToken: string | null = null;

function setCsrf(token: string): void {
  csrfToken = token;
}

/**
 * The token to send with a write.
 *
 * The cookie is the authority — the server compares the header against it — so read
 * it fresh on every request rather than trusting a value cached at startup. Caching
 * it meant that anything which changed the cookie (another tab bootstrapping, a
 * re-login elsewhere) left this tab sending a stale token and failing every write
 * until it was reloaded. The cookie is deliberately not http-only so this can work.
 */
function currentCsrf(): string | null {
  const match =
    typeof document !== "undefined"
      ? new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]*)`).exec(document.cookie)
      : null;
  return match ? decodeURIComponent(match[1]) : csrfToken;
}

function isCsrfRejection(status: number, detail: string): boolean {
  return status === 403 && detail.toLowerCase().includes("csrf");
}

async function send(path: string, options: RequestInit): Promise<Response> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  const token = currentCsrf();
  if (method !== "GET" && method !== "HEAD" && token) {
    headers.set("X-CSRF-Token", token);
  }
  const isForm = options.body instanceof FormData;
  if (options.body && !isForm) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`/api${path}`, { ...options, headers, credentials: "same-origin" });
}

async function request<T>(path: string, options: RequestInit = {}, retry = true): Promise<T> {
  let res = await send(path, options);

  if (!res.ok && res.status === 403 && retry && path !== "/auth/csrf") {
    // No usable token — most likely the cookie was never set for this tab. Ask for
    // one and try once more, so a write does not fail for a recoverable reason.
    const peek = await res.clone().json().catch(() => null);
    const reason =
      typeof peek === "object" && peek !== null && "detail" in peek
        ? String((peek as { detail: unknown }).detail)
        : "";
    if (isCsrfRejection(403, reason)) {
      try {
        const refreshed = await request<{ csrf_token: string }>("/auth/csrf", {}, false);
        setCsrf(refreshed.csrf_token);
        res = await send(path, options);
      } catch {
        // Fall through and report the original rejection.
      }
    }
  }

  if (res.status === 204) {
    return undefined as T;
  }

  const isJson = res.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const detail =
      typeof data === "object" && data !== null && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

function saveMe(me: Me): Me {
  setCsrf(me.csrf_token);
  return me;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

export interface PeriodParams {
  period: string;
  start?: string;
  end?: string;
}

export const api = {
  async csrf(): Promise<void> {
    const r = await request<{ csrf_token: string }>("/auth/csrf");
    setCsrf(r.csrf_token);
  },
  status: () => request<{ initialised: boolean }>("/auth/status"),
  setup: (body: SetupBody) =>
    request<Me>("/auth/setup", { method: "POST", body: JSON.stringify(body) }).then(saveMe),
  login: (email: string, password: string) =>
    request<Me>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }).then(
      saveMe,
    ),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<Me>("/auth/me").then(saveMe),

  household: () => request<Me["household"]>("/household"),
  updateHousehold: (patch: Record<string, unknown>) =>
    request<Me["household"]>("/household", { method: "PATCH", body: JSON.stringify(patch) }),

  accounts: () => request<Account[]>("/accounts"),
  createAccount: (body: { name: string; type: string; institution?: string | null }) =>
    request<Account>("/accounts", { method: "POST", body: JSON.stringify(body) }),

  categories: () => request<Category[]>("/categories"),

  rules: () => request<Rule[]>("/rules"),
  createRule: (body: {
    match_type: MatchType;
    pattern: string;
    category_id: string;
    priority?: number;
  }) => request<Rule>("/rules", { method: "POST", body: JSON.stringify(body) }),
  updateRule: (
    id: string,
    patch: {
      match_type?: MatchType;
      pattern?: string;
      category_id?: string;
      priority?: number;
      is_active?: boolean;
    },
  ) => request<Rule>(`/rules/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteRule: (id: string) => request<void>(`/rules/${id}`, { method: "DELETE" }),
  applyRule: (id: string) =>
    request<{ updated: number }>(`/rules/${id}/apply`, { method: "POST" }),
  previewRule: (body: { match_type: MatchType; pattern: string }) =>
    request<RulePreview>("/rules/preview", { method: "POST", body: JSON.stringify(body) }),

  insights: (period?: string) => request<Insights>(`/insights${qs({ period })}`),

  recurring: (period?: string) => request<RecurringOut>(`/recurring${qs({ period })}`),
  upcomingBills: (days = 60, period?: string) =>
    request<UpcomingBills>(`/recurring/upcoming${qs({ days, period })}`),

  forecast: (days = 90, adjustments: ForecastAdjustment[] = [], period?: string) =>
    request<Forecast>(`/forecast${qs({ period })}`, {
      method: "POST",
      body: JSON.stringify({ days, adjustments }),
    }),

  notifications: () => request<NotificationList>("/notifications"),
  markNotificationRead: (id: string) =>
    request<Notification>(`/notifications/${id}/read`, { method: "POST" }),
  markAllNotificationsRead: () =>
    request<{ message: string }>("/notifications/read-all", { method: "POST" }),
  notificationSettings: () => request<NotificationSettings>("/notifications/settings"),
  updateNotificationSettings: (patch: Partial<Omit<NotificationSettings, "smtp_configured">>) =>
    request<NotificationSettings>("/notifications/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  sendTestEmail: () => request<{ message: string }>("/notifications/test", { method: "POST" }),

  reportYears: () => request<FYReportOption[]>("/reports/years"),

  periodOptions: () => request<PeriodOptions>("/periods/options"),
  resolvePeriod: (period: string) =>
    request<ResolvedPeriod>(`/periods/resolve${qs({ period })}`),

  aiSettings: () => request<AiSettings>("/ai/settings"),
  updateAiSettings: (patch: {
    provider?: AiSettings["provider"];
    base_url?: string | null;
    model?: string | null;
    privacy_mode?: AiSettings["privacy_mode"];
    api_key?: string;
  }) => request<AiSettings>("/ai/settings", { method: "PATCH", body: JSON.stringify(patch) }),
  aiChat: (messages: ChatMessage[], period?: string) =>
    request<{ reply: string }>(`/ai/chat${qs({ period })}`, {
      method: "POST",
      body: JSON.stringify({ messages }),
    }),
  aiModels: (provider?: string) =>
    request<AiModel[]>(`/ai/models${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`),
  aiTest: () => request<{ message: string }>("/ai/test", { method: "POST" }),

  benchmarks: (period?: string) => request<Benchmark>(`/benchmarks${qs({ period })}`),

  transactions: (params: Record<string, string | number | boolean | undefined>) =>
    request<TransactionList>(`/transactions${qs(params)}`),
  recategorise: (
    id: string,
    body: {
      category_id: string | null;
      scope?: RecategoriseScope;
      pattern?: string | null;
      make_rule?: boolean;
      lock?: boolean;
    },
  ) =>
    request<RecategoriseResult>(`/transactions/${id}/recategorise`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  bulkCategorise: (ids: string[], categoryId: string | null) =>
    request<{ updated: number }>("/transactions/bulk-categorise", {
      method: "POST",
      body: JSON.stringify({ ids, category_id: categoryId, set_category: true }),
    }),
  bulkLock: (ids: string[], locked: boolean) =>
    request<{ updated: number }>("/transactions/bulk-categorise", {
      method: "POST",
      body: JSON.stringify({ ids, set_category: false, lock: locked }),
    }),
  updateTransaction: (
    id: string,
    patch: { category_locked?: boolean; is_transfer?: boolean; notes?: string | null },
  ) =>
    request<Transaction>(`/transactions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  transactionGroups: (by: "merchant" | "description", uncategorised = true) =>
    request<TxnGroups>(`/transactions/groups${qs({ by, uncategorised })}`),
  createManual: (body: {
    account_id: string;
    txn_date: string;
    amount_cents: number;
    description: string;
    category_id?: string | null;
  }) => request<Transaction>("/transactions", { method: "POST", body: JSON.stringify(body) }),

  sniff: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<SniffResult>("/imports/sniff", { method: "POST", body: form });
  },
  scanAccounts: (file: File, mapping: unknown) => {
    const form = new FormData();
    form.append("file", file);
    form.append("mapping", JSON.stringify(mapping));
    return request<AccountScanRow[]>("/imports/accounts/scan", { method: "POST", body: form });
  },
  preview: (
    file: File,
    accountId: string,
    fileFormat: string,
    mapping: unknown,
    assignments?: AccountAssignment[],
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (accountId) form.append("account_id", accountId);
    form.append("file_format", fileFormat);
    if (mapping) form.append("mapping", JSON.stringify(mapping));
    if (assignments?.length) form.append("assignments", JSON.stringify(assignments));
    return request<ImportPreview>("/imports/preview", { method: "POST", body: form });
  },
  commit: (
    file: File,
    accountId: string,
    fileFormat: string,
    mapping: unknown,
    // Row indexes the reviewer overrode in the preview: probable duplicates to import
    // anyway, and otherwise-new rows to leave out.
    decisions?: { forceImport?: number[]; forceSkip?: number[] },
    assignments?: AccountAssignment[],
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (accountId) form.append("account_id", accountId);
    form.append("file_format", fileFormat);
    if (mapping) form.append("mapping", JSON.stringify(mapping));
    if (assignments?.length) form.append("assignments", JSON.stringify(assignments));
    if (decisions?.forceImport?.length)
      form.append("force_import", JSON.stringify(decisions.forceImport));
    if (decisions?.forceSkip?.length)
      form.append("force_skip", JSON.stringify(decisions.forceSkip));
    return request<ImportCommit>("/imports/commit", { method: "POST", body: form });
  },

  summary: (p: PeriodParams) => request<Summary>(`/dashboard/summary${qs({ ...p })}`),
  breakdown: (p: PeriodParams) => request<CategoryBreakdown>(`/dashboard/categories${qs({ ...p })}`),
  trends: (p: PeriodParams) => request<Trend>(`/dashboard/trends${qs({ ...p })}`),

  budgets: (period?: string) => request<Budget[]>(`/budgets${qs({ period })}`),
  createBudget: (body: { category_id: string; period: string; limit_cents: number }) =>
    request<Budget>("/budgets", { method: "POST", body: JSON.stringify(body) }),
  updateBudget: (id: string, patch: { period?: string; limit_cents?: number }) =>
    request<Budget>(`/budgets/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteBudget: (id: string) => request<void>(`/budgets/${id}`, { method: "DELETE" }),

  netWorth: (period?: string) => request<NetWorth>(`/net-worth${qs({ period })}`),
  createNetWorthItem: (body: { name: string; kind: string; value_cents: number }) =>
    request<NetWorth>("/net-worth/items", { method: "POST", body: JSON.stringify(body) }),
  updateNetWorthItem: (id: string, patch: { name?: string; value_cents?: number }) =>
    request<NetWorth>(`/net-worth/items/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteNetWorthItem: (id: string) =>
    request<NetWorth>(`/net-worth/items/${id}`, { method: "DELETE" }),
  recordNetWorthSnapshot: () => request<NetWorth>("/net-worth/snapshot", { method: "POST" }),

  goals: (period?: string) => request<SavingsGoal[]>(`/goals${qs({ period })}`),
  createGoal: (body: {
    name: string;
    target_cents: number;
    target_date?: string | null;
    account_id?: string | null;
    current_cents?: number;
  }) => request<SavingsGoal>("/goals", { method: "POST", body: JSON.stringify(body) }),
  updateGoal: (
    id: string,
    patch: {
      name?: string;
      target_cents?: number;
      target_date?: string | null;
      account_id?: string | null;
      current_cents?: number;
    },
  ) => request<SavingsGoal>(`/goals/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteGoal: (id: string) => request<void>(`/goals/${id}`, { method: "DELETE" }),

  seedDemo: () => request<{ message: string; transactions: number }>("/admin/seed-demo", {
    method: "POST",
  }),

  meta: () => request<{ version: string }>("/meta"),
  updateCheck: (force = false) =>
    request<UpdateStatus>(`/admin/update-check${force ? "?force=true" : ""}`),
  runUpdate: () => request<{ status: string }>("/admin/update", { method: "POST" }),
};
