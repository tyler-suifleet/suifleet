// Up-front network-cost estimate for a fleet budget (SF-083), from the SF-054
// measurements in docs/business/sf054. scripts/test_fleet_budget.cjs checks
// these constants against those evidence files. Estimates, not quotes: the
// wallet shows each transaction's actual cost before approval.

/** Measured inputs, with their SF-054 sources. */
export const MEASURED = {
  /** Applied status receipt, net MIST (computation + storage − rebate); historical Testnet effects. */
  receiptNetMist: BigInt(1_251_568),
  /** Every Sui transaction pays at least this computation fee (MIST, at the measured reference price). */
  minComputationMist: BigInt(1_000_000),
  /** One fleet_budget::draw_gas: computation plus the new coin's storage (net MIST). Updated from the SF-083 testnet run. */
  drawNetMist: BigInt(1_988_000),
  walrus: {
    nShards: 1000,
    /** FROST per encoded MiB per epoch (Mainnet epoch 42). */
    storagePricePerUnit: BigInt(57_321),
    /** FROST per encoded MiB written. */
    writePricePerUnit: BigInt(104_135),
    /** Nominal Mainnet epoch; Testnet epochs are one day. */
    mainnetEpochDays: 14,
  },
  /** Indicative CoinGecko spot quotes; exclude spread, slippage, taxes and acquisition fees. */
  usd: { SUI: 1.048, WAL: 0.04023726, observedAt: 1792604183 },
} as const;

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

/** Storage for `epochs` plus the one-time write fee, in FROST. */
export function blobCostFrost(bytes: number, epochs: number) {
  const units = storageUnits(encodedBlobLength(bytes));
  const storage = units * MEASURED.walrus.storagePricePerUnit * BigInt(epochs);
  const write = units * MEASURED.walrus.writePricePerUnit;
  return { encoded: encodedBlobLength(bytes), units, storage, write };
}

export interface EstimateInput {
  devices: number;
  months: number;
  /** Status receipts per device per month. */
  receiptsPerDeviceMonth: number;
  /** Other device transactions (data anchors) per device per month. */
  anchorsPerDeviceMonth: number;
  /** Releases uploaded per month; each is one artifact plus its recipe. */
  releasesPerMonth: number;
  artifactBytes: number;
  /** Device data stored on Walrus per device per month (one blob each). */
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
  /** Indicative USD at the recorded spot quotes. */
  usd: { sui: number; wal: number; total: number };
}

const RECIPE_ENVELOPE_BYTES = 860;

function checked(name: string, value: number, max: number) {
  if (!Number.isFinite(value) || value < 0 || value > max || !Number.isInteger(value)) throw new Error(`${name} must be a whole number from 0 to ${max}`);
  return value;
}

function margin(value: bigint, percent: number): bigint {
  return value + (value * BigInt(Math.round(percent * 100))) / BigInt(10_000);
}

export function estimate(input: EstimateInput): Estimate {
  const devices = checked("devices", input.devices, 1_000_000);
  const months = checked("months", input.months, 120);
  const perDevice = checked("receipts", input.receiptsPerDeviceMonth, 100_000) + checked("anchors", input.anchorsPerDeviceMonth, 100_000);
  checked("releases", input.releasesPerMonth, 10_000);
  checked("artifact bytes", input.artifactBytes, 1 << 30);
  checked("data bytes", input.dataBytesPerDeviceMonth, 1 << 30);
  checked("storage epochs", input.storageEpochs, 53);
  checked("margin", input.marginPercent, 1000);
  if (input.drawMist <= BigInt(0) || input.floatMist < BigInt(0)) throw new Error("draw must be positive and float non-negative");
  const deviceTransactions = devices * months * perDevice;
  const spend = BigInt(deviceTransactions) * MEASURED.receiptNetMist;
  // The float covers the first spend; later spend is drawn in draw-sized steps.
  const perDeviceSpend = devices ? spend / BigInt(devices) : BigInt(0);
  const drawsPerDevice = perDeviceSpend > input.floatMist ? Number((perDeviceSpend - input.floatMist + input.drawMist - BigInt(1)) / input.drawMist) : 0;
  const draws = drawsPerDevice * devices;
  const gasMist = spend + BigInt(draws) * MEASURED.drawNetMist;
  const floatsMist = BigInt(devices) * input.floatMist;
  const releases = input.releasesPerMonth * months;
  const artifact = blobCostFrost(input.artifactBytes, input.storageEpochs), recipe = blobCostFrost(RECIPE_ENVELOPE_BYTES, input.storageEpochs);
  const artifactFrost = BigInt(releases) * (artifact.storage + artifact.write + recipe.storage + recipe.write);
  const data = input.dataBytesPerDeviceMonth ? blobCostFrost(input.dataBytesPerDeviceMonth, input.storageEpochs) : undefined;
  const dataFrost = data ? BigInt(devices * months) * (data.storage + data.write) : BigInt(0);
  // The floats are paid from the deposit, so they are part of it (not extra spend).
  const suiDepositMist = margin(gasMist, input.marginPercent) + floatsMist;
  const walDepositFrost = margin(artifactFrost + dataFrost, input.marginPercent);
  const sui = Number(suiDepositMist) / 1e9 * MEASURED.usd.SUI, wal = Number(walDepositFrost) / 1e9 * MEASURED.usd.WAL;
  return { deviceTransactions, draws, gasMist, floatsMist, suiDepositMist, artifactFrost, dataFrost, walDepositFrost,
    usd: { sui, wal, total: sui + wal } };
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
