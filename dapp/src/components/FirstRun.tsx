"use client";

import { useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useCurrentAccount, useCurrentClient, useCurrentWallet, useDAppKit } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Transaction } from "@mysten/sui/transactions";
import { NETWORK } from "@/lib/constants";
import { verifyDeployment } from "@/lib/deployment-manifest";
import { displaySui, formatSui, onboardingConfig, onboardingProgress, registrationInput, shortAddress, type OnboardingStep, type StepState } from "@/lib/onboarding";
import { budgetSupported, buildRegisterAndEnrollTx, defaultReserve } from "@/lib/fleet-budget";
import { formatToken, type Estimate } from "@/lib/network-costs";
import { CardFunding, DepositForm, EstimatePanel, useFleetBudget } from "@/components/FleetBudget";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useDevices } from "@/hooks/useDevices";
import { useFleetTransaction, type TransactionPhase } from "@/hooks/useFleetTransaction";

const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700";
const PRIMARY = `inline-flex items-center justify-center rounded-md bg-blue-700 px-4 py-2 font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:bg-gray-300 disabled:text-gray-600 ${FOCUS}`;
const SECONDARY = `inline-flex items-center justify-center rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:text-gray-400 ${FOCUS}`;
const LINK = `font-medium text-blue-700 underline-offset-2 hover:underline ${FOCUS}`;
const PHASE: Record<TransactionPhase, string> = {
  validating: "Verifying network and account…",
  wallet: "Approve the registration in your wallet…",
  confirming: "Confirming on the network…",
};
const BADGE: Record<StepState, string> = {
  done: "bg-green-700 text-white",
  current: "bg-blue-700 text-white",
  blocked: "bg-red-700 text-white",
  upcoming: "bg-gray-200 text-gray-700",
};
const STATUS_TEXT: Record<StepState, string> = { done: "text-green-800", current: "text-blue-800", blocked: "text-red-800", upcoming: "text-gray-600" };

