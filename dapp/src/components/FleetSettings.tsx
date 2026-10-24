"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NETWORK } from "@/lib/constants";
import { useDevices } from "@/hooks/useDevices";
import { useCostQuote } from "@/hooks/useCostQuote";
import { useFleetStorage } from "@/hooks/useFleetStorage";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { quoteDate } from "@/lib/cost-quote";
import { SETTING_KEYS, fleetPrice, loadPriceTable, type PriceTable, type SettingKey } from "@/lib/fleet-pricing";
import {
  DEFAULT_WORKLOAD, defaultValues, depositShortfall, epochDays, fleetNetworkCost, recommendDeposit, retentionEpochs, settingNetworkCost, toSettings, type Workload,
} from "@/lib/fleet-settings-cost";
import { SETTINGS_BOUNDS, fetchFleetSettings, settingsSupported, type FleetSettings } from "@/lib/fleet-settings";
import { settingsSaver } from "@/lib/fleet-settings-tx";
import { MEASURED, formatToken, rescaled } from "@/lib/network-costs";

const TITLES: Record<SettingKey, string> = {
  checkIntervalMs: "Update check",
  rolloutCheckIntervalMs: "Rollout fast-check",
  healthIntervalMs: "On-chain health",
  archiveIntervalMs: "Log archives",
  archiveRetentionDays: "Log retention",
};
const MIB = 1024 * 1024;

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
  if (!table) return <p role="alert" className="text-sm">Prices unavailable: {error}</p>;
  return <SettingsForm table={table} />;
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
      const price = fleetPrice(table, workload.devices, { values, encryptedDelivery: encrypted, annualPrepay: annual });
      const deposit = recommendDeposit(settings, workload, months, quote);
      return { settings, network, price, deposit };
    } catch (cause) { return { error: (cause as Error).message }; }
  }, [values, workload, months, encrypted, annual, quote, table]);

  const option = (key: SettingKey, value: number): { settings?: FleetSettings; reason?: string } => {
    try { return { settings: toSettings({ ...values, [key]: value }, NETWORK) }; }
    catch (cause) {
      if (key !== "archiveRetentionDays") return { reason: (cause as Error).message };
      return { reason: `Unavailable here: needs ${retentionEpochs(value, NETWORK)} Walrus epochs of ${epochDays(NETWORK)} day(s); the limit is ${SETTINGS_BOUNDS.archiveRetentionEpochs[1]}` };
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

  const set = (key: keyof Workload, scale = 1) => (value: number) => setWorkload({ ...workload, [key]: Math.round(value * scale) });
  const fee = rescaled(quote);
  return <div className="space-y-4 text-sm">
    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
      <NumberField label="Devices" value={workload.devices} onChange={set("devices")} />
      <NumberField label="Deployments per month" value={workload.deploymentsPerMonth} onChange={set("deploymentsPerMonth")} />
      <NumberField label="Log data per device per month (MiB)" step={0.1} value={workload.logBytesPerDeviceMonth / MIB} onChange={set("logBytesPerDeviceMonth", MIB)} />
      <NumberField label="Releases per month" value={workload.releasesPerMonth} onChange={set("releasesPerMonth")} />
      <NumberField label="Release artifact size (MiB)" step={0.1} value={workload.artifactBytes / MIB} onChange={set("artifactBytes", MIB)} />
      <NumberField label="Months to prepay" value={months} onChange={setMonths} />
    </div>

    {SETTING_KEYS.map((key) => <fieldset key={key} className="border rounded p-2">
      <legend className="px-1 text-xs font-semibold">{TITLES[key]}</legend>
      <table className="w-full text-xs">
        <thead><tr className="text-left">
          <th className="p-1">Option</th><th className="p-1">Usage per device</th>
          <th className="p-1 text-right">Network cost per device</th><th className="p-1 text-right">Network cost per fleet</th>
          <th className="p-1 text-right">SuiFleet price per device</th><th className="p-1 text-right">SuiFleet price per fleet</th>
        </tr></thead>
        <tbody>{table.settings[key].map((o) => {
          const { settings, reason } = option(key, o.value);
          const network = settings ? settingNetworkCost(key, settings, workload, quote) : undefined;
          const id = `${key}-${o.value}`;
          return <tr key={o.value} className="border-t" data-testid={id}>
            <td className="p-1"><label htmlFor={id} className="flex gap-1 items-center">
              <input id={id} type="radio" name={key} checked={values[key] === o.value} disabled={!settings} onChange={() => { setValues({ ...values, [key]: o.value }); setTouched(true); }} />
              {o.label}{o.default ? " (default)" : ""}</label></td>
            <td className="p-1">{network?.detail ?? reason}</td>
            <td className="p-1 text-right">{network ? usd(network.usdPerDevice) : "–"}</td>
            <td className="p-1 text-right">{network ? usd(network.usdPerFleet) : "–"}</td>
            <td className="p-1 text-right">{o.usdPerDeviceMonth ? usd(o.usdPerDeviceMonth) : "included"}</td>
            <td className="p-1 text-right">{o.usdPerDeviceMonth ? usd(o.usdPerDeviceMonth * workload.devices) : "included"}</td>
          </tr>;
        })}</tbody>
      </table>
    </fieldset>)}
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
        <p className="text-xs text-gray-600">Sui gas and Walrus fees only. SuiFleet adds no markup to this deposit.</p>
      </div>
      <div className="border rounded p-2" data-testid="suifleet-price">
        <h3 className="font-semibold text-xs">SuiFleet price per month</h3>
        <p className="text-xs">Base {usd(result.price.base.usd)}{result.price.base.custom ? " (custom pricing above the last tier, from this rate)" : ""}; add-ons {usd(result.price.addOns.reduce((sum, a) => sum + a.usdPerFleet, 0))}
          {result.price.minimumApplied ? `; ${usd(table.base.monthlyMinimumUsd)} monthly minimum applies` : ""}{result.price.discount ? `; annual prepay −${usd(result.price.discount)}` : ""}.</p>
        <p className="text-xs">Per fleet <strong>{usd(result.price.monthly)}</strong>; per device {usd(result.price.perDevice)}. Activation {usd(result.price.activationOneTime)} one-time.</p>
        <p className="text-xs text-gray-600">From {table.source}.</p>
      </div>
    </div>}

    <div className="text-xs text-gray-700 border rounded p-2" data-testid="quote">
      <p>Gas: {quote.referenceGasPrice.toString()} MIST per unit, {quote.gasSource}, {quoteDate(quote.gasObservedAt)}{gasError ? ` (live read failed: ${gasError})` : ""}. A deployment receipt costs about {formatToken(fee.receiptNetMist)} SUI and a health report {formatToken(fee.recurringHealthNetMist)} SUI at this price: Testnet measurements with computation rescaled from {MEASURED.referenceGasPrice.toString()} MIST.</p>
      <p>Walrus: {quote.walrusStoragePrice.toString()} FROST per MiB-epoch and {quote.walrusWritePrice.toString()} per MiB written, {quote.walrusSource}, {quoteDate(quote.walrusObservedAt)}{walrusError ? ` (live read failed: ${walrusError})` : ""}. Epochs here last {epochDays(NETWORK)} day(s).</p>
      <p>USD: SUI ${quote.usd.SUI}, WAL ${quote.usd.WAL}, {quote.usdSource}, {quoteDate(quote.usdObservedAt)}.{" "}
        <button type="button" className="underline" disabled={usdLoading} onClick={() => void refreshUsd()}>Fetch live USD prices from CoinGecko</button>
        {usdError && <span role="alert"> {usdError}</span>}</p>
      <p>Estimates, not quotes: the wallet shows each transaction&apos;s actual cost before approval.</p>
    </div>

    {result.deposit && shortfall && <div className="border rounded p-2 text-xs" data-testid="deposit">
      <h3 className="font-semibold">Recommended deposit for {months} month(s) of these settings</h3>
      <p>{formatToken(result.deposit.suiDepositMist)} SUI (device gas and floats, 20% headroom) and {formatToken(result.deposit.walDepositFrost)} WAL (Walrus storage and writes), about {usd(result.deposit.usd.total)}.</p>
      <p>Your fleet budget holds {budget ? `${formatToken(budget.balance)} SUI` : "nothing yet (no budget)"}{walCounted ? `; fleet storage holds ${storage ? `${formatToken(storage.walBalance)} WAL` : "nothing yet"}` : "; this deployment has no fleet storage, so Walrus fees are paid by each upload's wallet"}.</p>
      {shortfall.enough ? <p role="status">The current deposit covers this plan.</p>
        : <p>Short by {formatToken(shortfall.suiMist)} SUI{walCounted ? ` and ${formatToken(shortfall.walFrost)} WAL` : ""}. <a className="underline" href="/budget/">Deposit on the Network budget page</a>.</p>}
    </div>}

    <div className="space-y-2 text-xs">
      {!account && <p>Connect a wallet to save settings.</p>}
      {saver.reason && <p>{saver.reason}</p>}
      {saved.error && <p role="alert">Saved settings unavailable: {saved.error.message}</p>}
      {saved.data === null && <p data-testid="saved">No settings saved yet: devices keep their local configuration until you save.</p>}
      {saved.data && <p data-testid="saved">Saved on chain: checks every {saved.data.checkIntervalMs / 60_000} min, rollout fast checks {saved.data.rolloutCheckIntervalMs ? `every ${saved.data.rolloutCheckIntervalMs / 1000} s` : "off"}, health every {saved.data.healthIntervalMs / 3_600_000} h, archives every {saved.data.archiveIntervalMs / 3_600_000} h kept {saved.data.archiveRetentionEpochs} epoch(s){savedValues(table, saved.data) ? "" : " (not one of the listed options; choosing here replaces it)"}.</p>}
      <button type="button" className="bg-blue-600 text-white rounded px-2 py-1 disabled:opacity-40" disabled={!account || !saver.build || !result.settings || mutation.isPending}
        onClick={() => void save(false)}>Save settings</button>
      {warning && shortfall && !shortfall.enough && <div role="alert" className="border border-amber-500 bg-amber-50 rounded p-2 space-y-1">
        <p>Not enough deposit for these settings: the fleet needs {formatToken(shortfall.suiMist)} more SUI{walCounted ? ` and ${formatToken(shortfall.walFrost)} more WAL` : ""} to run {months} month(s).
          Devices stop sending health reports and archives when their gas runs out. Nothing has been signed.</p>
        <div className="flex gap-2">
          <a className="underline" href="/budget/">Deposit first</a>
          <button type="button" className="border rounded px-2 py-1" disabled={mutation.isPending} onClick={() => void save(true)}>Save anyway</button>
        </div>
      </div>}
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  </div>;
}
