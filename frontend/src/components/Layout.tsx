import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../format";
import { usePeriod } from "../period/context";
import { PeriodPicker } from "../period/PeriodPicker";
import { useMediaQuery, WIDE } from "../hooks/useMediaQuery";
import { SPA_VERSION } from "../version";
import { Sidebar } from "./Sidebar";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/insights", label: "Insights", end: false },
  { to: "/advisor", label: "Advisor", end: false },
  { to: "/alerts", label: "Alerts", end: false },
  { to: "/transactions", label: "Transactions", end: false },
  { to: "/accounts", label: "Accounts", end: false },
  { to: "/budgets", label: "Budgets", end: false },
  { to: "/bills", label: "Bills", end: false },
  { to: "/forecast", label: "Forecast", end: false },
  { to: "/net-worth", label: "Net worth", end: false },
  { to: "/goals", label: "Goals", end: false },
  { to: "/benchmarks", label: "Benchmarks", end: false },
  { to: "/import", label: "Import", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export function Layout({ children }: { children: ReactNode }) {
  const { me, logout } = useAuth();
  const { resolved, isPast } = usePeriod();
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

  // The banners are identical in both shells; only the navigation differs.
  const banners = (
    <>
      {isPast && resolved && (
        // Past and future windows look identical to the current one at a glance, so
        // say plainly that these figures are not today's.
        <div className="period-bar">
          Viewing <strong>{resolved.label}</strong> ({formatDate(resolved.start)} –{" "}
          {formatDate(resolved.end)}) — not the current period.
        </div>
      )}

      {reloadNeeded && (
        <div className="update-bar">
          A new version of Saiva is ready.
          <button className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      )}
    </>
  );

  // One shell at a time, chosen in JS: rendering both and hiding one with CSS would
  // put all fourteen links in the document twice. Below 1080px the original top bar
  // is untouched — the drawer that replaces it lands in its own step.
  if (wide) {
    return (
      <div className="app app-wide">
        <a className="skip-link" href="#content">
          Skip to content
        </a>
        <Sidebar
          unread={unread}
          updateAvailable={updateAvailable}
          household={me?.household.name}
          onSignOut={() => void logout()}
        />
        <div className="main">
          <header className="appbar">
            <PeriodPicker />
          </header>
          {banners}
          <main id="content" className="content">
            {children}
          </main>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <a className="skip-link" href="#content">Skip to content</a>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">≈</span> Saiva
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className="nav-link">
              {item.label}
              {item.to === "/settings" && updateAvailable && (
                <span className="dot" title="Update available" />
              )}
              {item.to === "/alerts" && unread > 0 && (
                <span className="dot" title={`${unread} unread`} />
              )}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-right">
          <PeriodPicker />
          <span className="muted hide-mobile">{me?.household.name}</span>
          <button className="btn btn-ghost" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </header>

      {banners}

      <main id="content" className="content">{children}</main>
    </div>
  );
}
