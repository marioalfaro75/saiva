import { usePeriod } from "./context";

/**
 * The app-wide period selector. Financial years, their quarters and months all come
 * from the household's own FY settings, so a July–June household sees quarters
 * starting in July and a calendar-year one sees them starting in January.
 */
export function PeriodPicker() {
  const { period, setPeriod, options } = usePeriod();
  if (!options) return null;

  // Quarters and months are only useful for a year you can see, so show them for
  // the selected one (or the current year when a relative period is selected).
  const selectedYear =
    options.financial_years.find(
      (y) => y.value === period || y.quarters.concat(y.months).some((o) => o.value === period),
    ) ?? options.financial_years.find((y) => y.value === options.default);

  return (
    <select
      className="pill-select period-picker"
      value={period}
      aria-label="Period"
      onChange={(e) => setPeriod(e.target.value)}
    >
      <optgroup label="Financial years">
        {options.financial_years.map((y) => (
          <option key={y.value} value={y.value}>
            {y.label}
          </option>
        ))}
      </optgroup>
      {selectedYear && (
        <>
          <optgroup label={`Quarters — ${selectedYear.label}`}>
            {selectedYear.quarters.map((q) => (
              <option key={q.value} value={q.value}>
                {q.label}
              </option>
            ))}
          </optgroup>
          <optgroup label={`Months — ${selectedYear.label}`}>
            {selectedYear.months.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </optgroup>
        </>
      )}
      <optgroup label="Relative">
        {options.relative.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
        <option value="all">All time</option>
      </optgroup>
    </select>
  );
}