export function FirstRun({ connect }: { connect: ReactNode }) {
  const { fleet } = useFleetSelection();
  const account = useCurrentAccount(), client = useCurrentClient(), cache = useQueryClient();
  const wallet = useDAppKit(), currentWallet = useCurrentWallet();
  const devices = useDevices(), mutation = useFleetTransaction();
  const [message, setMessage] = useState<{ address: string; text: string } | null>(null);
  const [error, setError] = useState<{ address: string; text: string } | null>(null);
  const [phase, setPhase] = useState<TransactionPhase | null>(null);
  const [estimate, setEstimate] = useState<Estimate | undefined>();
  const { budget } = useFleetBudget();
  const [fromBudget, setFromBudget] = useState(true);
  // Synchronous guard: a second click can arrive before React re-renders the disabled button.
  const inFlight = useRef(false);
  const form = useRef<HTMLFormElement>(null);
  const config = onboardingConfig(NETWORK, process.env.NEXT_PUBLIC_ENOKI_API_KEY, process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID);
  const state = useQuery({
    queryKey: ["first-run-account", NETWORK, account?.address],
    enabled: !!account,
    retry: false,
    refetchInterval: 60_000,
    queryFn: async () => {
      if (!account!.chains.includes(`sui:${NETWORK}`)) throw new Error(`This account does not support sui:${NETWORK}. Choose a wallet account on this network.`);
      await verifyDeployment(client);
      return (await client.getBalance({ owner: account!.address })).balance.balance;
    },
  });
  const ready = !!account && state.isSuccess && BigInt(state.data) > BigInt(0);
  const steps = onboardingProgress(!!account,
    state.isSuccess ? { status: "success", value: state.data } : state.isError ? { status: "error" } : { status: "pending" },
    devices.isSuccess ? { status: "success", value: devices.data.length } : devices.isError ? { status: "error" } : { status: "pending" });
  const next = steps.find((step) => step.state === "current" || step.state === "blocked");
  const accountName = account?.label || currentWallet?.name;

  async function submit(transaction: Transaction) {
    if (!account || inFlight.current) return;
    inFlight.current = true;
    const address = account.address;
    setError(null); setMessage(null);
    try {
      if (wallet.stores.$connection.get().account?.address !== address) throw new Error("Wallet account changed; refresh before retrying");
      transaction.setSender(address);
      const result = await mutation.mutateAsync({ transaction, onProgress: setPhase });
      form.current?.reset();
      setMessage({ address, text: `Confirmed transaction ${result.digest}. The device is registered to your account; it is not online until its daemon starts and reports in.` });
      await Promise.all([cache.invalidateQueries({ queryKey: ["devices"] }), cache.invalidateQueries({ queryKey: ["first-run-account"] })]);
    } catch (cause) {
      setError({ address, text: `${cause instanceof Error ? cause.message : "Wallet request failed"}. If submission may have reached the network, refresh inventory and check the wallet transaction history before retrying. Rejecting approval creates no registration. A request that reaches the network and fails may still incur a network fee.` });
    } finally {
      inFlight.current = false;
      setPhase(null);
    }
  }

  return <div className="max-w-4xl space-y-6 text-sm text-gray-900">
    <header className="space-y-1">
      <h1 className="text-2xl font-semibold tracking-tight">Get started with SuiFleet</h1>
      <p className="text-gray-700">Connect your account, add funds for network fees, register a device and send your first update.</p>
      <p className="flex flex-wrap gap-x-4 gap-y-1">
        <a className={LINK} href="https://docs.suifleet.io/install/">Install the device client</a>
        <a className={LINK} href="https://docs.suifleet.io/enrollment/">Enrollment guide</a>
        <a className={LINK} href="https://docs.suifleet.io/components/">Publish and deploy components</a>
      </p>
    </header>

    <nav aria-label="Onboarding progress">
      <ol className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {steps.map((step, index) => <ProgressItem key={step.id} step={step} index={index} current={step === next} />)}
      </ol>
    </nav>

    {next && <p role="status" className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-blue-950">
      <span className="font-semibold">Next: </span>{nextAction(next, config.enabled)}
    </p>}

    <StepCard id="account" index={1} title="Sign in or connect" state={steps[0].state}>
      {account ? <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">{accountName ?? "Wallet account"}</span>
        <span className="font-mono text-gray-700" title={account.address}>{shortAddress(account.address)}</span>
        <NetworkBadge />
      </div> : <>
        <p className="text-gray-700">{config.enabled ? "Use Google sign-in or Slush" : "Use Slush"} in the wallet menu—no extension needed. Existing extension wallets also work.</p>
        {!config.enabled && <p role="status" className="text-gray-700">{config.reason}</p>}
      </>}
      <p className="text-gray-700">Your wallet is the account app that approves changes and payments. SuiFleet asks for approval before submitting an action.</p>
      <div>{connect}</div>
      <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-amber-950">
        <p className="font-semibold">You hold this fleet’s keys</p>
        <ul className="mt-1 list-disc space-y-0.5 pl-5">
          <li>Keep your sign-in account and its recovery methods current. SuiFleet cannot reset ownership.</li>
          <li>Never share a seed phrase, login token or device key.</li>
          <li>Disconnect on shared computers.</li>
        </ul>
      </div>
      <Disclosure summary="About account custody and sessions">
        <p>A Google zkLogin address is controlled by your Google account and Enoki’s app-specific salt service. Losing that account can mean losing your fleet and funds. Enoki availability is also required to sign in and generate proofs.</p>
        <p>Your sign-in session expires periodically. Sign in again when prompted.</p>
        <p>For a business fleet, use stronger custody such as a hardware-backed extension wallet or multisig before making large deposits. Move ownership with the recipient-confirmed fleet handover (under Advanced below) while you still have account access.</p>
      </Disclosure>
    </StepCard>

    <StepCard id="funding" index={2} title="Add funds" state={steps[1].state}>
      {!account ? <p className="text-gray-700">Sign in to check your balance on <strong>{NETWORK}</strong>.</p> : <>
        {state.isPending && <p role="status">Checking deployment and account balance…</p>}
        {state.error && <p role="alert" className="text-red-800">{state.error.message}</p>}
        {state.isSuccess && <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="text-2xl font-semibold" aria-label={`Network fee balance ${displaySui(state.data)}`}>{displaySui(state.data)}</span>
          <span className="text-gray-700">available for network fees on {NETWORK}</span>
        </div>}
        {state.isSuccess && (BigInt(state.data) === BigInt(0)
          ? <p className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-amber-950">Fund this address before registering. Send SUI on <strong>{NETWORK}</strong> to <span className="break-all font-mono">{account.address}</span>.</p>
          : <p className="text-gray-700">Adding a device costs a small network fee. Your approval app shows the amount before you confirm. This account balance is separate from your fleet budget.</p>)}
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" disabled={state.isFetching} onClick={() => { void state.refetch(); void devices.refetch(); }} className={SECONDARY}>Refresh account and inventory</button>
          {state.isFetching && !state.isPending && <span className="text-gray-600">Refreshing…</span>}
        </div>
        <Disclosure summary="How to fund this address">
          <p>Send SUI on this network to your address from another wallet. For localnet use your disposable node’s faucet; for testnet use the Sui testnet faucet. Test tokens have no monetary value. Do not send mainnet funds to a test network.</p>
        </Disclosure>
        {budgetSupported() && <div className="space-y-3 rounded-md border border-gray-200 p-4">
          <h3 className="font-semibold">Prepay network costs</h3>
          <p className="text-gray-700">Your account balance pays for actions you approve. The fleet budget is separate: prepay device network costs and set spending limits. Each device receives a small starting balance and can refill only within those limits.</p>
          <EstimatePanel onEstimate={setEstimate} />
          <h4 className="font-semibold">Fund by card (optional)</h4>
          <CardFunding asset="SUI" amount={estimate ? formatToken(estimate.suiDepositMist) : "1"} />
          <h4 className="font-semibold">Deposit</h4>
          <DepositForm suggestedMist={estimate?.suiDepositMist} />
          <p className="text-gray-700">Manage the balance, per-device usage and limits on the <Link className={LINK} href="/budget">Network budget page</Link>.</p>
        </div>}
        <Disclosure summary="Technical details">
          <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
            <dt className="text-gray-600">Network</dt><dd>{NETWORK}</dd>
            <dt className="text-gray-600">Address</dt><dd className="break-all font-mono">{account.address}</dd>
            {state.isSuccess && <><dt className="text-gray-600">Exact balance</dt><dd>{formatSui(state.data)} ({BigInt(state.data).toLocaleString("en-US")} MIST)</dd></>}
          </dl>
        </Disclosure>
      </>}
    </StepCard>

    <StepCard id="register" index={3} title="Register your first device" state={steps[2].state}>
      {!account ? <p className="text-gray-700">Connect your account and add funds first.</p> : <>
        <p className="text-gray-700">Start the SuiFleet client on the device and copy the device address it displays. Registration adds the device to your account and authorizes it to connect. It does not install software or confirm that the device is online.</p>
        {devices.error && <p role="alert" className="text-red-800">Inventory unavailable: {devices.error.message}. Refresh before retrying a registration.</p>}
        {devices.isLoading && <p role="status">Loading inventory…</p>}
        {devices.isSuccess && (devices.data.length === 0
          ? <p className="text-gray-700">No devices registered to this account yet.</p>
          : <div>
            <p>{devices.data.length} device(s) registered to this account:</p>
            <ul className="mt-1 flex flex-wrap gap-2">
              {devices.data.slice(0, 5).map((device) => <li key={device.objectId} className="rounded-full bg-gray-100 px-3 py-1 text-gray-800">{device.name}</li>)}
              {devices.data.length > 5 && <li className="px-1 py-1 text-gray-600">and {devices.data.length - 5} more</li>}
            </ul>
            <p className="mt-1 text-gray-700">Online health appears on the <Link className={LINK} href="/devices">Devices page</Link> once a daemon reports.</p>
          </div>)}
        {/* SF-100 adds address-free naming and bulk enrollment; integrate it here once its API is merged. */}
        <form ref={form} key={account.address} className="grid gap-3 sm:grid-cols-2" onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          try {
            const input = registrationInput(String(data.get("name")), String(data.get("deviceAddress")), String(data.get("arch")));
            const funding = budget && fromBudget ? { budgetId: budget.id, capId: budget.capId, floatMist: budget.defaultLimits.floatCap,
              reserveMist: defaultReserve(budget.defaultLimits, budget.balance, budget.defaultLimits.floatCap) } : undefined;
            void submit(buildRegisterAndEnrollTx(input.name, input.deviceAddress, input.arch, account.address, funding, fleet));
          } catch (cause) { setError({ address: account.address, text: (cause as Error).message }); }
        }}>
          <label className="block font-medium">Device name
            <input className={`mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 font-normal ${FOCUS}`} name="name" required placeholder="Kitchen Pi" autoComplete="off" />
          </label>
          <label className="block font-medium">Architecture
            <select className={`mt-1 block w-full rounded-md border border-gray-300 bg-white px-3 py-2 font-normal ${FOCUS}`} name="arch"><option>aarch64</option><option>armv7</option><option>x86_64</option></select>
          </label>
          <label className="block font-medium sm:col-span-2">Device public address
            <input className={`mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 font-mono font-normal ${FOCUS}`} name="deviceAddress" required placeholder="0x…" autoComplete="off" spellCheck={false} />
            <span className="mt-1 block font-normal text-gray-600">The device’s own address from the daemon—not your wallet address.</span>
          </label>
          {budget && <label className="flex items-start gap-2 sm:col-span-2">
            <input type="checkbox" className={`mt-0.5 ${FOCUS}`} checked={fromBudget} onChange={(e) => setFromBudget(e.target.checked)} />
            <span>Enroll in my fleet budget: send the device a {formatToken(budget.defaultLimits.floatCap)} SUI float and give its own account a {formatToken(defaultReserve(budget.defaultLimits, budget.balance, budget.defaultLimits.floatCap))} SUI reserve to draw from</span>
          </label>}
          <div className="space-y-2 sm:col-span-2">
            <p className="text-gray-700">You will sign as <span className="break-all font-mono text-gray-900">{account.address}</span> on <strong>{NETWORK}</strong>.</p>
            <button className={PRIMARY} disabled={!ready || !devices.isSuccess || mutation.isPending}>{mutation.isPending ? "Registering…" : "Register device"}</button>
            {!ready && state.isSuccess && <p className="text-gray-600">Add funds to this account to enable registration.</p>}
          </div>
        </form>
        <div aria-live="polite">{phase && <p className="text-blue-900">{PHASE[phase]}</p>}</div>
        {message?.address === account.address && <div className="rounded-md border border-green-300 bg-green-50 px-4 py-3 text-green-950"><p role="status">Device registered. It is not online until its client starts and sends a report.</p><details><summary>Confirmation details</summary><p className="break-all">{message.text}</p></details></div>}
        {error?.address === account.address && <p role="alert" className="rounded-md border border-red-300 bg-red-50 px-4 py-3 text-red-950">{error.text}</p>}
      </>}
    </StepCard>

    <StepCard id="update" index={4} title="Deploy a first update" state={steps[3].state}>
      <p className="text-gray-700">Package a component and target your device. The update applies after the device’s daemon is running and picks it up; check results on Deployments.</p>
      {steps[3].state === "current"
        ? <Link className={PRIMARY} href="/firmware">Deploy a component</Link>
        : <p className="text-gray-600">Available after you register a device.</p>}
    </StepCard>

    {account && <details className="group rounded-lg border border-gray-200 bg-white">
      <summary className={`cursor-pointer select-none rounded-lg px-5 py-4 font-semibold ${FOCUS}`}>Advanced: move fleet ownership</summary>
      <div className="space-y-3 border-t border-gray-200 px-5 py-4 text-gray-800">
        <p>While this account can still sign, use recipient-confirmed fleet handover to move to an extension wallet or multisig. The recipient must confirm control before you execute any transfer; verify its signing and recovery procedure first.</p>
        <p>Inventory device owner capabilities, group administration and retained native deployment records together. Pause rollouts and admission before starting. Handover is resumable, not fleet-wide atomic: incomplete transfers can stop existing deployments from applying. Cancellation or expiry does not undo completed steps.</p>
        <p>Catalog-bound deployments need replacement; credentials, provider access and funds need separate cutover. Follow the handover inventory and verify the recipient’s authority before retiring this account.</p>
        <Link className={LINK} href="/handover">Open fleet handover</Link>
      </div>
    </details>}
  </div>;
}

