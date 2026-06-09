import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { SPA_VERSION } from "../version";

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

  return (
    <div className="app">
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
          <span className="muted hide-mobile">{me?.household.name}</span>
          <button className="btn btn-ghost" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </header>

      {reloadNeeded && (
        <div className="update-bar">
          A new version of Saiva is ready.
          <button className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      )}

      <main className="content">{children}</main>
    </div>
  );
}
