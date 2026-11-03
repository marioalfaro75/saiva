import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

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

/** What the three bootstrap requests between them establish. */
interface Session {
  me: Me | null;
  loading: boolean;
  /** Whether a household exists yet; assumed until the server says otherwise, so a
   *  failed check offers the sign-in form rather than the create-household one. */
  initialised: boolean;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({
    me: null,
    loading: true,
    initialised: true,
  });

  // Bootstrap: a CSRF token, whether the instance has been set up, and who is signed
  // in. Published as one state in one commit when all three have settled, rather than
  // as each lands: nothing renders anything but "Loading…" until `loading` clears, so
  // the intermediate states were never worth a render — and setting state per request
  // meant the first of them ran synchronously inside the effect, cascading renders
  // (react-hooks/set-state-in-effect).
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        await api.csrf();
      } catch {
        // CSRF token will be re-fetched on demand.
      }
      let initialised: boolean;
      try {
        initialised = (await api.status()).initialised;
      } catch {
        initialised = true;
      }
      let me: Me | null;
      try {
        me = await api.me();
      } catch {
        me = null;
      }
      if (live) setSession({ me, loading: false, initialised });
    })();
    return () => {
      live = false;
    };
  }, []);

  const setMe = (me: Me | null) => setSession((s) => ({ ...s, me }));

  const login = async (email: string, password: string) => {
    setMe(await api.login(email, password));
  };
  const setup = async (body: SetupBody) => {
    const me = await api.setup(body);
    setSession((s) => ({ ...s, me, initialised: true }));
  };
  const logout = async () => {
    await api.logout();
    setMe(null);
    await api.csrf();
  };

  return (
    <AuthContext.Provider
      value={{
        me: session.me,
        loading: session.loading,
        initialised: session.initialised,
        login,
        setup,
        logout,
      }}
    >
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
