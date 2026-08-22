import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../format";
import { usePeriod } from "../period/context";
import { PeriodPicker } from "../period/PeriodPicker";
import { PHONE, useMediaQuery, WIDE } from "../hooks/useMediaQuery";
import { SPA_VERSION } from "../version";
import { Sidebar } from "./Sidebar";


export function Layout({ children }: { children: ReactNode }) {
  const { me, logout } = useAuth();
  const { resolved } = usePeriod();
  const isOwner = me?.user.role === "owner";

  // Server version (polled) drives the "reload to update" nudge (Layer 3).
  const meta = useQuery({
    queryKey: ["meta"],
    queryFn: api.meta,
    refetchInterval: 300_000,
    refetchOnWindowFocus: true,
  });
  // Owner-only check against GitHub for a newer release (Layer 1).
  const update = useQuery({
    queryKey: ["update-check"],
    queryFn: () => api.updateCheck(),
    enabled: isOwner,
  });
  // Unread alert count drives a dot on the Alerts nav item.
  const notifs = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
    refetchInterval: 300_000,
  });

  const reloadNeeded = !!meta.data && meta.data.version !== SPA_VERSION;
  const updateAvailable = !!update.data?.update_available;
  const unread = notifs.data?.unread ?? 0;
  const wide = useMediaQuery(WIDE);
  const narrow = !useMediaQuery(PHONE);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  // A drawer left open while the window grows would otherwise leak into the wide
  // layout as a stuck overlay.
  useEffect(() => {
    if (wide) setOpen(false);
  }, [wide]);

  // is_current is a containment test, so "not current" is exactly past or future.
  // They need different words: one says the figures are stale, the other that
  // nothing has happened yet.
  const periodState = !resolved || resolved.is_current
    ? "current"
    : new Date(resolved.end) < new Date()
      ? "past"
      : "future";
  const range = resolved ? `${formatDate(resolved.start)} – ${formatDate(resolved.end)}` : "";
  const periodNote =
    periodState === "past"
      ? `Past period — ${range}. Not the current period.`
      : periodState === "future"
        ? `Future period — ${range}. Nothing has happened yet.`
        : range;

  const reloadBanner = reloadNeeded && (
    <div className="update-bar">
      A new version of Saiva is ready.
      <button className="btn btn-primary" onClick={() => window.location.reload()}>
        Reload
      </button>
    </div>
  );

  // One shell for every width now. Wide screens get the column in flow; narrower
  // ones get the same component as an off-canvas drawer, so there is a single
  // navigation in the document rather than one per breakpoint.
  return (
    <div className={wide ? "app app-wide" : "app app-narrow"} data-drawer={open || undefined}>
      <a className="skip-link" href="#content">
        Skip to content
      </a>
      <Sidebar
        unread={unread}
        updateAvailable={updateAvailable}
        household={me?.household.name}
        onSignOut={() => void logout()}
        onNavigate={close}
        drawer={!wide}
        open={open}
      />
      {!wide && open && <div className="scrim" />}
      <div className="main">
        <header className="appbar" data-period={periodState}>
          {!wide && (
            <button
              className="btn btn-ghost nav-toggle"
              aria-label="Menu"
              aria-expanded={open}
              onClick={() => setOpen(true)}
            >
              ☰
            </button>
          )}
          {!wide && <span className="brand-mark appbar-mark">≈</span>}
          <PeriodPicker />
          {resolved && !narrow && (
            <span className="appbar-range">
              {periodState !== "current" && <span aria-hidden="true">⚠ </span>}
              {periodNote}
            </span>
          )}
          <span className="appbar-spacer" />
          <span className="muted hide-mobile">{me?.household.name}</span>
        </header>
        {/* Below 640px the warning cannot be squeezed into a 52px bar, so it takes
            its own sticky band rather than being truncated away. */}
        {narrow && periodState !== "current" && (
          <div className="period-bar">
            <span aria-hidden="true">⚠ </span>
            {periodNote}
          </div>
        )}
        {reloadBanner}
        <main id="content" className="content">
          {children}
        </main>
      </div>
    </div>
  );
}
