import { type RefObject, useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Shared behaviour for anything that opens over the page — the categorise dialog
 * today, the navigation drawer next.
 *
 * Dismissal fires only when the gesture *started* outside. Closing on a plain click
 * meant selecting text inside a field and releasing beyond the edge threw the edit
 * away, in the app's most repeated workflow.
 */
export function useDismissable<T extends HTMLElement>(
  open: boolean,
  onClose: () => void,
): RefObject<T> {
  const ref = useRef<T>(null);
  const returnTo = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    const panel = ref.current;
    returnTo.current = document.activeElement;

    // Move focus in, so the keyboard is inside the thing that just opened.
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel)?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel) return;
      // Keep Tab inside; otherwise focus walks the page behind the overlay.
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const [head, tail] = [items[0], items[items.length - 1]];
      if (e.shiftKey && document.activeElement === head) {
        e.preventDefault();
        tail.focus();
      } else if (!e.shiftKey && document.activeElement === tail) {
        e.preventDefault();
        head.focus();
      }
    };

    // Remember where the gesture began so a drag ending outside doesn't dismiss.
    let startedOutside = false;
    const onPointerDown = (e: PointerEvent) => {
      startedOutside = !!panel && !panel.contains(e.target as Node);
    };
    const onPointerUp = (e: PointerEvent) => {
      if (startedOutside && panel && !panel.contains(e.target as Node)) onClose();
      startedOutside = false;
    };

    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("pointerup", onPointerUp, true);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointerup", onPointerUp, true);
      document.body.style.overflow = previousOverflow;
      // Send focus back where it came from, so dismissing doesn't lose the user's place.
      (returnTo.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onClose]);

  return ref;
}
