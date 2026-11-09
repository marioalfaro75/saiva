import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { BackupStatus } from "../api/types";
import { BackupsPanel } from "./BackupsPanel";

/**
 * The panel's one job is to not say more than it knows, and its one headline is a
 * number: how old the newest VERIFIED backup is.
 *
 * Every test below is a sentence the panel must say, or one it must not. The ones worth
 * reading twice are the pairs that look alike and are not: a dump that was taken against
 * a dump that was proved restorable, a copy on another disk against a copy on another
 * machine, a run that failed against a run that was skipped. Each pair is a place where
 * one word of slippage turns "you have no backup" into "you have a backup", which is the
 * only failure this regime exists to prevent.
 */

const HOURS = 3_600_000;
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString();

/** A box doing everything right: verified nine hours ago, copy on an NFS share. */
const HEALTHY: BackupStatus = {
  available: true,
  stale_after_hours: 36,
  run_id: "20261109T023700Z-1234",
  phase: "ok",
  step: "done",
  trigger: "timer",
  started_at: iso(9 * HOURS),
  finished_at: iso(9 * HOURS - 60_000),
  message: "Backed up and verified by restoring it.",
  exit_code: "0",
  dump_name: "saiva-20261109-023700-nightly.sql.gz",
  dump_bytes: "4194304",
  dump_revision: "a1b2c3d4e5f6",
  verified: true,
  verify_message: "Restored into a throwaway database: 504 rows across 23 tables.",
  rows_checked: "504",
  last_success_at: iso(9 * HOURS),
  last_verified_at: iso(9 * HOURS),
  offsite: "remote",
  offsite_target: "/mnt/backups/saiva",
  offsite_at: iso(9 * HOURS),
  offsite_message: "/mnt/backups/saiva: copied, to nfs4 -- off this machine",
  backup_count: "14",
  oldest_at: "2026-10-27T02:37:00Z",
  kept: "14",
  pruned: "1",
  log_tail: ["→ dump", "→ verify", "✔ ok"],
};

