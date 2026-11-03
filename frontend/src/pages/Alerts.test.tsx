import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NotificationSettings } from "../api/types";
import { renderApp } from "../testing/harness";
import { Alerts } from "./Alerts";

/**
 * The two threshold boxes are seeded from the server and then edited. That used to be
 * an effect copying the fetched settings into state, which meant any refetch landing
 * mid-edit — and saving the preferences above them causes one — wrote the stored value
 * back over whatever was in the box. They are now derived, with a typed value winning
 * over the fetched one, so these tests pin both halves of that: the stored number
 * shows up when nobody has touched it, and stops being authoritative once they have.
 */

const SETTINGS: NotificationSettings = {
  email_enabled: false,
  digest: "off",
  large_txn_threshold_cents: 10_000,
  low_balance_threshold_cents: 20_000,
  smtp_configured: true,
};

function json(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    clone: () => ({ json: () => Promise.resolve(body) }),
  });
}

/** Serves whatever `current` holds now, so a test can change it between fetches. */
function stubAlerts(current: { settings: NotificationSettings }) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = String(input);
      if (url.startsWith("/api/notifications/settings")) return json(current.settings);
      if (url.startsWith("/api/notifications")) return json({ unread: 0, items: [] });
      return json({});
    }),
  );
}

const largeTxnBox = () => screen.getByLabelText("Large transaction over");
const lowBalBox = () => screen.getByLabelText("Warn if projected balance below");

afterEach(() => vi.unstubAllGlobals());

describe("Alerts thresholds", () => {
  it("shows the stored thresholds, in dollars", async () => {
    stubAlerts({ settings: SETTINGS });
    renderApp(<Alerts />);
    await waitFor(() => expect(largeTxnBox()).toHaveValue("100"));
    expect(lowBalBox()).toHaveValue("200");
  });

  it("leaves the boxes empty rather than showing a zero before the settings arrive", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderApp(<Alerts />);
    expect(largeTxnBox()).toHaveValue("");
  });

  it("keeps what is being typed when the settings refetch underneath it", async () => {
    const current = { settings: SETTINGS };
    stubAlerts(current);
    renderApp(<Alerts />);
    await waitFor(() => expect(largeTxnBox()).toHaveValue("100"));

    // A new threshold, typed but not yet saved.
    fireEvent.change(largeTxnBox(), { target: { value: "250" } });

    // Meanwhile both stored thresholds change — another tab, another device, or simply
    // the server normalising what it was last sent.
    current.settings = {
      ...SETTINGS,
      large_txn_threshold_cents: 50_000,
      low_balance_threshold_cents: 90_000,
    };

    // Saving the email preferences invalidates these settings, so the refetch arrives
    // while the threshold box is still mid-edit.
    fireEvent.click(screen.getByLabelText("Email me alerts"));

    // The box nobody touched follows the server, which is also how we know the
    // refetch has landed and the assertion below is not just reading a stale render.
    await waitFor(() => expect(lowBalBox()).toHaveValue("900"));

    expect(largeTxnBox()).toHaveValue("250");
  });

  it("sends the typed thresholds, converted to cents", async () => {
    const current = { settings: SETTINGS };
    stubAlerts(current);
    renderApp(<Alerts />);
    await waitFor(() => expect(largeTxnBox()).toHaveValue("100"));

    fireEvent.change(largeTxnBox(), { target: { value: "250" } });
    fireEvent.change(lowBalBox(), { target: { value: "75.50" } });
    fireEvent.click(screen.getByRole("button", { name: "Save thresholds" }));

    await waitFor(() => {
      const patch = vi
        .mocked(fetch)
        .mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(String((patch![1] as RequestInit).body))).toEqual({
        large_txn_threshold_cents: 25_000,
        low_balance_threshold_cents: 7_550,
      });
    });
  });
});
