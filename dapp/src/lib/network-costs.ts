// Up-front network-cost estimate for a fleet budget (SF-083), from the SF-054
// measurements in docs/business/sf054 and SF-127 localnet receipt effects.
// scripts/test_fleet_budget.cjs checks
// these constants against those evidence files. Estimates, not quotes: the
// wallet shows each transaction's actual cost before approval.
//
// SF-131: gas was measured on Testnet, whose reference gas price is ten times
// Mainnet's. Only the computation part of a fee scales with that price; storage
// gas is a protocol constant (76 on both networks, docs/business/sf131). Each
// measurement keeps its effects breakdown so `netMist` can rescale it to any
// quoted reference gas price.

/** A transaction's measured gas effects, in MIST at `MEASURED.referenceGasPrice`. */
export interface GasEffects { computation: bigint; storage: bigint; rebate: bigint }
const gas = (computation: number, storage: number, rebate: number): GasEffects =>
  ({ computation: BigInt(computation), storage: BigInt(storage), rebate: BigInt(rebate) });

/** Measured inputs, with their recorded sources. */
export const MEASURED = {
  /** The reference gas price the effects below were paid at (MIST per computation unit). */
  referenceGasPrice: BigInt(1000),
  /** Highest measured single v3 report at 1,000 targets (docs/validation/sf127-localnet.json).
   * Later cleanup rebates belong to the cleanup sender, not the reporting device. */
  receipt: gas(1_160_000, 9_515_200, 4_378_968),
  /** One fleet_budget::draw_gas, measured on testnet (docs/validation/sf083-testnet.json). */
  draw: gas(1_000_000, 6_391_600, 5_403_060),
  /** Observed Testnet SF-082 effects; fixture observations, not fleet-size gas ceilings or a production quote. */
  fleetData: {
    initialHealth: gas(1_000_000, 11_232_800, 4_977_502),
    /**
     * The upper median of the recorded recurring reports. Their maximum
     * includes the summary object's one-time growth after the first report;
     * later reports rewrite it in place, so they pay computation and the 1%
     * non-refundable storage, with swings both ways as the payload changes.
     */
    recurringHealth: gas(1_000_000, 14_690_800, 14_681_732),
    /** Registration, certification and anchor of one encrypted archive. */
    archive: [gas(1_000_000, 142_576_000, 123_920_807), gas(1_000_000, 5_601_200, 5_570_243), gas(1_000_000, 17_274_800, 11_885_211)],
  },
  /** Every Sui transaction pays at least this computation fee (MIST, at the measured reference price). */
  minComputationMist: BigInt(1_000_000),
  walrus: {
    nShards: 1000,
    /** FROST per encoded MiB per epoch (Mainnet epoch 42, SF-054). */
    storagePricePerUnit: BigInt(57_321),
    /** FROST per encoded MiB written. */
    writePricePerUnit: BigInt(104_135),
    /** Nominal Mainnet epoch; Testnet epochs are one day. */
    mainnetEpochDays: 14,
  },
  /** Indicative CoinGecko spot quotes; exclude spread, slippage, taxes and acquisition fees. */
  usd: { SUI: 1.048, WAL: 0.04023726, observedAt: 1792604183 },
} as const;

/** One transaction's net fee at `referenceGasPrice`: computation scales with the price, storage and rebate do not. */
export function netMist(effects: GasEffects | readonly GasEffects[], referenceGasPrice: bigint): bigint {
  const all: readonly GasEffects[] = Array.isArray(effects) ? effects : [effects as GasEffects];
  return all.reduce((sum, e) => sum + e.computation * referenceGasPrice / MEASURED.referenceGasPrice + e.storage - e.rebate, BigInt(0));
}

/**
 * The prices an estimate is computed at. Each part records where it came from
 * and when, so the page can show its date: a live read of the connected
 * network, or the recorded Mainnet snapshot.
 */