function show(status: Partial<BackupStatus> = {}) {
  vi.spyOn(api, "backupStatus").mockResolvedValue({ ...HEALTHY, ...status });
  vi.spyOn(api, "runBackup").mockResolvedValue({ status: "requested", request_id: "b1" });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <BackupsPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

/**
 * The headline's TONE, parameterised.
 *
 * A third review mutated this component and ran the suite against each mutation; twelve
 * one-line changes that turn a warning into good news all kept it green, because only one
 * of nine tone decisions was pinned. The card's whole judgement is that tone -- it is what
 * somebody glancing at the page actually reads -- so it is asserted directly here rather
 * than inferred from the words beside it.
 *
 * `notice-good` is the claim "this household has a backup, and a copy of it is not on this
 * machine". Nothing below may produce it.
 */
const HEADLINE_TITLES =
  /^(Last verified backup:|The last verified backup|No backup has been verified|Nothing has been backed up)/;

function headlineNotice(): HTMLElement {
  // The headline is the FIRST .notice on the card, and its title is its <strong>. Matched
  // on the title rather than on any text, because several of the notices below it quote
  // the same words -- the off-box note says "left this machine" too, in the negative.
  const titles = screen.getAllByText(HEADLINE_TITLES, { selector: "strong" });
  const notice = titles[0]?.closest(".notice");
  if (!notice) throw new Error("the headline is not a <strong> inside a .notice any more");
  return notice as HTMLElement;
}

describe("the headline's tone, which is the whole judgement", () => {
  it("is good only when a fresh verified backup is also off this machine", async () => {
    show();
    await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
    expect(headlineNotice().className).toContain("notice-good");
  });

  // Every off-box state the host can record except `remote`, plus the two it can leave
  // unset. scripts/backup.sh declares the vocabulary as SAIVA_OFFSITE_STATES, and
  // backend/tests/test_deployment_hardening.py keeps that declaration and this component
  // in step -- so this list going stale is itself a test failure there.
  it.each(["same-filesystem", "same-host", "unknown", "failed", "off", null])(
    "is not good when the copy's state is %s",
    async (offsite) => {
      show({ offsite });
      await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
      expect(headlineNotice().className).not.toContain("notice-good");
      // The AFFIRMATIVE claim. The warning branch says "no copy of it is known to have
      // left this machine", which contains the same words and is the correct thing to say.
      expect(headlineNotice().textContent ?? "").not.toMatch(/a copy of it left this machine/i);
    },
  );

  it("is not good when the dump could not be restored", async () => {
    show({ verified: false, last_verified_at: null, phase: "failed" });
    await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
    expect(headlineNotice().className).not.toContain("notice-good");
  });

  it("is not good once the age passes the hours the API reports", async () => {
    show({ last_verified_at: iso(40 * HOURS), stale_after_hours: 36 });
    await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
    expect(headlineNotice().className).not.toContain("notice-good");
  });

  it("is still good just inside that window", async () => {
    show({ last_verified_at: iso(30 * HOURS), stale_after_hours: 36 });
    await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
    expect(headlineNotice().className).toContain("notice-good");
  });

  it("is not good when the timestamp is in the future", async () => {
    show({ last_verified_at: new Date(Date.now() + 4 * HOURS).toISOString() });
    await screen.findByText(HEADLINE_TITLES, { selector: "strong" });
    expect(headlineNotice().className).not.toContain("notice-good");
  });
});

describe("the headline is the age of the newest verified backup", () => {
  it("gives the age as a number", async () => {
    show();

    expect(await screen.findByText(/last verified backup: 9 hours ago/i)).toBeInTheDocument();
  });

  it("never falls back to last_success_at when nothing has been verified", async () => {
    // The whole regime exists to keep these two apart. A dump was taken an hour ago and
    // nobody has restored it, so the honest headline is "no verified backup" -- not "1
    // hour ago", which is the age of something that has never been shown to work.
    show({ last_verified_at: null, last_success_at: iso(1 * HOURS), verified: null });

    expect(await screen.findByText(/no backup has been verified on this box/i)).toBeInTheDocument();
    expect(screen.queryByText(/last verified backup: 1 hour ago/i)).not.toBeInTheDocument();
    expect(screen.getByText(/no evidence it is a backup/i)).toBeInTheDocument();
  });

  it("says plainly when nothing at all has been backed up", async () => {
    show({ last_verified_at: null, last_success_at: null, phase: null, verified: null });

    expect(await screen.findByText(/nothing has been backed up yet/i)).toBeInTheDocument();
  });

  it("calls out a timestamp from the future instead of rendering a negative age", async () => {
    // An LXC takes its clock from the Proxmox host, so restoring a snapshot really does
    // produce this. "-4 hours ago" is nonsense and "0 hours ago" is a fresh backup the
    // box does not have, so neither may appear.
    show({ last_verified_at: new Date(Date.now() + 4 * HOURS + 60_000).toISOString() });

    expect(await screen.findByText(/host clock may be wrong/i)).toBeInTheDocument();
    expect(screen.getByText(/[0-9]+ hours ahead of this browser/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 hours ago/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/-4/)).not.toBeInTheDocument();
  });

  it("measures staleness against the hours the API reports, not a number of its own", async () => {
    // The policy lives on the host (SAIVA_BACKUP_STALE_HOURS). A panel with 36 compiled
    // into it tells an owner who chose 12 that a day-old backup is fine.
    show({ stale_after_hours: 12, last_verified_at: iso(20 * HOURS) });

    expect(await screen.findByText(/last verified backup: 20 hours ago/i)).toBeInTheDocument();
    expect(screen.getByText(/older than the 12 hours this box expects/i)).toBeInTheDocument();
  });

  it("does not call a backup stale while it is inside the host's own window", async () => {
    show({ stale_after_hours: 48, last_verified_at: iso(40 * HOURS) });

    expect(await screen.findByText(/last verified backup: 40 hours ago/i)).toBeInTheDocument();
    expect(screen.queryByText(/older than the 48 hours/i)).not.toBeInTheDocument();
  });

  it("reports a days-old backup in days, and as a failure", async () => {
    show({ last_verified_at: iso(5 * 24 * HOURS), last_success_at: iso(5 * 24 * HOURS) });

    expect(await screen.findByText(/last verified backup: 5 days ago/i)).toBeInTheDocument();
    expect(screen.getByText(/in the database and nowhere else/i)).toBeInTheDocument();
  });
});

describe("a dump that is not a backup", () => {
  it("says so in those words, with what the restore actually proved", async () => {
    // verified === false is the single most important state on the card. pg_dump exited
    // 0, gzip is intact, and the thing still does not restore -- which is exactly the
    // case a regime that checked only exit codes would have ticked.
    show({
      verified: false,
      phase: "failed",
      verify_message: "The restored copy held 19 rows; the dump declares 504.",
      last_verified_at: iso(30 * HOURS),
    });

    expect(
      await screen.findByText(/could not be restored .* so it is not a backup/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/the dump declares 504/i)).toBeInTheDocument();
  });

  it("does not claim a verification nobody performed", async () => {
    show({ verified: null, phase: "running", started_at: iso(60_000), step: "dump" });

    await screen.findByText(/backing up…/i);
    expect(screen.queryByText(/could not be restored/i)).not.toBeInTheDocument();
  });
});

describe("how far the copy actually got", () => {
  it("treats a network destination as the only good news", async () => {
    show();

    expect(await screen.findByText(/a copy is off this box/i)).toBeInTheDocument();
  });

  it("does not treat `ok` as off-box, because the host does not write it", async () => {
    // This panel accepted `ok` as a synonym for `remote` while scripts/backup.sh wrote it
    // — which it did only because the final `status_set offsite "${worst:-ok}"` defaulted
    // to the most reassuring value in the union, so a branch that forgot to record what
    // happened said "copied off this box". The script was fixed and now declares its
    // vocabulary; keeping `ok` here would mean the panel calls good news a value the host
    // cannot produce. An `ok` from some future host is rendered as itself.
    show({ offsite: "ok" });

    expect(await screen.findByText(/unrecognised state/i)).toBeInTheDocument();
    expect(screen.queryByText(/a copy is off this box/i)).not.toBeInTheDocument();
  });

  it("warns about the disk when the copy is on the same filesystem", async () => {
    show({ offsite: "same-filesystem", offsite_message: "same disk" });

    expect(
      await screen.findByText(/same filesystem as the database/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/losing that disk loses the database/i)).toBeInTheDocument();
  });

  it("warns about the machine when the copy is merely elsewhere on it", async () => {
    // Distinct wording from same-filesystem on purpose, and the reason it exists at all:
    // a btrfs subvolume, a ZFS dataset, an LVM thin volume and a Proxmox mount point all
    // report different device numbers while sharing one pool, so a device comparison
    // would have called every one of them "off this box".
    show({ offsite: "same-host", offsite_message: "ext4, a different device" });

    expect(await screen.findByText(/on this same machine/i)).toBeInTheDocument();
    expect(screen.getByText(/does not survive losing the machine/i)).toBeInTheDocument();
    expect(screen.queryByText(/same filesystem as the database/i)).not.toBeInTheDocument();
  });

  it("names the setting to change when nothing is copied off the box", async () => {
    show({ offsite: "off", offsite_target: null, offsite_at: null, offsite_message: null });

    expect(await screen.findByText(/nothing is copied off this box/i)).toBeInTheDocument();
    expect(screen.getByText(/SAIVA_BACKUP_COPY_TO/)).toBeInTheDocument();
    expect(screen.getByText(/\/etc\/saiva\/agent\.conf/)).toBeInTheDocument();
  });

  it("admits when it could not work out how far away the destination is", async () => {
    show({ offsite: "unknown" });

    expect(
      await screen.findByText(/could not work out how far away the copy went/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/will not claim either/i)).toBeInTheDocument();
  });

  it("separates a failed copy from a failed backup", async () => {
    show({ offsite: "failed", offsite_message: "/mnt/backups: the copy failed" });

    expect(await screen.findByText(/off-box copy did not happen/i)).toBeInTheDocument();
    expect(screen.getByText(/verified dump itself\s+is still here/i)).toBeInTheDocument();
  });

  it("does not let a fresh verified backup read as a tick while the copy is on the same disk", async () => {
    // The absence-of-bad-news tick, one card along from where it was removed. The age is
    // genuinely fine; the household still loses everything with the disk.
    show({ offsite: "same-filesystem" });

    const head = await screen.findByText(/last verified backup: 9 hours ago/i);
    expect(head.closest(".notice")).toHaveClass("notice-warn");
    expect(screen.getByText(/no\s+copy of it is known to have left this machine/i)).toBeInTheDocument();
  });

  it("says nothing either way when the host recorded no off-box state", async () => {
    show({ offsite: null });

    expect(
      await screen.findByText(/has not recorded whether any copy of a backup has left/i),
    ).toBeInTheDocument();
  });

  it("does not hide an off-box state it does not recognise", async () => {
    show({ offsite: "s3-glacier", offsite_message: "pushed to a vault" });

    expect(
      await screen.findByText(/off-box copy reported an unrecognised state: s3-glacier/i),
    ).toBeInTheDocument();
  });
});

describe("what it says about the run itself", () => {
  it("treats a skipped run as information, and keeps the age counting", async () => {
    // A unit that reports failure every night because the stack is off trains its owner
    // to ignore it, and then the one real failure is ignored too.
    show({
      phase: "skipped",
      message: "A release held the deploy lock, so no backup was taken this time.",
      last_verified_at: iso(20 * HOURS),
    });

    expect(await screen.findByText(/no backup was taken on the last run, on purpose/i)).toBeInTheDocument();
    expect(screen.getByText(/last verified backup: 20 hours ago/i)).toBeInTheDocument();
  });

  it("reports a failed run as a failure", async () => {
    show({ phase: "failed", message: "There is no saiva_default network." });

    expect(await screen.findByText(/last backup run failed/i)).toBeInTheDocument();
    expect(screen.getByText(/no saiva_default network/i)).toBeInTheDocument();
  });

  it("does not present a clean run as an answer to whether the box is backed up", async () => {
    // "The last run worked" is news about that run. On a box whose last run worked six
    // weeks ago it is true and useless, so the judgement stays with the age.
    show({ phase: "ok", last_verified_at: iso(9 * HOURS) });

    const run = await screen.findByText(/last run finished cleanly/i);
    expect(run.closest(".notice")).not.toHaveClass("notice-good");
  });

  it("does not hide a phase it does not recognise", async () => {
    show({ phase: "quiescing", message: "a thing happened" });

    expect(
      await screen.findByText(/backup run reported an unrecognised state: quiescing/i),
    ).toBeInTheDocument();
  });

  it("says when the record itself could not be parsed", async () => {
    show({ phase: "unreadable" });

    expect(await screen.findByText(/record could not be read/i)).toBeInTheDocument();
  });
});

describe("waits that would otherwise never end", () => {
  it("still calls a run that started moments ago a running run", async () => {
    show({ phase: "running", step: "verify", started_at: new Date().toISOString() });

    expect(await screen.findByText(/backing up…/i)).toBeInTheDocument();
  });

  it("stops calling a long-dead run 'Backing up…'", async () => {
    // systemd kills saiva-backup.service at TimeoutStartSec=3600, so a status file still
    // saying `running` two hours later is one nobody came back to. Spinning forever is
    // the same shape of failure as a false tick.
    show({ phase: "running", step: "dump", started_at: iso(2 * 60 * 60_000) });

    expect(await screen.findByText(/started and never finished/i)).toBeInTheDocument();
    expect(screen.getByText(/saiva-backup\.service/)).toBeInTheDocument();
  });

  it("gives up on a host that never picks a press up, and says what to run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      show();
      await screen.findByText(/last verified backup: 9 hours ago/i);
      fireEvent.click(screen.getByRole("button", { name: /back up now/i }));
      await screen.findByText(/asked the host to back up/i);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(125_000);
      });

      expect(screen.getByText(/host has not picked this up/i)).toBeInTheDocument();
      expect(screen.getByText(/install-updater\.sh/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  }, 20_000);
});

