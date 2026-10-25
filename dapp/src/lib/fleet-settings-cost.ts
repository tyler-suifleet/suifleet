// SF-131: the network cost of each per-fleet cost setting per device and per
// fleet, and the fleet-budget deposit the settings need. The settings, their
// bounds and the change transaction are SF-130's (lib/fleet-settings.ts).
// Network cost is computed from lib/network-costs.ts at a dated quote; the
// SuiFleet price comes only from lib/fleet-pricing.ts. The two are never added
// together.
import { DEFAULT_LIMITS } from "@/lib/fleet-budget";
import { blobCostFrost, estimate, rescaled, type CostQuote, type Estimate, type EstimateInput } from "@/lib/network-costs";
import { SETTING_KEYS, defaultOption, type PriceTable, type SettingKey } from "@/lib/fleet-pricing";
import { settingsErrors, type FleetSettings } from "@/lib/fleet-settings";

const HOUR = 3_600_000, DAY = 24 * HOUR;
/** An average calendar month. */
export const MONTH_MS = 365.25 / 12 * DAY;

/** Days per Walrus epoch on this network: two weeks on Mainnet, one day elsewhere. */
export function epochDays(network: string): number {
  return network === "mainnet" ? 14 : 1;
}

/** Retention days to whole Walrus epochs, rounded up so archives are never kept shorter than chosen. */
export function retentionEpochs(days: number, network: string): number {
  return Math.ceil(days / epochDays(network));
}

/** Refuse settings the contract would reject before anything is priced or signed. */
export function validateSettings(s: FleetSettings): FleetSettings {
  const errors = settingsErrors(s);
  if (errors.length) throw new Error(errors.join("; "));
  return s;
}

/** Price-table option values (retention in days) to on-chain settings for `network`. */
export function toSettings(values: Record<SettingKey, number>, network: string): FleetSettings {
  return validateSettings({
    checkIntervalMs: values.checkIntervalMs, rolloutCheckIntervalMs: values.rolloutCheckIntervalMs, healthIntervalMs: values.healthIntervalMs,
    archiveIntervalMs: values.archiveIntervalMs, archiveRetentionEpochs: retentionEpochs(values.archiveRetentionDays, network),
  });
}

/** The price table's defaults (they match the business plan's base product). */
export function defaultValues(table: PriceTable): Record<SettingKey, number> {
  return Object.fromEntries(SETTING_KEYS.map((key) => [key, defaultOption(table, key).value])) as Record<SettingKey, number>;
}

/** What the fleet does each month besides its settings. */
export interface Workload {
  devices: number;
  /** Deployments each device reports a receipt for, per month. */
  deploymentsPerMonth: number;
  /** Whether devices upload logs. Off by default for new plans (BUSINESS_PLAN.md, 2026-10-23); the archive cadence and retention then cost nothing. */
  logUploads: boolean;
  /** Device log data archived per device per month, when log uploads are on. */
  logBytesPerDeviceMonth: number;
  releasesPerMonth: number;
  artifactBytes: number;
}

/** The business plan's typical fleet: four 50 MiB releases a month, log uploads off (10 MiB per device a month once enabled). */
export const DEFAULT_WORKLOAD: Workload = { devices: 100, deploymentsPerMonth: 4, logUploads: false, logBytesPerDeviceMonth: 10 * 1024 * 1024, releasesPerMonth: 4, artifactBytes: 50 * 1024 * 1024 };

/** Log bytes the devices actually upload per device per month: none unless log uploads are on. */
export function uploadedLogBytes(workload: Workload): number {
  return workload.logUploads ? workload.logBytesPerDeviceMonth : 0;
}

export interface NetworkCost {
  /** Sui gas, MIST, and Walrus fees, FROST, per device per month. */
  mistPerDevice: number;
  frostPerDevice: number;
  usdPerDevice: number;
  usdPerFleet: number;
  /** What the line counts per device per month, for display. */
  detail: string;
}

const cost = (mist: number, frost: number, quote: CostQuote, devices: number, detail: string): NetworkCost => {
  const usd = mist / 1e9 * quote.usd.SUI + frost / 1e9 * quote.usd.WAL;
  return { mistPerDevice: mist, frostPerDevice: frost, usdPerDevice: usd, usdPerFleet: usd * devices, detail };
};

/**
 * The monthly network cost one setting adds, per device and per fleet, at
 * `quote`. Update and rollout checks are chain reads: no network fee. Health
 * reports and archives are device transactions; retention is the Walrus
 * storage those archives keep.
 */