export interface CostQuote {
  network: string;
  /** MIST per computation unit. */
  referenceGasPrice: bigint;
  gasSource: string;
  gasObservedAt: number;
  /** FROST per encoded MiB per epoch, and per encoded MiB written. */
  walrusStoragePrice: bigint;
  walrusWritePrice: bigint;
  walrusSource: string;
  walrusObservedAt: number;
  usd: { SUI: number; WAL: number };
  usdSource: string;
  usdObservedAt: number;
}

/** Read-only Mainnet snapshot, docs/business/sf131/mainnet-quote.json (2026-10-23). */
export const MAINNET_QUOTE: CostQuote = {
  network: "mainnet",
  referenceGasPrice: BigInt(100), gasSource: "Mainnet reference gas price (recorded)", gasObservedAt: Date.parse("2026-10-23T21:22:19.715Z"),
  walrusStoragePrice: BigInt(57_389), walrusWritePrice: BigInt(104_316), walrusSource: "Mainnet Walrus epoch 42 (recorded)",
  walrusObservedAt: Date.parse("2026-10-23T21:22:19.715Z"),
  usd: { SUI: 0.979727, WAL: 0.03634265 }, usdSource: "CoinGecko spot (recorded)", usdObservedAt: 1792790518 * 1000,
};

/** The measured constants rescaled to the quote's reference gas price, net MIST per transaction. */
export function rescaled(quote: CostQuote = MAINNET_QUOTE) {
  const rgp = quote.referenceGasPrice;
  return {
    receiptNetMist: netMist(MEASURED.receipt, rgp),
    drawNetMist: netMist(MEASURED.draw, rgp),
    initialHealthNetMist: netMist(MEASURED.fleetData.initialHealth, rgp),
    recurringHealthNetMist: netMist(MEASURED.fleetData.recurringHealth, rgp),
    archiveNetMist: netMist(MEASURED.fleetData.archive, rgp),
  };
}

/** Mainnet-rescaled measured constants, at the recorded Mainnet reference gas price. */
export const MAINNET = rescaled(MAINNET_QUOTE);

/** Archive growth projection; v3 keyed protection has no fleet-wide slot cap. */

export function protectedArchiveCapacity(existing: number, devices: number, uploadsPerDeviceMonth: number, months: number) {
  checked("existing protected blobs", existing, Number.MAX_SAFE_INTEGER);
  checked("devices", devices, 1_000_000);
  checked("device archive uploads", uploadsPerDeviceMonth, 100_000);
  checked("months", months, 120);
  const monthlyUploads = devices * uploadsPerDeviceMonth;
  const planned = monthlyUploads * months;
  return { existing, planned };
}

const MIB = BigInt(1024 * 1024);
export const MIST_PER_SUI = BigInt(1_000_000_000);
export const FROST_PER_WAL = BigInt(1_000_000_000);

/** Walrus RS2 encoded length, as walrus::redstuff::encoded_blob_length computes it on chain. */
export function encodedBlobLength(unencoded: number, nShards: number = MEASURED.walrus.nShards): bigint {
  const n = BigInt(nShards);
  const byzantine = (n - BigInt(1)) / BigInt(3);
  const primary = n - BigInt(2) * byzantine, secondary = n - byzantine;
  const symbols = primary * secondary;
  let symbol = (BigInt(Math.max(1, unencoded)) - BigInt(1)) / symbols + BigInt(1);
  if (symbol % BigInt(2) === BigInt(1)) symbol += BigInt(1);
  const metadata = n * BigInt(32 * 2) + BigInt(32);
  return n * ((primary + secondary) * symbol + metadata);
}

export function storageUnits(encoded: bigint): bigint {
  return (encoded + MIB - BigInt(1)) / MIB;
}

/** Storage for `epochs` plus the one-time write fee, in FROST, at the given (default: SF-054) Walrus prices. */
export function blobCostFrost(bytes: number, epochs: number,
  prices: { storage: bigint; write: bigint } = { storage: MEASURED.walrus.storagePricePerUnit, write: MEASURED.walrus.writePricePerUnit }) {
  const units = storageUnits(encodedBlobLength(bytes));
  const storage = units * prices.storage * BigInt(epochs);
  const write = units * prices.write;
  return { encoded: encodedBlobLength(bytes), units, storage, write };
}

