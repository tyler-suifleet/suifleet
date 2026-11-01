"use client";
import { formatStorageQueryError } from "@/lib/storage-errors";

import { useEffect, useMemo, useRef, useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { addSessionPublisher, sessionEndpoints, removeSessionPublisher } from "@/lib/walrus";
import { NETWORK } from "@/lib/constants";
import { useDevices } from "@/hooks/useDevices";
import { useCostQuote } from "@/hooks/useCostQuote";
import { useFleetStorage } from "@/hooks/useFleetStorage";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { quoteDate } from "@/lib/cost-quote";
import { LOG_SETTING_KEYS, SETTING_KEYS, fleetPrice, loadPriceTable, type PriceTable, type SettingKey } from "@/lib/fleet-pricing";
import {
  DEFAULT_WORKLOAD, defaultValues, depositShortfall, epochDays, fleetNetworkCost, recommendDeposit, retentionEpochs, settingNetworkCost, toSettings, uploadedLogBytes, type Workload,
} from "@/lib/fleet-settings-cost";
import { SETTINGS_BOUNDS, fetchFleetSettings, settingsSupported, type FleetSettings } from "@/lib/fleet-settings";
import { settingsSaver } from "@/lib/fleet-settings-tx";
import { MEASURED, formatToken, rescaled } from "@/lib/network-costs";

const TITLES: Record<SettingKey, string> = {
  checkIntervalMs: "Update check",
  rolloutCheckIntervalMs: "Checks during an update",
  healthIntervalMs: "Health reports",
  archiveIntervalMs: "Log archives",
  archiveRetentionDays: "Log retention",
};
const MIB = 1024 * 1024;
const resultRetentionDays = (epochs: number) => epochs * epochDays(NETWORK);

/** "$0.0012", "$4.20", "$1,234.50" */
export function usd(value: number): string {
  if (value === 0) return "$0";
  const digits = Math.abs(value) < 1 ? 4 : 2;
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function NumberField({ label, value, onChange, step }: { label: string; value: number; onChange: (value: number) => void; step?: number }) {
  return <label className="block text-xs">{label}
    <input type="number" min={0} step={step ?? 1} className="border p-1 block w-full" value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>;
}

/** The fleet's cost settings with each option's network cost and SuiFleet price, and the deposit they need. */
export function FleetSettingsPanel() {
  const { table, error } = loadPriceTable();
  return <><StorageEndpoints />{!table ? <p role="alert" className="text-sm">Prices unavailable: {error}</p> : <SettingsForm table={table} />}</>;
}

/** Price-table option values for saved settings, when every saved value is one of the table's options. */
function savedValues(table: PriceTable, saved: FleetSettings): Record<SettingKey, number> | undefined {
  const values = {} as Record<SettingKey, number>;
  for (const key of SETTING_KEYS) {
    const match = table.settings[key].find((o) => key === "archiveRetentionDays"
      ? retentionEpochs(o.value, NETWORK) === saved.archiveRetentionEpochs : o.value === saved[key]);
    if (!match) return undefined;
    values[key] = match.value;
  }
  return values;
}

function SettingsForm({ table }: { table: PriceTable }) {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { quote, gasError, walrusError, usdError, usdLoading, refreshUsd } = useCostQuote();
  const { budget, storage, config } = useFleetStorage();
  const devices = useDevices();
  const mutation = useFleetTransaction();
  const cache = useQueryClient();
  const saved = useQuery({
    queryKey: ["fleet-settings", NETWORK, budget?.id],
    enabled: !!budget && settingsSupported(),
    queryFn: async () => (await fetchFleetSettings(client, budget!.id)) ?? null,
  });
  const [values, setValues] = useState(() => defaultValues(table));
  const [loaded, setLoaded] = useState(false), [touched, setTouched] = useState(false);
  const [workload, setWorkload] = useState<Workload>(DEFAULT_WORKLOAD);
  const [sized, setSized] = useState(false);
  const [months, setMonths] = useState(3);
  const [encrypted, setEncrypted] = useState(false), [annual, setAnnual] = useState(false);
  const [warning, setWarning] = useState(false);
  const [message, setMessage] = useState<string>(), [error, setError] = useState<string>();
  // The form belongs to one fleet: another account or budget starts over from that fleet's
  // size and saved settings, with no confirmation or result carried across. A budget that
  // only finishes loading for the same account keeps the owner's edits.
  const fleet = { account: account?.address, budget: budget?.id };
  const [scope, setScope] = useState(fleet);
  if (scope.account !== fleet.account || scope.budget !== fleet.budget) {
    setScope(fleet);
    if (scope.account !== fleet.account || scope.budget !== undefined) {
      setValues(defaultValues(table)); setLoaded(false); setTouched(false);
      setWorkload(DEFAULT_WORKLOAD); setSized(false);
      setWarning(false); setMessage(undefined); setError(undefined);
    }
  }
  const fleetKey = `${fleet.account}:${fleet.budget}`, current = useRef(fleetKey);
  useEffect(() => { current.current = fleetKey; }, [fleetKey]);
  // Size the fleet from the enrolled devices once they load; the owner can still change it.
  useEffect(() => {
    if (!sized && devices.data?.length) { setWorkload((w) => ({ ...w, devices: devices.data!.length })); setSized(true); }
  }, [devices.data, sized]);
  // Start from the fleet's saved settings once they load, when the table offers them,
  // unless the owner has already chosen while they were loading.
  useEffect(() => {
    if (loaded || saved.data === undefined) return;
    const fromChain = saved.data && savedValues(table, saved.data);
    if (fromChain && !touched) setValues(fromChain);
    setLoaded(true);
  }, [saved.data, loaded, touched, table]);
  // A changed choice needs a fresh deposit check before signing.
  useEffect(() => setWarning(false), [values, workload, months]);

  const result = useMemo(() => {
    try {
      const settings = toSettings(values, NETWORK);
      const network = fleetNetworkCost(settings, workload, quote);
      const price = fleetPrice(table, workload.devices, { values, logUploads: uploadedLogBytes(workload) > 0, encryptedDelivery: encrypted, annualPrepay: annual, dataBytesPerDeviceMonth: uploadedLogBytes(workload), retentionDays: resultRetentionDays(settings.archiveRetentionEpochs) });
      const deposit = recommendDeposit(settings, workload, months, quote);
      return { settings, network, price, deposit };
    } catch (cause) { return { error: (cause as Error).message }; }
  }, [values, workload, months, encrypted, annual, quote, table]);

  const option = (key: SettingKey, value: number): { settings?: FleetSettings; reason?: string } => {
    try { return { settings: toSettings({ ...values, [key]: value }, NETWORK) }; }
    catch (cause) {
      if (key !== "archiveRetentionDays") return { reason: (cause as Error).message };
      return { reason: `Not available on this network. Choose a retention period of ${SETTINGS_BOUNDS.archiveRetentionEpochs[1] * epochDays(NETWORK)} days or less.` };
    }
  };
  const walCounted = !!config;
  const shortfall = result.deposit
    ? depositShortfall({ suiDepositMist: result.deposit.suiDepositMist, walDepositFrost: walCounted ? result.deposit.walDepositFrost : BigInt(0) }, budget?.balance, storage?.walBalance)
    : undefined;
  const saver = settingsSaver(budget);

  async function save(confirmed: boolean) {
    setMessage(undefined); setError(undefined);
    if (!saver.build || !result.settings || !shortfall || !account) return;
    if (!shortfall.enough && !confirmed) { setWarning(true); return; }
    setWarning(false);
    // A result that arrives after the owner switched fleets is not this form's to report.
    const at = current.current;
    try {
      const tx = await saver.build(result.settings);
      tx.setSender(account.address);
      const done = await mutation.mutateAsync({ transaction: tx });
      if (current.current === at) setMessage(`Settings saved in ${done.digest}. Devices apply them at their next check.`);
      await cache.invalidateQueries({ queryKey: ["fleet-settings"] });
    } catch (cause) {
      if (current.current === at) setError(`${cause instanceof Error ? cause.message : "Wallet request failed"}. Nothing changed unless the transaction confirms; refresh before retrying.`);
    }
  }

  const set = (key: Exclude<keyof Workload, "logUploads">, scale = 1) => (value: number) => setWorkload({ ...workload, [key]: Math.round(value * scale) });
  const fee = rescaled(quote);
  const descriptions: Record<SettingKey, string> = {
    checkIntervalMs: "How often devices look for new software and settings.",
    rolloutCheckIntervalMs: "Check more frequently while a gradual update is in progress.",
    healthIntervalMs: "How often devices send a verified health summary.",
    archiveIntervalMs: "How often enabled log uploads are collected. This does not turn logging on.",
    archiveRetentionDays: "How long uploaded device data is kept before it can expire.",
  };
  return <div className="space-y-4 text-sm">
    {SETTING_KEYS.map((key) => <fieldset key={key} className="rounded-xl border border-slate-200 bg-white p-5 space-y-3">
      <legend className="px-2 text-sm font-semibold text-slate-900">{TITLES[key]}{LOG_SETTING_KEYS.includes(key) && !workload.logUploads && !(workload.dataBytesPerDeviceMonth && workload.dataBytesPerDeviceMonth > 0) ? " (applies when device data uploads are on; no cost while off)" : ""}</legend>
      <p className="text-sm text-slate-500">{descriptions[key]}</p>
      <div className="grid gap-2 sm:grid-cols-3">{table.settings[key].map(o => {
        const { settings, reason } = option(key, o.value); const selected = values[key] === o.value;
        return <label key={o.value} className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 text-sm ${selected ? "border-blue-500 bg-blue-50 text-blue-950 ring-1 ring-blue-500" : "border-slate-200 bg-white"} ${!settings ? "opacity-60" : ""}`}>
          <input id={`${key}-${o.value}`} type="radio" name={key} checked={selected} disabled={!settings} onChange={() => { setValues({ ...values, [key]: o.value }); setTouched(true); }} />
          <span><span className="font-medium">{o.label}</span>{o.default && <span className="ml-2 text-xs text-slate-500">Recommended</span>}{reason && <span className="block text-xs">{reason}</span>}</span>
        </label>;
      })}</div>
      <details className="text-xs text-slate-500"><summary>Compare estimated costs for these options</summary><div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead><tr className="text-left">
          <th className="p-1">Option</th><th className="p-1">Usage per device</th>
          <th className="p-1 text-right">Network cost per device</th><th className="p-1 text-right">Network cost per fleet</th>
          <th className="p-1 text-right">SuiFleet price per device</th><th className="p-1 text-right">SuiFleet price per fleet</th>
        </tr></thead>
        <tbody>{table.settings[key].map((o) => {
          const idle = LOG_SETTING_KEYS.includes(key) && !workload.logUploads && !(workload.dataBytesPerDeviceMonth && workload.dataBytesPerDeviceMonth > 0);
          const { settings, reason } = option(key, o.value);
          const network = settings && !result.error ? settingNetworkCost(key, settings, workload, quote) : undefined;
          const id = `${key}-${o.value}`;
          return <tr key={o.value} className="border-t" data-testid={id}>
            <td className="p-1"><span>{o.label}{o.default ? " (default)" : ""}</span></td>
            <td className="p-1">{network?.detail ?? reason}</td>
            <td className="p-1 text-right">{network ? usd(network.usdPerDevice) : "–"}</td>
            <td className="p-1 text-right">{network ? usd(network.usdPerFleet) : "–"}</td>
            <td className="p-1 text-right">{!o.usdPerDeviceMonth ? "included" : idle ? "not charged" : usd(o.usdPerDeviceMonth)}</td>
            <td className="p-1 text-right">{!o.usdPerDeviceMonth ? "included" : idle ? "not charged" : usd(o.usdPerDeviceMonth * workload.devices)}</td>
          </tr>;
        })}</tbody>
      </table></div></details>
    </fieldset>)}
    <details className="rounded-xl border border-slate-200 bg-slate-50 p-5" data-testid="cost-planning"><summary className="font-semibold text-slate-800">Plan expected usage and compare costs</summary>
      <p className="my-3 text-sm text-slate-600">These are planning estimates, including proposed subscription rates. Changing these assumptions does not purchase a plan, change device counts or enable logging. Only the reporting and retention choices above are saved.</p>
    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
      <NumberField label="Devices" value={workload.devices} onChange={set("devices")} />
      <NumberField label="Deployments per month" value={workload.deploymentsPerMonth} onChange={set("deploymentsPerMonth")} />
      {workload.logUploads && <NumberField label="Log data per device per month (MiB)" step={0.1} value={workload.logBytesPerDeviceMonth / MIB} onChange={set("logBytesPerDeviceMonth", MIB)} />}
      <NumberField label="Additional device data per device per month (MiB)" step={0.1} value={(workload.dataBytesPerDeviceMonth ?? 0) / MIB} onChange={set("dataBytesPerDeviceMonth", MIB)} />
      <NumberField label="Releases per month" value={workload.releasesPerMonth} onChange={set("releasesPerMonth")} />
      <NumberField label="Release artifact size (MiB)" step={0.1} value={workload.artifactBytes / MIB} onChange={set("artifactBytes", MIB)} />
      <NumberField label="Months to prepay" value={months} onChange={setMonths} />
    </div>
    <label className="flex gap-1 items-center text-xs"><input type="checkbox" checked={workload.logUploads} onChange={(e) => setWorkload({ ...workload, logUploads: e.target.checked })} />
      Estimate with device log uploads (off for new plans). This only sizes the estimate: the saved archive cadence and retention do not turn uploads on.</label>

    <div className="flex gap-4 text-xs">
      <label className="flex gap-1 items-center"><input type="checkbox" checked={encrypted} onChange={(e) => setEncrypted(e.target.checked)} />
        {table.addOns.encryptedDelivery.label}: {usd(table.addOns.encryptedDelivery.usdPerDeviceMonth)} per device per month (chosen per release)</label>
      <label className="flex gap-1 items-center"><input type="checkbox" checked={annual} onChange={(e) => setAnnual(e.target.checked)} />
        Annual prepay ({table.base.annualPrepayDiscountPercent}% off the SuiFleet price)</label>
    </div>

    {result.error && <p role="alert">{result.error}</p>}
    {result.network && result.price && <div className="grid md:grid-cols-2 gap-3">
      <div className="border rounded p-2" data-testid="network-cost">
        <h3 className="font-semibold text-xs">Network cost per month, at cost (paid from your fleet budget)</h3>
        <p className="text-xs">Per device {usd(result.network.usdPerDevice)}; per fleet {usd(result.network.usdPerFleet)}, including {usd(result.network.base.releasesUsdPerFleet)} of release uploads.</p>
        <p className="text-xs text-gray-600">Device activity and storage fees only. This deposit is separate from the SuiFleet subscription and has no SuiFleet markup.</p>
      </div>
      <div className="border rounded p-2" data-testid="suifleet-price">
        <h3 className="font-semibold text-xs">SuiFleet price per month</h3>
        <p className="text-xs">Base {usd(result.price.base.usd)}{result.price.base.custom ? " (custom pricing above the last tier, from this rate)" : ""}; add-ons {usd(result.price.addOns.reduce((sum, a) => sum + a.usdPerFleet, 0))}
          {result.price.minimumApplied ? `; ${usd(table.base.monthlyMinimumUsd)} monthly minimum applies` : ""}{result.price.discount ? `; annual prepay −${usd(result.price.discount)}` : ""}.</p>
        <p className="text-xs">Per fleet <strong>{usd(result.price.monthly)}</strong>; per device {usd(result.price.perDevice)}. Activation {usd(result.price.activationOneTime)} one-time.</p>
        <p className="text-xs">Device data: {usd(result.price.data.monthlyUsd)} per month ({usd(result.price.data.writeUsd)} writes + {usd(result.price.data.storageUsd)} storage), added after the plan minimum and annual discount. {result.price.data.allowance.toLocaleString("en-US")} bytes written per month included across this fleet, with {table.data.includedRetentionDays} days storage for that allowance.</p>
        <p className="text-xs">Proposed data rates: {usd(table.data.writeUsdPerGB)}/GB written and {usd(table.data.storageUsdPerGBMonth)}/GB-month stored (decimal GB). Steady-state storage at the selected retention; owner release uploads are excluded from this data add-on. Log and additional device-data volume both fund the recommended network deposit below.</p>
        <p className="text-xs text-gray-600">Planning prices only. This page does not activate or charge a subscription.</p>
      </div>
    </div>}

    <p className="text-xs text-gray-600">Estimated costs can change. Your account approval app shows the actual network fee before you confirm.</p>
    <details className="text-xs text-gray-700 border rounded p-2" data-testid="quote"><summary>Technical cost assumptions and exchange rates</summary>
      <p>Gas: {quote.referenceGasPrice.toString()} MIST per unit, {quote.gasSource}, {quoteDate(quote.gasObservedAt)}{gasError ? ` (live read failed: ${formatStorageQueryError(gasError, "Gas price")})` : ""}. A deployment receipt costs about {formatToken(fee.receiptNetMist)} SUI and a health report {formatToken(fee.recurringHealthNetMist)} SUI at this price: controlled update-result and test-network health measurements with computation rescaled from {MEASURED.referenceGasPrice.toString()} MIST.</p>
      <p>Walrus: {quote.walrusStoragePrice.toString()} FROST per MiB-epoch and {quote.walrusWritePrice.toString()} per MiB written, {quote.walrusSource}, {quoteDate(quote.walrusObservedAt)}{walrusError ? ` (live read failed: ${formatStorageQueryError(walrusError, "Walrus price")})` : ""}. Epochs here last {epochDays(NETWORK)} day(s).</p>
      <p>USD: SUI ${quote.usd.SUI}, WAL ${quote.usd.WAL}, {quote.usdSource}, {quoteDate(quote.usdObservedAt)}.{" "}
        <button type="button" className="underline" disabled={usdLoading} onClick={() => void refreshUsd()}>Fetch live USD prices from CoinGecko</button>
        {usdError && <span role="alert"> {usdError}</span>}</p>
      <p>Estimates, not quotes: the wallet shows each transaction&apos;s actual cost before approval.</p>
    </details>

    {result.deposit && shortfall && <div className="border rounded p-2 text-xs" data-testid="deposit">
      <h3 className="font-semibold">Recommended deposit for {months} month(s) of these settings</h3>
      <p>{formatToken(result.deposit.suiDepositMist)} SUI (device activity and starting balances, with 20% headroom) and {formatToken(result.deposit.walDepositFrost)} WAL (storage and uploads), about {usd(result.deposit.usd.total)}.</p>
      <p>Your fleet budget holds {budget ? `${formatToken(budget.balance)} SUI` : "nothing yet (no budget)"}{walCounted ? `; fleet storage holds ${storage ? `${formatToken(storage.walBalance)} WAL` : "nothing yet"}` : "; this deployment has no fleet storage, so Walrus fees are paid by each upload's wallet"}.</p>
      {shortfall.enough ? <p role="status">The current deposit covers this plan.</p>
        : <p>Short by {formatToken(shortfall.suiMist)} SUI{walCounted ? ` and ${formatToken(shortfall.walFrost)} WAL` : ""}. <a className="underline" href="/budget/">Deposit on the Network budget page</a>.</p>}
    </div>}

    </details>
    <section className="rounded-xl border border-blue-100 bg-blue-50/60 p-5 space-y-3">
      <h2 className="font-semibold">Review and save</h2><p className="text-sm text-slate-600">Save only the device reporting and retention settings above. They are stored on Sui with your fleet budget; devices apply them at their next check. Your wallet pays the network fee for this save. No deposit or subscription payment is made.</p>
    <div className="space-y-2 text-xs">
      {!account && <p>Connect a wallet to save settings.</p>}
      {saver.reason && <p>{saver.reason}</p>}
      {saved.error && <p role="alert">{formatStorageQueryError(saved.error, "Saved fleet settings")}</p>}
      {saved.data === null && <p data-testid="saved">No settings saved yet: devices keep their local configuration until you save.</p>}
      {saved.data && <details data-testid="saved"><summary>Current saved reporting and storage settings</summary><p>Saved on chain: checks every {saved.data.checkIntervalMs / 60_000} min, rollout fast checks {saved.data.rolloutCheckIntervalMs ? `every ${saved.data.rolloutCheckIntervalMs / 1000} s` : "off"}, health every {saved.data.healthIntervalMs / 3_600_000} h, archives every {saved.data.archiveIntervalMs / 3_600_000} h kept {saved.data.archiveRetentionEpochs} epoch(s){savedValues(table, saved.data) ? "" : " (not one of the listed options; choosing here replaces it)"}.</p></details>}
      <button type="button" className="bg-blue-600 text-white rounded px-2 py-1 disabled:opacity-40" disabled={!account || !saver.build || !result.settings || mutation.isPending}
        onClick={() => void save(false)}>Save settings</button>
      {warning && shortfall && !shortfall.enough && <div role="alert" className="border border-amber-500 bg-amber-50 rounded p-2 space-y-1">
        <p>Not enough deposit for these settings: the fleet needs {formatToken(shortfall.suiMist)} more SUI{walCounted ? ` and ${formatToken(shortfall.walFrost)} more WAL` : ""} to run {months} month(s).
          Devices stop sending health reports and archives when they cannot pay network fees. You have not approved any changes yet.</p>
        <div className="flex gap-2">
          <a className="underline" href="/budget/">Deposit first</a>
          <button type="button" className="border rounded px-2 py-1" disabled={mutation.isPending} onClick={() => void save(true)}>Save anyway</button>
        </div>
      </div>}
      {message && <div><p role="status">Settings saved. Devices apply them at their next check.</p><details><summary>Confirmation details</summary><p className="break-all">{message}</p></details></div>}
      {error && <p role="alert">{error}</p>}
    </div>
    </section>
  </div>;
}

function StorageEndpoints() {
  const [publishers, setPublishers] = useState<string[]>([]);
  useEffect(() => { setPublishers(sessionEndpoints()); }, []);
  const [url, setUrl] = useState("");
  const [message, setMessage] = useState("");
  return <section className="border rounded p-4 space-y-2"><h2>Storage endpoints</h2>
    <p>Publishers receive the bytes you upload. Only add a service you trust. This override lasts for this tab session; fleet-paid storage keeps its own route.</p>
    <label>Publisher URL<input type="url" className="border p-2 block w-full" value={url} onChange={e => setUrl(e.target.value)} /></label>
    <button type="button" onClick={() => { try { setMessage(`Added ${addSessionPublisher(url)} for this session; it will be tried first.`); setUrl(""); setPublishers(sessionEndpoints()); } catch (e) { setMessage(e instanceof Error ? e.message : String(e)); } }}>Add publisher for session</button>
    <ul>{publishers.map(endpoint => <li key={endpoint}>{endpoint} <button type="button" onClick={() => { try { removeSessionPublisher(endpoint); setPublishers(sessionEndpoints()); setMessage(`Removed ${endpoint}`); } catch (e) { setMessage(String(e)); } }}>Remove publisher</button></li>)}</ul>
    {message && <p role="status">{message}</p>}
  </section>;
}
