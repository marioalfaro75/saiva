import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { Accounts } from "./pages/Accounts";
import { Budgets } from "./pages/Budgets";
import { ImportPage } from "./pages/Import";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Settings } from "./pages/Settings";
import { Transactions } from "./pages/Transactions";

export default function App() {
  const { me, loading, initialised } = useAuth();

  if (loading) {
    return <div className="screen-centre muted">Loading…</div>;
  }
  if (!me) {
    return <Login initialised={initialised} />;
  }

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/transactions" element={<Transactions />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/budgets" element={<Budgets />} />
        <Route path="/import" element={<ImportPage />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