export interface EstimateInput {
  devices: number;
  months: number;
  /** Status receipts per device per month. */
  receiptsPerDeviceMonth: number;
  /** Other device transactions (data anchors) per device per month. */
  anchorsPerDeviceMonth: number;
  /** Signed on-chain health summaries; zero for legacy HTTP telemetry. */
  healthSummariesPerDeviceMonth?: number;
  /** Include registration/certification/anchor gas for each encrypted archive. */
  serverlessData?: boolean;
  /** Existing protected records, including non-device blobs; zero assumes empty storage. */
  existingProtectedBlobs?: number;
  /** Releases uploaded per month; each is one artifact plus its recipe. */
  releasesPerMonth: number;
  artifactBytes: number;
  /** Archive uploads per device per month. Legacy callers default to one. */
  dataUploadsPerDeviceMonth?: number;
  /** Total device data stored on Walrus per device per month, across those uploads. */
  dataBytesPerDeviceMonth: number;
  /** Walrus epochs each blob is kept. */
  storageEpochs: number;
  /** Initial float sent to each device at registration (MIST). */
  floatMist: bigint;
  /** Each device draw (MIST); sets how many draws the gas needs. */
  drawMist: bigint;
  /** Extra headroom, percent. */
  marginPercent: number;
}

export interface Estimate {
  protectedCapacity?: ReturnType<typeof protectedArchiveCapacity>;
  deviceTransactions: number;
  draws: number;
  /** Device gas for receipts/anchors, plus the gas of the draws themselves. */
  gasMist: bigint;
  floatsMist: bigint;
  /** Recommended SUI deposit: floats + gas + margin. */
  suiDepositMist: bigint;
  artifactFrost: bigint;
  dataFrost: bigint;
  /** Recommended WAL deposit (storage + write fees) + margin. */
  walDepositFrost: bigint;
  /** Indicative USD at the quote's spot prices. */
  usd: { sui: number; wal: number; total: number };
  /** The prices this estimate used. */
  quote: CostQuote;
}

const RECIPE_ENVELOPE_BYTES = 860;

function checked(name: string, value: number, max: number) {
  if (!Number.isFinite(value) || value < 0 || value > max || !Number.isInteger(value)) throw new Error(`${name} must be a whole number from 0 to ${max}`);
  return value;
}

function margin(value: bigint, percent: number): bigint {
  return value + (value * BigInt(Math.round(percent * 100))) / BigInt(10_000);
}

