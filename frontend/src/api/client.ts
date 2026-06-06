import type {
  Account,
  Benchmark,
  Budget,
  Category,
  CategoryBreakdown,
  ImportCommit,
  ImportPreview,
  Insights,
  Me,
  NetWorth,
  SavingsGoal,
  SetupBody,
  SniffResult,
  Summary,
  Transaction,
  TransactionList,
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

let csrfToken: string | null = null;

function setCsrf(token: string): void {
  csrfToken = token;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const isForm = options.body instanceof FormData;
  if (options.body && !isForm) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`/api${path}`, { ...options, headers, credentials: "same-origin" });
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

  insights: () => request<Insights>("/insights"),

  benchmarks: () => request<Benchmark>("/benchmarks"),

  transactions: (params: Record<string, string | number | boolean | undefined>) =>
    request<TransactionList>(`/transactions${qs(params)}`),
  recategorise: (id: string, categoryId: string | null, applyToSimilar: boolean, makeRule: boolean) =>
    request<Transaction>(`/transactions/${id}/recategorise`, {
      method: "POST",
      body: JSON.stringify({
        category_id: categoryId,
        apply_to_similar: applyToSimilar,
        make_rule: makeRule,
      }),
    }),
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
  preview: (file: File, accountId: string, fileFormat: string, mapping: unknown) => {
    const form = new FormData();
    form.append("file", file);
    form.append("account_id", accountId);
    form.append("file_format", fileFormat);
    if (mapping) form.append("mapping", JSON.stringify(mapping));
    return request<ImportPreview>("/imports/preview", { method: "POST", body: form });
  },
  commit: (file: File, accountId: string, fileFormat: string, mapping: unknown) => {
    const form = new FormData();
    form.append("file", file);
    form.append("account_id", accountId);
    form.append("file_format", fileFormat);
    if (mapping) form.append("mapping", JSON.stringify(mapping));
    return request<ImportCommit>("/imports/commit", { method: "POST", body: form });
  },

  summary: (p: PeriodParams) => request<Summary>(`/dashboard/summary${qs({ ...p })}`),
  breakdown: (p: PeriodParams) => request<CategoryBreakdown>(`/dashboard/categories${qs({ ...p })}`),
  trends: (p: PeriodParams) => request<Trend>(`/dashboard/trends${qs({ ...p })}`),

  budgets: () => request<Budget[]>("/budgets"),
  createBudget: (body: { category_id: string; period: string; limit_cents: number }) =>
    request<Budget>("/budgets", { method: "POST", body: JSON.stringify(body) }),
  updateBudget: (id: string, patch: { period?: string; limit_cents?: number }) =>
    request<Budget>(`/budgets/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteBudget: (id: string) => request<void>(`/budgets/${id}`, { method: "DELETE" }),

  netWorth: () => request<NetWorth>("/net-worth"),
  createNetWorthItem: (body: { name: string; kind: string; value_cents: number }) =>
    request<NetWorth>("/net-worth/items", { method: "POST", body: JSON.stringify(body) }),
  updateNetWorthItem: (id: string, patch: { name?: string; value_cents?: number }) =>
    request<NetWorth>(`/net-worth/items/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteNetWorthItem: (id: string) =>
    request<NetWorth>(`/net-worth/items/${id}`, { method: "DELETE" }),
  recordNetWorthSnapshot: () => request<NetWorth>("/net-worth/snapshot", { method: "POST" }),

  goals: () => request<SavingsGoal[]>("/goals"),
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