describe("pressing the button", () => {
  it("does not present the previous run's result as the result of the press just made", async () => {
    show({ phase: "failed", run_id: "the-old-run", message: "last night went wrong" });
    await screen.findByText(/last backup run failed/i);

    fireEvent.click(screen.getByRole("button", { name: /back up now/i }));

    await waitFor(() =>
      expect(screen.getByText(/asked the host to back up/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/last backup run failed/i)).not.toBeInTheDocument();
  });

  it("keeps the age on screen while it waits, because that fact did not change", async () => {
    show({ run_id: "the-old-run", last_verified_at: iso(9 * HOURS) });
    await screen.findByText(/last verified backup: 9 hours ago/i);

    fireEvent.click(screen.getByRole("button", { name: /back up now/i }));

    await screen.findByText(/asked the host to back up/i);
    expect(screen.getByText(/last verified backup: 9 hours ago/i)).toBeInTheDocument();
  });

  it("will not take a press before it has read the record", async () => {
    // A press with no run id on screen has nothing to supersede, so the previous run's
    // result would stand as the answer to it.
    show();

    expect(screen.getByRole("button", { name: /back up now/i })).toBeDisabled();
    await screen.findByText(/last verified backup: 9 hours ago/i);
    expect(screen.getByRole("button", { name: /back up now/i })).toBeEnabled();
  });

  it("says the request could not be made, rather than appearing to wait", async () => {
    show();
    await screen.findByText(/last verified backup: 9 hours ago/i);
    vi.spyOn(api, "runBackup").mockRejectedValue(new Error("the drop-box is not there"));

    fireEvent.click(screen.getByRole("button", { name: /back up now/i }));

    expect(await screen.findByText(/couldn.t ask the host to back up/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is running/i)).toBeInTheDocument();
  });
});

describe("a record and the ability to ask for one are different questions", () => {
  // `available` means only "this container can write a backup request". Gating the whole
  // record on it meant a box whose timer runs perfectly, with a verified backup from four
  // hours ago, rendered "nightly backups aren't set up on this box" and nothing else the
  // moment the drop-box was missing or its mode was wrong -- the page hiding the answer
  // while contradicting it. Found by an adversarial review.
  it("shows a real record even when the drop-box this page writes to is missing", async () => {
    const fourHoursAgo = new Date(Date.now() - 4 * 3_600_000).toISOString();
    show({
      available: false,
      phase: "ok",
      verified: true,
      last_verified_at: fourHoursAgo,
      last_success_at: fourHoursAgo,
      dump_name: "saiva-20261109-023700-nightly.sql.gz",
    });

    expect(await screen.findByText(/last verified backup/i)).toBeInTheDocument();
    expect(screen.queryByText(/aren.t set up on this box/i)).not.toBeInTheDocument();
    // And it says what IS missing, rather than what is not.
    expect(screen.getByText(/can.t ask for one/i)).toBeInTheDocument();
    // Present but disabled, with the notice above it saying why. Hiding it would leave the
    // owner wondering where it went; this says it cannot be pressed and what to run.
    expect(screen.getByRole("button", { name: /back up now/i })).toBeDisabled();
  });

  it("still says nothing is set up when there is no record either", async () => {
    show({ available: false, phase: null });

    expect(await screen.findByText(/aren.t set up on this box/i)).toBeInTheDocument();
    expect(screen.getByText(/install-updater\.sh/)).toBeInTheDocument();
  });
});

describe("when the regime is not installed", () => {
  it("says so and names the command, and offers no button to press", async () => {
    show({ available: false, phase: null, last_verified_at: null, last_success_at: null });

    expect(await screen.findByText(/aren.t set up on this box/i)).toBeInTheDocument();
    expect(screen.getByText("sudo ./scripts/install-updater.sh")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back up now/i })).toBeDisabled();
    // And it must not go on to report an age it has no basis for.
    expect(screen.queryByText(/last verified backup/i)).not.toBeInTheDocument();
  });
});

