import type { ReactNode } from "react";

/**
 * The heading block every page opens with.
 *
 * Three things used to sit directly in the flex row — the title, a line of status
 * text, and an action button — so the title's vertical alignment depended on which
 * of them a page happened to have. Here the title and its subtitle are one column
 * and everything else is another, which aligns the same way on every page.
 *
 * `sub` is for pages whose name does not say what they are for. It answers the
 * question in one line rather than leaving it to be inferred from the contents.
 */
export function PageHead({
  title,
  sub,
  children,
}: {
  title: ReactNode;
  sub?: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div className="page-title">
        <h1>{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {children && <div className="page-actions">{children}</div>}
    </div>
  );
}
