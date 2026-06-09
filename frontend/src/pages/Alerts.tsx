import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Notification } from "../api/types";
import { dollarsToCents } from "../format";

const PILL: Record<string, string> = { alert: "over", warn: "warning", info: "info" };

function when(iso: string): string {
  const norm = iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`;
  return new Date(norm).toLocaleString("en-AU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Row({ note, onRead }: { note: Notification; onRead: (id: string) => void }) {
  const unread = note.read_at === null;
  return (
    <div className={`card insight insight-${note.severity}`} style={{ opacity: unread ? 1 : 0.6 }}>
      <div className="spread">
        <strong>{note.title}</strong>
        <span className={`status-pill ${PILL[note.severity] ?? "info"}`}>{note.severity}</span>
      </div>
      <p className="muted" style={{ margin: "8px 0 0" }}>
        {note.body}
      </p>
      <div className="spread" style={{ marginTop: 10 }}>
        <span className="muted" style={{ fontSize: 13 }}>
          {when(note.created_at)}
          {note.link && (
            <>
              {" · "}
              <Link to={note.link}>View</Link>
            </>
          )}
        </span>
        {unread && (
          <button className="btn btn-ghost" onClick={() => onRead(note.id)}>
            Mark read
          </button>
        )}
      </div>
    </div>
  );
}

export function Alerts() {
  const qc = useQueryClient();
  const notes = useQuery({ queryKey: ["notifications"], queryFn: api.notifications });
  const settings = useQuery({
    queryKey: ["notification-settings"],
    queryFn: api.notificationSettings,
  });

  const [largeTxn, setLargeTxn] = useState("");
  const [lowBal, setLowBal] = useState("");
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    if (settings.data) {
      setLargeTxn((settings.data.large_txn_threshold_cents / 100).toString());
      setLowBal((settings.data.low_balance_threshold_cents / 100).toString());
    }
  }, [settings.data]);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["notifications"] });
    void qc.invalidateQueries({ queryKey: ["notification-settings"] });
  };
  const save = useMutation({
    mutationFn: api.updateNotificationSettings,
    onSuccess: invalidate,
  });
  const read = useMutation({
    mutationFn: (id: string) => api.markNotificationRead(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const readAll = useMutation({
    mutationFn: api.markAllNotificationsRead,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const test = useMutation({
    mutationFn: api.sendTestEmail,
    onSuccess: (r) => setFlash(r.message),
    onError: (e) => setFlash(e instanceof Error ? e.message : "Could not send test email"),
  });

  const s = settings.data;
  const items = notes.data?.items ?? [];

  return (
    <div>
      <div className="page-head">
        <h1>Alerts</h1>
        {(notes.data?.unread ?? 0) > 0 && (
          <button className="btn" onClick={() => readAll.mutate()}>
            Mark all read
          </button>
        )}
      </div>

      {flash && <div className="notice">{flash}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Email & preferences</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {s?.smtp_configured
            ? "Email is configured. Alerts and digests are opt-in below."
            : "Email is off. Set SMTP_* in your .env to enable alert emails and digests."}
        </p>
        <div className="row">
          <label
            className="muted"
            style={{ display: "flex", alignItems: "center", gap: 8, margin: 0 }}
          >
            <input
              type="checkbox"
              style={{ width: "auto" }}
              disabled={!s?.smtp_configured || save.isPending}
              checked={s?.email_enabled ?? false}
              onChange={(e) => save.mutate({ email_enabled: e.target.checked })}
            />
            Email me alerts
          </label>
          <div>
            <label>Digest</label>
            <select
              className="pill-select"
              value={s?.digest ?? "off"}
              disabled={!s?.smtp_configured || save.isPending}
              onChange={(e) =>
                save.mutate({ digest: e.target.value as "off" | "weekly" | "monthly" })
              }
            >
              <option value="off">Off</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
            </select>
          </div>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <div>
            <label>Large transaction over</label>
            <input value={largeTxn} onChange={(e) => setLargeTxn(e.target.value)} />
          </div>
          <div>
            <label>Warn if projected balance below</label>
            <input value={lowBal} onChange={(e) => setLowBal(e.target.value)} />
          </div>
        </div>
        <div className="toolbar" style={{ marginTop: 12, marginBottom: 0 }}>
          <button
            className="btn btn-primary"
            disabled={save.isPending}
            onClick={() =>
              save.mutate({
                large_txn_threshold_cents: Math.abs(dollarsToCents(largeTxn)),
                low_balance_threshold_cents: dollarsToCents(lowBal),
              })
            }
          >
            Save thresholds
          </button>
          <button
            className="btn"
            disabled={!s?.smtp_configured || test.isPending}
            onClick={() => test.mutate()}
          >
            Send test email
          </button>
        </div>
      </div>

      {items.length > 0 ? (
        <div style={{ display: "grid", gap: 12 }}>
          {items.map((n) => (
            <Row key={n.id} note={n} onRead={(id) => read.mutate(id)} />
          ))}
        </div>
      ) : (
        <div className="card">
          <p className="muted">
            No alerts right now. Saiva flags over-budget categories, unusual spend, upcoming bills,
            large transactions, and a low projected balance here.
          </p>
        </div>
      )}
    </div>
  );
}
