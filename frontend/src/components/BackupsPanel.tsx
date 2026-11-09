import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { BackupStatus } from "../api/types";

/**
 * Backups.
 *
 * This panel has one headline and it is a number: how old the newest VERIFIED backup is.
 * Everything else on the card is subordinate to it, including a run that has just
 * finished successfully, because "last night's run worked" and "this box has a backup"
 * are not the same claim — a box whose last successful run was six weeks ago has a
 * successful run and no backup.
 *
 * The age is derived from `last_verified_at` and never from `last_success_at`. Those two
 * fields exist separately because a dump that has not been restored is not evidence of
 * anything: the host research found that `psql --single-transaction -v ON_ERROR_STOP=1`
 * exits 0 on a dump truncated in the middle of a COPY, so "pg_dump finished and psql
 * accepted it" would have put a tick over a dump missing most of a household's ledger.
 * Falling back to `last_success_at` for the headline would quietly undo all of that, so
 * no branch below does it.
 *
 * It is UpdatesPanel's sibling and obeys the same two rules:
 *
 *   1. Nothing is asserted that is not in the record. Every field is nullable and each
 *      absent one renders as a specific unknown, never as a default that reads like good
 *      news — and an unrecognised state is rendered as itself rather than dropped.
 *   2. Every wait is bounded. A spinner that never resolves tells the owner as little as
 *      a false tick does, and leaves them unable to tell "still working" from "nobody
 *      came back".
 */

const POLL_WHILE_RUNNING_MS = 2000;
// How long to wait for the host to pick a pressed request up. Same number and same
// reasoning as UpdatesPanel: the agent is a systemd path unit, so if it fires at all it
// fires within a second, and two minutes is generous and finite.
const HOST_PICKUP_TIMEOUT_MS = 120_000;
// And how long a run may claim to be running. scripts/install-updater.sh writes
// `TimeoutStartSec=3600` into saiva-backup.service, so systemd itself kills a run at an
// hour; five minutes past that, a status file still saying `running` is one nobody came
// back to — a host restarted mid-dump, a power cut, an OOM kill. This is deliberately
// much longer than the update unit's bound (1800s there) because a dump followed by a
// real restore into a throwaway database is slow work, and calling a live verify dead
// would be its own wrong answer.
const STALE_RUNNING_MS = 65 * 60_000;
// How often the age on screen catches up with the clock. A minute: the headline is
// measured in hours, and a page left open overnight should not still be claiming the
// number it was opened with.
const AGE_TICK_MS = 60_000;

// Where the off-box destination is configured. In agent.conf rather than .env because
// root acts on it: a path or a command read out of the checkout and run as root would be
// a privilege escalation for anyone who can write the checkout.
const CONF = "/etc/saiva/agent.conf";

type Tone = "good" | "warn" | "bad" | "info";

interface Notice {
  tone: Tone;
  title: string;
  detail: string;
}

interface Age {
  /** Signed: negative means the timestamp is in the future. */
  signedMs: number;
  /** Clamped at zero, so nothing downstream can render a negative age. */
  ms: number;
  hours: number;
  future: boolean;
}

/**
 * The age of a host timestamp, measured against a clock read outside render.
 *
 * `future` is a state this really reaches rather than a theoretical one: an LXC container
 * takes its clock from the Proxmox host, so restoring a container snapshot, or a host
 * whose RTC has drifted, produces a `last_verified_at` later than now. The clamp keeps
 * "-4 hours ago" off the screen; the flag is what stops the clamped zero being rendered
 * as a fresh backup, which would be the worst of the three possible answers.
 */
function ageOf(iso: string | null, nowMs: number | null): Age | null {
  if (!iso || nowMs === null) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const signedMs = nowMs - at;
  const ms = Math.max(signedMs, 0);
  return { signedMs, ms, hours: ms / 3_600_000, future: signedMs < 0 };
}

