import "@testing-library/jest-dom";
import { act } from "@testing-library/react";

// jsdom implements no ResizeObserver, and Recharts' ResponsiveContainer constructs
// one on mount. Without this any test that renders a chart throws asynchronously.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// jsdom has no matchMedia. Default to a narrow viewport, let tests widen it with
// setViewport(), and notify listeners so components that react to a resize can be
// tested rather than only their initial state.
let viewportWidth = 800;
const listeners = new Set<{ query: string; fn: (e: MediaQueryListEvent) => void }>();

const evaluate = (query: string): boolean => {
  const min = /min-width:\s*(\d+)px/.exec(query);
  const max = /max-width:\s*(\d+)px/.exec(query);
  if (min) return viewportWidth >= Number(min[1]);
  if (max) return viewportWidth <= Number(max[1]);
  return false;
};

export function setViewport(width: number): void {
  viewportWidth = width;
  // A resize is a real event that re-renders whatever is listening, so it belongs
  // inside `act` — otherwise every width change warns about an unwrapped update.
  act(() => {
    for (const { query, fn } of listeners) {
      fn({ matches: evaluate(query), media: query } as MediaQueryListEvent);
    }
  });
}

globalThis.matchMedia ??= ((query: string) => {
  const entry = { query, fn: (_e: MediaQueryListEvent) => {} };
  return {
    get matches() {
      return evaluate(query);
    },
    media: query,
    onchange: null,
    addEventListener: (_t: string, fn: (e: MediaQueryListEvent) => void) => {
      entry.fn = fn;
      listeners.add(entry);
    },
    removeEventListener: () => {
      listeners.delete(entry);
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  } as unknown as MediaQueryList;
}) as typeof matchMedia;
