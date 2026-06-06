import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Insight } from "../api/types";

const PILL_CLASS: Record<string, string> = { alert: "over", warn: "warning", info: "info" };
const PILL_LABEL: Record<string, string> = { alert: "Alert", warn: "Heads up", info: "FYI" };

function InsightCard({ insight }: { insight: Insight }) {
  return (
    <div className={`card insight insight-${insight.severity}`}>
      <div className="spread">
        <strong>{insight.title}</strong>
        <span className={`status-pill ${PILL_CLASS[insight.severity] ?? "info"}`}>
          {PILL_LABEL[insight.severity] ?? insight.severity}
        </span>
      </div>
      <p className="muted" style={{ margin: "8px 0 0" }}>
        {insight.body}
      </p>
      {insight.action && insight.link && (
        <div style={{ marginTop: 12 }}>
          <Link className="btn" to={insight.link}>
            {insight.action} →
          </Link>
        </div>
      )}
    </div>
  );
}

export function Insights() {
  const insights = useQuery({ queryKey: ["insights"], queryFn: api.insights });
  const data = insights.data;
  const list = data?.insights ?? [];

  return (
    <div>
      <div className="page-head">
        <h1>Insights</h1>
        {data && list.length > 0 && <span className="muted">Based on {data.generated_for}</span>}
      </div>

      {list.length > 0 ? (
        <div style={{ display: "grid", gap: 12 }}>
          {list.map((i) => (
            <InsightCard key={i.key} insight={i} />
          ))}
        </div>
      ) : (
        <div className="card">
          <p className="muted">
            No insights yet. Once a couple of months of transactions are in (or you load demo data
            from Settings), Saiva flags spending changes, budget alerts, fees, possible duplicate
            charges, and savings-goal nudges here.
          </p>
        </div>
      )}
    </div>
  );
}
