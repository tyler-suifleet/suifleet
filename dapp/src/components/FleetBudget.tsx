"use client";

import { useEffect, useMemo, useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Transaction } from "@mysten/sui/transactions";
import { NETWORK } from "@/lib/constants";
import { useDevices } from "@/hooks/useDevices";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useBudgets, useFleetStorage } from "@/hooks/useFleetStorage";
import {
  DEFAULT_LIMITS, DEFAULT_QUOTA, budgetAlerts, budgetSupported, buildBuyStorageTx, buildCreateBudgetTx, buildCreateStorageTx, buildDepositTx,
  buildDepositWalTx, buildEnrollDeviceTx, buildExtendPoolTx, buildFundDeviceTx, buildFundReserveTx, buildReclaimReserveTx, buildRemoveDeviceTx,
  buildSetDeviceSuspendedTx, buildSetPausedTx, buildSwapAndDepositWalTx, buildWithdrawTx, buildWithdrawWalTx, defaultReserve, fetchAccount,
  minWalOut, periodUsage, readExchange, storageAlerts, storageConfig, storageCost, swapRoute, type AccountView, type StorageConfig,
} from "@/lib/fleet-budget";
import { MEASURED, encodedBlobLength, estimate, formatToken, parseToken, runwayDays, type Estimate, type EstimateInput } from "@/lib/network-costs";
import { FEE_NOTES, checkoutUrl, onrampProvider, purchaseStatus, type OnrampAsset, type PendingPurchase } from "@/lib/onramp";

const sui = (mist: bigint) => `${formatToken(mist)} SUI`;
const wal = (frost: bigint) => `${formatToken(frost)} WAL`;

export const DEFAULT_ESTIMATE: EstimateInput = {
  devices: 1, months: 12, receiptsPerDeviceMonth: 30, anchorsPerDeviceMonth: 0, releasesPerMonth: 1,
  artifactBytes: 1024 * 1024, dataBytesPerDeviceMonth: 0, storageEpochs: 2,
  floatMist: DEFAULT_LIMITS.floatCap, drawMist: BigInt(50_000_000), marginPercent: 20,
};

function NumberField({ label, value, onChange, step }: { label: string; value: number; onChange: (value: number) => void; step?: number }) {
  return <label className="block text-xs">{label}
    <input type="number" min={0} step={step ?? 1} className="border p-1 block w-full" value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>;
}

/** The up-front estimate, each charge and fee listed separately before approval. */
export function EstimatePanel({ onEstimate }: { onEstimate?: (estimate: Estimate | undefined) => void }) {
  const [input, setInput] = useState<EstimateInput>(DEFAULT_ESTIMATE);
  const set = (key: keyof EstimateInput) => (value: number) => setInput({ ...input, [key]: key === "artifactBytes" || key === "dataBytesPerDeviceMonth" ? Math.round(value * 1024 * 1024) : value });
  const result = useMemo(() => { try { return { estimate: estimate(input) }; } catch (error) { return { error: (error as Error).message }; } }, [input]);
  useEffect(() => onEstimate?.(result.estimate), [result, onEstimate]);
  const route = swapRoute();
  const storage = storageConfig();
  const epochDays = NETWORK === "mainnet" ? MEASURED.walrus.mainnetEpochDays : 1;
  return <div className="space-y-3 text-sm">
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
      <NumberField label="Devices" value={input.devices} onChange={set("devices")} />
      <NumberField label="Months" value={input.months} onChange={set("months")} />
      <NumberField label="Receipts per device per month" value={input.receiptsPerDeviceMonth} onChange={set("receiptsPerDeviceMonth")} />
      <NumberField label="Data anchors per device per month" value={input.anchorsPerDeviceMonth} onChange={set("anchorsPerDeviceMonth")} />
      <NumberField label="Releases per month" value={input.releasesPerMonth} onChange={set("releasesPerMonth")} />
      <NumberField label="Artifact size (MiB)" value={input.artifactBytes / 1024 / 1024} step={0.1} onChange={set("artifactBytes")} />
      <NumberField label="Device data stored per month (MiB)" value={input.dataBytesPerDeviceMonth / 1024 / 1024} step={0.1} onChange={set("dataBytesPerDeviceMonth")} />
      <NumberField label={`Storage epochs (~${epochDays} day(s) each)`} value={input.storageEpochs} onChange={set("storageEpochs")} />
    </div>
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
        <tr><td className="p-1">Indicative value (spot quotes, {new Date(MEASURED.usd.observedAt * 1000).toISOString().slice(0, 10)})</td><td className="p-1 text-right">≈ ${result.estimate.usd.total.toFixed(2)}</td></tr>
      </tbody>
    </table>}
    <p className="text-xs text-gray-600">From SF-054 measurements: a status receipt costs about {sui(MEASURED.receiptNetMist)} net; Walrus Mainnet rates {MEASURED.walrus.storagePricePerUnit.toString()} FROST per MiB-epoch and {MEASURED.walrus.writePricePerUnit.toString()} FROST per MiB written. An estimate, not a quote: the wallet shows each transaction&apos;s actual cost. {storage.reason ?? ""}</p>
  </div>;
}

