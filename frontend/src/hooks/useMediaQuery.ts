import { useEffect, useState } from "react";

/**
 * Tracks a media query in JS.
 *
 * Used where the two layouts differ structurally rather than cosmetically: the
 * alternative is rendering both shells and hiding one with CSS, which would put
 * every navigation link in the document twice and announce them twice.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia?.(query).matches === true,
  );

  useEffect(() => {
    const mql = window.matchMedia?.(query);
    if (!mql) return;
    setMatches(mql.matches);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** Width at which the navigation becomes a persistent sidebar. */
export const WIDE = "(min-width: 1080px)";

/** Above this the app bar has room for the period range beside the picker. */
export const PHONE = "(min-width: 640px)";

/**
 * Evaluates a query once, for state that must be decided at mount and then left
 * alone — a page size, for instance: recomputing it on resize would renumber the
 * pages under someone who is midway through reviewing them.
 */
export function matchesAtMount(query: string): boolean {
  return typeof window !== "undefined" && window.matchMedia?.(query).matches === true;
}
