"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useCurrentAccount, useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Transaction } from "@mysten/sui/transactions";
import { NETWORK } from "@/lib/constants";
import { useDevices } from "@/hooks/useDevices";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useBudgets, useFleetStorage } from "@/hooks/useFleetStorage";
import {
  DEFAULT_LIMITS, DEFAULT_QUOTA, budgetAlerts, budgetSupported, buildCreateBudgetTx, buildCreateStorageTx, buildDepositTx,
  buildDepositWalTx, buildEnrollDeviceTx, buildFundDeviceTx, buildFundReserveTx, buildReclaimReserveTx, buildRefillPoolTx, buildRemoveDeviceTx,
  buildSetDeviceSuspendedTx, buildSetPausedTx, buildSwapAndDepositWalTx, buildWithdrawTx, buildWithdrawWalTx, defaultReserve, fetchAccount, fetchAccounts,
  minWalOut, periodUsage, planRefill, readExchange, storageAlerts, storageConfig, swapRoute, type AccountView, type StorageConfig,
} from "@/lib/fleet-budget";
import { MEASURED, encodedBlobLength, estimate, formatToken, parseToken, rescaled, runwayDays, type Estimate, type EstimateInput } from "@/lib/network-costs";
import { DEFAULT_PRICE_TABLE } from "@/lib/fleet-pricing";
import { DEFAULT_WORKLOAD, defaultValues, depositInput, toSettings } from "@/lib/fleet-settings-cost";
import { quoteDate } from "@/lib/cost-quote";
import { useCostQuote } from "@/hooks/useCostQuote";
import {
  FEE_NOTES, checkoutUrl, clearPurchase, loadPurchase, onrampProvider, purchaseStatus, savePurchase, type OnrampAsset, type PendingPurchase,
} from "@/lib/onramp";

import { enrollBudgetDevices, enrollmentReads, prepareEnrollment, type EnrollmentProgress } from "@/lib/budget-enrollment";

import { DATA_CONFIG } from "@/lib/fleet-data";
import { buildAuthorizeRenewalTx, buildConfigureRetentionTx, buildFundRetentionTx, buildMigrateRetentionTx,
  buildWithdrawRetentionTx, fetchRetention, retentionFacts, retentionOrigin, buildConfigureDeviceRetentionTx, buildClearDeviceRetentionTx, fetchDeviceRetention } from '@/lib/retention';

const sui = (mist: bigint) => `${formatToken(mist)} SUI`;
const wal = (frost: bigint) => `${formatToken(frost)} WAL`;

// The default fleet settings (SF-131: daily health) for one device over a year, with log uploads
// disabled as new fleets default (2026-10-23); the Fleet settings page sizes archives from the chosen settings.
const SETTINGS_ESTIMATE = depositInput(toSettings(defaultValues(DEFAULT_PRICE_TABLE), NETWORK), { ...DEFAULT_WORKLOAD, devices: 1, releasesPerMonth: 1, artifactBytes: 1024 * 1024, logBytesPerDeviceMonth: 0 }, 12);
export const DEFAULT_ESTIMATE: EstimateInput = DATA_CONFIG ? { ...SETTINGS_ESTIMATE, serverlessData: true }
  : { ...SETTINGS_ESTIMATE, healthSummariesPerDeviceMonth: 0, serverlessData: false, dataBytesPerDeviceMonth: 0, dataUploadsPerDeviceMonth: 0, storageEpochs: 2 };

function NumberField({ label, value, onChange, step }: { label: string; value: number; onChange: (value: number) => void; step?: number }) {
  return <label className="block text-xs">{label}
    <input type="number" min={0} step={step ?? 1} className="border p-1 block w-full" value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>;
}

