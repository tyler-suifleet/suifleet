// Test-only entry: the real Fleet settings page over controlled dependencies.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SettingsPage from "@/app/settings/page";
import { EstimatePanel } from "@/components/FleetBudget";
import { formatToken } from "@/lib/network-costs";

// The Network budget page's estimate, as /budget and onboarding hold it: the
// parent stores each estimate, so an unstable quote would re-render forever.
function BudgetEstimate() {
  const [estimate, setEstimate] = React.useState();
  window.__control.budgetRenders = (window.__control.budgetRenders ?? 0) + 1;
  return <div data-testid="budget-estimate"><EstimatePanel onEstimate={setEstimate} />
    <p>Suggested {estimate ? formatToken(estimate.suiDepositMist) : "none"} SUI</p></div>;
}

createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient()}><SettingsPage /><BudgetEstimate /></QueryClientProvider>);
