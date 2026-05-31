import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/transactions", label: "Transactions", end: false },
  { to: "/accounts", label: "Accounts", end: false },
  { to: "/import", label: "Import", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export function Layout({ children }: { children: ReactNode }) {
  const { me, logout } = useAuth();

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
      <main className="content">{children}</main>
    </div>
  );
}
