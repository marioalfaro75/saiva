import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { Accounts } from "./pages/Accounts";
import { Alerts } from "./pages/Alerts";
import { Benchmarks } from "./pages/Benchmarks";
import { Bills } from "./pages/Bills";
import { Budgets } from "./pages/Budgets";
import { Forecast } from "./pages/Forecast";
import { Goals } from "./pages/Goals";
import { ImportPage } from "./pages/Import";
import { Insights } from "./pages/Insights";
import { Login } from "./pages/Login";
import { NetWorth } from "./pages/NetWorth";
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
        <Route path="/insights" element={<Insights />} />
        <Route path="/alerts" element={<Alerts />} />
        <Route path="/transactions" element={<Transactions />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/budgets" element={<Budgets />} />
        <Route path="/bills" element={<Bills />} />
        <Route path="/forecast" element={<Forecast />} />
        <Route path="/net-worth" element={<NetWorth />} />
        <Route path="/goals" element={<Goals />} />
        <Route path="/benchmarks" element={<Benchmarks />} />
        <Route path="/import" element={<ImportPage />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