describe("when the panel itself cannot get an answer", () => {
  it("says it cannot read the status, and leaves no reassuring sentence standing", async () => {
    vi.spyOn(api, "backupStatus").mockRejectedValue(new Error("Failed to fetch"));
    vi.spyOn(api, "runBackup").mockResolvedValue({ status: "requested", request_id: "b1" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <BackupsPanel />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/can.t read the backup status/i)).toBeInTheDocument();
    expect(screen.queryByText(/last verified backup/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/a copy is off this box/i)).not.toBeInTheDocument();
    // And it names the route that still works, and why there is no public one.
    expect(screen.getByText("./scripts/backup.sh --status")).toBeInTheDocument();
  });

  it("drops the age it knew before rather than keeping it on screen after a failed refetch", async () => {
    // react-query holds the previous answer in `data` when a refetch errors. "Last
    // verified backup: 9 hours ago" standing on an answer we could not get is the same
    // mistake as a tick that survives a failed update check.
    show();
    await screen.findByText(/last verified backup: 9 hours ago/i);
    vi.spyOn(api, "backupStatus").mockRejectedValue(new Error("Failed to fetch"));

    fireEvent.click(screen.getByRole("button", { name: /back up now/i }));

    await waitFor(() =>
      expect(screen.queryByText(/last verified backup: 9 hours ago/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/can.t read the backup status/i)).toBeInTheDocument();
  });
});

describe("the shape of the retained set", () => {
  it("reports what is kept, and names the absence when the host did not say", async () => {
    show({ backup_count: null, kept: null, oldest_at: null, pruned: null });

    expect(
      await screen.findByText(/has not said how many dumps it is keeping/i),
    ).toBeInTheDocument();
  });

  it("reports the counts when it has them", async () => {
    show();

    expect(await screen.findByText(/14 nightly dumps kept/i)).toBeInTheDocument();
    expect(screen.getByText(/1 pruned on the last run/i)).toBeInTheDocument();
  });
});
