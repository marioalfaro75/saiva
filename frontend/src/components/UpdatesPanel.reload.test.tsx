import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { UpdateRun, UpdateStatus } from "../api/types";
import { UpdatesPanel } from "./UpdatesPanel";

/**
 * "Reload to use it" -- found on the real box after its first proper update.
 *
 * The button was shown whenever the last run was a success, which is to say for ever:
 * pressing it reloaded the page, the page re-read the same successful record, and the
 * button came straight back. Reloading only helps a page that is OLDER than what the box
 * now runs, so that is the only time it may be offered. In its own file because the
 * page's version is a module constant, and mocking it here leaves the main suite's
 * "dev" page alone.
 */

// The version THIS page was built as. A getter on a hoisted object, so each test can set
// it and the component reads the current value when it renders.
const page = vi.hoisted(() => ({ version: "v0.17.0" }));
vi.mock("../version", () => ({
  get SPA_VERSION() {
    return page.version;
  },
}));

const STATUS: UpdateStatus = {
  current_version: "v0.17.0",
  current_build: null,
  latest_version: "v0.17.0",
  update_available: false,
  apply_available: true,
  check_enabled: true,
  check_failed: false,
  comparable: true,
  release_url: null,
  release_notes: null,
  published_at: null,
};

const APPLIED: UpdateRun = {
  available: true,
  run_id: "r1",
  phase: "applied",
  step: "done",
  trigger: "cli",
  started_at: "2026-11-14T04:39:47Z",
  finished_at: "2026-11-14T04:40:20Z",
  message: "Update applied and the site answered at https://192.168.0.11.",
  exit_code: "0",
  from_version: "v0.16.1",
  to_version: "v0.17.0",
  schema_before: null,
  schema_after: null,
  db_revision: null,
  log_tail: null,
};

function show(run: Partial<UpdateRun> = {}) {
  vi.spyOn(api, "updateCheck").mockResolvedValue(STATUS);
  vi.spyOn(api, "updateRun").mockResolvedValue({ ...APPLIED, ...run });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UpdatesPanel />
    </QueryClientProvider>,
  );
}

const reload = { name: /reload to use it/i };

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("Reload to use it", () => {
  it("is not offered when this page already runs the version the box updated to", async () => {
    page.version = "v0.17.0";
    show();

    // Still says what happened and when -- quietly, as history.
    expect(await screen.findByText(/last update: updated to v0\.17\.0/i)).toBeInTheDocument();
    expect(screen.getByText(/finished 2026-11-14T04:40:20Z/)).toBeInTheDocument();
    expect(screen.queryByRole("button", reload)).not.toBeInTheDocument();
  });

  it("is offered when this page is older than what the box now runs", async () => {
    page.version = "v0.16.1";
    show();

    expect(await screen.findByRole("button", reload)).toBeInTheDocument();
    expect(screen.getByText(/updated to v0\.17\.0/i)).toBeInTheDocument();
  });

  it("never turns a failure into a quiet line, even on a page that is current", async () => {
    page.version = "v0.17.0";
    show({ phase: "rolled_back", from_version: "v0.17.0", to_version: "v0.18.0" });

    expect(await screen.findByText(/rolled back to v0\.17\.0/i)).toBeInTheDocument();
    expect(screen.queryByText(/last update:/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", reload)).not.toBeInTheDocument();
  });
});
