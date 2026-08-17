import type { ReactNode } from "react";

import type { Table } from "./useTable";

/**
 * A sortable column heading. The clickable part is a real button so the column can be
 * sorted from the keyboard, and `aria-sort` tells a screen reader the current order.
 */
export function SortHeader<T>({
  table,
  col,
  numeric,
  children,
}: {
  table: Table<T>;
  col: string;
  numeric?: boolean;
  children: ReactNode;
}) {
  const active = table.sort?.key === col;
  const dir = active ? table.sort?.dir : undefined;
  return (
    <th
      className={numeric ? "num sortable" : "sortable"}
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button type="button" className="sort-btn" onClick={() => table.toggleSort(col)}>
        {children}
        <span className={`sort-arrow${active ? " on" : ""}`} aria-hidden="true">
          {active ? (dir === "asc" ? "▲" : "▼") : "▾"}
        </span>
      </button>
    </th>
  );
}
