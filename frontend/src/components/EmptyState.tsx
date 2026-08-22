import type { ReactNode } from "react";

/**
 * What a page says when it has nothing to show.
 *
 * A greyed-out sentence reads like a system message; an empty state should read
 * like the page talking to you, and it should say what to do next — with links
 * rather than the name of a page you then have to go and find.
 */
export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card empty">
      <p className="empty-title">{title}</p>
      <p className="muted">{children}</p>
    </div>
  );
}
