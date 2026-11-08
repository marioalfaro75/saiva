import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { UpdateRun, UpdateStatus } from "../api/types";
import { SPA_VERSION } from "../version";

/**
 * Software updates.
 *
 * This panel used to lie, and it is worth saying how, because the shape of the fix
 * follows from it. Pressing "Update now" started a four-minute poll and then called
 * `setPhase("done")` unconditionally, rendering "Update applied." whether the version
 * had changed or the poll had simply run out. Separately, "You're on the latest
 * version ✅" appeared whenever the update check was enabled and no newer release was
 * found — including when the request to GitHub had failed outright, so a network
 * failure read as reassurance.
 *
 * Neither was fixable here alone. The api container was the thing being replaced, so it
 * could never report on its own update. Now a root-owned agent on the host does the work
 * and writes a record to a file; this panel only ever READS that record. Two rules
 * follow, and every branch below obeys them:
 *
 *   1. Nothing is asserted that is not in the record. Every field is nullable, and an
 *      absent one renders as a specific unknown, never as a default that happens to look
 *      like good news.
 *   2. A tick requires three conditions to be true at once — the check ran, it succeeded,
 *      and the running version was comparable — not the absence of bad news.
 */

const POLL_WHILE_RUNNING_MS = 2000;

type Tone = "good" | "warn" | "bad" | "info";

function phaseSummary(run: UpdateRun): { tone: Tone; title: string; detail: string } | null {
  if (!run.phase) return null;
  const from = run.from_version ?? "the previous version";
  const to = run.to_version ?? "the new version";
  switch (run.phase) {
    case "running":
      return {
        tone: "info",
        title: `Updating… (${run.step ?? "starting"})`,
        detail: run.message ?? "The host is applying the release.",
      };
    case "applied":
      return {
        tone: "good",
        title: `Updated to ${to}`,
        detail: run.message ?? "The site answered after the update.",
      };
    case "rolled_back":
      return {
        tone: "warn",
        title: `Update failed — rolled back to ${from}`,
        detail: run.message ?? "The box was put back the way it was and the site answered again.",
      };
    case "rolled_back_code_only":
      // The one case no design can automate away: the migration succeeded, so the
      // database is at the new schema while the code is the old one. Saying "rolled
      // back" here would be the same class of lie this panel was rewritten to remove.
      return {
        tone: "bad",
        title: `Rolled the app back, but not the database`,
        detail:
          run.message ??
          `This box is running ${from} against the ${to} database schema. Roll forward, or ` +
            `restore the pre-migration dump on the host with ./scripts/restore-backup.sh ` +
            `(which destroys everything written since the update).`,
      };
    case "rollback_incomplete":
      // The site answers, but on images the updater did not choose: nothing recorded
      // which digest was running, and a rolling channel now points at the release that
      // failed. Calling this "rolled back" would be the same class of claim this panel
      // was rewritten to remove.
      return {
        tone: "bad",
        title: "Put the configuration back, but not the app images",
        detail:
          run.message ??
          `Nothing recorded which images were running, so this box may still be on the ` +
            `release that failed. Pin SAIVA_VERSION in .env to a known-good tag and ` +
            `update again.`,
      };
    case "rollback_failed":
      return {
        tone: "bad",
        title: "Update failed, and so did the rollback",
        detail:
          run.message ??
          "The site is not answering. This needs a terminal on the host: check " +
            "`docker compose -f docker-compose.prod.yml logs --tail 60`.",
      };
    case "failed":
      return {
        tone: "bad",
        title: "Update failed",
        detail: run.message ?? "Nothing was applied.",
      };
    case "unreadable":
      return {
        tone: "warn",
        title: "The updater's record could not be read",
        detail: "Run `./scripts/deploy.sh --status` on the host to see what happened.",
      };
    default:
      // An unrecognised phase is shown as itself. A panel that quietly ignores a state
      // it does not know is how the old one came to report success for everything.
      return {
        tone: "warn",
        title: `Updater reported an unrecognised state: ${run.phase}`,
        detail: run.message ?? "Run `./scripts/deploy.sh --status` on the host.",
      };
  }
}

function versionLine(data: UpdateStatus | undefined): string {
  if (!data) return "…";
  const build = data.current_build ? ` (build ${data.current_build})` : "";
  return `${data.current_version}${build}`;
}

