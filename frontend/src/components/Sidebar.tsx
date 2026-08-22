import { NavLink } from "react-router-dom";

import { useDismissable } from "../hooks/useDismissable";
import { CLUSTERS, SETTINGS } from "./navItems";

interface Props {
  /** Unread alerts. Shown as a count — a vertical row has space for the number,
   *  and "3" tells you more than a dot. */
  unread: number;
  updateAvailable: boolean;
  household?: string;
  onSignOut: () => void;
  /** Called after following a link, so the drawer can close itself. */
  onNavigate?: () => void;
  /** Set when the sidebar is an off-canvas drawer rather than a persistent column. */
  drawer?: boolean;
  open?: boolean;
}

export function Sidebar({
  unread,
  updateAvailable,
  household,
  onSignOut,
  onNavigate,
  drawer = false,
  open = false,
}: Props) {
  // Escape, focus trap, focus return and scroll lock — the same behaviour the
  // categorise dialog uses, so there is one overlay implementation, not two.
  const panel = useDismissable<HTMLElement>(drawer && open, onNavigate ?? (() => {}));
  return (
    <nav
      className={drawer ? "sidebar sidebar-drawer" : "sidebar"}
      aria-label="Main"
      ref={drawer ? panel : undefined}
      {...(drawer ? { "aria-hidden": !open, inert: open ? undefined : "" } : {})}
    >
      <div className="sidebar-brand">
        <span className="brand-mark">≈</span> Saiva
      </div>

      <div className="sidebar-items">
        {CLUSTERS.map((cluster, i) => (
          <div className="sidebar-cluster" key={i}>
            {cluster.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className="side-link"
                onClick={onNavigate}
              >
                <span>{item.label}</span>
                {item.to === "/alerts" && unread > 0 && (
                  <span className="badge">
                    {unread}
                    <span className="sr-only"> unread</span>
                  </span>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </div>

      <div className="sidebar-foot">
        <NavLink to={SETTINGS.to} className="side-link" onClick={onNavigate}>
          <span>{SETTINGS.label}</span>
          {updateAvailable && (
            <span className="dot">
              <span className="sr-only">Update available</span>
            </span>
          )}
        </NavLink>
        {household && <div className="sidebar-household muted">{household}</div>}
        <button className="btn btn-ghost sidebar-signout" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </nav>
  );
}
