import type { SortState } from "./sorting";

/** How a table was last left: which column it was sorted by and what was filtered. */
export interface TableState {
  sort: SortState | null;
  filters: Record<string, string>;
  filtersOpen: boolean;
}

export const EMPTY_STATE: TableState = { sort: null, filters: {}, filtersOpen: false };

const key = (id: string) => `saiva.table.${id}`;

function isSort(value: unknown): value is SortState {
  if (typeof value !== "object" || value === null) return false;
  const s = value as Partial<SortState>;
  return typeof s.key === "string" && (s.dir === "asc" || s.dir === "desc");
}

/**
 * Restore a table's state. Anything malformed — hand-edited storage, or a shape from
 * an older build — is discarded rather than thrown, so a bad entry can never stop a
 * page rendering.
 */
export function loadTableState(id: string): TableState {
  try {
    const raw = localStorage.getItem(key(id));
    if (!raw) return EMPTY_STATE;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return EMPTY_STATE;
    const { sort, filters, filtersOpen } = parsed as Partial<TableState>;
    const cleanFilters: Record<string, string> = {};
    if (typeof filters === "object" && filters !== null) {
      for (const [k, v] of Object.entries(filters)) {
        if (typeof v === "string" && v !== "") cleanFilters[k] = v;
      }
    }
    return {
      sort: isSort(sort) ? sort : null,
      filters: cleanFilters,
      // Reopen the filter row when something is being filtered, so restored state is
      // never invisibly hiding rows.
      filtersOpen: filtersOpen === true || Object.keys(cleanFilters).length > 0,
    };
  } catch {
    return EMPTY_STATE;
  }
}

export function saveTableState(id: string, state: TableState): void {
  try {
    localStorage.setItem(key(id), JSON.stringify(state));
  } catch {
    // Storage full or blocked (private browsing): sorting still works for this visit.
  }
}