function purchaseKey(address: string) {
  return `suifleet:onramp:${NETWORK}:${address}`;
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
    const saved = localStorage.getItem(purchaseKey(account.address));
    setPending(saved ? JSON.parse(saved) as PendingPurchase : null);
  }, [account]);
  const balance = useQuery({
    queryKey: ["onramp-balance", NETWORK, account?.address, asset],
    enabled: !!account && !!pending,
    refetchInterval: 15_000,
    queryFn: async () => BigInt((await client.getBalance({ owner: account!.address, coinType: asset === "SUI" ? "0x2::sui::SUI" : storageConfig().config?.walrus.walType })).balance.balance),
  });
  if (!account) return null;
  if (!provider) return <p className="text-xs">{reason}</p>;
  async function open() {
    setError(null);
    try {
      const url = checkoutUrl(provider!, account!.address, asset, amount);
      const start = (await client.getBalance({ owner: account!.address, coinType: asset === "SUI" ? "0x2::sui::SUI" : storageConfig().config?.walrus.walType })).balance.balance;
      const record: PendingPurchase = { address: account!.address, asset, amount, startBalance: String(start), startedAt: Date.now() };
      localStorage.setItem(purchaseKey(account!.address), JSON.stringify(record));
      setPending(record);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (cause) { setError((cause as Error).message); }
  }
  function dismiss() { localStorage.removeItem(purchaseKey(account!.address)); setPending(null); }
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
        cache.invalidateQueries({ queryKey: ["first-run-account"] }), cache.invalidateQueries({ queryKey: ["fleet-storage"] })]);
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
  const { submit, message, error, pending } = useSubmit();
  const [withdraw, setWithdraw] = useState("");
  const activated = (devices.data ?? []).filter((device) => device.ownerCapId);
  const accounts = useQuery<(AccountView | undefined)[]>({
    queryKey: ["fleet-budget-accounts", NETWORK, budget?.id, activated.map((d) => d.objectId).join()],
    enabled: !!budget && devices.isSuccess,
    refetchInterval: 15_000,
    queryFn: () => Promise.all(activated.map((device) => fetchAccount(client, budget!.id, device.objectId))),
  });
  if (!account || !budget) return isLoading ? <p className="text-xs">Loading…</p> : readError ? <p role="alert" className="text-xs">{readError.message}</p> : null;
  const now = BigInt(Date.now());
  const enrolled = (accounts.data ?? []).filter((a): a is AccountView => !!a);
  const spent = enrolled.reduce((sum, a) => sum + periodUsage(a, now).drawn, BigInt(0));
  const reserves = enrolled.reduce((sum, a) => sum + a.reserve, BigInt(0));
  const runway = runwayDays(budget.balance + reserves, spent, Number(budget.defaultLimits.periodMs) / 86_400_000);
  const alerts = budgetAlerts(budget, enrolled, now, runway);
  return <div className="space-y-3 text-xs">
    <p>Unallocated {sui(budget.balance)} (spendable {sui(budget.balance)}; reserved 0) and {sui(reserves)} in device reserves. Deposited {sui(budget.totalDeposited)}, withdrawn {sui(budget.totalWithdrawn)}, paid straight to devices {sui(budget.totalPaid)}, allocated to reserves {sui(budget.totalAllocated)}, reclaimed {sui(budget.totalReclaimed)}. {runway === null ? "No draws this period." : `About ${runway.toFixed(1)} days left at this period's rate.`}</p>
    <p>Each device draws only from its own reserve, through its own on-chain account, so devices never wait on each other or on the fleet budget. Refill reserves from the budget; reclaim unused reserves before withdrawing.</p>
    {alerts.length > 0 && <ul role="alert" className="list-disc ml-5">{alerts.map((alert, index) =>
      <li key={index} className={alert.severity === "critical" ? "text-red-700" : "text-amber-700"}>{alert.deviceId ? `Device ${alert.deviceId.slice(0, 10)}…: ` : ""}{alert.text}</li>)}</ul>}
    <div className="flex gap-2 items-center flex-wrap">
      <button type="button" className="border rounded px-2 py-1" disabled={pending} onClick={() => void submit(() => buildSetPausedTx(budget.id, budget.capId, !budget.paused))}>{budget.paused ? "Resume all devices" : "Pause all devices"}</button>
      <input className="border p-1 w-28" placeholder="SUI" value={withdraw} onChange={(e) => setWithdraw(e.target.value)} />
      <button type="button" className="border rounded px-2 py-1" disabled={pending} onClick={() => void submit(() => buildWithdrawTx(budget.id, budget.capId, parseToken(withdraw), account.address))}>Withdraw unallocated funds</button>
    </div>
    <p>SUI already sent to a device stays with it: a compromised device can spend at most its float plus one period allowance per period, never more than its reserve, until you suspend or remove it here, pause the budget or revoke its key on the Devices page.</p>
    <table className="w-full border">
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
          </> : <button type="button" className="underline" disabled={pending} onClick={() => void submit(() => buildEnrollDeviceTx(budget.id, budget.capId, device.ownerCapId!, budget.defaultLimits.floatCap,
            defaultReserve(budget.defaultLimits, budget.balance, budget.defaultLimits.floatCap)))}>Enroll with float and reserve</button>}</td>
        </tr>;
      })}</tbody>
    </table>
    {accounts.error && <p role="alert">Usage unavailable: {accounts.error.message}</p>}
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
      return { epoch: state.committee.epoch, storagePrice: BigInt(state.storage_price_per_unit_size), writePrice: BigInt(state.write_price_per_unit_size) };
    },
  });
}

