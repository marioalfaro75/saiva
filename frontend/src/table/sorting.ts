/**
 * Sorting and per-column filtering shared by every table in the app.
 *
 * Columns declare how to read a row rather than how to draw it, so a table keeps its
 * own markup — inline edit forms, checkboxes, popovers and action buttons all stay
 * exactly as they are.
 */

/** A value a column can be ordered by. `null` means "no value", and sorts last. */
export type SortValue = string | number | boolean | null | undefined;

export interface ColumnSpec<T> {
  key: string;
  /** The underlying value to order by — a number or date, never the rendered string,
   *  so $1,000 does not sort before $9 and dates sort chronologically. */
  sort?: (row: T) => SortValue;
  /** Text a per-column filter matches against. Defaults to the sort value. */
  text?: (row: T) => string;
}

export type SortDir = "asc" | "desc";

export interface SortState {
  key: string;
  dir: SortDir;
}

const collator = new Intl.Collator("en-AU", { numeric: true, sensitivity: "base" });

/**
 * Order two column values. Blanks always sort last regardless of direction — an
 * uncategorised row topping the list on the first click is never what was wanted.
 * Strings compare with a numeric-aware collator so "Item 2" precedes "Item 10".
 */
export function compareValues(a: SortValue, b: SortValue, dir: SortDir): number {
  const aBlank = a === null || a === undefined || a === "";
  const bBlank = b === null || b === undefined || b === "";
  if (aBlank || bBlank) return aBlank && bBlank ? 0 : aBlank ? 1 : -1;

  let result: number;
  if (typeof a === "number" && typeof b === "number") result = a - b;
  else if (typeof a === "boolean" && typeof b === "boolean")
    result = Number(a) - Number(b);
  else result = collator.compare(String(a), String(b));

  return dir === "asc" ? result : -result;
}

/** Sort a copy of `rows`. Stable, so rows that compare equal keep their prior order. */
export function sortRows<T>(rows: T[], column: ColumnSpec<T> | undefined, dir: SortDir): T[] {
  if (!column?.sort) return rows;
  const read = column.sort;
  return [...rows].sort((x, y) => compareValues(read(x), read(y), dir));
}

/** The text a column's filter matches against. */
export function columnText<T>(column: ColumnSpec<T>, row: T): string {
  if (column.text) return column.text(row);
  const value = column.sort?.(row);
  return value === null || value === undefined ? "" : String(value);
}

/**
 * Keep rows matching every active filter. Filters combine with AND, and each is a
 * case-insensitive substring — so "45" in an amount column finds $45.00 and $145.00.
 */
export function filterRows<T>(
  rows: T[],
  columns: ColumnSpec<T>[],
  filters: Record<string, string>,
): T[] {
  const active = Object.entries(filters)
    .map(([key, term]) => [columns.find((c) => c.key === key), term.trim().toLowerCase()] as const)
    .filter((entry): entry is readonly [ColumnSpec<T>, string] => !!entry[0] && entry[1] !== "");
  if (active.length === 0) return rows;
  return rows.filter((row) =>
    active.every(([column, term]) => columnText(column, row).toLowerCase().includes(term)),
  );
}

/**
 * Next sort state for a header click: ascending, then descending, then back to the
 * table's natural order, so a column can always be un-sorted again.
 */
export function nextSort(current: SortState | null, key: string): SortState | null {
  if (current?.key !== key) return { key, dir: "asc" };
  if (current.dir === "asc") return { key, dir: "desc" };
  return null;
}
