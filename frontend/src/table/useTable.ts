import { useCallback, useEffect, useMemo, useState } from "react";

import { EMPTY_STATE, loadTableState, saveTableState, type TableState } from "./persist";
import {
  type ColumnSpec,
  filterRows,
  nextSort,
  type SortState,
  sortRows,
} from "./sorting";

export interface UseTableOptions {
  /** Stable id used to remember how the table was left. Omit for transient tables
   *  (the import wizard) where restoring a filter would hide rows in a new file. */
  id?: string;
  /** Ordering applied before the user picks a column. */
  defaultSort?: SortState | null;
}

/**
 * What the header and filter-row components need. Implemented both by `useTable`
 * (rows held in memory) and by `useServerTable` (the paginated transactions list),
 * so the same markup drives either.
 */
export interface TableControls {
  sort: SortState | null;
  toggleSort: (key: string) => void;
  filters: Record<string, string>;
  setFilter: (key: string, value: string) => void;
  clearFilters: () => void;
  filtersOpen: boolean;
  toggleFilters: () => void;
  activeFilterCount: number;
  /** Rows after filtering, and rows before it — for a "12 of 340" count. */
  matched: number;
  total: number;
}

export interface Table<T> extends TableControls {
  /** Filtered and sorted rows to render. */
  rows: T[];
  columns: ColumnSpec<T>[];
}

/**
 * Sorting and per-column filtering for a table whose rows are all in memory.
 *
 * Not for server-paginated data: filtering a single page in the browser would look
 * like a real search while only ever considering the rows already fetched. The
 * transactions list therefore sorts and filters on the server instead.
 */
export function useTable<T>(
  rows: T[],
  columns: ColumnSpec<T>[],
  options: UseTableOptions = {},
): Table<T> {
  const { id, defaultSort = null } = options;
  const [state, setState] = useState<TableState>(() =>
    id ? loadTableState(id) : { ...EMPTY_STATE },
  );

  useEffect(() => {
    if (id) saveTableState(id, state);
  }, [id, state]);

  const toggleSort = useCallback((key: string) => {
    setState((s) => ({ ...s, sort: nextSort(s.sort, key) }));
  }, []);

  const setFilter = useCallback((key: string, value: string) => {
    setState((s) => {
      const filters = { ...s.filters };
      if (value === "") delete filters[key];
      else filters[key] = value;
      return { ...s, filters };
    });
  }, []);

  const clearFilters = useCallback(() => setState((s) => ({ ...s, filters: {} })), []);
  const toggleFilters = useCallback(
    () => setState((s) => ({ ...s, filtersOpen: !s.filtersOpen })),
    [],
  );

  const sort = state.sort ?? defaultSort;
  const visible = useMemo(() => {
    const filtered = filterRows(rows, columns, state.filters);
    if (!sort) return filtered;
    return sortRows(filtered, columns.find((c) => c.key === sort.key), sort.dir);
  }, [rows, columns, state.filters, sort]);

  return {
    rows: visible,
    columns,
    // Report the effective ordering so headers show an arrow on the default column.
    sort,
    toggleSort,
    filters: state.filters,
    setFilter,
    clearFilters,
    filtersOpen: state.filtersOpen,
    toggleFilters,
    activeFilterCount: Object.keys(state.filters).length,
    matched: visible.length,
    total: rows.length,
  };
}