function nextAction(step: OnboardingStep, google: boolean) {
  switch (step.id) {
    case "account": return google ? "Sign in with Google, Slush or a wallet extension." : "Connect Slush or a wallet extension.";
    case "funding": return step.state === "blocked" ? "Resolve the balance check below, then refresh." : step.status === "Not funded" ? "Add SUI to this account to cover network fees." : "Checking your balance…";
    case "register": return step.state === "blocked" ? "Inventory is unavailable; refresh before registering." : "Register the device running the SuiFleet client.";
    case "update": return "Deploy a component to your registered device.";
  }
}

function ProgressItem({ step, index, current }: { step: OnboardingStep; index: number; current: boolean }) {
  return <li aria-current={current ? "step" : undefined} className={`flex items-center gap-3 rounded-lg border bg-white px-3 py-2 ${current ? "border-blue-700 ring-1 ring-blue-700" : "border-gray-200"}`}>
    <span aria-hidden className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${BADGE[step.state]}`}>{step.state === "done" ? <Check /> : step.state === "blocked" ? "!" : index + 1}</span>
    <span className="min-w-0">
      <a href={`#step-${step.id}`} className={`block font-medium leading-snug ${FOCUS}`}>{step.title}</a>
      <span className={`block text-xs ${STATUS_TEXT[step.state]}`}>{step.status}</span>
    </span>
  </li>;
}

function StepCard({ id, index, title, state, children }: { id: string; index: number; title: string; state: StepState; children: ReactNode }) {
  return <section id={`step-${id}`} aria-labelledby={`step-${id}-title`} className={`scroll-mt-4 rounded-lg border bg-white p-5 shadow-sm ${state === "current" ? "border-blue-300" : "border-gray-200"}`}>
    <h2 id={`step-${id}-title`} className="mb-3 flex items-center gap-3 text-base font-semibold">
      <span aria-hidden className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs ${BADGE[state]}`}>{state === "done" ? <Check /> : state === "blocked" ? "!" : index}</span>
      {title}
    </h2>
    <div className="space-y-3">{children}</div>
  </section>;
}

function Check() {
  return <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M3 8.5l3 3 7-7" /></svg>;
}

function NetworkBadge() {
  return <span className="rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-semibold text-blue-900">Network: {NETWORK}</span>;
}

function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  return <details className="rounded-md border border-gray-200">
    <summary className={`cursor-pointer select-none rounded-md px-3 py-2 font-medium text-gray-800 ${FOCUS}`}>{summary}</summary>
    <div className="space-y-2 border-t border-gray-200 px-3 py-2 text-gray-700">{children}</div>
  </details>;
}
