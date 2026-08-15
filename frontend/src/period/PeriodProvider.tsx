import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { PeriodCtx } from "./context";

const STORAGE_KEY = "saiva.period";
/** Used until the server says which financial year is current. */
const FALLBACK = "this_fy";

export function PeriodProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  // The URL wins on load so a shared or bookmarked link opens on its period;
  // otherwise pick up where this browser left off.
  const [period, setPeriodState] = useState<string>(
    () => searchParams.get("period") || localStorage.getItem(STORAGE_KEY) || FALLBACK,
  );

  const options = useQuery({ queryKey: ["period-options"], queryFn: api.periodOptions });
  const resolved = useQuery({
    queryKey: ["period-resolve", period],
    queryFn: () => api.resolvePeriod(period),
  });

  // Once the catalogue arrives, turn the placeholder into the concrete current year
  // so the picker has something selected and links carry an explicit period.
  useEffect(() => {
    if (period === FALLBACK && options.data) setPeriodState(options.data.default);
  }, [period, options.data]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, period);
    if (searchParams.get("period") !== period) {
      const next = new URLSearchParams(searchParams);
      next.set("period", period);
      setSearchParams(next, { replace: true });
    }
  }, [period, searchParams, setSearchParams]);

  const setPeriod = useCallback((value: string) => setPeriodState(value), []);

  return (
    <PeriodCtx.Provider
      value={{
        period,
        setPeriod,
        resolved: resolved.data ?? null,
        options: options.data,
        isPast: resolved.data ? !resolved.data.is_current : false,
      }}
    >
      {children}
    </PeriodCtx.Provider>
  );
}
