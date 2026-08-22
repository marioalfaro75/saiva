import { type FormEvent, useId, useState } from "react";

import { ApiError } from "../api/client";
import type { SetupBody } from "../api/types";
import { useAuth } from "../auth/AuthContext";

type Basis = "calendar" | "weekly" | "fortnightly" | "monthly";

export function Login({ initialised }: { initialised: boolean }) {
  const fid = useId();
  const { login, setup } = useAuth();
  const [householdName, setHouseholdName] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [periodBasis, setPeriodBasis] = useState<Basis>("calendar");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (initialised) {
        await login(email, password);
      } else {
        const body: SetupBody = {
          household_name: householdName,
          name,
          email,
          password,
          period_basis: periodBasis,
        };
        await setup(body);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="screen-centre">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark">≈</span> Saiva
        </div>
        <p className="muted">
          {initialised ? "Sign in to your household." : "Create your household to get started."}
        </p>
        {error && <div className="error">{error}</div>}

        {!initialised && (
          <>
            <div className="field">
              <label htmlFor={`${fid}-household-name`}>Household name</label>
              <input
                id={`${fid}-household-name`}
                value={householdName}
                onChange={(e) => setHouseholdName(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-your-name`}>Your name</label>
              <input
                id={`${fid}-your-name`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor={`${fid}-budget-period`}>Budget period</label>
              <select
                id={`${fid}-budget-period`}
                value={periodBasis}
                onChange={(e) => setPeriodBasis(e.target.value as Basis)}
              >
                <option value="calendar">Calendar months</option>
                <option value="weekly">Weekly pay cycle</option>
                <option value="fortnightly">Fortnightly pay cycle</option>
                <option value="monthly">Monthly pay cycle</option>
              </select>
            </div>
          </>
        )}

        <div className="field">
          <label htmlFor={`${fid}-email`}>Email</label>
          <input
            id={`${fid}-email`}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor={`${fid}-password`}>Password</label>
          <input id={`${fid}-password`}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={initialised ? undefined : 10}
          />
        </div>
        <button className="btn btn-primary" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Please wait…" : initialised ? "Sign in" : "Create household"}
        </button>
      </form>
    </div>
  );
}
