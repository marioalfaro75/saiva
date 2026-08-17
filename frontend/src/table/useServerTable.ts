import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { nextSort, type SortState } from "./sorting";
import type { TableControls } from "./useTable";

export interface ServerTable extends TableControls {
  /** Query params to pass to the API, which does the ordering and filtering. */
  params: Record<string, string | undefined>;
}

/**
 * Sort and filter state for a table the server paginates.
 *
 * The rows in the browser are one page of a larger set, so ordering and filtering
 * have to be sent to the API — doing either locally would quietly answer for the
 * fetched page while looking like it answered for everything.
 *
 * State lives in the URL, next to the period, so a sorted and filtered view can be
 * reloaded, shared or reached with the back button.
 */
export function useServerTable(filterKeys: string[]): ServerTable {
  const [searchParams, setSearchParams] = useSearchParams();

  const sortKey = searchParams.get("sort");
  const sortDir = searchParams.get("dir") === "desc" ? "desc" : "asc";
  // Memoised so the object identity is stable between renders; otherwise every
  // callback depending on it would be rebuilt and refetch the list.
  const sort = useMemo<SortState | null>(
    () => (sortKey ? { key: sortKey, dir: sortDir } : null),
    [sortKey, sortDir],
  );

  const filters = useMemo(() => {
    const out: Record<string, string> = {};
    for (const key of filterKeys) {
      const value = searchParams.get(`f_${key}`);
      if (value) out[key] = value;
    }
    return out;
  }, [searchParams, filterKeys]);

  const update = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      const next = new URLSearchParams(searchParams);
      mutate(next);
      // Any change to ordering or filtering invalidates the page number.
      next.delete("page");
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const toggleSort = useCallback(
    (key: string) =>
      update((p) => {
        const next = nextSort(sort, key);
        if (!next) {
          p.delete("sort");
          p.delete("dir");
        } else {
          p.set("sort", next.key);
          p.set("dir", next.dir);
        }
      }),
    [update, sort],
  );

  const setFilter = useCallback(
    (key: string, value: string) =>
      update((p) => (value ? p.set(`f_${key}`, value) : p.delete(`f_${key}`))),
    [update],
  );

  const clearFilters = useCallback(
    () => update((p) => filterKeys.forEach((k) => p.delete(`f_${k}`))),
    [update, filterKeys],
  );

  // Opening the filter row is view state, not something worth putting in a shareable
  // link — but it must start open when the link already carries filters.
  const explicitlyOpen = searchParams.get("filters") === "1";
  const filtersOpen = explicitlyOpen || Object.keys(filters).length > 0;
  const toggleFilters = useCallback(
    () => update((p) => (filtersOpen ? p.delete("filters") : p.set("filters", "1"))),
    [update, filtersOpen],
  );

  const params = useMemo(() => {
    const out: Record<string, string | undefined> = {
      sort: sort?.key,
      dir: sort ? sort.dir : undefined,
    };
    for (const [key, value] of Object.entries(filters)) out[`f_${key}`] = value;
    return out;
  }, [sort, filters]);

  return {
    sort,
    toggleSort,
    filters,
    setFilter,
    clearFilters,
    filtersOpen,
    toggleFilters,
    activeFilterCount: Object.keys(filters).length,
    // The row count comes from the response, so it is supplied where it is rendered.
    matched: 0,
    total: 0,
    params,
  };
}
