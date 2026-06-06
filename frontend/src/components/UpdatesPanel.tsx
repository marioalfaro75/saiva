import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { SPA_VERSION } from "../version";

type Phase = "idle" | "updating" | "done";

export function UpdatesPanel() {
  const qc = useQueryClient();
  const check = useQuery({ queryKey: ["update-check"], queryFn: () => api.updateCheck() });
  const recheck = useMutation({
    mutationFn: () => api.updateCheck(true),
    onSuccess: (d) => qc.setQueryData(["update-check"], d),
  });
  const [phase, setPhase] = useState<Phase>("idle");

  const runUpdate = async () => {
    setPhase("updating");
    const before = check.data?.current_version ?? SPA_VERSION;
    try {
      await api.runUpdate();
    } catch {
      // The API container is recreated mid-update, so a dropped request is expected.
    }
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      try {
        const m = await api.meta();
        if (m.version && m.version !== before) break;
      } catch {
        // API still restarting — keep polling.
      }
    }
    setPhase("done");
  };

  const data = check.data;

  return (
    <div className="card">
      <h2>Software updates</h2>

      {phase === "done" ? (
        <>
          <div className="notice">Update applied. Reload to use the new version.</div>
          <button className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload now
          </button>
        </>
      ) : phase === "updating" ? (
        <p className="muted">Updating… the app will restart, this can take a minute.</p>
      ) : (
        <>
          <p className="muted">
            Current version: <strong>{data?.current_version ?? "…"}</strong>
            {data?.latest_version ? <> · latest: {data.latest_version}</> : null}
          </p>

          {data && !data.check_enabled && (
            <p className="muted">Update checks are disabled on this deployment.</p>
          )}

          {data?.update_available ? (
            <div className="notice">
              <strong>Update available: {data.latest_version}</strong>
              {data.published_at ? <> ({data.published_at.slice(0, 10)})</> : null}
              {data.release_url ? (
                <>
                  {" · "}
                  <a href={data.release_url} target="_blank" rel="noreferrer">
                    release notes
                  </a>
                </>
              ) : null}
            </div>
          ) : data?.check_enabled ? (
            <p className="muted">You're on the latest version. ✅</p>
          ) : null}

          <div className="toolbar" style={{ marginTop: 12 }}>
            <button className="btn" onClick={() => recheck.mutate()} disabled={recheck.isPending}>
              {recheck.isPending ? "Checking…" : "Check for updates"}
            </button>
            {data?.update_available && data.apply_available && (
              <button className="btn btn-primary" onClick={() => void runUpdate()}>
                Update now
              </button>
            )}
          </div>

          {data?.update_available && !data.apply_available && (
            <p className="muted" style={{ marginTop: 8 }}>
              In‑app update isn't enabled here — update with <code>make pull</code>.
            </p>
          )}
        </>
      )}
    </div>
  );
}
