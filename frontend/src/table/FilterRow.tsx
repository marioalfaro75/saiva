import type { TableControls } from "./useTable";

/**
 * A row of per-column filter inputs, sitting under the header. Columns without a
 * filterable value (checkboxes, action buttons) get an empty cell so the row still
 * lines up with the header above it.
 */
export function FilterRow({
  table,
  labels,
  columns,
}: {
  table: TableControls;
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

/** The filter toggle and, when filtering, how much of the table is being shown.
 *  `count` overrides the row tally for tables the server paginates, where only the
 *  number of matching rows is known. */
export function FilterToggle({ table, count }: { table: TableControls; count?: number }) {
  const filtering = table.activeFilterCount > 0;
  return (
    <span className="filter-toggle">
      {filtering && (
        <span className="muted">
          {/* A server-paginated table only knows how many rows match, not the
              unfiltered total, so it reports the one figure it has. */}
          {count !== undefined
            ? `${count} matching`
            : `${table.matched} of ${table.total}`}
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
