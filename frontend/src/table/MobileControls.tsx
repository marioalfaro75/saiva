import type { TableControls } from "./useTable";

/**
 * Phone presentations of the same `TableControls` a table header uses.
 *
 * These are siblings of `SortHeader` and `FilterRow`, not replacements: the sorting
 * hook renders nothing and only reads and writes URL or component state, so a chip
 * calling `toggleSort` runs the identical code path as a column header, including
 * the ascending → descending → off cycle. Nothing in the shared table code is
 * forked or special-cased for mobile.
 */

const ARROW = { asc: "▲", desc: "▼" } as const;

export function SortChips({
  table,
  labels,
  columns,
}: {
  table: TableControls;
  labels: Record<string, string>;
  /** Column keys, in the order the chips should read. */
  columns: string[];
}) {
  return (
    <div className="sort-chips" role="group" aria-label="Sort by">
      {columns.map((key) => {
        const active = table.sort?.key === key;
        return (
          <button
            key={key}
            type="button"
            className={`chip${active ? " on" : ""}`}
            aria-pressed={active}
            onClick={() => table.toggleSort(key)}
          >
            {labels[key] ?? key}
            {active && <span aria-hidden="true"> {ARROW[table.sort!.dir]}</span>}
            {active && <span className="sr-only">, sorted {table.sort!.dir}ending</span>}
          </button>
        );
      })}
    </div>
  );
}

/** The filter row's fields, stacked instead of laid out across a table row. */
export function FilterFields({
  table,
  labels,
  columns,
}: {
  table: TableControls;
  labels: Record<string, string>;
  columns: string[];
}) {
  if (!table.filtersOpen) return null;
  return (
    <div className="filter-fields">
      {columns.map((key) => (
        <div className="field" key={key}>
          <label htmlFor={`f-${key}`}>{labels[key] ?? key}</label>
          <input
            id={`f-${key}`}
            value={table.filters[key] ?? ""}
            aria-label={`Filter ${labels[key] ?? key}`}
            placeholder="Filter…"
            onChange={(e) => table.setFilter(key, e.target.value)}
          />
        </div>
      ))}
    </div>
  );
}
