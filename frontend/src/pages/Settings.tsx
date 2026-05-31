import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";

interface Form {
  state: string;
  period_basis: string;
  fy_start_month: number;
  pay_cycle_anchor: string;
}

export function Settings() {
  const qc = useQueryClient();
  const household = useQuery({ queryKey: ["household"], queryFn: api.household });
  const [form, setForm] = useState<Form>({
    state: "",
    period_basis: "calendar",
    fy_start_month: 7,
    pay_cycle_anchor: "",
  });

  useEffect(() => {
    if (household.data) {
      setForm({
        state: household.data.state ?? "",
        period_basis: household.data.period_basis,
        fy_start_month: household.data.fy_start_month,
        pay_cycle_anchor: household.data.pay_cycle_anchor ?? "",
      });
    }
  }, [household.data]);

  const save = useMutation({
    mutationFn: () =>
      api.updateHousehold({
        state: form.state || null,
        period_basis: form.period_basis,
        fy_start_month: Number(form.fy_start_month),
        pay_cycle_anchor: form.pay_cycle_anchor || null,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["household"] }),
  });

  const demo = useMutation({
    mutationFn: api.seedDemo,
    onSuccess: () => qc.invalidateQueries(),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  const showAnchor = form.period_basis === "weekly" || form.period_basis === "fortnightly";

  return (
    <div>
      <div className="page-head">
        <h1>Settings</h1>
      </div>

      <div className="grid">
        <div className="card">
          <h2>Household &amp; period</h2>
          <form onSubmit={onSubmit}>
            <div className="row">
              <div className="field">
                <label>State / territory</label>
                <select
                  value={form.state}
                  onChange={(e) => setForm({ ...form, state: e.target.value })}
                >
                  <option value="">—</option>
                  {["NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"].map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Budget period basis</label>
                <select
                  value={form.period_basis}
                  onChange={(e) => setForm({ ...form, period_basis: e.target.value })}
                >
                  <option value="calendar">Calendar months</option>
                  <option value="weekly">Weekly pay cycle</option>
                  <option value="fortnightly">Fortnightly pay cycle</option>
                  <option value="monthly">Monthly pay cycle</option>
                </select>
              </div>
              <div className="field">
                <label>Financial year starts (month)</label>
                <input
                  type="number"
                  min={1}
                  max={12}
                  value={form.fy_start_month}
                  onChange={(e) => setForm({ ...form, fy_start_month: Number(e.target.value) })}
                />
              </div>
            </div>
            {showAnchor && (
              <div className="field">
                <label>Pay-cycle anchor date (a recent payday)</label>
                <input
                  type="date"
                  value={form.pay_cycle_anchor}
                  onChange={(e) => setForm({ ...form, pay_cycle_anchor: e.target.value })}
                />
              </div>
            )}
            {save.isSuccess && <div className="notice">Saved.</div>}
            <button className="btn btn-primary" disabled={save.isPending}>
              Save settings
            </button>
          </form>
        </div>

        <div className="card">
          <h2>Data</h2>
          <p className="muted">
            Explore Saiva with realistic sample data, or export everything as open JSON.
          </p>
          <div className="toolbar">
            <button
              className="btn"
              onClick={() => demo.mutate()}
              disabled={demo.isPending}
            >
              {demo.isPending ? "Loading…" : "Load demo data"}
            </button>
            <a className="btn" href="/api/admin/export" target="_blank" rel="noreferrer">
              Export data (JSON)
            </a>
          </div>
          {demo.isSuccess && (
            <div className="notice">Added {demo.data?.transactions} demo transactions.</div>
          )}
          {demo.isError && <div className="error">Demo data is only for an empty household.</div>}
        </div>
      </div>
    </div>
  );
}