/** The up-front estimate, each charge and fee listed separately before approval. */
export function EstimatePanel({ onEstimate }: { onEstimate?: (estimate: Estimate | undefined) => void }) {
  const [input, setInput] = useState<EstimateInput>(DEFAULT_ESTIMATE);
  const set = (key: keyof EstimateInput) => (value: number) => setInput({ ...input, [key]: key === "artifactBytes" || key === "dataBytesPerDeviceMonth" ? Math.round(value * 1024 * 1024) : value });
  const { quote } = useCostQuote();
  const result = useMemo(() => { try { return { estimate: estimate(input, quote) }; } catch (error) { return { error: (error as Error).message }; } }, [input, quote]);
  useEffect(() => onEstimate?.(result.estimate), [result, onEstimate]);
  const route = swapRoute();
  const storage = storageConfig();
  const epochDays = NETWORK === "mainnet" ? MEASURED.walrus.mainnetEpochDays : 1;
  return <div className="space-y-3 text-sm">
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
      <NumberField label="Devices" value={input.devices} onChange={set("devices")} />
      <NumberField label="Existing protected blobs (all fleet uses)" value={input.existingProtectedBlobs ?? 0} onChange={set("existingProtectedBlobs")} />
      <NumberField label="Months" value={input.months} onChange={set("months")} />
      <NumberField label="Receipts per device per month" value={input.receiptsPerDeviceMonth} onChange={set("receiptsPerDeviceMonth")} />
      <NumberField label="Other device transactions per month" value={input.anchorsPerDeviceMonth} onChange={set("anchorsPerDeviceMonth")} />
      <NumberField label="Health summaries per device per month (daily = 31, hourly = 731)" value={input.healthSummariesPerDeviceMonth ?? 0} onChange={set("healthSummariesPerDeviceMonth")} />
      <NumberField label="Releases per month" value={input.releasesPerMonth} onChange={set("releasesPerMonth")} />
      <NumberField label="Artifact size (MiB)" value={input.artifactBytes / 1024 / 1024} step={0.1} onChange={set("artifactBytes")} />
      <NumberField label="Archive uploads per device per month (weekly = 5, daily = 31; add diagnostics/crashes)" value={input.dataUploadsPerDeviceMonth ?? 1} onChange={set("dataUploadsPerDeviceMonth")} />
      <NumberField label="Device data stored per month (MiB)" value={input.dataBytesPerDeviceMonth / 1024 / 1024} step={0.1} onChange={set("dataBytesPerDeviceMonth")} />
      <NumberField label={`Storage epochs (~${epochDays} day(s) each)`} value={input.storageEpochs} onChange={set("storageEpochs")} />
    </div>
    {input.serverlessData && <p>Defaults follow the default fleet settings (daily health) with log archives disabled; enabling archives in this estimate does not configure the device, and existing fleets keep their on-chain settings. The Fleet settings page sizes this from your chosen settings. Includes observed first-use and recurring health gas plus registration, certification and anchoring for each archive. Cadence and payload growth affect fees; include diagnostic/crash headroom. These Testnet measurements are fixture observations, not a gas ceiling. Migrated v3 storage uses keyed protection without a history scan or fleet-wide slot cap; localnet registration gas stayed flat from 10 to 10,000 protected blobs. Unmigrated v2 storage retains the scan and 1,024-slot limit. Protocol transaction limits and funding still apply.</p>}
    {result.estimate?.protectedCapacity && <p role="status">Projected archives: {result.estimate.protectedCapacity.planned}; existing covered blobs entered: {result.estimate.protectedCapacity.existing}. Migrated v3 storage has no fleet-wide protection slot cap. Archives purchased through their full owner-approved target need no renewal reserve. Releases and recipes still require finite renewal authorization. Check actual coverage, reserves and per-blob expiry before funding.</p>}
    {result.error && <p role="alert">{result.error}</p>}
    {result.estimate && <table className="w-full text-xs border">
      <tbody>
        <tr><td className="p-1">Device gas for {result.estimate.deviceTransactions} receipts/anchors and {result.estimate.draws} top-up draws</td><td className="p-1 text-right">{sui(result.estimate.gasMist)}</td></tr>
        <tr><td className="p-1">Initial device floats (part of the deposit, sent to devices at registration)</td><td className="p-1 text-right">{sui(result.estimate.floatsMist)}</td></tr>
        <tr className="font-semibold"><td className="p-1">Network-cost deposit in SUI, with {input.marginPercent}% headroom</td><td className="p-1 text-right">{sui(result.estimate.suiDepositMist)}</td></tr>
        <tr><td className="p-1">Walrus storage and write fees for releases{input.dataBytesPerDeviceMonth ? " and device data" : ""} (gross, before any subsidy)</td><td className="p-1 text-right">{wal(result.estimate.walDepositFrost)}</td></tr>
        <tr><td className="p-1">SUI→WAL swap</td><td className="p-1">{route.explanation}{route.route ? " Exchange fee: none beyond the posted rate; network gas applies." : ""}</td></tr>
        <tr><td className="p-1">Card or on-ramp fees</td><td className="p-1">{FEE_NOTES.onramp}</td></tr>
        <tr><td className="p-1">Regional availability</td><td className="p-1">{FEE_NOTES.regions}</td></tr>
        <tr><td className="p-1">SuiFleet service fee</td><td className="p-1">{FEE_NOTES.service}</td></tr>
        <tr><td className="p-1">Indicative value ({quote.usdSource}, {quoteDate(quote.usdObservedAt)})</td><td className="p-1 text-right">≈ ${result.estimate.usd.total.toFixed(2)}</td></tr>
      </tbody>
    </table>}
    <p className="text-xs text-gray-600">From SF-054 Testnet measurements rescaled to {quote.referenceGasPrice.toString()} MIST gas ({quote.gasSource}, {quoteDate(quote.gasObservedAt)}): a status receipt costs about {sui(rescaled(quote).receiptNetMist)} net. Walrus {quote.walrusStoragePrice.toString()} FROST per MiB-epoch and {quote.walrusWritePrice.toString()} FROST per MiB written ({quote.walrusSource}, {quoteDate(quote.walrusObservedAt)}). An estimate, not a quote: the wallet shows each transaction&apos;s actual cost. {storage.reason ?? ""}</p>
  </div>;
}

/**
 * Open the provider's hosted checkout; then watch the wallet. Funds count only
 * when they arrive, and the budget only after the deposit confirms. A failed
 * or cancelled purchase is shown on its own, never as budget exhaustion.
 */