export function UpdatesPanel() {
  const qc = useQueryClient();
  const [requested, setRequested] = useState(false);

  const check = useQuery({ queryKey: ["update-check"], queryFn: () => api.updateCheck() });
  const run = useQuery({
    queryKey: ["update-run"],
    queryFn: () => api.updateRun(),
    // Poll only while something is happening. `refetchInterval` returning false stops
    // it; an error keeps it going, because an api container being recreated mid-update
    // is exactly when the next poll matters most.
    refetchInterval: (query) => {
      const phase = query.state.data?.phase;
      if (requested && !phase) return POLL_WHILE_RUNNING_MS;
      return phase === "running" ? POLL_WHILE_RUNNING_MS : false;
    },
    retry: false,
  });

  const recheck = useMutation({
    mutationFn: () => api.updateCheck(true),
    onSuccess: (d) => qc.setQueryData(["update-check"], d),
  });

  const startUpdate = useMutation({
    mutationFn: () => api.runUpdate(),
    onSuccess: () => {
      setRequested(true);
      void qc.invalidateQueries({ queryKey: ["update-run"] });
    },
  });

  const data = check.data;
  const summary = run.data ? phaseSummary(run.data) : null;
  const busy = summary?.tone === "info";

  // Three conditions, all of which must hold. The old version showed a tick for the
  // absence of bad news, which includes "the check failed" and "this build has no
  // version to compare".
  const confirmedUpToDate =
    !!data && data.check_enabled && !data.check_failed && data.comparable && !data.update_available;

  return (
    <div className="card">
      <h2>Software updates</h2>

      <p className="muted">
        Current version: <strong>{versionLine(data)}</strong>
        {data?.latest_version ? <> · latest release: {data.latest_version}</> : null}
        {SPA_VERSION && SPA_VERSION !== data?.current_version ? (
          <> · this page: {SPA_VERSION}</>
        ) : null}
      </p>

      {summary && (
        <div className={`notice notice-${summary.tone}`}>
          <strong>{summary.title}</strong>
          <div>{summary.detail}</div>
          {run.data?.started_at && (
            <div className="muted" style={{ marginTop: 4, fontSize: "0.9em" }}>
              started {run.data.started_at}
              {run.data.finished_at ? ` · finished ${run.data.finished_at}` : null}
              {run.data.trigger ? ` · ${run.data.trigger}` : null}
            </div>
          )}
          {summary.tone === "good" && (
            <button
              className="btn btn-primary"
              style={{ marginTop: 8 }}
              onClick={() => window.location.reload()}
            >
              Reload to use it
            </button>
          )}
        </div>
      )}

      {data && !data.check_enabled && (
        <p className="muted">Update checks are turned off on this deployment.</p>
      )}

      {data?.check_failed && (
        <p className="muted">
          Couldn&rsquo;t reach GitHub to check for updates, so this page cannot say whether one
          exists. It is not saying you are up to date.
        </p>
      )}

      {data && data.check_enabled && !data.check_failed && !data.comparable && (
        <p className="muted">
          This box runs the <code>{data.current_version}</code> channel, which has no release
          number to compare against a published release. Update checks can&rsquo;t tell you
          anything here; switch <code>SAIVA_VERSION</code> to <code>latest</code> in{" "}
          <code>.env</code> to follow releases.
        </p>
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
      ) : confirmedUpToDate ? (
        <p className="muted">You&rsquo;re on the latest release.</p>
      ) : null}

      <div className="toolbar" style={{ marginTop: 12 }}>
        <button className="btn" onClick={() => recheck.mutate()} disabled={recheck.isPending || busy}>
          {recheck.isPending ? "Checking…" : "Check for updates"}
        </button>
        {data?.update_available && data.apply_available && (
          <button
            className="btn btn-primary"
            onClick={() => startUpdate.mutate()}
            disabled={startUpdate.isPending || busy}
          >
            {startUpdate.isPending ? "Asking the host…" : "Update now"}
          </button>
        )}
      </div>

      {startUpdate.isError && (
        <p className="muted" style={{ marginTop: 8 }}>
          Couldn&rsquo;t start the update: {(startUpdate.error as Error).message}
        </p>
      )}

      {data?.update_available && !data.apply_available && (
        <p className="muted" style={{ marginTop: 8 }}>
          In-app updates aren&rsquo;t set up on this box. Run{" "}
          <code>sudo ./scripts/install-updater.sh</code> on the host once to enable the button, or
          update with <code>make update</code>.
        </p>
      )}

      {run.data?.log_tail && run.data.log_tail.length > 0 && summary?.tone !== "good" && (
        <details style={{ marginTop: 12 }}>
          <summary className="muted">What the updater logged</summary>
          <pre style={{ overflowX: "auto", fontSize: "0.85em" }}>
            {run.data.log_tail.join("\n")}
          </pre>
        </details>
      )}
    </div>
  );
}
