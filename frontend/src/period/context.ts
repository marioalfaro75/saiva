import { createContext, useContext } from "react";

import type { PeriodOptions, ResolvedPeriod } from "../api/types";

export interface PeriodState {
  /** The selector every period-aware request carries, e.g. "fy:2024". */
  period: string;
  setPeriod: (value: string) => void;
  /** What the selector covers, once the server has resolved it. */
  resolved: ResolvedPeriod | null;
  options: PeriodOptions | undefined;
  /** True when the window has ended or has not started — the app is not showing "now". */
  isPast: boolean;
}

export const PeriodCtx = createContext<PeriodState | null>(null);

export function usePeriod(): PeriodState {
  const ctx = useContext(PeriodCtx);
  if (!ctx) throw new Error("usePeriod must be used inside PeriodProvider");
  return ctx;
}