export function CardFunding({ asset, amount }: { asset: OnrampAsset; amount: string }) {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { provider, reason } = onrampProvider();
  const [pending, setPending] = useState<PendingPurchase | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!account) { setPending(null); return; }
    setPending(loadPurchase(localStorage, NETWORK, account.address, asset));
  }, [account, asset]);
  const coinType = (of: OnrampAsset) => of === "SUI" ? "0x2::sui::SUI" : storageConfig().config?.walrus.walType;
  const balance = useQuery({
    queryKey: ["onramp-balance", NETWORK, account?.address, pending?.asset],
    enabled: !!account && !!pending,
    refetchInterval: 60_000,
    queryFn: async () => BigInt((await client.getBalance({ owner: account!.address, coinType: coinType(pending!.asset) })).balance.balance),
  });
  if (!account) return null;
  if (!provider) return <p className="text-xs">{reason}</p>;
  async function open() {
    setError(null);
    try {
      const url = checkoutUrl(provider!, account!.address, asset, amount);
      const start = (await client.getBalance({ owner: account!.address, coinType: coinType(asset) })).balance.balance;
      const record: PendingPurchase = { address: account!.address, asset, amount, startBalance: String(start), startedAt: Date.now() };
      savePurchase(localStorage, NETWORK, record);
      setPending(record);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (cause) { setError((cause as Error).message); }
  }
  function dismiss() { clearPurchase(localStorage, NETWORK, account!.address, asset); setPending(null); }
  const status = pending && balance.isSuccess ? purchaseStatus(pending, balance.data, Date.now()) : undefined;
  return <div className="space-y-2 text-xs">
    <button type="button" className="bg-gray-800 text-white rounded px-2 py-1" onClick={() => void open()}>Buy {amount} {asset} by card with {provider.name}</button>
    <p>Checkout opens on {provider.name}&apos;s own site, pre-filled with this wallet address. Identity checks and payment stay with the provider; SuiFleet never sees your card details.</p>
    {error && <p role="alert">{error}</p>}
    {pending && <div role="status" className="border rounded p-2 bg-gray-50">
      {!status && <p>Checking the wallet for your purchase…</p>}
      {status?.state === "waiting" && <p>Purchase started {new Date(pending.startedAt).toLocaleTimeString()}: waiting for {pending.asset} to arrive in this wallet. Nothing is credited until it arrives and your deposit confirms.</p>}
      {status?.state === "arrived" && <p>{formatToken(status.received)} {pending.asset} arrived in your wallet. Deposit it into the fleet budget below; it is credited only once that transaction confirms.</p>}
      {status?.state === "unconfirmed" && <p>Purchase not confirmed: nothing arrived in this wallet. It may have failed, been cancelled or still be processing with the provider; check with them. Your fleet, storage and devices are unaffected, and this is not a budget shortfall.</p>}
      <button type="button" className="underline" onClick={dismiss}>Dismiss</button>
    </div>}
  </div>;
}

/** The budget this account manages, if any (the first one). */
export function useFleetBudget() {
  const budgets = useBudgets();
  return { ...budgets, budget: budgets.data?.[0] };
}

function useSubmit() {
  const account = useCurrentAccount(), cache = useQueryClient(), mutation = useFleetTransaction();
  const [message, setMessage] = useState<string | null>(null), [error, setError] = useState<string | null>(null);
  async function submit(build: () => Transaction | Promise<Transaction>) {
    setMessage(null); setError(null);
    try {
      const tx = await build();
      tx.setSender(account!.address);
      const result = await mutation.mutateAsync({ transaction: tx });
      setMessage(`Confirmed ${result.digest}.`);
      await Promise.all([cache.invalidateQueries({ queryKey: ["fleet-budgets"] }), cache.invalidateQueries({ queryKey: ["fleet-budget-accounts"] }),
        cache.invalidateQueries({ queryKey: ["retention"] }), cache.invalidateQueries({ queryKey: ["first-run-account"] }), cache.invalidateQueries({ queryKey: ["fleet-storage"] })]);
    } catch (cause) {
      setError(`${cause instanceof Error ? cause.message : "Wallet request failed"}. Nothing is credited unless the transaction confirms; if it may have reached the network, refresh before retrying.`);
    }
  }
  return { submit, message, error, pending: mutation.isPending };
}

/** Create the budget with its first deposit (or deposit more into it). */
export function DepositForm({ suggestedMist }: { suggestedMist?: bigint }) {
  const account = useCurrentAccount();
  const { budget, isLoading, error: readError } = useFleetBudget();
  const { submit, message, error, pending } = useSubmit();
  const [amount, setAmount] = useState("");
  if (!account) return null;
  if (!budgetSupported()) return <p className="text-xs">This deployment&apos;s contracts have no fleet budget yet; devices pay their own gas.</p>;
  if (isLoading) return <p className="text-xs">Loading your fleet budget…</p>;
  if (readError) return <p role="alert" className="text-xs">Budget unavailable: {readError.message}</p>;
  const value = amount || (suggestedMist ? formatToken(suggestedMist) : "");
  return <form className="space-y-2 text-xs" onSubmit={(event) => {
    event.preventDefault();
    let mist: bigint;
    try { mist = parseToken(value); } catch (cause) { return void submit(() => { throw cause; }); }
    void submit(() => budget ? buildDepositTx(budget.id, budget.capId, mist)
      : buildCreateBudgetTx(mist, DEFAULT_LIMITS, mist / BigInt(5), account.address));
  }}>
    <p>{budget ? `Fleet budget balance: ${sui(budget.balance)}.` : "No fleet budget yet. Your first deposit creates it; you hold its owner capability."} The budget is separate from your wallet&apos;s gas: devices draw from it only within on-chain limits.</p>
    <label className="block">{budget ? "Deposit (SUI)" : "Initial deposit (SUI)"} <input className="border p-1" value={value} onChange={(e) => setAmount(e.target.value)} /></label>
    <button className="bg-blue-600 text-white rounded px-2 py-1 disabled:opacity-40" disabled={pending}>{budget ? "Deposit" : "Create budget and deposit"}</button>
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </form>;
}

