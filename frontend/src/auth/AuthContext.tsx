import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { api } from "../api/client";
import type { Me, SetupBody } from "../api/types";

interface AuthValue {
  me: Me | null;
  loading: boolean;
  initialised: boolean;
  login: (email: string, password: string) => Promise<void>;
  setup: (body: SetupBody) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialised, setInitialised] = useState(true);

  const bootstrap = useCallback(async () => {
    setLoading(true);
    try {
      await api.csrf();
    } catch {
      // CSRF token will be re-fetched on demand.
    }
    try {
      const status = await api.status();
      setInitialised(status.initialised);
    } catch {
      setInitialised(true);
    }
    try {
      setMe(await api.me());
    } catch {
      setMe(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const login = async (email: string, password: string) => {
    setMe(await api.login(email, password));
  };
  const setup = async (body: SetupBody) => {
    setMe(await api.setup(body));
    setInitialised(true);
  };
  const logout = async () => {
    await api.logout();
    setMe(null);
    await api.csrf();
  };

  return (
    <AuthContext.Provider value={{ me, loading, initialised, login, setup, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
