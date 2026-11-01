"use client";

import { useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { PageHeader, Section, InfoCard } from "@/components/ui";
import { BudgetManager, CardFunding, DepositForm, EstimatePanel, StoragePanel } from "@/components/FleetBudget";
import { formatToken, type Estimate } from "@/lib/network-costs";

export default function BudgetPage() {
  const account = useCurrentAccount();
  const [estimate, setEstimate] = useState<Estimate | undefined>();
  return <div className="max-w-5xl space-y-6">
    <PageHeader title="Network budget" subtitle="Keep devices funded for activity and storage, with spending limits you control." />
    <InfoCard title="Two balances, each with a different job">
      <p>Device activity funds pay for update results and health reports. Storage funds pay for software and device-data uploads. Your personal account balance is separate until you approve a deposit.</p>
      <p>Deposits are prepaid network funds, not a SuiFleet subscription purchase. Every funding action asks for approval. Saving Fleet settings does not make a deposit here.</p>
    </InfoCard>
    <div className="grid gap-4 md:grid-cols-2">
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm"><span className="text-xs font-semibold uppercase tracking-wide text-blue-700">Device activity</span><h2 className="mt-2 text-xl font-semibold">Keep devices reporting</h2><p className="mt-2 text-sm text-slate-600">Add SUI to your fleet budget, then allocate it to devices within their spending limits.</p>{account ? <div className="mt-5"><DepositForm suggestedMist={estimate?.suiDepositMist} /></div> : <p className="mt-5 text-sm font-medium text-slate-500">Connect your account to see and fund this balance.</p>}</section>
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm"><span className="text-xs font-semibold uppercase tracking-wide text-indigo-700">Storage</span><h2 className="mt-2 text-xl font-semibold">Keep uploads available</h2><p className="mt-2 text-sm text-slate-600">Storage uses WAL. Add funds and choose a storage period; availability ends unless retained or renewed.</p><p className="mt-5 text-sm"><a href="#storage-funding" className="font-medium text-blue-700 hover:underline">Manage storage funds →</a></p></section>
    </div>
    {account && <details className="rounded-xl border border-slate-200 bg-white p-5"><summary className="text-sm font-semibold">Need funds in your account first?</summary><p className="my-3 text-sm text-slate-600">If your account already holds SUI, use the deposit above. Optional card purchases use a separate provider and do not automatically fund the fleet.</p><CardFunding asset="SUI" amount={estimate ? formatToken(estimate.suiDepositMist) : "1"} /></details>}
    <details className="rounded-xl border border-slate-200 bg-white p-5"><summary className="text-sm font-semibold">Estimate a deposit before funding</summary><p className="my-3 text-sm text-slate-600">Adjust the expected activity to estimate the funds needed. The estimate does not change device settings or move money. Expand the breakdown if you need to understand individual costs.</p><EstimatePanel onEstimate={setEstimate} /></details>
    {account && <details className="rounded-xl border border-slate-200 bg-white p-5"><summary className="text-sm font-semibold">Device balances, spending limits and withdrawals</summary><p className="my-3 text-sm text-slate-600">Review allocations, pause spending or recover unallocated funds. Money already sent to a device remains with that device.</p><BudgetManager /></details>}
    <div id="storage-funding">{account ? <Section title="Storage funding"><StoragePanel suggestedWalFrost={estimate?.walDepositFrost} /></Section> : <Section title="Storage funding"><p className="text-sm text-slate-600">Connect your account to review storage balances and funding options.</p></Section>}</div>
  </div>;
}