/** Balance, warnings, per-device reserves and usage, and the owner controls. */
export function BudgetManager() {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { budget, isLoading, error: readError } = useFleetBudget();
  const devices = useDevices();
  const { submit, message, error, pending: singlePending } = useSubmit();
  const batchMutation = useFleetTransaction(), cache = useQueryClient(), wallet = useDAppKit();
  const batchBusy = useRef(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchProgress, setBatchProgress] = useState<EnrollmentProgress>();
  const [batchError, setBatchError] = useState("");
  const pending = singlePending || batchRunning;
  const [withdraw, setWithdraw] = useState("");
  const activated = (devices.data ?? []).filter((device) => device.ownerCapId);
  const accounts = useQuery<(AccountView | undefined)[]>({
    queryKey: ["fleet-budget-accounts", NETWORK, budget?.id, activated.map((d) => d.objectId).join()],
    enabled: !!budget && devices.isSuccess,
    refetchInterval: 60_000,
    queryFn: () => fetchAccounts(client, budget!.id, activated.map((device) => device.objectId)),
  });
  if (!account || !budget) return isLoading ? <p className="text-xs">Loading…</p> : readError ? <p role="alert" className="text-xs">{readError.message}</p> : null;
  async function enrollAll() {
    if (batchBusy.current || singlePending || !account || !budget) return;
    batchBusy.current = true; setBatchRunning(true); setBatchError("");
    try {
      await enrollBudgetDevices({
        devices: activated.map(d => ({ objectId: d.objectId, ownerCapId: d.ownerCapId! })),
        ...enrollmentReads(client, account.address, budget.id),
        prepare: tx => prepareEnrollment(tx, client, account.address),
        execute: tx => {
          if (wallet.stores.$connection.get().account?.address !== account.address) throw new Error("Wallet account changed; stop and resume with the original owner");
          return batchMutation.mutateAsync({ transaction: tx });
        },
        onProgress: setBatchProgress,
      });
    } catch (cause) {
      setBatchError(`${cause instanceof Error ? cause.message : "Enrollment stopped"}. Confirmed chunks remain enrolled. Resume rechecks every device on chain; an uncertain submission may require waiting for indexing before retrying.`);
    } finally {
      batchBusy.current = false; setBatchRunning(false);
      await Promise.all([cache.invalidateQueries({ queryKey: ["fleet-budgets"] }),
        cache.invalidateQueries({ queryKey: ["fleet-budget-accounts"] })]);
    }
  }
  const accountsReady = !accounts.error && accounts.data !== undefined && devices.isSuccess;
  const now = BigInt(Date.now());
  const enrolled = (accounts.data ?? []).filter((a): a is AccountView => !!a);
  const spent = enrolled.reduce((sum, a) => sum + periodUsage(a, now).drawn, BigInt(0));
  const reserves = enrolled.reduce((sum, a) => sum + a.reserve, BigInt(0));
  const runway = runwayDays(budget.balance + reserves, spent, Number(budget.defaultLimits.periodMs) / 86_400_000);
  const alerts = budgetAlerts(budget, enrolled, now, runway);
  return <div className="space-y-3 text-xs">
    <p>Unallocated {sui(budget.balance)} (spendable {sui(budget.balance)}; reserved 0) {accountsReady && <>and {sui(reserves)} in device reserves.</>} Deposited {sui(budget.totalDeposited)}, withdrawn {sui(budget.totalWithdrawn)}, paid straight to devices {sui(budget.totalPaid)}, allocated to reserves {sui(budget.totalAllocated)}, reclaimed {sui(budget.totalReclaimed)}. {accountsReady && (runway === null ? "No draws this period." : `About ${runway.toFixed(1)} days left at this period's rate.`)}</p>
    <p>Each device draws only from its own reserve, through its own on-chain account, so devices never wait on each other or on the fleet budget. Refill reserves from the budget; reclaim unused reserves before withdrawing.</p>
    {accountsReady && alerts.length > 0 && <ul role="alert" className="list-disc ml-5">{alerts.map((alert, index) =>
      <li key={index} className={alert.severity === "critical" ? "text-red-700" : "text-amber-700"}>{alert.deviceId ? `Device ${alert.deviceId.slice(0, 10)}…: ` : ""}{alert.text}</li>)}</ul>}
    <div className="flex gap-2 items-center flex-wrap">
      <button type="button" className="border rounded px-2 py-1" disabled={pending} onClick={() => void submit(() => buildSetPausedTx(budget.id, budget.capId, !budget.paused))}>{budget.paused ? "Resume all devices" : "Pause all devices"}</button>
      <input className="border p-1 w-28" placeholder="SUI" value={withdraw} onChange={(e) => setWithdraw(e.target.value)} />
      <button type="button" className="border rounded px-2 py-1" disabled={pending} onClick={() => void submit(() => buildWithdrawTx(budget.id, budget.capId, parseToken(withdraw), account.address))}>Withdraw unallocated funds</button>
    </div>
    <p>SUI already sent to a device stays with it: a compromised device can spend at most its float plus one period allowance per period, never more than its reserve, until you suspend or remove it here, pause the budget or revoke its key on the Devices page.</p>
    <div className="space-y-1">
      <button type="button" className="border rounded px-2 py-1" disabled={pending || !accountsReady || !activated.length} onClick={() => void enrollAll()}>
        {batchProgress?.phase === "stopped" ? "Resume enrollment" : "Enroll all unenrolled devices"}
      </button>
      <p>Up to 200 devices per approval (5 approvals for 1,000 new devices). Each chunk is checked for size and gas before signing. Initial floats and up to two period allowances per device come from the budget; limited reserves are shared evenly.</p>
      {batchProgress && <div role="status">
        <p>{batchProgress.confirmed} of {batchProgress.total} confirmed this run; {batchProgress.skipped} already enrolled; {batchProgress.excluded.length} excluded. {batchProgress.phase === "approval" ? `Approve chunk ${batchProgress.chunk} (gas budget ${sui(BigInt(batchProgress.estimatedGas!))}).` : batchProgress.phase}.</p>
        {batchProgress.excluded.map(item => <p key={item.deviceId}>Excluded {item.deviceId}: {item.reason}</p>)}
        {batchProgress.digests.map(digest => <p key={digest}>Confirmed {digest}</p>)}
      </div>}
      {batchError && <p role="alert">{batchError}</p>}
    </div>
    {accounts.error ? <p role="alert">Budget accounts unavailable: {accounts.error.message}</p>
      : devices.error ? <p role="alert">Budget devices unavailable: {devices.error.message}</p>
      : !accountsReady ? <p>Loading budget accounts…</p> : <table className="w-full border">
      <thead><tr className="text-left"><th className="p-1">Device</th><th className="p-1">Reserve</th><th className="p-1">This period</th><th className="p-1">Draws</th><th className="p-1">Total paid</th><th className="p-1">Actions</th></tr></thead>
      <tbody>{activated.map((device, index) => {
        const a = accounts.data?.[index];
        const usage = a ? periodUsage(a, now) : undefined;
        const target = a ? a.limits.periodAllowance * BigInt(2) : BigInt(0);
        return <tr key={device.objectId} className="border-t">
          <td className="p-1">{device.name}{a?.suspended ? " (suspended)" : ""}</td>
          <td className="p-1">{a ? sui(a.reserve) : "not enrolled"}</td>
          <td className="p-1">{a ? `${sui(usage!.drawn)} of ${sui(a.limits.periodAllowance)}` : ""}</td>
          <td className="p-1">{a ? `${usage!.draws}/${a.limits.maxDrawsPerPeriod}` : ""}</td>
          <td className="p-1">{a ? sui(a.totalDrawn) : ""}</td>
          <td className="p-1 space-x-2">{a ? <>
            <button type="button" className="underline" disabled={pending || a.reserve >= target} onClick={() => void submit(() => buildFundReserveTx(budget.id, budget.capId, a.accountId, target - a.reserve))}>Refill reserve</button>
            <button type="button" className="underline" disabled={pending || a.reserve === BigInt(0)} onClick={() => void submit(() => buildReclaimReserveTx(budget.id, budget.capId, a.accountId, a.reserve))}>Reclaim reserve</button>
            <button type="button" className="underline" disabled={pending} onClick={() => void submit(() => buildFundDeviceTx(budget.id, budget.capId, a.accountId, a.limits.floatCap))}>Send float</button>
            <button type="button" className="underline" disabled={pending} onClick={() => void submit(() => buildSetDeviceSuspendedTx(budget.id, budget.capId, a.accountId, !a.suspended))}>{a.suspended ? "Resume" : "Suspend"}</button>
            <button type="button" className="underline" disabled={pending} onClick={() => void submit(() => buildRemoveDeviceTx(budget.id, budget.capId, a.accountId))}>Remove</button>
          </> : <button type="button" className="underline" disabled={pending || !accounts.isSuccess} onClick={() => void submit(() => buildEnrollDeviceTx(budget.id, budget.capId, device.ownerCapId!, budget.defaultLimits.floatCap,
            defaultReserve(budget.defaultLimits, budget.balance, budget.defaultLimits.floatCap)))}>Enroll with float and reserve</button>}</td>
        </tr>;
      })}</tbody>
    </table>}

    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

/** The live Walrus epoch and storage price, from the pinned Testnet system (fleet storage builds only). */
function useWalrusState(config?: StorageConfig) {
  const client = useCurrentClient();
  return useQuery({
    queryKey: ["walrus-state", NETWORK, config?.walrus.system],
    enabled: !!config,
    refetchInterval: 60_000,
    queryFn: async () => {
      const { walrus, TESTNET_WALRUS_PACKAGE_CONFIG } = await import("@mysten/walrus");
      const state = await client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus.systemState();
      const staking = await client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus.stakingState();
      return { epoch: state.committee.epoch, storagePrice: BigInt(state.storage_price_per_unit_size), writePrice: BigInt(state.write_price_per_unit_size),
        epochDurationMs: Number(staking.epoch_duration), firstEpochStartMs: Number(staking.first_epoch_start) };
    },
  });
}

const MIB = 1024 * 1024;

/**
 * Owner-prepaid Walrus storage held by the fleet: create it, fund its WAL
 * (from SUI in one transaction with a slippage limit, or WAL bought
 * directly), refill and keep storage with that WAL at any epoch, and
 * withdraw unused WAL.
 * Uploads choose it on the Releases and Components pages.
 */
export function StoragePanel({ suggestedWalFrost }: { suggestedWalFrost?: bigint }) {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { budget, storage, config, reason } = useFleetStorage();
  const walrusState = useWalrusState(config);
  const { submit, message, error, pending } = useSubmit();
  const [suiAmount, setSuiAmount] = useState("1"), [slippage, setSlippage] = useState(100);
  const [buyMib, setBuyMib] = useState(64), [epochs, setEpochs] = useState(2), [alsoBuy, setAlsoBuy] = useState(true);
  const [walAmount, setWalAmount] = useState(""), [withdrawWal, setWithdrawWal] = useState("");
  const route = swapRoute();
  if (!account || !budget) return null;
  if (!config) return <p className="text-xs">{reason}</p>;
  const addBytes = BigInt(Math.max(0, Math.round(buyMib * MIB)));
  // The refill as fleet_storage::refill_pool will apply it at the current Walrus epoch.
  let plan: ReturnType<typeof planRefill> | undefined, planError: string | undefined;
  if (walrusState.data) {
    try { plan = planRefill(storage, walrusState.data.epoch, addBytes, epochs, walrusState.data.storagePrice); }
    catch (e) { planError = e instanceof Error ? e.message : String(e); }
  }
  const refill = plan ? { addBytes, endEpoch: plan.endEpoch } : undefined;
  async function swap() {
    const mist = parseToken(suiAmount);
    // The first pinned exchange holding enough WAL at its posted rate.
    for (const exchangeId of config!.walrus.exchange.objects) {
      const exchange = await readExchange(client, config!, exchangeId);
      const minWal = minWalOut(mist, exchange.rate, slippage);
      if (exchange.walAvailable < minWal) continue;
      return buildSwapAndDepositWalTx(config!, exchangeId, storage!.id, budget!.id, budget!.capId, mist, minWal,
        alsoBuy ? refill : undefined);
    }
    throw new Error("No pinned Testnet exchange holds enough WAL for this swap; buy WAL directly and deposit it.");
  }
  const alerts = storage ? storageAlerts(storage, walrusState.data?.epoch) : [];
  return <div className="space-y-3 text-xs">
    {!storage && <div className="space-y-2">
      <p>No fleet storage yet. Create it to prepay Walrus storage for releases and device uploads: blobs it pays for stay fleet-owned, and no device or upload can delete or shorten them.</p>
      <p>Each device may upload up to {(Number(DEFAULT_QUOTA.encodedBytesPerPeriod) / MIB).toFixed(0)} MiB encoded in {DEFAULT_QUOTA.uploadsPerPeriod.toString()} uploads per day, at most {(Number(DEFAULT_QUOTA.maxBlobSize) / MIB).toFixed(0)} MiB per blob and {wal(DEFAULT_QUOTA.writeFeesPerPeriod)} in write fees.</p>
      <button type="button" className="bg-blue-600 text-white rounded px-2 py-1 disabled:opacity-40" disabled={pending} onClick={() => void submit(() => buildCreateStorageTx(config, budget.id, budget.capId, DEFAULT_QUOTA))}>Create fleet storage</button>
    </div>}
    {storage && <>
      <RetentionPanel config={config} storage={storage.id} version={storage.version} budget={budget.id} cap={budget.capId} epoch={walrusState.data?.epoch} schedule={walrusState.data} />
      <p>Prepaid storage: {(Number(storage.poolSize) / MIB).toFixed(2)} MiB encoded{storage.endEpoch !== undefined ? `, Walrus epochs ${storage.startEpoch}–${storage.endEpoch}` : ""} (current epoch {walrusState.data?.epoch ?? "…"}). WAL for fees and purchases: {wal(storage.walBalance)}. {storage.blobCount.toString()} fleet blob(s), {(Number(storage.totalEncodedBytes) / MIB).toFixed(2)} MiB encoded, {wal(storage.totalWriteFees)} in write fees so far.</p>
      {alerts.length > 0 && <ul role="alert" className="list-disc ml-5">{alerts.map((alert, index) =>
        <li key={index} className={alert.severity === "critical" ? "text-red-700" : "text-amber-700"}>{alert.text}</li>)}</ul>}
      <fieldset className="border rounded p-2 space-y-2" disabled={pending}>
        <legend className="px-1 font-semibold">Fund with SUI</legend>
        {route.route ? <>
          <p>{route.explanation}</p>
          <div className="flex flex-wrap gap-2 items-end">
            <label>SUI to swap <input className="border p-1 w-24 block" value={suiAmount} onChange={(e) => setSuiAmount(e.target.value)} /></label>
            <label>Slippage limit (basis points) <input type="number" min={0} max={1000} className="border p-1 w-24 block" value={slippage} onChange={(e) => setSlippage(Number(e.target.value))} /></label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={alsoBuy && !!refill} disabled={!refill} onChange={(e) => setAlsoBuy(e.target.checked)} /> and refill the storage below in the same transaction</label>
            <button type="button" className="bg-blue-600 text-white rounded px-2 py-1" onClick={() => void submit(swap)}>Swap and deposit</button>
          </div>
        </> : <p>{route.explanation}</p>}
        <div className="flex flex-wrap gap-2 items-end">
          <label>WAL from this wallet <input className="border p-1 w-24 block" placeholder={suggestedWalFrost ? formatToken(suggestedWalFrost) : "WAL"} value={walAmount} onChange={(e) => setWalAmount(e.target.value)} /></label>
          <button type="button" className="border rounded px-2 py-1" onClick={() => void submit(() => buildDepositWalTx(config, storage.id, budget.id, budget.capId, parseToken(walAmount)))}>Deposit WAL</button>
        </div>
        <CardFunding asset="WAL" amount={walAmount || (suggestedWalFrost ? formatToken(suggestedWalFrost) : "1")} />
      </fieldset>
      <fieldset className="border rounded p-2 space-y-2" disabled={pending}>
        <legend className="px-1 font-semibold">Storage</legend>
        <div className="flex flex-wrap gap-2 items-end">
          <label>Add encoded MiB <input type="number" min={0} className="border p-1 w-24 block" value={buyMib} onChange={(e) => setBuyMib(Number(e.target.value))} /></label>
          <label>Keep for epochs from now <input type="number" min={1} className="border p-1 w-20 block" value={epochs} onChange={(e) => setEpochs(Number(e.target.value))} /></label>
          <button type="button" className="border rounded px-2 py-1" disabled={!refill} onClick={() => void submit(() => buildRefillPoolTx(config, storage.id, budget.id, budget.capId, refill!))}>Refill with fleet WAL</button>
        </div>
        {plan && <p>The pool will hold {(Number(plan.poolSize) / MIB).toFixed(2)} MiB encoded for Walrus epochs {plan.startEpoch}–{plan.endEpoch}, for about {wal(plan.cost)} at the current price, paid from the fleet&apos;s WAL; no WAL leaves the fleet.{plan.droppedBytes > BigInt(0) ? ` The expired pool's unused ${(Number(plan.droppedBytes) / MIB).toFixed(2)} MiB are dropped.` : ""}</p>}
        {planError && <p role="alert">{planError}</p>}
        <p>Walrus joins storage only over the same epochs, so a refill first drops the pool&apos;s elapsed epochs, keeps its unused space and cannot end before the pool does. Walrus encodes a blob to about {(Number(encodedBlobLength(MIB)) / MIB).toFixed(1)}× its size: a 1 MiB artifact needs that many MiB of storage.</p>
        <div className="flex flex-wrap gap-2 items-end">
          <label>Withdraw WAL <input className="border p-1 w-24 block" value={withdrawWal} onChange={(e) => setWithdrawWal(e.target.value)} /></label>
          <button type="button" className="border rounded px-2 py-1" onClick={() => void submit(() => buildWithdrawWalTx(config, storage.id, budget.id, budget.capId, parseToken(withdrawWal), account.address))}>Withdraw unused WAL</button>
        </div>
      </fieldset>
    </>}
    {walrusState.error && <p role="alert">Walrus state unavailable: {walrusState.error.message}</p>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

export type { AccountView };

function RetentionPanel({ config, storage, version, budget, cap, epoch, schedule }: {
  config: StorageConfig; storage: string; version: bigint; budget: string; cap: string; epoch?: number;
  schedule?: { epochDurationMs: number; firstEpochStartMs: number };
}) {
  const client = useCurrentClient(), account = useCurrentAccount();
  const { submit, message, error, pending } = useSubmit();
  const origin = retentionOrigin();
  const [executor, setExecutor] = useState(''), [margin, setMargin] = useState(2);
  const [walLimit, setWalLimit] = useState('1'), [gasLimit, setGasLimit] = useState('0.1'), [reward, setReward] = useState('0.01');
  const [walFunds, setWalFunds] = useState('1'), [gasFunds, setGasFunds] = useState('0.1');
  const [blob, setBlob] = useState(''), [target, setTarget] = useState(0), [maxFee, setMaxFee] = useState('1');
  const [withdrawConfirmed, setWithdrawConfirmed] = useState(false);
  const view = useQuery({ queryKey: ['retention', NETWORK, storage, origin], enabled: !!origin && version === BigInt(3), refetchInterval: 60_000,
    queryFn: () => fetchRetention(client, storage, origin!, config.storageOrigin ?? config.packageId) });
  const deviceView = useQuery({ queryKey: ['device-retention', NETWORK, storage, config.packageId], enabled: !!origin && version === BigInt(3), refetchInterval: 60_000,
    queryFn: () => fetchDeviceRetention(client, config, storage) });
  if (!origin) return <p>Protected retention is not configured for this build. No renewal or out-of-browser alerts are guaranteed.</p>;
  const policy = view.data?.policy, blobs = view.data?.blobs ?? [];
  return <fieldset disabled={pending} className="border rounded p-2 space-y-2">
    <legend>Protected retention</legend>
    <p role="status">{view.isSuccess ? `${blobs.filter((item) => item.policy).length} renewal policies across the fleet.` : "Renewal usage unavailable."} Migrated v3 storage uses keyed records with no fleet-wide slot cap or registration expiry scan. An expired blob needs its own restore/reference review; unrelated registrations continue when coverage and reserves are adequate. Changing the new-archive target does not renew old blobs.</p>
    <p>Renewal executor rewards and gas use these separate protected reserves. They are additional to device health/archive transaction estimates; match the estimate storage horizon to the finite target.</p>
    {version < BigInt(3) ? <button type="button" onClick={() => void submit(() => buildMigrateRetentionTx(config, storage, budget, cap))}>Migrate storage to retention v3 (retires old mutation calls)</button> : <>
      {retentionFacts(policy, blobs, epoch).map((fact) => <p key={fact} role="status">{fact}</p>)}
      <p>Run the owner-configured renewal worker separately from this browser. Check its private status file for persistent failures and notifier delivery acknowledgments. No running notifier means no alert guarantee.</p>
      {policy && <p>Discretionary WAL is separate from protected reserves: {wal(BigInt(policy.wal.value))} and {sui(BigInt(policy.gas.value))}. Unpaid renewal ceilings (zero after capacity purchase): {wal(BigInt(policy.pending_wal))}. Executor {policy.executor}; policy revision {policy.revision}. Funds are prepaid; no recurring card debit.</p>}
      <div className="flex flex-wrap gap-2">
        <label>Executor address <input className="border" value={executor} onChange={(e) => setExecutor(e.target.value)} /></label>
        <label>Safety margin (epochs) <input className="border w-16" type="number" min={1} value={margin} onChange={(e) => setMargin(Number(e.target.value))} /></label>
        <label>WAL per epoch <input className="border w-20" value={walLimit} onChange={(e) => setWalLimit(e.target.value)} /></label>
        <label>SUI per epoch <input className="border w-20" value={gasLimit} onChange={(e) => setGasLimit(e.target.value)} /></label>
        <label>SUI reward per renewal <input className="border w-20" value={reward} onChange={(e) => setReward(e.target.value)} /></label>
        <button type="button" onClick={() => void submit(() => buildConfigureRetentionTx(config, storage, budget, cap, executor, margin, parseToken(walLimit), parseToken(gasLimit), parseToken(reward)))}>Set bounded executor policy</button>
      </div>
      <p>Protect every fleet blob, including recipes, configuration, dependencies, rollback/recovery, site/manifest and history. External blobs are not protected by this storage; retain verified backups and import/re-upload them with owner-authorized references. Newly registered blobs pause further discretionary writes until authorized.</p>
      <table><thead><tr><th>Blob</th><th>Exclusive expiry epoch</th><th>Projected exclusive expiry / runway</th><th>Authorized target</th><th>Last successful renewal epoch</th></tr></thead><tbody>
        {blobs.map((item) => <tr key={item.id}><td><button type="button" onClick={() => { setBlob(item.id); setTarget(item.end + 1); }}>{item.id}</button></td><td>{item.end}</td><td>{schedule ? new Date(schedule.firstEpochStartMs + item.end * schedule.epochDurationMs).toISOString() : 'schedule unavailable'}; {epoch === undefined ? '?' : Math.max(0, item.end - epoch)} epoch(s). Projection assumes unchanged epoch duration.</td><td>{item.policy?.target ?? 'no renewal policy; check coverage'}{item.policy && item.policy.last_success_epoch === null && item.policy.max_fee === '0' ? ' (capacity prepaid)' : ''}</td><td>{item.policy?.last_success_epoch ?? 'none'}</td></tr>)}
      </tbody></table>
      <label>Blob object ID <input className="border" value={blob} onChange={(e) => setBlob(e.target.value)} /></label>
      <label>Finite target epoch <input className="border w-16" type="number" value={target} onChange={(e) => setTarget(Number(e.target.value))} /></label>
      <label>Maximum WAL quote <input className="border w-20" value={maxFee} onChange={(e) => setMaxFee(e.target.value)} /></label>
      <button type="button" onClick={() => void submit(() => buildAuthorizeRenewalTx(config, storage, budget, cap, blob, target, parseToken(maxFee)))}>Authorize this renewal</button>
      <p>Device archives require explicit owner approval while protected retention is enabled. Their initial purchase and finite renewal are paid atomically; quotas and isolated reserve limits still apply. The owner-run worker must apply purchased renewals before expiry.</p>
      <p>{deviceView.data ? `New archive target: epoch ${deviceView.data.target}; maximum WAL per renewal ${wal(BigInt(deviceView.data.max_fee))}.` : 'No device archive approval is verified.'}</p>
      <button type="button" disabled={!policy || !deviceView.isSuccess} onClick={() => void submit(() => buildConfigureDeviceRetentionTx(config, storage, budget, cap, target, parseToken(maxFee)))}>Approve new archives to this finite target</button>
      <button type="button" disabled={!deviceView.data} onClick={() => void submit(() => buildClearDeviceRetentionTx(config, storage, budget, cap))}>Pause new archive admission</button>
      {deviceView.error && <p role="alert">Archive policy API unavailable in the configured storage package.</p>}
      <div><label>Reserve WAL <input className="border w-20" value={walFunds} onChange={(e) => setWalFunds(e.target.value)} /></label>
        <label>Reserve SUI <input className="border w-20" value={gasFunds} onChange={(e) => setGasFunds(e.target.value)} /></label>
        <button type="button" disabled={!policy} onClick={() => void submit(() => buildFundRetentionTx(config, storage, parseToken(walFunds), parseToken(gasFunds)))}>Deposit protected reserves</button>
        <label><input type="checkbox" checked={withdrawConfirmed} onChange={(e) => setWithdrawConfirmed(e.target.checked)} /> I understand withdrawing these amounts can leave every protected blob at risk at its shown expiry.</label>
        <button type="button" disabled={!withdrawConfirmed || !account || !policy} onClick={() => void submit(() => buildWithdrawRetentionTx(config, storage, budget, cap, parseToken(walFunds), parseToken(gasFunds), account!.address))}>Withdraw reserves with expiry risk</button>
      </div>
    </>}
    {view.error && <p role="alert">Retention state unavailable: {view.error.message}</p>}
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
  </fieldset>;
}