export function settingNetworkCost(key: SettingKey, settings: FleetSettings, workload: Workload, quote: CostQuote): NetworkCost {
  const fee = rescaled(quote), devices = workload.devices;
  const prices = { storage: quote.walrusStoragePrice, write: quote.walrusWritePrice };
  const uploads = MONTH_MS / settings.archiveIntervalMs;
  const logBytes = uploadedLogBytes(workload), idle = workload.logUploads ? "no log data" : "log uploads off";
  const perUpload = logBytes > 0 ? blobCostFrost(Math.ceil(logBytes / uploads), settings.archiveRetentionEpochs, prices) : undefined;
  switch (key) {
    case "checkIntervalMs": {
      const checks = MONTH_MS / settings.checkIntervalMs;
      return cost(0, 0, quote, devices, `${Math.round(checks).toLocaleString("en-US")} reads, no network fee`);
    }
    case "rolloutCheckIntervalMs":
      return cost(0, 0, quote, devices, settings.rolloutCheckIntervalMs ? `${Math.round(HOUR / settings.rolloutCheckIntervalMs)} reads per rollout hour, no network fee` : "off");
    case "healthIntervalMs": {
      const reports = MONTH_MS / settings.healthIntervalMs;
      return cost(reports * Number(fee.recurringHealthNetMist), 0, quote, devices, `${trim(reports)} signed health reports`);
    }
    case "archiveIntervalMs":
      if (!perUpload) return cost(0, 0, quote, devices, idle);
      return cost(uploads * Number(fee.archiveNetMist), uploads * Number(perUpload.write), quote, devices, `${trim(uploads)} archives (3 transactions and a Walrus write each)`);
    case "archiveRetentionDays":
      if (!perUpload) return cost(0, 0, quote, devices, idle);
      return cost(0, uploads * Number(perUpload.storage), quote, devices, `${settings.archiveRetentionEpochs} Walrus epoch(s) of storage per archive`);
  }
}

/** Network cost outside the settings: device receipts per device, and owner release uploads per fleet. */
export function baseNetworkCost(workload: Workload, quote: CostQuote, retention: number) {
  const fee = rescaled(quote), prices = { storage: quote.walrusStoragePrice, write: quote.walrusWritePrice };
  const receipts = cost(workload.deploymentsPerMonth * Number(fee.receiptNetMist), 0, quote, workload.devices, `${workload.deploymentsPerMonth} deployment receipts`);
  const artifact = workload.releasesPerMonth ? blobCostFrost(workload.artifactBytes, retention, prices) : undefined;
  const releaseFrost = artifact ? workload.releasesPerMonth * Number(artifact.storage + artifact.write) : 0;
  return { receipts, releasesUsdPerFleet: releaseFrost / 1e9 * quote.usd.WAL, releasesFrostPerFleet: releaseFrost };
}

/** Network cost of every setting plus the base, per device and per fleet. */
export function fleetNetworkCost(settings: FleetSettings, workload: Workload, quote: CostQuote) {
  const lines = SETTING_KEYS.map((key) => ({ key, ...settingNetworkCost(key, settings, workload, quote) }));
  const base = baseNetworkCost(workload, quote, settings.archiveRetentionEpochs);
  const perDevice = lines.reduce((sum, l) => sum + l.usdPerDevice, base.receipts.usdPerDevice);
  const perFleet = perDevice * workload.devices + base.releasesUsdPerFleet;
  return { lines, base, usdPerDevice: perDevice, usdPerFleet: perFleet };
}

/**
 * The estimator input for prepaying `months` of these settings. Per-month
 * counts round up to whole reports/archives, so the deposit never undershoots
 * the displayed rate. Releases keep the archive retention.
 */
export function depositInput(settings: FleetSettings, workload: Workload, months: number): EstimateInput {
  const logBytes = uploadedLogBytes(workload), archives = logBytes > 0 ? Math.ceil(MONTH_MS / settings.archiveIntervalMs) : 0;
  return {
    devices: workload.devices, months, receiptsPerDeviceMonth: workload.deploymentsPerMonth, anchorsPerDeviceMonth: 0,
    healthSummariesPerDeviceMonth: Math.ceil(MONTH_MS / settings.healthIntervalMs), serverlessData: archives > 0,
    releasesPerMonth: workload.releasesPerMonth, artifactBytes: workload.artifactBytes,
    dataUploadsPerDeviceMonth: archives, dataBytesPerDeviceMonth: logBytes, storageEpochs: settings.archiveRetentionEpochs,
    floatMist: DEFAULT_LIMITS.floatCap, drawMist: BigInt(50_000_000), marginPercent: 20,
  };
}

/** The recommended deposit for these settings, fleet size and prepaid months. */
export function recommendDeposit(settings: FleetSettings, workload: Workload, months: number, quote: CostQuote): Estimate {
  return estimate(depositInput(validateSettings(settings), workload, months), quote);
}

/** How far the current budget and fleet storage fall short of the recommendation (zero when enough). */
export function depositShortfall(recommended: Pick<Estimate, "suiDepositMist" | "walDepositFrost">, suiBalanceMist: bigint | undefined, walBalanceFrost: bigint | undefined) {
  const zero = BigInt(0), short = (want: bigint, have: bigint | undefined) => want > (have ?? zero) ? want - (have ?? zero) : zero;
  const sui = short(recommended.suiDepositMist, suiBalanceMist), wal = short(recommended.walDepositFrost, walBalanceFrost);
  return { suiMist: sui, walFrost: wal, enough: sui === zero && wal === zero };
}

function trim(n: number): string {
  return n >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(1).replace(/\.0$/, "");
}
