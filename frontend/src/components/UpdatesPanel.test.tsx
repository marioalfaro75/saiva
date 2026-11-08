import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { UpdateRun, UpdateStatus } from "../api/types";
import { UpdatesPanel } from "./UpdatesPanel";

/**
 * The panel's one job is to not say more than it knows.
 *
 * It used to say more than it knew twice. Pressing "Update now" started a four-minute
 * poll and then rendered "Update applied." whether the version had changed or the poll
 * had simply run out; and "You're on the latest version" appeared whenever the check was
 * enabled and no newer release was found — including when the request to GitHub had
 * failed outright, so a network failure read as reassurance.
 *
 * Every test here is a sentence the panel must not say, or one it must.
 */

const UP_TO_DATE: UpdateStatus = {
  current_version: "v0.16.1",
  current_build: null,
  latest_version: "v0.16.1",
  update_available: false,
  apply_available: true,
  check_enabled: true,
  check_failed: false,
  comparable: true,
  release_url: null,
  release_notes: null,
  published_at: null,
};

const NO_RUN: UpdateRun = {
  available: true,
  run_id: null,
  phase: null,
  step: null,
  trigger: null,
  started_at: null,
  finished_at: null,
  message: null,
  exit_code: null,
  from_version: null,
  to_version: null,
  schema_before: null,
  schema_after: null,
  db_revision: null,
  log_tail: null,
};

function show(status: Partial<UpdateStatus> = {}, run: Partial<UpdateRun> = {}) {
  vi.spyOn(api, "updateCheck").mockResolvedValue({ ...UP_TO_DATE, ...status });
  vi.spyOn(api, "updateRun").mockResolvedValue({ ...NO_RUN, ...run });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UpdatesPanel />
    </QueryClientProvider>,
  );
}

const tick = /you.?re on the latest release/i;

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("what the panel is allowed to claim", () => {
  it("says you are up to date only when it actually checked and could compare", async () => {
    show();
    expect(await screen.findByText(tick)).toBeInTheDocument();
  });

  it("does not claim you are up to date when the check failed", async () => {
    show({ check_failed: true, latest_version: null });

    expect(await screen.findByText(/couldn.t reach github/i)).toBeInTheDocument();
    expect(screen.queryByText(tick)).not.toBeInTheDocument();
    // And it must say what it is NOT saying, because silence reads as reassurance.
    expect(screen.getByText(/not saying you are up to date/i)).toBeInTheDocument();
  });

  it("does not claim you are up to date on a build with no release number", async () => {
    show({ current_version: "edge", comparable: false, latest_version: "v0.17.0" });

    expect(await screen.findByText(/no release number to compare/i)).toBeInTheDocument();
    expect(screen.queryByText(tick)).not.toBeInTheDocument();
  });

  it("does not claim anything when update checks are turned off", async () => {
    show({ check_enabled: false, latest_version: null });

    expect(await screen.findByText(/checks are turned off/i)).toBeInTheDocument();
    expect(screen.queryByText(tick)).not.toBeInTheDocument();
  });

  it("offers the update when one exists and the host can apply it", async () => {
    show({ update_available: true, latest_version: "v0.17.0" });

    expect(await screen.findByRole("button", { name: /update now/i })).toBeInTheDocument();
  });

  it("does not offer a button the host cannot act on, and says why", async () => {
    show({ update_available: true, latest_version: "v0.17.0", apply_available: false });

    expect(await screen.findByText(/aren.t set up on this box/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /update now/i })).not.toBeInTheDocument();
    expect(screen.getByText(/install-updater\.sh/)).toBeInTheDocument();
  });
});

describe("what it says about a run that has happened", () => {
  it("reports a clean application", async () => {
    show({}, { phase: "applied", to_version: "v0.17.0" });

    expect(await screen.findByText(/updated to v0\.17\.0/i)).toBeInTheDocument();
  });

  it("calls a rollback a rollback", async () => {
    show({}, { phase: "rolled_back", from_version: "v0.16.1", to_version: "v0.17.0" });

    expect(await screen.findByText(/rolled back to v0\.16\.1/i)).toBeInTheDocument();
  });

  it("refuses to call a half-rollback a rollback", async () => {
    // The migration succeeded, so the schema is ahead of the code now running. Saying
    // "rolled back" here is the same class of claim as the old "Update applied."
    show({}, {
      phase: "rolled_back_code_only",
      from_version: "v0.16.1",
      to_version: "v0.17.0",
    });

    const heading = await screen.findByText(/rolled the app back, but not the database/i);
    expect(heading).toBeInTheDocument();
    expect(screen.getByText(/restore-backup\.sh/)).toBeInTheDocument();
    expect(screen.queryByText(/^Update failed — rolled back/)).not.toBeInTheDocument();
  });

  it("says when the configuration went back but the images did not", async () => {
    show({}, { phase: "rollback_incomplete", from_version: "v0.16.1" });

    expect(
      await screen.findByText(/put the configuration back, but not the app images/i),
    ).toBeInTheDocument();
  });

  it("does not hide a state it does not recognise", async () => {
    // A phase the updater gained and the panel has not learned yet must be visible, not
    // silently dropped. backend/tests/test_deployment_hardening.py keeps the two in step;
    // this is what happens on the day that test is wrong.
    show({}, { phase: "something_new", message: "a thing happened" });

    expect(await screen.findByText(/unrecognised state: something_new/i)).toBeInTheDocument();
  });

  it("says nothing about a run when there has never been one", async () => {
    show({}, { phase: null });

    await waitFor(() => expect(screen.getByText(tick)).toBeInTheDocument());
    expect(screen.queryByText(/updated to/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/rolled back/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/update failed/i)).not.toBeInTheDocument();
  });

  it("shows the updater's log when something went wrong, and not when it did not", async () => {
    show({}, { phase: "failed", message: "the pull failed", log_tail: ["line one", "line two"] });

    expect(await screen.findByText(/what the updater logged/i)).toBeInTheDocument();
  });

  it("does not bury a successful run under its log", async () => {
    show({}, { phase: "applied", to_version: "v0.17.0", log_tail: ["noise"] });

    await screen.findByText(/updated to v0\.17\.0/i);
    expect(screen.queryByText(/what the updater logged/i)).not.toBeInTheDocument();
  });
});

describe("the version line", () => {
  it("shows the build when the version alone cannot identify it", async () => {
    show({ current_version: "edge", current_build: "sha-1a2b3c4", comparable: false });

    expect(await screen.findByText(/build sha-1a2b3c4/)).toBeInTheDocument();
  });
});
