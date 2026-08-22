import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import { AuthProvider } from "../auth/AuthContext";
import { PeriodProvider } from "../period/PeriodProvider";

/**
 * Renders a component with the providers the app shell needs.
 *
 * Retries are off and the cache is per-render, so a test never waits on a failed
 * fetch and never inherits another test's data.
 */
export function renderApp(
  ui: ReactElement,
  { route = "/" }: { route?: string } = {},
): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui, { wrapper: Wrapper });
}

/** Stub `fetch` so a component under test resolves its queries predictably. */
export function stubApi(byPath: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = String(input);
      const key = Object.keys(byPath).find((p) => url.startsWith(`/api${p}`));
      const body = key ? byPath[key] : {};
      return Promise.resolve({
        ok: true,
        status: 200,
        statusText: "OK",
        headers: { get: () => "application/json" },
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
        clone: () => ({ json: () => Promise.resolve(body) }),
      });
    }),
  );
}

/**
 * Renders the app shell with the real auth and period providers, so a shell test
 * exercises the same wiring the app does rather than a hand-made stand-in.
 */
export function renderShell(
  ui: ReactElement,
  { route = "/" }: { route?: string } = {},
): RenderResult {
  return renderApp(
    <AuthProvider>
      <PeriodProvider>{ui}</PeriodProvider>
    </AuthProvider>,
    { route },
  );
}
