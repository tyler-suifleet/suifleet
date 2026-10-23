// SF-131: per-fleet cost settings, their network cost per device and per fleet,
// and the fleet-budget deposit they need. Fields and bounds follow the SF-130
// proposal on #265 (check_interval_ms, rollout_check_interval_ms,
// health_interval_ms, archive_interval_ms, archive_retention_epochs); switch to
// SF-130's generated interfaces once it merges. Network cost is computed from
// lib/network-costs.ts at a dated quote; the SuiFleet price comes only from
// lib/fleet-pricing.ts. The two are never added together.
import { DEFAULT_LIMITS } from "@/lib/fleet-budget";
import { blobCostFrost, estimate, rescaled, type CostQuote, type Estimate, type EstimateInput } from "@/lib/network-costs";
import { SETTING_KEYS, defaultOption, type PriceTable, type SettingKey } from "@/lib/fleet-pricing";

const MINUTE = 60_000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
/** An average calendar month. */
export const MONTH_MS = 365.25 / 12 * DAY;

/** The on-chain settings, in SF-130's units. */
export interface FleetSettings {
  checkIntervalMs: number;
  /** 0 = off. */
  rolloutCheckIntervalMs: number;
  healthIntervalMs: number;
  archiveIntervalMs: number;
  archiveRetentionEpochs: number;
}

/** SF-130's proposed contract bounds (#265). */
export const SETTINGS_BOUNDS = {
  checkIntervalMs: { min: 5 * MINUTE, max: 6 * HOUR },
  rolloutCheckIntervalMs: { min: 30_000, max: 10 * MINUTE, off: 0 },
  healthIntervalMs: { min: MINUTE, max: DAY },
  archiveIntervalMs: { min: HOUR, max: 7 * DAY },
  archiveRetentionEpochs: { min: 1, max: 53 },
} as const;

/** Days per Walrus epoch on this network: two weeks on Mainnet, one day elsewhere. */
export function epochDays(network: string): number {
  return network === "mainnet" ? 14 : 1;
}

/** Retention days to whole Walrus epochs, rounded up so archives are never kept shorter than chosen. */
export function retentionEpochs(days: number, network: string): number {
  return Math.ceil(days / epochDays(network));
}

/** Refuse a value outside SF-130's bounds before anything is priced or signed. */
export function validateSettings(s: FleetSettings): FleetSettings {
  const within = (name: keyof FleetSettings, value: number, min: number, max: number) => {
    if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error(`${name} must be a whole number from ${min} to ${max}`);
  };
  within("checkIntervalMs", s.checkIntervalMs, SETTINGS_BOUNDS.checkIntervalMs.min, SETTINGS_BOUNDS.checkIntervalMs.max);
  if (s.rolloutCheckIntervalMs !== SETTINGS_BOUNDS.rolloutCheckIntervalMs.off)
    within("rolloutCheckIntervalMs", s.rolloutCheckIntervalMs, SETTINGS_BOUNDS.rolloutCheckIntervalMs.min, SETTINGS_BOUNDS.rolloutCheckIntervalMs.max);
  within("healthIntervalMs", s.healthIntervalMs, SETTINGS_BOUNDS.healthIntervalMs.min, SETTINGS_BOUNDS.healthIntervalMs.max);
  within("archiveIntervalMs", s.archiveIntervalMs, SETTINGS_BOUNDS.archiveIntervalMs.min, SETTINGS_BOUNDS.archiveIntervalMs.max);
  within("archiveRetentionEpochs", s.archiveRetentionEpochs, SETTINGS_BOUNDS.archiveRetentionEpochs.min, SETTINGS_BOUNDS.archiveRetentionEpochs.max);
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
  /** Device log data archived per device per month. */
  logBytesPerDeviceMonth: number;
  releasesPerMonth: number;
  artifactBytes: number;
}

/** The business plan's typical fleet: four 50 MiB releases and 10 MiB of logs per device a month. */
export const DEFAULT_WORKLOAD: Workload = { devices: 100, deploymentsPerMonth: 4, logBytesPerDeviceMonth: 10 * 1024 * 1024, releasesPerMonth: 4, artifactBytes: 50 * 1024 * 1024 };

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
  const perUpload = workload.logBytesPerDeviceMonth > 0 ? blobCostFrost(Math.ceil(workload.logBytesPerDeviceMonth / uploads), settings.archiveRetentionEpochs, prices) : undefined;
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
      if (!perUpload) return cost(0, 0, quote, devices, "no log data");
      return cost(uploads * Number(fee.archiveNetMist), uploads * Number(perUpload.write), quote, devices, `${trim(uploads)} archives (3 transactions and a Walrus write each)`);
    case "archiveRetentionDays":
      if (!perUpload) return cost(0, 0, quote, devices, "no log data");
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
  const archives = workload.logBytesPerDeviceMonth > 0 ? Math.ceil(MONTH_MS / settings.archiveIntervalMs) : 0;
  return {
    devices: workload.devices, months, receiptsPerDeviceMonth: workload.deploymentsPerMonth, anchorsPerDeviceMonth: 0,
    healthSummariesPerDeviceMonth: Math.ceil(MONTH_MS / settings.healthIntervalMs), serverlessData: archives > 0,
    releasesPerMonth: workload.releasesPerMonth, artifactBytes: workload.artifactBytes,
    dataUploadsPerDeviceMonth: archives, dataBytesPerDeviceMonth: workload.logBytesPerDeviceMonth, storageEpochs: settings.archiveRetentionEpochs,
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
