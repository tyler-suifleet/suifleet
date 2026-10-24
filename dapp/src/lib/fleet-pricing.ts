// SF-131: the SuiFleet price list, in one table. Every default and add-on price
// the settings page shows comes from here; nothing else hard-codes a price.
// Source: the 2026-10-23 pricing proposal in docs/BUSINESS_PLAN.md (merged in
// PR #268), not yet approved as a final price. A deployment can replace the whole table
// with NEXT_PUBLIC_FLEET_PRICE_TABLE (JSON of the same shape); it is validated
// and refused, not partially applied, when malformed.
//
// This is the SuiFleet price, kept separate from network cost
// (lib/network-costs.ts), which is paid from the fleet budget.

/** The fleet cost settings (SF-130 fields) that carry a price per option. */
export const SETTING_KEYS = ["checkIntervalMs", "rolloutCheckIntervalMs", "healthIntervalMs", "archiveIntervalMs", "archiveRetentionDays"] as const;
export type SettingKey = typeof SETTING_KEYS[number];

export interface PriceOption {
  label: string;
  /** The setting's value: milliseconds for intervals (0 = off), days for retention. */
  value: number;
  /** Add-on price, USD per device per month; 0 when included in the base price. */
  usdPerDeviceMonth: number;
  default?: boolean;
}

export interface PriceTable {
  source: string;
  currency: "USD";
  base: {
    /** Graduated per-device tiers; each tier applies to devices up to `upToDevices`. */
    tiers: { upToDevices: number; usdPerDeviceMonth: number }[];
    /** Above the last tier, pricing is custom, starting from this rate. */
    customFromUsdPerDeviceMonth: number;
    monthlyMinimumUsd: number;
    activationUsdPerDevice: number;
    annualPrepayDiscountPercent: number;
  };
  settings: Record<SettingKey, PriceOption[]>;
  addOns: { encryptedDelivery: { label: string; usdPerDeviceMonth: number } };
}

const MINUTE = 60_000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;

export const DEFAULT_PRICE_TABLE: PriceTable = {
  source: "docs/BUSINESS_PLAN.md pricing proposal, 2026-10-23 (not yet approved)",
  currency: "USD",
  base: {
    tiers: [{ upToDevices: 10_000, usdPerDeviceMonth: 0.08 }, { upToDevices: 100_000, usdPerDeviceMonth: 0.05 }, { upToDevices: 1_000_000, usdPerDeviceMonth: 0.04 }],
    customFromUsdPerDeviceMonth: 0.03,
    monthlyMinimumUsd: 49,
    activationUsdPerDevice: 0.10,
    annualPrepayDiscountPercent: 10,
  },
  settings: {
    checkIntervalMs: [
      { label: "Every 5 minutes", value: 5 * MINUTE, usdPerDeviceMonth: 0.05 },
      { label: "Every 15 minutes", value: 15 * MINUTE, usdPerDeviceMonth: 0.01 },
      { label: "Every 30 minutes", value: 30 * MINUTE, usdPerDeviceMonth: 0, default: true },
      { label: "Hourly", value: HOUR, usdPerDeviceMonth: 0 },
    ],
    rolloutCheckIntervalMs: [
      { label: "Every 60 seconds during rollouts", value: MINUTE, usdPerDeviceMonth: 0, default: true },
      { label: "Every 5 minutes during rollouts", value: 5 * MINUTE, usdPerDeviceMonth: 0 },
      { label: "Off (normal checks only)", value: 0, usdPerDeviceMonth: 0 },
    ],
    healthIntervalMs: [
      { label: "Hourly", value: HOUR, usdPerDeviceMonth: 0.12 },
      { label: "Every 6 hours", value: 6 * HOUR, usdPerDeviceMonth: 0.03 },
      { label: "Daily", value: DAY, usdPerDeviceMonth: 0, default: true },
    ],
    archiveIntervalMs: [
      { label: "Daily", value: DAY, usdPerDeviceMonth: 0.08 },
      { label: "Weekly", value: 7 * DAY, usdPerDeviceMonth: 0, default: true },
    ],
    archiveRetentionDays: [
      { label: "6 weeks", value: 42, usdPerDeviceMonth: 0, default: true },
      { label: "6 months", value: 182, usdPerDeviceMonth: 0.02 },
      { label: "1 year", value: 365, usdPerDeviceMonth: 0.04 },
    ],
  },
  addOns: { encryptedDelivery: { label: "Encrypted delivery (Seal)", usdPerDeviceMonth: 0.02 } },
};

function fail(message: string): never { throw new Error(`Price table: ${message}`); }
const price = (value: unknown, name: string) => {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1_000_000) fail(`${name} must be a price from 0 to 1,000,000`);
  return value;
};
const whole = (value: unknown, name: string, min: number) => {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min) fail(`${name} must be a whole number of at least ${min}`);
  return value;
};

