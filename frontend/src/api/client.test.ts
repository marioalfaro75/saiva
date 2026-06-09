import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

function mockFetch(body: unknown, status = 200) {
  const res = {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  };
  const fn = vi.fn((..._args: unknown[]) => Promise.resolve(res));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function lastCall(fn: ReturnType<typeof mockFetch>): [string, RequestInit] {
  return fn.mock.calls[0] as unknown as [string, RequestInit];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client wiring", () => {
  it("recategorise posts the scope/rule/lock body and returns the count", async () => {
    const fn = mockFetch({ transaction: {}, updated_count: 3 });
    const r = await api.recategorise("t1", {
      category_id: "c1",
      scope: "merchant",
      make_rule: true,
      lock: true,
    });
    expect(r.updated_count).toBe(3);
    const [url, opts] = lastCall(fn);
    expect(url).toBe("/api/transactions/t1/recategorise");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body as string)).toMatchObject({
      category_id: "c1",
      scope: "merchant",
      make_rule: true,
      lock: true,
    });
  });

  it("bulkCategorise opts in to changing the category", async () => {
    const fn = mockFetch({ updated: 1 });
    await api.bulkCategorise(["a"], "c1");
    expect(JSON.parse(lastCall(fn)[1].body as string)).toEqual({
      ids: ["a"],
      category_id: "c1",
      set_category: true,
    });
  });

  it("bulkLock leaves the category untouched", async () => {
    const fn = mockFetch({ updated: 2 });
    await api.bulkLock(["a", "b"], true);
    const [url, opts] = lastCall(fn);
    expect(url).toBe("/api/transactions/bulk-categorise");
    expect(JSON.parse(opts.body as string)).toEqual({
      ids: ["a", "b"],
      set_category: false,
      lock: true,
    });
  });

  it("transactionGroups builds the query string", async () => {
    const fn = mockFetch({ by: "merchant", groups: [] });
    await api.transactionGroups("merchant", true);
    expect(lastCall(fn)[0]).toBe("/api/transactions/groups?by=merchant&uncategorised=true");
  });

  it("previewRule posts to the preview endpoint", async () => {
    const fn = mockFetch({ matched: 2, fillable: 1, samples: ["x"] });
    const r = await api.previewRule({ match_type: "contains", pattern: "woolworths" });
    expect(r.matched).toBe(2);
    expect(lastCall(fn)[0]).toBe("/api/rules/preview");
  });

  it("recurring hits the recurring endpoint", async () => {
    const fn = mockFetch({
      series: [],
      monthly_committed_cents: 0,
      subscriptions_count: 0,
      subscriptions_monthly_cents: 0,
      income_monthly_cents: 0,
    });
    await api.recurring();
    expect(lastCall(fn)[0]).toBe("/api/recurring");
  });

  it("upcomingBills builds the days query", async () => {
    const fn = mockFetch({ horizon_days: 30, total_cents: 0, bills: [] });
    await api.upcomingBills(30);
    expect(lastCall(fn)[0]).toBe("/api/recurring/upcoming?days=30");
  });

  it("forecast posts days and adjustments", async () => {
    const fn = mockFetch({
      starting_balance_cents: 0,
      end_balance_cents: 0,
      low_balance_cents: 0,
      low_balance_date: "2026-01-01",
      horizon_days: 90,
      monthly_income_cents: 0,
      monthly_expense_cents: 0,
      points: [],
    });
    await api.forecast(90, [{ category_id: "c1", pct: -20 }]);
    const [url, opts] = lastCall(fn);
    expect(url).toBe("/api/forecast");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body as string)).toEqual({
      days: 90,
      adjustments: [{ category_id: "c1", pct: -20 }],
    });
  });

  it("notifications GET and settings PATCH", async () => {
    const list = mockFetch({ items: [], unread: 0 });
    await api.notifications();
    expect(lastCall(list)[0]).toBe("/api/notifications");

    const patch = mockFetch({
      email_enabled: true,
      digest: "weekly",
      large_txn_threshold_cents: 100000,
      low_balance_threshold_cents: 0,
      smtp_configured: false,
    });
    await api.updateNotificationSettings({ digest: "weekly" });
    const [url, opts] = lastCall(patch);
    expect(url).toBe("/api/notifications/settings");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body as string)).toEqual({ digest: "weekly" });
  });
});
