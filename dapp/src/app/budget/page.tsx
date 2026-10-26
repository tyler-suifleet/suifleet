"use client";

import { useState } from "react";
import { useCurrentAccount } from "@mysten/dapp-kit-react";
import { PageHeader, Section } from "@/components/ui";
import { BudgetManager, CardFunding, DepositForm, EstimatePanel, StoragePanel } from "@/components/FleetBudget";
import { formatToken, type Estimate } from "@/lib/network-costs";

export default function BudgetPage() {
  const account = useCurrentAccount();
  const [estimate, setEstimate] = useState<Estimate | undefined>();
  return <div className="max-w-4xl space-y-4">
    <PageHeader title="Network budget" subtitle="Prepay device activity and storage costs. Each device can spend only within the limits you set." />
    <Section title="1. Estimate"><EstimatePanel onEstimate={setEstimate} /></Section>
    {!account && <p className="text-sm">Connect a wallet to fund and manage the budget.</p>}
    {account && <Section title="2. Fund your wallet (optional)">
      <CardFunding asset="SUI" amount={estimate ? formatToken(estimate.suiDepositMist) : "1"} />
    </Section>}
    {account && <Section title="3. Deposit"><DepositForm suggestedMist={estimate?.suiDepositMist} /></Section>}
    {account && <Section title="4. Balance, usage and controls"><BudgetManager /></Section>}
    {account && <Section title="5. Prepaid Walrus storage"><StoragePanel suggestedWalFrost={estimate?.walDepositFrost} /></Section>}
  </div>;
}
