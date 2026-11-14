/**
 * The categories this browser picked most recently, newest first.
 *
 * A per-viewer convenience for CategoryPicker's "Recent" group, so it lives in this
 * browser's storage and nowhere else. Every read and write is guarded: a private window,
 * blocked site data or a hand-edited value means no recents, never an error.
 */

const RECENT_KEY = "saiva.recentCategories";
const RECENT_MAX = 5;

/** The categories picked most recently, newest first. A per-browser convenience only. */
export function readRecentCategories(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    // Private window, blocked storage, a hand-edited value: start with no recents.
    return [];
  }
}

export function rememberCategory(id: string): void {
  try {
    const next = [id, ...readRecentCategories().filter((x) => x !== id)].slice(0, RECENT_MAX);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // Not being able to remember is not worth an error: the picker works without it.
  }
}
