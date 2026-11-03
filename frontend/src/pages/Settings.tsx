import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api } from "../api/client";
import type { AiSettings } from "../api/types";
import { UpdatesPanel } from "../components/UpdatesPanel";
import { PageHead } from "../components/PageHead";

interface Form {
  state: string;
  period_basis: string;
  fy_start_month: number;
  pay_cycle_anchor: string;
}

interface AiForm {
  provider: AiSettings["provider"];
  base_url: string;
  model: string;
  privacy_mode: AiSettings["privacy_mode"];
  /** Write-only: the server never sends a key back, so this is blank unless typed. */
  api_key: string;
}

export function Settings() {
  const fid = useId();
  const qc = useQueryClient();
  const household = useQuery({ queryKey: ["household"], queryFn: api.household });
  // Each of the three forms below is seeded from a query without being copied into
  // state: `null` means untouched, so the fetched values show through until someone
  // edits, and from then on the edit wins. Mirroring fetched data into state from an
  // effect cost a render each and, worse, let a refetch landing mid-edit overwrite
  // what was being typed — on this page a part-entered API key or pay-cycle date.
  // The latch is per form, not per field: a form is sent as one patch, so what gets
  // saved should be what was on screen when the edit started.
  const [editedForm, setForm] = useState<Form | null>(null);
  const form: Form = editedForm ?? {
    state: household.data?.state ?? "",
    period_basis: household.data?.period_basis ?? "calendar",
    fy_start_month: household.data?.fy_start_month ?? 7,
    pay_cycle_anchor: household.data?.pay_cycle_anchor ?? "",
  };

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
  // "" already means "nothing chosen", so the newest year is simply what that reads
  // as until one is picked — there is no second copy of it to keep.
  const [chosenFy, setFy] = useState<number | "">("");
  const fy = chosenFy === "" ? (reportYears.data?.[0]?.year ?? "") : chosenFy;

  const aiQuery = useQuery({ queryKey: ["ai-settings"], queryFn: api.aiSettings });
  const [editedAi, setAi] = useState<AiForm | null>(null);
  const ai: AiForm = editedAi ?? {
    provider: aiQuery.data?.provider ?? "none",
    base_url: aiQuery.data?.base_url ?? "",
    model: aiQuery.data?.model ?? "",
    privacy_mode: aiQuery.data?.privacy_mode ?? "aggregates",
    api_key: "",
  };
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
      // Untouched means the key box is already blank, by derivation.
      setAi((a) => (a ? { ...a, api_key: "" } : null));
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
      <PageHead title="Settings" />

      <div className="grid">
        <div className="card">
          <h2>Household &amp; period</h2>
          <form onSubmit={onSubmit}>
            <div className="row">
              <div className="field">
                <label htmlFor={`${fid}-state`}>State / territory</label>
                <select id={`${fid}-state`}
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
                <label htmlFor={`${fid}-period-basis`}>Budget period basis</label>
                <select id={`${fid}-period-basis`}
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
                <label htmlFor={`${fid}-fy-start`}>Financial year starts (month)</label>
                <input id={`${fid}-fy-start`}
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
                <label htmlFor={`${fid}-pay-anchor`}>Pay-cycle anchor date (a recent payday)</label>
                <input id={`${fid}-pay-anchor`}
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
              <label htmlFor={`${fid}-provider`}>Provider</label>
              <select id={`${fid}-provider`}
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
              <label htmlFor={`${fid}-privacy-mode`}>Privacy mode</label>
              <select id={`${fid}-privacy-mode`}
                className="pill-select"
                value={ai.privacy_mode}
                onChange={(e) =>
                  setAi({ ...ai, privacy_mode: e.target.value as AiSettings["privacy_mode"] })
                }
              >
                <option value="local_only">Local only — a model on your network</option>
                <option value="aggregates">Aggregates only — no raw transactions</option>
                <option value="full">Full detail — includes transactions</option>
              </select>
              {ai.privacy_mode === "local_only" && (
                <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                  Needs a Base URL pointing at a model on your own network — for example{" "}
                  <code>http://localhost:11434/v1</code> for Ollama. A cloud provider is
                  refused in this mode rather than used quietly.
                </p>
              )}
            </div>
          </div>
          {ai.provider !== "none" && (
            <>
              <div className="field" style={{ marginTop: 8 }}>
                <label htmlFor={`${fid}-api-key`}>
                  API key {aiQuery.data?.has_key ? "(set — leave blank to keep)" : ""}
                </label>
                <input id={`${fid}-api-key`}
                  type="password"
                  value={ai.api_key}
                  onChange={(e) => setAi({ ...ai, api_key: e.target.value })}
                  placeholder={aiQuery.data?.has_key ? "••••••••" : "Paste your API key"}
                />
              </div>
              {ai.provider === "openai" && (
                <div className="field">
                  <label htmlFor={`${fid}-base-url`}>Base URL</label>
                  <input id={`${fid}-base-url`}
                    value={ai.base_url}
                    onChange={(e) => setAi({ ...ai, base_url: e.target.value })}
                    placeholder="https://api.openai.com/v1 or http://ollama:11434/v1"
                  />
                </div>
              )}
              <div className="field">
                <label htmlFor={`${fid}-model`}>Model</label>
                <select id={`${fid}-model`}
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
