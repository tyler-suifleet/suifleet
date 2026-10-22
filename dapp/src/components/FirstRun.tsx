"use client";

import { useState, type ReactNode } from "react";
import { useCurrentAccount, useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Transaction } from "@mysten/sui/transactions";
import { NETWORK } from "@/lib/constants";
import { verifyDeployment } from "@/lib/deployment-manifest";
import { onboardingConfig, registrationInput } from "@/lib/onboarding";
import { budgetSupported, buildRegisterAndEnrollTx } from "@/lib/fleet-budget";
import { formatToken, type Estimate } from "@/lib/network-costs";
import { CardFunding, DepositForm, EstimatePanel, useFleetBudget } from "@/components/FleetBudget";
import { useDevices } from "@/hooks/useDevices";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";

export function FirstRun({ connect }: { connect: ReactNode }) {
  const account = useCurrentAccount(), client = useCurrentClient(), cache = useQueryClient();
  const wallet = useDAppKit();
  const devices = useDevices(), mutation = useFleetTransaction();
  const [message, setMessage] = useState<{ address: string; text: string } | null>(null);
  const [error, setError] = useState<{ address: string; text: string } | null>(null);
  const [estimate, setEstimate] = useState<Estimate | undefined>();
  const { budget } = useFleetBudget();
  const [fromBudget, setFromBudget] = useState(true);
  const config = onboardingConfig(NETWORK, process.env.NEXT_PUBLIC_ENOKI_API_KEY, process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID);
  const state = useQuery({
    queryKey: ["first-run-account", NETWORK, account?.address],
    enabled: !!account,
    retry: false,
    refetchInterval: 15_000,
    queryFn: async () => {
      if (!account!.chains.includes(`sui:${NETWORK}`)) throw new Error(`This account does not support sui:${NETWORK}. Choose a wallet account on this network.`);
      await verifyDeployment(client);
      return (await client.getBalance({ owner: account!.address })).balance.balance;
    },
  });
  const ready = !!account && state.isSuccess && BigInt(state.data) > BigInt(0);

  async function submit(transaction: Transaction) {
    if (!account) return;
    const address = account.address;
    setError(null); setMessage(null);
    try {
      if (wallet.stores.$connection.get().account?.address !== address) throw new Error("Wallet account changed; refresh before retrying");
      transaction.setSender(address);
      const result = await mutation.mutateAsync({ transaction });
      setMessage({ address, text: `Confirmed transaction ${result.digest}. Refreshing your inventory; device activation on the daemon is a separate step.` });
      await Promise.all([cache.invalidateQueries({ queryKey: ["devices"] }), cache.invalidateQueries({ queryKey: ["first-run-account"] })]);
    } catch (cause) {
      setError({ address, text: `${cause instanceof Error ? cause.message : "Wallet request failed"}. If submission may have reached the network, refresh inventory and check the wallet transaction history before retrying. Rejected requests create no registration; an executed failure may charge gas.` });
    }
  }

  return <div className="max-w-3xl space-y-6 text-sm">
    <h1 className="text-2xl font-semibold">Get started with SuiFleet</h1>
    <section className="bg-white p-5 rounded border space-y-3">
      <h2 className="font-semibold">1. Sign in or connect</h2>
      <p>No extension needed: choose Google sign-in or Slush in the wallet menu. Existing extension wallets also work.</p>
      {!config.enabled && <p role="status">{config.reason}</p>}
      {connect}
      <p>A Google zkLogin address is controlled by your Google account and Enoki’s app-specific salt service. Keep access to that account and its recovery methods. Losing it can mean losing your fleet and funds. Enoki availability is also required to sign in and generate proofs.</p>
      <p>Sessions expire at the current Sui epoch boundary; sign in again when prompted. Disconnect on shared computers. Never share a seed phrase, login token or device key with SuiFleet.</p>
      <p>For a business fleet, use stronger custody such as a hardware-backed extension wallet or multisig before making large deposits. Use the recipient-confirmed fleet handover linked below while you still have account access.</p>
    </section>
    <section className="bg-white p-5 rounded border space-y-3">
      <h2 className="font-semibold">2. Check network and gas</h2>
      <p>Network: <strong>{NETWORK}</strong>. Wallet address: <span className="break-all">{account?.address ?? "Not connected"}</span></p>
      {state.isPending && account && <p>Checking deployment and account balance…</p>}
      {state.error && <p role="alert">{state.error.message}</p>}
      {state.isSuccess && <p>Gas balance: {state.data} MIST (1 SUI = 1,000,000,000 MIST). {BigInt(state.data) === BigInt(0) ? "Fund this address before registering." : "Registration requires gas; the wallet shows the actual transaction cost before approval."}</p>}
      <p>Today, send SUI on this network to the address above from a wallet. For localnet use your disposable node’s faucet; for testnet use the Sui testnet faucet. Test tokens have no monetary value. Do not send mainnet funds to a test network.</p>
      <button type="button" disabled={!account || state.isFetching} onClick={() => { void state.refetch(); void devices.refetch(); }} className="text-blue-700 underline">Refresh account and inventory</button>
    </section>
    {account && budgetSupported() && <section className="bg-white p-5 rounded border space-y-3">
      <h2 className="font-semibold">3. Prepay network costs</h2>
      <p>Your wallet&apos;s gas above pays for your own transactions. The fleet budget is separate: you deposit network costs up front, each device gets a small float at registration and tops up only through the contract, within limits you set.</p>
      <EstimatePanel onEstimate={setEstimate} />
      <h3 className="font-semibold">Fund by card (optional)</h3>
      <CardFunding asset="SUI" amount={estimate ? formatToken(estimate.suiDepositMist) : "1"} />
      <h3 className="font-semibold">Deposit</h3>
      <DepositForm suggestedMist={estimate?.suiDepositMist} />
      <p>Manage the balance, per-device usage and limits on the <a className="underline" href="/budget">Network budget page</a>.</p>
    </section>}
    {account && <section className="bg-white p-5 rounded border space-y-3">
      <h2 className="font-semibold">{budgetSupported() ? "4" : "3"}. Register your first device</h2>
      <p>Start the daemon with its own identity and copy its public Sui address here. Registration creates the device, issues its device capability and activates it atomically. It does not install software or prove the device is online.</p>
      {devices.error && <p role="alert">Inventory unavailable: {devices.error.message}. Refresh before retrying a registration.</p>}
      {devices.isLoading && <p>Loading inventory…</p>}
      {devices.isSuccess && <p>{devices.data.length} device(s) registered to this account. Manage them on the <a className="underline" href="/devices">Devices page</a>.</p>}
      <form key={account.address} className="space-y-3" onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        try {
          const input = registrationInput(String(data.get("name")), String(data.get("deviceAddress")), String(data.get("arch")));
          const funding = budget && fromBudget ? { budgetId: budget.id, capId: budget.capId, floatMist: budget.defaultLimits.floatCap } : undefined;
          void submit(buildRegisterAndEnrollTx(input.name, input.deviceAddress, input.arch, account.address, funding));
        } catch (cause) { setError({ address: account.address, text: (cause as Error).message }); }
      }}>
        <label className="block">Device name <input className="border p-2 block w-full" name="name" required /></label>
        <label className="block">Device public address <input className="border p-2 block w-full" name="deviceAddress" required placeholder="0x…" /></label>
        <label className="block">Architecture <select className="border p-2 ml-2" name="arch"><option>aarch64</option><option>armv7</option><option>x86_64</option></select></label>
        {budget && <label className="block"><input type="checkbox" checked={fromBudget} onChange={(e) => setFromBudget(e.target.checked)} /> Enroll in my fleet budget and send the device a {formatToken(budget.defaultLimits.floatCap)} SUI float</label>}
        <button className="bg-blue-600 text-white rounded p-2 disabled:opacity-40" disabled={!ready || !devices.isSuccess || mutation.isPending}>Register device</button>
      </form>
      {message?.address === account.address && <p role="status" className="break-all">{message.text}</p>}
      {error?.address === account.address && <p role="alert">{error.text}</p>}
    </section>}
    {account && <section className="bg-white p-5 rounded border space-y-3">
      <h2 className="font-semibold">Move fleet ownership</h2>
      <p>While this account can still sign, use recipient-confirmed fleet handover to move to an extension wallet or multisig. The recipient must confirm control before you execute any transfer; verify its signing and recovery procedure first.</p>
      <p>Inventory device owner capabilities, group administration and retained native deployment records together. Pause rollouts and admission before starting. Handover is resumable, not fleet-wide atomic: incomplete transfers can stop existing deployments from applying. Cancellation or expiry does not undo completed steps.</p>
      <p>Catalog-bound deployments need replacement; credentials, provider access and funds need separate cutover. Follow the handover inventory and verify the recipient’s authority before retiring this account.</p>
      <a className="text-blue-700 underline" href="/handover">Open fleet handover</a>
    </section>}
  </div>;
}
