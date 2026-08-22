import "@testing-library/jest-dom";

// jsdom implements no ResizeObserver, and Recharts' ResponsiveContainer constructs
// one on mount. Without this any test that renders a chart throws asynchronously.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// jsdom has no matchMedia. Default to a narrow viewport and let tests widen it via
// setViewport(); without this every media-query branch silently takes one path.
let viewportWidth = 800;

export function setViewport(width: number): void {
  viewportWidth = width;
}

globalThis.matchMedia ??= ((query: string) => {
  const min = /min-width:\s*(\d+)px/.exec(query);
  const matches = min ? viewportWidth >= Number(min[1]) : false;
  return {
    matches,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  } as MediaQueryList;
}) as typeof matchMedia;