/** Check a table's shape and consistency: positive rising tiers, one default per setting, distinct values. */
export function validatePriceTable(raw: unknown): PriceTable {
  const t = raw as PriceTable;
  if (!t || typeof t !== "object" || !t.base || !t.settings || !t.addOns?.encryptedDelivery) fail("missing base, settings or addOns");
  if (typeof t.source !== "string" || !t.source) fail("source is required");
  if (t.currency !== "USD") fail("currency must be USD");
  if (!Array.isArray(t.base.tiers) || t.base.tiers.length === 0) fail("at least one base tier is required");
  let previous = 0;
  for (const [i, tier] of t.base.tiers.entries()) {
    if (whole(tier.upToDevices, `tier ${i + 1} upToDevices`, 1) <= previous) fail("tiers must rise in upToDevices");
    previous = tier.upToDevices;
    price(tier.usdPerDeviceMonth, `tier ${i + 1} price`);
  }
  for (const key of ["customFromUsdPerDeviceMonth", "monthlyMinimumUsd", "activationUsdPerDevice"] as const) price(t.base[key], key);
  if (price(t.base.annualPrepayDiscountPercent, "annualPrepayDiscountPercent") > 100) fail("annualPrepayDiscountPercent must be at most 100");
  for (const key of SETTING_KEYS) {
    const options = t.settings[key];
    if (!Array.isArray(options) || options.length === 0) fail(`${key} needs at least one option`);
    if (options.filter((o) => o.default).length !== 1) fail(`${key} needs exactly one default`);
    if (new Set(options.map((o) => o.value)).size !== options.length) fail(`${key} options must have distinct values`);
    for (const option of options) {
      if (typeof option.label !== "string" || !option.label) fail(`${key} option needs a label`);
      whole(option.value, `${key} value`, 0);
      price(option.usdPerDeviceMonth, `${key} ${option.label}`);
    }
  }
  if (typeof t.addOns.encryptedDelivery.label !== "string") fail("encryptedDelivery needs a label");
  price(t.addOns.encryptedDelivery.usdPerDeviceMonth, "encryptedDelivery");
  return t;
}

/** The configured table: the build's override when set, otherwise the 2026-10-23 proposal. */
export function loadPriceTable(override: string | undefined = process.env.NEXT_PUBLIC_FLEET_PRICE_TABLE): { table?: PriceTable; error?: string } {
  if (!override) return { table: DEFAULT_PRICE_TABLE };
  try { return { table: validatePriceTable(JSON.parse(override)) }; }
  catch (error) { return { error: error instanceof Error ? error.message : String(error) }; }
}

export function defaultOption(table: PriceTable, key: SettingKey): PriceOption {
  return table.settings[key].find((o) => o.default)!;
}

/** The option of `key` with `value`, if the table offers it. */
export function optionFor(table: PriceTable, key: SettingKey, value: number): PriceOption | undefined {
  return table.settings[key].find((o) => o.value === value);
}

/** Base price per month for `devices`: graduated tiers; above the last tier the custom starting rate. */
export function basePrice(table: PriceTable, devices: number) {
  let usd = 0, counted = 0;
  for (const tier of table.base.tiers) {
    const inTier = Math.max(0, Math.min(devices, tier.upToDevices) - counted);
    usd += inTier * tier.usdPerDeviceMonth;
    counted += inTier;
  }
  const custom = devices > counted;
  usd += (devices - counted) * table.base.customFromUsdPerDeviceMonth;
  return { usd, custom };
}

export interface PriceSelection { values: Record<SettingKey, number>; encryptedDelivery: boolean; annualPrepay: boolean }

/** The SuiFleet price for a fleet: base, each add-on per device and per fleet, the minimum, annual discount and activation. */
export function fleetPrice(table: PriceTable, devices: number, selection: PriceSelection) {
  if (!Number.isSafeInteger(devices) || devices < 0) throw new Error("devices must be a whole number");
  const base = basePrice(table, devices);
  const addOns = SETTING_KEYS.map((key) => {
    const option = optionFor(table, key, selection.values[key]);
    if (!option) throw new Error(`${key}: ${selection.values[key]} is not an option in the price table`);
    return { key, label: option.label, usdPerDevice: option.usdPerDeviceMonth, usdPerFleet: option.usdPerDeviceMonth * devices };
  });
  if (selection.encryptedDelivery) {
    const seal = table.addOns.encryptedDelivery;
    addOns.push({ key: "encryptedDelivery" as SettingKey, label: seal.label, usdPerDevice: seal.usdPerDeviceMonth, usdPerFleet: seal.usdPerDeviceMonth * devices });
  }
  const subtotal = base.usd + addOns.reduce((sum, a) => sum + a.usdPerFleet, 0);
  const minimumApplied = subtotal < table.base.monthlyMinimumUsd;
  const beforeDiscount = Math.max(subtotal, table.base.monthlyMinimumUsd);
  const discount = selection.annualPrepay ? beforeDiscount * table.base.annualPrepayDiscountPercent / 100 : 0;
  const monthly = beforeDiscount - discount;
  return { base, addOns, subtotal, minimumApplied, discount, monthly,
    perDevice: devices ? monthly / devices : 0, activationOneTime: devices * table.base.activationUsdPerDevice };
}
