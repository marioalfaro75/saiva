import type { Table } from "./useTable";

/**
 * A row of per-column filter inputs, sitting under the header. Columns without a
 * filterable value (checkboxes, action buttons) get an empty cell so the row still
 * lines up with the header above it.
 */
export function FilterRow<T>({
  table,
  labels,
  columns,
}: {
  table: Table<T>;
  /** Human name per column key, used for the input's accessible label. */
  labels: Record<string, string>;
  /** Column keys in render order; `null` leaves a cell empty. */
  columns: (string | null)[];
}) {
  if (!table.filtersOpen) return null;
  return (
    <tr className="filter-row">
      {columns.map((key, i) => (
        <td key={key ?? `blank-${i}`}>
          {key && (
            <input
              value={table.filters[key] ?? ""}
              aria-label={`Filter ${labels[key] ?? key}`}
              placeholder="Filter…"
              onChange={(e) => table.setFilter(key, e.target.value)}
            />
          )}
        </td>
      ))}
    </tr>
  );
}

/** The filter toggle and, when filtering, how much of the table is being shown. */
export function FilterToggle<T>({ table }: { table: Table<T> }) {
  const filtering = table.activeFilterCount > 0;
  return (
    <span className="filter-toggle">
      {filtering && (
        <span className="muted">
          {table.matched} of {table.total}
        </span>
      )}
      <button
        type="button"
        className="btn btn-ghost"
        aria-expanded={table.filtersOpen}
        onClick={table.toggleFilters}
      >
        Filter{filtering ? ` (${table.activeFilterCount})` : ""}
      </button>
      {filtering && (
        <button type="button" className="btn btn-ghost" onClick={table.clearFilters}>
          Clear
        </button>
      )}
    </span>
  );
}