/** A duration as a number and a unit, with no direction attached to it. */
function spanLabel(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(ms / 3_600_000);
  // Hours up to two days, because "36 hours" is the unit the staleness policy is written
  // in and "1.5 days" is not comparable to it at a glance.
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.floor(ms / 86_400_000);
  return `${days} day${days === 1 ? "" : "s"}`;
}

/** A size, or the host's raw value when it is not a number this can read. */
function sizeLabel(bytes: string): string {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return bytes;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

/**
 * The headline: how old the newest verified backup is.
 *
 * Its tone is the one judgement on this card, so it is gated on every fact that bears on
 * "is this household backed up" — the age, the verification, and whether a copy left the
 * box. A green headline over "the copy is on the same disk as the database" would be the
 * absence-of-bad-news tick that the update panel was rewritten to remove, one card along.
 */
function headline(data: BackupStatus, age: Age | null): Notice {
  if (age === null) {
    // No verified backup. The one thing this branch must not do is reach for
    // last_success_at and present its age instead; a dump nobody has restored is not a
    // backup, and the whole regime exists to keep those two sentences apart.
    if (data.last_verified_at) {
      return {
        tone: "warn",
        title: "The last verified backup has no readable date",
        detail:
          `The host recorded "${data.last_verified_at}", which this page cannot read as a ` +
          `time, so it cannot tell you how old your backup is. Run ` +
          `\`./scripts/backup.sh --status\` on the host for the raw record.`,
      };
    }
    if (data.last_success_at) {
      return {
        tone: "bad",
        title: "No backup has been verified on this box",
        detail:
          `A dump was taken (the last one at ${data.last_success_at}), but nothing on this ` +
          `box has been restored from it, so there is no evidence it is a backup. A dump ` +
          `can be complete, gzip cleanly and still restore into an empty database. Press ` +
          `"Back up now", or run \`./scripts/backup.sh --verify-only\` on the host, to turn ` +
          `the newest dump into something you can rely on.`,
      };
    }
    return {
      tone: "bad",
      title: "Nothing has been backed up yet",
      detail:
        `The regime is installed, but no run has finished on this box, so there is no ` +
        `backup of this household's data at all. Press "Back up now" to take and verify ` +
        `the first one rather than waiting for tonight's timer.`,
    };
  }

  if (age.future) {
    // Clamped, and the clamp is said out loud. The number offered is the skew, because
    // that one is true: the age is not knowable while the clock it would be measured
    // against is the thing in doubt.
    return {
      tone: "warn",
      title: "The last verified backup is timestamped in the future",
      detail:
        `The host recorded it at ${data.last_verified_at}, which is ` +
        `${spanLabel(-age.signedMs)} ahead of this browser, so the host clock may be wrong ` +
        `and this page cannot tell you how old your backup is. An LXC container takes its ` +
        `clock from the Proxmox host, and restoring a snapshot produces exactly this. ` +
        `Check \`timedatectl\` on the host.`,
    };
  }

  const label = `Last verified backup: ${spanLabel(age.ms)} ago`;
  if (age.hours > data.stale_after_hours) {
    return {
      tone: "bad",
      title: label,
      detail:
        `That is older than the ${data.stale_after_hours} hours this box expects ` +
        `(SAIVA_BACKUP_STALE_HOURS), so the nightly run has not been working for at least ` +
        `a day. Everything entered since is in the database and nowhere else. Check ` +
        `\`systemctl list-timers saiva-backup.timer\` and ` +
        `\`journalctl -u saiva-backup.service\` on the host.`,
    };
  }
  // The age is inside the window. Whether that is good news depends on two more facts,
  // because a tick here is the one sentence an owner will read as "backups are fine":
  // whether the LAST run's dump could be restored, and whether any copy left the box. A
  // green headline over either failure is the absence-of-bad-news tick that the update
  // panel one card along was rewritten to remove.
  if (data.verified === false) {
    return {
      tone: "warn",
      title: label,
      detail:
        `That one was restored to prove it works, so you are not without a backup — but ` +
        `the most recent run produced a dump that would NOT restore, so the regime is ` +
        `failing now and this number will stop moving. See below.`,
    };
  }
  if (!offsiteIsOffBox(data.offsite)) {
    return {
      tone: "warn",
      title: label,
      detail:
        `It was taken and restored to prove it works, so the dump itself is sound — but no ` +
        `copy of it is known to have left this machine. See the off-box note below; until ` +
        `that is settled, one failed disk loses the database and its backups together.`,
    };
  }
  return {
    tone: "good",
    title: label,
    detail:
      `It was taken and then restored into a throwaway database to prove it works, and a ` +
      `copy of it left this machine. ` +
      (data.verify_message ?? "The host did not say what the restore proved."),
  };
}

/**
 * Whether the recorded off-box state means the copy actually left the machine.
 *
 * `remote` and nothing else. This accepted `ok` as well while scripts/backup.sh wrote it
 * — which it did only because the final `status_set offsite "${worst:-ok}"` defaulted to
 * the most reassuring value in the union, so a branch that forgot to record what happened
 * said "copied off this box". That defect was fixed in the script, and the vocabulary is
 * now declared there as SAIVA_OFFSITE_STATES; carrying `ok` here any longer would mean
 * this panel treats as good news a value the host cannot produce, which is the same shape
 * of claim in the other direction.
 *
 * backend/tests/test_deployment_hardening.py keeps the declared set and this switch in
 * step, so an `ok` from some future host lands in the default branch and is rendered as
 * itself rather than as a tick.
 */
function offsiteIsOffBox(offsite: string | null): boolean {
  return offsite === "remote";
}

function offsiteNotice(data: BackupStatus): Notice | null {
  if (!data.offsite) return null;
  const where = data.offsite_target ? ` (${data.offsite_target})` : "";
  const said = data.offsite_message ? ` ${data.offsite_message}.` : "";
  switch (data.offsite) {
    case "remote":
      return {
        tone: "good",
        title: `A copy is off this box${where}`,
        detail:
          `The destination is a network filesystem, or a push command you configured ` +
          `accepted it, so losing this machine does not lose the backup.${said}`,
      };
    case "same-filesystem":
      // Distinct from same-host on purpose: this is the disk, not the machine.
      return {
        tone: "warn",
        title: `The copy is on the same filesystem as the database${where}`,
        detail:
          `It was copied, but the destination is the same filesystem the database lives ` +
          `on, so losing that disk loses the database and every backup of it in the same ` +
          `moment. This protects you from deleting the volume; it does not protect you ` +
          `from the disk. Point SAIVA_BACKUP_COPY_TO in ${CONF} at a network share or ` +
          `another machine.${said}`,
      };
    case "same-host":
      // Distinct from same-filesystem on purpose: this is the machine, not the disk.
      return {
        tone: "warn",
        title: `The copy is on a different filesystem, but on this same machine${where}`,
        detail:
          `That survives the Docker volume being deleted, a bad migration, and a single ` +
          `failing disk if the two are genuinely different disks — which this box cannot ` +
          `tell from the inside, because a btrfs subvolume, a ZFS dataset, an LVM thin ` +
          `volume and a Proxmox mount point all look like separate devices while sharing ` +
          `one pool. It does not survive losing the machine, so it is not yet a backup of ` +
          `this box.${said}`,
      };
    case "off":
      return {
        tone: "warn",
        title: "Nothing is copied off this box",
        detail:
          `The only copy of each verified dump is in the db_backups volume on this host, ` +
          `beside the database it protects, so one disk failure or one lost container ` +
          `takes both. Set SAIVA_BACKUP_COPY_TO in ${CONF} to a path on a network share, ` +
          `or SAIVA_BACKUP_COPY_CMD to a command that pushes it somewhere.`,
      };
    case "unknown":
      return {
        tone: "warn",
        title: `This box could not work out how far away the copy went${where}`,
        detail:
          `The copy was made, but nothing here could establish whether the destination is ` +
          `on this machine or another one, so this page will not claim either. Device ` +
          `numbers disagree with physical disks on btrfs, ZFS, LVM and Proxmox mount ` +
          `points, which is why it refuses to guess. Check by hand what ` +
          `SAIVA_BACKUP_COPY_TO in ${CONF} actually points at.${said}`,
      };
    case "failed":
      return {
        tone: "bad",
        title: `The off-box copy did not happen${where}`,
        detail:
          `The destination could not be written to, or the configured command exited ` +
          `non-zero, so no copy left this box on the last run. The verified dump itself ` +
          `is still here in the db_backups volume — this is about the copy, not the ` +
          `backup. Check that the destination is mounted and writable.${said}`,
      };
    default:
      // Rendered as itself. A value the host gained and this panel has not learned is a
      // thing the owner should see, not a thing to drop on the floor.
      return {
        tone: "warn",
        title: `The off-box copy reported an unrecognised state: ${data.offsite}`,
        detail:
          data.offsite_message ??
          `Run \`./scripts/backup.sh --self-check\` on the host to see what it means.`,
      };
  }
}

function phaseNotice(data: BackupStatus, runningMs: number | null): Notice | null {
  if (!data.phase) return null;
  if (data.phase === "running" && runningMs !== null) {
    return {
      tone: "bad",
      title: "A backup started and never finished",
      detail:
        `It began ${spanLabel(runningMs)} ago and nothing has been written since, so it is ` +
        `not running any more — systemd stops the unit after an hour, so the host was ` +
        `probably restarted or the dump was killed. The age above is still the number that ` +
        `matters. Check \`journalctl -u saiva-backup.service\` on the host.`,
    };
  }
  switch (data.phase) {
    case "running":
      return {
        tone: "info",
        title: `Backing up… (${data.step ?? "starting"})`,
        detail:
          data.message ??
          "The host is taking a dump and will restore it into a throwaway database to " +
            "check it before saying it worked.",
      };
    case "ok":
      // Deliberately not a tick. "The last run worked" is news about that run; whether
      // this household has a backup is the headline's question, and it is measured in
      // hours. A green notice here would read as an answer to the second question.
      return {
        tone: "info",
        title: "The last run finished cleanly",
        detail: data.message ?? "The host recorded a successful run but did not say more.",
      };
    case "failed":
      return {
        tone: "bad",
        title: "The last backup run failed",
        detail:
          data.message ??
          `The host recorded a failure without a message. Run \`./scripts/backup.sh\` on ` +
            `the host to see it happen.`,
      };
    case "skipped":
      // Information, not failure. A unit that reports failure every night because the
      // stack is deliberately off, or because a release held the lock, trains its owner
      // to ignore it — and then the one real failure is ignored too. The age above is
      // untouched by a skipped run and keeps counting, which is what does the shouting.
      return {
        tone: "info",
        title: "No backup was taken on the last run, on purpose",
        detail:
          data.message ??
          `Either a release held the deploy lock or the stack was down, so nothing was ` +
            `dumped. Nothing was lost and nothing was reset: the age above is still the ` +
            `age of your newest verified backup.`,
      };
    case "unreadable":
      return {
        tone: "warn",
        title: "The host's backup record could not be read",
        detail:
          `The file is there but this page could not parse it, so none of the fields below ` +
          `can be trusted. Run \`./scripts/backup.sh --status\` on the host for the raw ` +
          `record.`,
      };
    default:
      // An unrecognised phase is shown as itself, for the same reason as above.
      return {
        tone: "warn",
        title: `The backup run reported an unrecognised state: ${data.phase}`,
        detail: data.message ?? `Run \`./scripts/backup.sh --status\` on the host.`,
      };
  }
}

/** What is actually in the volume, as facts, with each absent one named as absent. */
function retainedLine(data: BackupStatus): string {
  const parts: string[] = [];
  parts.push(
    data.backup_count === null
      ? "the host has not said how many dumps it is keeping"
      : `${data.backup_count} nightly dump${data.backup_count === "1" ? "" : "s"} kept`,
  );
  if (data.kept) parts.push(`retention ${data.kept}`);
  if (data.oldest_at) parts.push(`oldest ${data.oldest_at}`);
  if (data.pruned && data.pruned !== "0") parts.push(`${data.pruned} pruned on the last run`);
  return parts.join(" · ");
}

export function BackupsPanel() {
  const qc = useQueryClient();
  // The run id that was on screen when "Back up now" was pressed. Until a DIFFERENT run
  // appears, the run showing is the previous one and its outcome is not news about the
  // press just made. Without this, pressing the button on a box that had backed up before
  // renders last night's result — including, in the worst case, last night's green
  // "finished cleanly" — as though it were the answer to the press.
  const [supersededRunId, setSupersededRunId] = useState<string | null | undefined>(undefined);
  const [pressedAt, setPressedAt] = useState<number | null>(null);
  const [gaveUpWaiting, setGaveUpWaiting] = useState(false);
  // Two clock-derived values, both held in state and written by timers rather than read
  // during render. The React Compiler lint forbids reading the clock while rendering and
  // is right to: a value that changes without a re-render is a value the screen stops
  // agreeing with. `runningMs` is non-null only once a `running` run is past its bound.
  const [runningMs, setRunningMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const status = useQuery({
    queryKey: ["backup-status"],
    queryFn: () => api.backupStatus(),
    // Poll only while something is happening, exactly as the update panel does. An error
    // keeps the poll alive on purpose: a request that has just been written is when the
    // next answer matters most.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (supersededRunId !== undefined && data?.run_id === supersededRunId) {
        const waited = pressedAt === null ? 0 : Date.now() - pressedAt;
        return waited < HOST_PICKUP_TIMEOUT_MS ? POLL_WHILE_RUNNING_MS : false;
      }
      if (data?.phase !== "running") return false;
      // A callback, not render: the clock is fair game here.
      const began = data.started_at ? Date.parse(data.started_at) : NaN;
      if (!Number.isNaN(began) && Date.now() - began > STALE_RUNNING_MS) return false;
      return POLL_WHILE_RUNNING_MS;
    },
    retry: false,
  });

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), AGE_TICK_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (pressedAt === null) return;
    setGaveUpWaiting(false);
    const timer = setTimeout(() => setGaveUpWaiting(true), HOST_PICKUP_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [pressedAt]);

  const startedAt = status.data?.started_at ?? null;
  const phase = status.data?.phase ?? null;
  useEffect(() => {
    if (phase !== "running") {
      setRunningMs(null);
      return;
    }
    const age = startedAt === null ? null : Date.now() - Date.parse(startedAt);
    if (age !== null && age > STALE_RUNNING_MS) {
      setRunningMs(age);
      return;
    }
    // A run that is live now becomes a run nobody came back to at a knowable moment, so
    // the bound is a timer rather than something to notice on the next render.
    const remaining = age === null ? STALE_RUNNING_MS : STALE_RUNNING_MS - age;
    const timer = setTimeout(() => setRunningMs(STALE_RUNNING_MS), Math.max(remaining, 0));
    return () => clearTimeout(timer);
  }, [phase, startedAt]);

  const startBackup = useMutation({
    mutationFn: () => api.runBackup(),
    onSuccess: () => {
      setSupersededRunId(status.data?.run_id ?? null);
      setPressedAt(Date.now());
      void qc.invalidateQueries({ queryKey: ["backup-status"] });
    },
  });

  const data = status.data;
  const superseded =
    supersededRunId !== undefined && (data?.run_id ?? null) === supersededRunId;
  // The record is only reportable when the last thing that happened was reading it
  // successfully. On a refetch that errors, react-query keeps the previous answer in
  // `data`, and rendering "Last verified backup: 9 hours ago" from it would be a
  // reassuring sentence standing on an answer we could not get — the same mistake as a
  // tick that survives a failed update check.
  const readable = !!data && !status.isError;
  const age = readable ? ageOf(data.last_verified_at, nowMs) : null;
  const pressSummary: Notice | null = !superseded
    ? null
    : gaveUpWaiting
      ? {
          tone: "bad",
          title: "The host has not picked this up",
          detail:
            "The request was written but nothing on the box acted on it within two " +
            "minutes. The backup agent is probably not installed or not enabled: run " +
            "`sudo ./scripts/install-updater.sh` on the host, or take one directly with " +
            "`sudo ./scripts/backup.sh`.",
        }
      : {
          tone: "info",
          title: "Asked the host to back up…",
          detail:
            "Waiting for the run to start. This page will say what happened, including " +
            "whether the dump could be restored — which is the part that decides whether " +
            "it is a backup at all.",
        };
  // Not derived from a tone: phase `ok` is info-toned on purpose (see phaseNotice), and
  // using the tone as the busy test would disable the button for good on a box whose last
  // run succeeded.
  const busy = superseded || (phase === "running" && runningMs === null);
  // The last run's own result, which a fresh press supersedes. The carried-forward facts
  // -- the age, and where the copy went -- are not superseded: they are standing facts
  // about the box rather than news about this press.
  const runNotice = readable && !superseded ? phaseNotice(data, runningMs) : null;
  const verifyFailed = readable && !superseded && data.verified === false;
  // TWO SEPARATE QUESTIONS, and conflating them hid the answer to the one that matters.
  //
  // `available` means only "this container can write a backup request" -- the drop-box
  // exists and is writable. backups.py's own docstring says it says nothing about whether
  // the nightly TIMER is installed. Gating the whole record on it meant a box whose timer
  // runs perfectly, with a verified backup from four hours ago, rendered "nightly backups
  // aren't set up on this box" and nothing else, the moment the drop-box was missing or
  // its mode was wrong. A reviewer built that state: the record was there, readable, and
  // the page hid it while contradicting it.
  //
  // So the RECORD is shown whenever there is one, and `available` gates only the button
  // and what is said about pressing it.
  const hasRecord = readable && data.phase !== null;
  const canRequest = readable && data.available;
  // The headline is shown whenever this page knows ANYTHING about backups on this box --
  // either a run has happened, or the regime is installed and none has yet. headline()
  // has a sentence for the second case ("no run has finished on this box"), and it is the
  // right thing to say on a fresh install. The off-box notice needs an actual run to be
  // about, so it waits for one.
  const knowsSomething = hasRecord || canRequest;
  const head = knowsSomething ? headline(data, age) : null;
  const offsite = hasRecord ? offsiteNotice(data) : null;

  return (
    <div className="card">
      <h2>Backups</h2>

      {readable && !data.available && !hasRecord && (
        <div className="notice notice-warn">
          <strong>Nightly backups aren&rsquo;t set up on this box</strong>
          <div>
            Nothing takes a backup of this household&rsquo;s data, so there is nothing to
            restore from. Run <code>sudo ./scripts/install-updater.sh</code> on the host
            once; it installs the nightly timer and the drop-box this button writes to.
          </div>
        </div>
      )}

      {readable && !data.available && hasRecord && (
        <div className="notice notice-warn">
          <strong>Backups are running, but this page can&rsquo;t ask for one</strong>
          <div>
            The record below is real &mdash; something on the host is taking backups. What is
            missing is the drop-box this page writes a request into, so{" "}
            <em>Back up now</em> has nothing to write to. Re-run{" "}
            <code>sudo ./scripts/install-updater.sh</code> on the host to create it;
            the nightly timer is unaffected either way.
          </div>
        </div>
      )}

      {knowsSomething && head && (
        <>
          <div className={`notice notice-${head.tone}`}>
            <strong>{head.title}</strong>
            <div>{head.detail}</div>
          </div>

          {/* The single most important state on the card: a dump exists, and it is not a
              backup. Loud, and above the run's own notice, because `phase: failed` with
              this set reads as "a run failed" when the fact is "the thing you would
              restore from does not restore". */}
          {verifyFailed && (
            <div className="notice notice-bad">
              <strong>A dump was taken, but it could not be restored &mdash; so it is not a backup</strong>
              <div>
                {data.verify_message ??
                  "The host did not record what the restore proved, only that it did not " +
                    "come back whole."}
              </div>
              <div style={{ marginTop: 4 }}>
                The dump has been kept for inspection and nothing was pruned. Run{" "}
                <code>./scripts/backup.sh --verify-only</code> on the host to see the
                restore fail in front of you.
              </div>
            </div>
          )}

          {pressSummary && (
            <div className={`notice notice-${pressSummary.tone}`}>
              <strong>{pressSummary.title}</strong>
              <div>{pressSummary.detail}</div>
            </div>
          )}

          {runNotice && (
            <div className={`notice notice-${runNotice.tone}`}>
              <strong>{runNotice.title}</strong>
              <div>{runNotice.detail}</div>
              <div className="muted" style={{ marginTop: 4, fontSize: "0.9em" }}>
                {data.started_at ? `started ${data.started_at}` : "the host did not record a start time"}
                {data.finished_at ? ` · finished ${data.finished_at}` : null}
                {data.trigger ? ` · ${data.trigger}` : null}
                {data.dump_name ? ` · ${data.dump_name}` : null}
                {data.dump_bytes ? ` · ${sizeLabel(data.dump_bytes)}` : null}
                {data.rows_checked ? ` · ${data.rows_checked} rows checked` : null}
                {data.dump_revision ? ` · schema ${data.dump_revision}` : null}
              </div>
            </div>
          )}

          {offsite ? (
            <div className={`notice notice-${offsite.tone}`}>
              <strong>{offsite.title}</strong>
              <div>{offsite.detail}</div>
              {offsite.tone === "good" && data.offsite_at ? (
                <div className="muted" style={{ marginTop: 4, fontSize: "0.9em" }}>
                  copied {data.offsite_at}
                </div>
              ) : null}
            </div>
          ) : (
            <p className="muted">
              The host has not recorded whether any copy of a backup has left this box, so
              this page cannot tell you either way.
            </p>
          )}

          <p className="muted">{retainedLine(data)}</p>
        </>
      )}

      {status.isError && (
        <div className="notice notice-warn">
          <strong>Can&rsquo;t read the backup status</strong>
          <div>
            {(status.error as Error).message}. This page is showing nothing rather than
            what it knew before, because the age of a backup read a while ago is not an
            answer. Unlike the update status there is no public copy of this one by design
            &mdash; when a household was last backed up is reconnaissance, and it is never
            what you need while the app is down &mdash; so{" "}
            <code>./scripts/backup.sh --status</code> on the host is the way to see it.
          </div>
        </div>
      )}

      <div className="toolbar" style={{ marginTop: 12 }}>
        <button
          className="btn"
          onClick={() => startBackup.mutate()}
          // Also until the record has loaded. A press made before then has no run id to
          // supersede, so the previous run's result would be presented as the result of
          // the press -- and, more simply, asking for a backup before knowing whether
          // anything on the box can take one is a request with nowhere to go.
          disabled={
            startBackup.isPending || status.isPending || busy || !canRequest
          }
        >
          {startBackup.isPending ? "Asking the host…" : "Back up now"}
        </button>
      </div>

      {startBackup.isError && (
        <p className="muted" style={{ marginTop: 8 }}>
          Couldn&rsquo;t ask the host to back up: {(startBackup.error as Error).message}. Nothing
          was requested, so nothing is running.
        </p>
      )}

      {readable && !superseded && data.log_tail && data.log_tail.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary className="muted">What the backup run logged</summary>
          <pre style={{ overflowX: "auto", fontSize: "0.85em" }}>{data.log_tail.join("\n")}</pre>
        </details>
      )}
    </div>
  );
}