const MIB = 1024 * 1024;

/**
 * Owner-prepaid Walrus storage held by the fleet: create it, fund its WAL
 * (from SUI in one transaction with a slippage limit, or WAL bought
 * directly), buy and extend storage with that WAL, and withdraw unused WAL.
 * Uploads choose it on the Releases and Components pages.
 */
export function StoragePanel({ suggestedWalFrost }: { suggestedWalFrost?: bigint }) {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { budget, storage, config, reason } = useFleetStorage();
  const walrusState = useWalrusState(config);
  const { submit, message, error, pending } = useSubmit();
  const [suiAmount, setSuiAmount] = useState("1"), [slippage, setSlippage] = useState(100);
  const [buyMib, setBuyMib] = useState(64), [epochs, setEpochs] = useState(2), [alsoBuy, setAlsoBuy] = useState(true);
  const [walAmount, setWalAmount] = useState(""), [extend, setExtend] = useState(2), [withdrawWal, setWithdrawWal] = useState("");
  const route = swapRoute();
  if (!account || !budget) return null;
  if (!config) return <p className="text-xs">{reason}</p>;
  const price = walrusState.data?.storagePrice;
  const buyBytes = BigInt(Math.max(0, Math.round(buyMib * MIB)));
  const cost = price !== undefined ? storageCost(buyBytes, epochs, price) : undefined;
  const sameRange = !storage?.startEpoch || walrusState.data?.epoch === storage.startEpoch;
  async function swap() {
    const mist = parseToken(suiAmount);
    // The first pinned exchange holding enough WAL at its posted rate.
    for (const exchangeId of config!.walrus.exchange.objects) {
      const exchange = await readExchange(client, config!, exchangeId);
      const minWal = minWalOut(mist, exchange.rate, slippage);
      if (exchange.walAvailable < minWal) continue;
      return buildSwapAndDepositWalTx(config!, exchangeId, storage!.id, budget!.id, budget!.capId, mist, minWal,
        alsoBuy && sameRange ? { encodedBytes: buyBytes, epochs } : undefined);
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
            <label className="flex items-center gap-1"><input type="checkbox" checked={alsoBuy && sameRange} disabled={!sameRange} onChange={(e) => setAlsoBuy(e.target.checked)} /> and buy the storage below in the same transaction</label>
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
          <label>Encoded MiB <input type="number" min={1} className="border p-1 w-24 block" value={buyMib} onChange={(e) => setBuyMib(Number(e.target.value))} /></label>
          <label>Epochs <input type="number" min={1} className="border p-1 w-20 block" value={epochs} onChange={(e) => setEpochs(Number(e.target.value))} /></label>
          <button type="button" className="border rounded px-2 py-1" disabled={!sameRange} onClick={() => void submit(() => buildBuyStorageTx(config, storage.id, budget.id, budget.capId, buyBytes, epochs))}>Buy with fleet WAL</button>
        </div>
        <p>{cost !== undefined ? `About ${wal(cost)} at the current price, paid from the fleet's WAL; no WAL leaves the fleet. ` : ""}Walrus encodes a blob to about {(Number(encodedBlobLength(MIB)) / MIB).toFixed(1)}× its size: a 1 MiB artifact needs that many MiB of storage.{!sameRange ? ` The pool started at epoch ${storage.startEpoch}; Walrus only joins storage with the same epoch range, so extend the pool instead.` : ""}</p>
        <div className="flex flex-wrap gap-2 items-end">
          <label>Extend the pool by epochs <input type="number" min={1} className="border p-1 w-20 block" value={extend} onChange={(e) => setExtend(Number(e.target.value))} /></label>
          <button type="button" className="border rounded px-2 py-1" disabled={!storage.endEpoch} onClick={() => void submit(() => buildExtendPoolTx(config, storage.id, budget.id, budget.capId, extend))}>Extend with fleet WAL</button>
        </div>
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
