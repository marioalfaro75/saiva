import "@testing-library/jest-dom";

// jsdom implements no ResizeObserver, and Recharts' ResponsiveContainer constructs
// one on mount. Without this any test that renders a chart throws asynchronously.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
