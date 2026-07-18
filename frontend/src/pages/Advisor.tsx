import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { ChatMessage } from "../api/types";

const PRIVACY_LABEL: Record<string, string> = {
  local_only: "Local only — nothing leaves your network",
  aggregates: "Aggregates only — category totals & summaries, no raw transactions",
  full: "Full detail — includes recent transactions",
};

export function Advisor() {
  const settings = useQuery({ queryKey: ["ai-settings"], queryFn: api.aiSettings });
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");

  const send = useMutation({
    mutationFn: (msgs: ChatMessage[]) => api.aiChat(msgs),
    onSuccess: (r) => setMessages((m) => [...m, { role: "assistant", content: r.reply }]),
  });

  const ask = () => {
    const text = draft.trim();
    if (!text || send.isPending) return;
    const next: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setDraft("");
    send.mutate(next);
  };

  const configured = settings.data?.configured ?? false;

  return (
    <div>
      <div className="page-head">
        <h1>Advisor</h1>
        {settings.data && (
          <span className="muted">{PRIVACY_LABEL[settings.data.privacy_mode]}</span>
        )}
      </div>

      <div className="notice">
        General information only — not personal financial advice. Answers are grounded in your
        Saiva data per the privacy mode above.
      </div>

      {!configured ? (
        <div className="card">
          <p className="muted">
            Connect an AI provider (Anthropic, OpenAI, Google Gemini, or a local Ollama endpoint) in{" "}
            <Link to="/settings">Settings</Link> to start asking questions about your finances.
          </p>
        </div>
      ) : (
        <div className="card">
          <div style={{ display: "grid", gap: 10, marginBottom: 12 }}>
            {messages.length === 0 && (
              <p className="muted">
                Ask things like “Where can we realistically save $200/month?” or “Why was last
                month more expensive?”
              </p>
            )}
            {messages.map((m, i) => (
              <div
                key={i}
                style={{
                  justifySelf: m.role === "user" ? "end" : "start",
                  maxWidth: "85%",
                  background: m.role === "user" ? "var(--surface-2)" : "transparent",
                  border: m.role === "user" ? "1px solid var(--border)" : "none",
                  borderRadius: 10,
                  padding: m.role === "user" ? "8px 12px" : "0 2px",
                  whiteSpace: "pre-wrap",
                }}
              >
                {m.content}
              </div>
            ))}
            {send.isPending && <span className="muted">Thinking…</span>}
            {send.isError && (
              <span className="negative">
                {send.error instanceof Error ? send.error.message : "Something went wrong"}
              </span>
            )}
          </div>
          <div className="toolbar" style={{ margin: 0 }}>
            <input
              placeholder="Ask about your spending, budgets, or forecast…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") ask();
              }}
              style={{ flex: 1, minWidth: 240 }}
            />
            <button className="btn btn-primary" disabled={send.isPending || !draft.trim()} onClick={ask}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
