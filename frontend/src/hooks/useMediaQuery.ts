import { useCallback, useSyncExternalStore } from "react";

/**
 * Tracks a media query in JS.
 *
 * Used where the two layouts differ structurally rather than cosmetically: the
 * alternative is rendering both shells and hiding one with CSS, which would put
 * every navigation link in the document twice and announce them twice.
 *
 * `matchMedia` is an external store, so it is read with the hook built for one
 * rather than copied into state by an effect: the value is sampled during render
 * instead of one render late, and a width that changes between the first render
 * and the subscription cannot be missed.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia?.(query);
      if (!mql) return () => {};
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    [query],
  );

  const read = useCallback(() => matchesAtMount(query), [query]);

  // No server render, but useSyncExternalStore insists on the third argument; the
  // narrow layout is the right thing to assume where no viewport can be measured.
  return useSyncExternalStore(subscribe, read, () => false);
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
