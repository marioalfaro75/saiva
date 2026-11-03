import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";

import { AuthProvider, useAuth } from "./AuthContext";

/**
 * Bootstrap is three requests — a CSRF token, whether the instance has been set up,
 * and who is signed in — and the whole app is blocked on "Loading…" until they are
 * done. It used to publish each answer as it arrived, which meant a consumer could be
 * handed a session that was true in parts: `initialised` already from the server while
 * `me` had not been asked for yet. Nothing rendered that state, so nothing broke, but
 * it is three commits to say one thing and the first `setState` ran synchronously
 * inside the effect. These tests pin the settled result and the single transition.
 */

const ME = {
  user: { id: "u1", email: "a@b.c", name: "Owner", role: "owner", is_active: true },
  household: { id: "h1", name: "Alfaro Household", state: "NSW", adults: 2, children: 2 },
  csrf_token: "t",
};

type Snapshot = { signedIn: boolean; loading: boolean; initialised: boolean };

/** The session as it stands now — the last commit the probe recorded. */
function settled(seen: Snapshot[]): Snapshot | undefined {
  return seen[seen.length - 1];
}

/**
 * Resolves on a timer rather than immediately. The delay is the point: three requests
 * that settle in the same microtask get batched into one commit whatever the provider
 * does, which would hide exactly the staging these tests are about. A real round trip
 * crosses a task boundary, and so does this.
 */
function reply(status: number, body: unknown) {
  const res = {
    ok: status < 400,
    status,
    statusText: status === 401 ? "Unauthorized" : "OK",
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    clone: () => ({ json: () => Promise.resolve(body) }),
  };
  return new Promise((resolve) => setTimeout(() => resolve(res), 0));
}

/** Bootstrap with the three answers a given instance would really give. */
function stubAuth({ initialised, signedIn }: { initialised: boolean; signedIn: boolean }) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = String(input);
      if (url.includes("/auth/csrf")) return reply(200, { csrf_token: "t" });
      if (url.includes("/auth/status")) return reply(200, { initialised });
      if (url.includes("/auth/me")) {
        return signedIn ? reply(200, ME) : reply(401, { detail: "Not authenticated" });
      }
      return reply(200, {});
    }),
  );
}

/** Records the session as each commit publishes it, so the sequence can be asserted. */
function Probe({ seen }: { seen: Snapshot[] }) {
  const { me, loading, initialised } = useAuth();
  useEffect(() => {
    seen.push({ signedIn: !!me, loading, initialised });
  });
  return null;
}

function bootstrap(opts: { initialised: boolean; signedIn: boolean }) {
  const seen: Snapshot[] = [];
  stubAuth(opts);
  render(
    <AuthProvider>
      <Probe seen={seen} />
    </AuthProvider>,
  );
  return seen;
}

afterEach(() => vi.unstubAllGlobals());

describe("auth bootstrap", () => {
  it("starts out loading, and says nothing about the session until it knows", () => {
    const seen = bootstrap({ initialised: true, signedIn: true });
    expect(seen[0]).toEqual({ signedIn: false, loading: true, initialised: true });
  });

  it("reports a signed-in session once all three answers are in", async () => {
    const seen = bootstrap({ initialised: true, signedIn: true });
    await waitFor(() => expect(settled(seen)).toEqual({
      signedIn: true,
      loading: false,
      initialised: true,
    }));
  });

  it("reports a fresh instance as not set up and nobody signed in", async () => {
    const seen = bootstrap({ initialised: false, signedIn: false });
    await waitFor(() => expect(settled(seen)).toEqual({
      signedIn: false,
      loading: false,
      initialised: false,
    }));
  });

  it("falls back to the sign-in form when the set-up check itself fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: unknown) => {
        const url = String(input);
        if (url.includes("/auth/status")) return reply(500, { detail: "boom" });
        if (url.includes("/auth/me")) return reply(401, { detail: "Not authenticated" });
        return reply(200, { csrf_token: "t" });
      }),
    );
    const seen: Snapshot[] = [];
    render(
      <AuthProvider>
        <Probe seen={seen} />
      </AuthProvider>,
    );
    // Assuming "already set up" is the safe guess: it offers sign-in rather than
    // inviting a second household to be created over the top of an existing one.
    await waitFor(() => expect(settled(seen)).toEqual({
      signedIn: false,
      loading: false,
      initialised: true,
    }));
  });

  it("publishes the session in one commit, never a half of it", async () => {
    // `initialised: false` is the telling case: it is the one answer that differs from
    // the starting assumption, so a consumer seeing it while `loading` is still true
    // could only have been handed a partly-applied bootstrap.
    const seen = bootstrap({ initialised: false, signedIn: false });
    await waitFor(() => expect(settled(seen)?.loading).toBe(false));
    expect(seen.filter((s) => s.loading && !s.initialised)).toEqual([]);
    expect(seen).toHaveLength(2);
  });
});
