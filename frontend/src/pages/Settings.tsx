import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { AiSettings } from "../api/types";
import { UpdatesPanel } from "../components/UpdatesPanel";

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

  const reportYears = useQuery({ queryKey: ["report-years"], queryFn: api.reportYears });
  const [fy, setFy] = useState<number | "">("");
  useEffect(() => {
    if (reportYears.data && reportYears.data.length > 0 && fy === "") {
      setFy(reportYears.data[0].year);
    }
  }, [reportYears.data, fy]);

  const aiQuery = useQuery({ queryKey: ["ai-settings"], queryFn: api.aiSettings });
  const [ai, setAi] = useState<{
    provider: AiSettings["provider"];
    base_url: string;
    model: string;
    privacy_mode: AiSettings["privacy_mode"];
    api_key: string;
  }>({ provider: "none", base_url: "", model: "", privacy_mode: "aggregates", api_key: "" });
  useEffect(() => {
    if (aiQuery.data) {
      setAi({
        provider: aiQuery.data.provider,
        base_url: aiQuery.data.base_url ?? "",
        model: aiQuery.data.model ?? "",
        privacy_mode: aiQuery.data.privacy_mode,
        api_key: "",
      });
    }
  }, [aiQuery.data]);
  const saveAi = useMutation({
    mutationFn: () =>
      api.updateAiSettings({
        provider: ai.provider,
        base_url: ai.base_url || null,
        model: ai.model || null,
        privacy_mode: ai.privacy_mode,
        ...(ai.api_key ? { api_key: ai.api_key } : {}),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ai-settings"] });
      void qc.invalidateQueries({ queryKey: ["ai-models"] });
      setAi((a) => ({ ...a, api_key: "" }));
    },
  });

  const aiModels = useQuery({
    queryKey: ["ai-models", ai.provider],
    queryFn: () => api.aiModels(ai.provider),
    enabled: ai.provider !== "none",
    retry: false,
  });
  const [customModel, setCustomModel] = useState(false);
  const [aiMsg, setAiMsg] = useState<string | null>(null);
  const testAi = useMutation({
    mutationFn: api.aiTest,
    onSuccess: (r) => setAiMsg(r.message),
    onError: (e) => setAiMsg(e instanceof Error ? e.message : "Test failed"),
  });
  const testConnection = async () => {
    setAiMsg(null);
    try {
      await saveAi.mutateAsync();
    } catch {
      return; // save error is shown by the Save button state
    }
    testAi.mutate();
  };
  const modelList = aiModels.data ?? [];
  const modelIsCustom = customModel || (ai.model !== "" && !modelList.some((m) => m.id === ai.model));

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

        <div className="card">
          <h2>Financial-year report</h2>
          <p className="muted">
            A PDF for your accountant — totals, spend by category, month-by-month and top
            merchants for the chosen financial year.
          </p>
          <div className="toolbar" style={{ marginBottom: 0 }}>
            <select
              className="pill-select"
              value={fy}
              onChange={(e) => setFy(Number(e.target.value))}
            >
              {reportYears.data?.map((o) => (
                <option key={o.year} value={o.year}>
                  {o.label}
                </option>
              ))}
            </select>
            <a
              className="btn btn-primary"
              href={fy ? `/api/reports/fy?year=${fy}` : "/api/reports/fy"}
              target="_blank"
              rel="noreferrer"
            >
              Download PDF
            </a>
          </div>
        </div>

        <div className="card">
          <h2>AI advisor</h2>
          <p className="muted">
            Connect your own AI to ask questions about your finances on the Advisor page. Keys are
            stored encrypted; the privacy mode controls how much data is sent.
          </p>
          <div className="row">
            <div>
              <label>Provider</label>
              <select
                className="pill-select"
                value={ai.provider}
                onChange={(e) =>
                  setAi({ ...ai, provider: e.target.value as AiSettings["provider"] })
                }
              >
                <option value="none">Off</option>
                <option value="anthropic">Anthropic (Claude)</option>
                <option value="openai">OpenAI-compatible (OpenAI / Ollama)</option>
                <option value="gemini">Google (Gemini)</option>
              </select>
            </div>
            <div>
              <label>Privacy mode</label>
              <select
                className="pill-select"
                value={ai.privacy_mode}
                onChange={(e) =>
                  setAi({ ...ai, privacy_mode: e.target.value as AiSettings["privacy_mode"] })
                }
              >
                <option value="local_only">Local only</option>
                <option value="aggregates">Aggregates only</option>
                <option value="full">Full detail</option>
              </select>
            </div>
          </div>
          {ai.provider !== "none" && (
            <>
              <div className="field" style={{ marginTop: 8 }}>
                <label>API key {aiQuery.data?.has_key ? "(set — leave blank to keep)" : ""}</label>
                <input
                  type="password"
                  value={ai.api_key}
                  onChange={(e) => setAi({ ...ai, api_key: e.target.value })}
                  placeholder={aiQuery.data?.has_key ? "••••••••" : "Paste your API key"}
                />
              </div>
              {ai.provider === "openai" && (
                <div className="field">
                  <label>Base URL</label>
                  <input
                    value={ai.base_url}
                    onChange={(e) => setAi({ ...ai, base_url: e.target.value })}
                    placeholder="https://api.openai.com/v1 or http://ollama:11434/v1"
                  />
                </div>
              )}
              <div className="field">
                <label>Model</label>
                <select
                  className="pill-select"
                  value={modelIsCustom ? "__custom__" : ai.model}
                  onChange={(e) => {
                    if (e.target.value === "__custom__") setCustomModel(true);
                    else {
                      setCustomModel(false);
                      setAi({ ...ai, model: e.target.value });
                    }
                  }}
                >
                  <option value="">Default</option>
                  {modelList.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                  <option value="__custom__">Custom…</option>
                </select>
                {modelIsCustom && (
                  <input
                    style={{ marginTop: 6 }}
                    value={ai.model}
                    onChange={(e) => setAi({ ...ai, model: e.target.value })}
                    placeholder={
                      ai.provider === "anthropic"
                        ? "claude-haiku-4-5-20251001"
                        : ai.provider === "gemini"
                          ? "gemini-2.5-flash"
                          : "gpt-4o-mini"
                    }
                  />
                )}
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  {aiModels.isFetching
                    ? "Loading models…"
                    : aiModels.isError
                      ? "Couldn't load models — pick Custom… and type one."
                      : `${modelList.length} model${modelList.length === 1 ? "" : "s"} available${
                          aiQuery.data?.has_key ? "" : " (suggested — save your key to load more)"
                        }`}
                  <button
                    className="btn btn-ghost"
                    style={{ marginLeft: 8, padding: "1px 8px" }}
                    onClick={() => void aiModels.refetch()}
                  >
                    ↻ Refresh
                  </button>
                </div>
              </div>
            </>
          )}
          <div className="toolbar" style={{ marginTop: 12, marginBottom: 0 }}>
            <button className="btn btn-primary" disabled={saveAi.isPending} onClick={() => saveAi.mutate()}>
              Save AI settings
            </button>
            {ai.provider !== "none" && (
              <button
                className="btn"
                disabled={!aiQuery.data?.has_key || testAi.isPending || saveAi.isPending}
                onClick={() => void testConnection()}
              >
                {testAi.isPending ? "Testing…" : "Test connection"}
              </button>
            )}
            {saveAi.isSuccess && !aiMsg && <span className="muted">Saved.</span>}
            {aiMsg && <span className={testAi.isError ? "negative" : "muted"}>{aiMsg}</span>}
          </div>
        </div>

        <UpdatesPanel />
      </div>
    </div>
  );
}