/** The estimate at `quote`'s gas and Walrus prices: Mainnet-rescaled by default. */
export function estimate(input: EstimateInput, quote: CostQuote = MAINNET_QUOTE): Estimate {
  const devices = checked("devices", input.devices, 1_000_000);
  const months = checked("months", input.months, 120);
  const perDevice = checked("receipts", input.receiptsPerDeviceMonth, 100_000) + checked("anchors", input.anchorsPerDeviceMonth, 100_000);
  checked("releases", input.releasesPerMonth, 10_000);
  checked("artifact bytes", input.artifactBytes, 1 << 30);
  checked("data bytes", input.dataBytesPerDeviceMonth, 1 << 30);
  const uploads = checked("device archive uploads", input.dataUploadsPerDeviceMonth ?? 1, 100_000);
  if (input.dataBytesPerDeviceMonth > 0 && uploads === 0) throw new Error("Device data needs at least one archive upload");
  checked("storage epochs", input.storageEpochs, 53);
  checked("margin", input.marginPercent, 1000);
  if (input.drawMist <= BigInt(0) || input.floatMist < BigInt(0)) throw new Error("draw must be positive and float non-negative");
  const health = checked("health summaries", input.healthSummariesPerDeviceMonth ?? 0, 100_000);
  const archiveUploads = input.serverlessData && input.dataBytesPerDeviceMonth > 0 ? uploads : 0;
  const protectedCapacity = input.serverlessData ? protectedArchiveCapacity(input.existingProtectedBlobs ?? 0, devices, archiveUploads, months) : undefined;
  if (quote.referenceGasPrice <= BigInt(0) || quote.walrusStoragePrice < BigInt(0) || quote.walrusWritePrice < BigInt(0)) throw new Error("quote prices must be positive");
  const fee = rescaled(quote);
  const deviceTransactions = devices * months * (perDevice + health + 3 * archiveUploads);
  const spend = BigInt(devices * months * perDevice) * fee.receiptNetMist
    + BigInt(devices * months * health) * fee.recurringHealthNetMist
    + (months > 0 && health > 0 ? BigInt(devices) * (fee.initialHealthNetMist - fee.recurringHealthNetMist) : BigInt(0))
    + BigInt(devices * months * archiveUploads) * fee.archiveNetMist;
  // The float covers the first spend; later spend is drawn in draw-sized steps.
  const perDeviceSpend = devices ? spend / BigInt(devices) : BigInt(0);
  const drawsPerDevice = perDeviceSpend > input.floatMist ? Number((perDeviceSpend - input.floatMist + input.drawMist - BigInt(1)) / input.drawMist) : 0;
  const draws = drawsPerDevice * devices;
  const gasMist = spend + BigInt(draws) * fee.drawNetMist;
  const floatsMist = BigInt(devices) * input.floatMist;
  const releases = input.releasesPerMonth * months;
  const prices = { storage: quote.walrusStoragePrice, write: quote.walrusWritePrice };
  const artifact = blobCostFrost(input.artifactBytes, input.storageEpochs, prices), recipe = blobCostFrost(RECIPE_ENVELOPE_BYTES, input.storageEpochs, prices);
  const artifactFrost = BigInt(releases) * (artifact.storage + artifact.write + recipe.storage + recipe.write);
  const data = input.dataBytesPerDeviceMonth ? blobCostFrost(Math.ceil(input.dataBytesPerDeviceMonth / Math.max(1, uploads)), input.storageEpochs, prices) : undefined;
  const dataFrost = data ? BigInt(devices * months * uploads) * (data.storage + data.write) : BigInt(0);
  // The floats are paid from the deposit, so they are part of it (not extra spend).
  const suiDepositMist = margin(gasMist, input.marginPercent) + floatsMist;
  const walDepositFrost = margin(artifactFrost + dataFrost, input.marginPercent);
  const sui = Number(suiDepositMist) / 1e9 * quote.usd.SUI, wal = Number(walDepositFrost) / 1e9 * quote.usd.WAL;
  return { protectedCapacity, deviceTransactions, draws, gasMist, floatsMist, suiDepositMist, artifactFrost, dataFrost, walDepositFrost,
    usd: { sui, wal, total: sui + wal }, quote };
}

/** Days until the budget is empty at the observed spend rate, or null without spend. */
export function runwayDays(balanceMist: bigint, spentMist: bigint, windowDays: number): number | null {
  if (spentMist <= BigInt(0) || windowDays <= 0) return null;
  return Number(balanceMist) / (Number(spentMist) / windowDays);
}

export function formatToken(value: bigint, decimals = 9): string {
  const sign = value < BigInt(0) ? "-" : "", abs = value < BigInt(0) ? -value : value;
  const scale = BigInt(10) ** BigInt(decimals);
  const fraction = (abs % scale).toString().padStart(decimals, "0").replace(/0+$/, "");
  return `${sign}${abs / scale}${fraction ? "." + fraction : ""}`;
}

/** Parse a decimal token amount ("1.5") into base units, refusing excess precision. */
export function parseToken(text: string, decimals = 9): bigint {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(text.trim());
  if (!match) throw new Error("Enter a positive decimal amount");
  const [, whole, fraction = ""] = match;
  if (fraction.length > decimals) throw new Error(`At most ${decimals} decimal places`);
  return BigInt(whole) * BigInt(10) ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, "0") || "0");
}
