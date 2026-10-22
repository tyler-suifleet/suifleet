// Prepaid fleet network costs (SF-083): transaction builders and reads for the
// on-chain fleet budget (sui_edge::fleet_budget) and, when configured, the
// owner-prepaid Walrus storage (fleet_storage). See docs/FLEET_BUDGET.md.
import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { Transaction, coinWithBalance } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { BUDGET_TYPES, MANIFEST, moveType, requireManifest, verifyDeployment } from "./deployment-manifest";
import { calls, fleet_budget_BudgetOwnerCapBcs, fleet_budget_DeviceKeyBcs, fleet_budget_FleetBudgetBcs,
  fleet_budget_GasAccountBcs, fleet_budget_GasDrawnBcs } from "./fleet-protocol";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { allEvents, allOwned } from "./sui";

const CLOCK = "0x6";

/** Whether this deployment's package has the fleet budget (all its type origins). */
export function budgetSupported(manifest = MANIFEST): boolean {
  return !!manifest && BUDGET_TYPES.every((name) => name in manifest.type_origins);
}

function requireBudget() {
  requireManifest();
  if (!budgetSupported()) throw new Error("This deployment's package has no fleet budget; upgrade the contracts first");
}

export interface Limits {
  floatCap: bigint;
  periodMs: bigint;
  periodAllowance: bigint;
  maxDrawsPerPeriod: bigint;
  minDrawIntervalMs: bigint;
}

/** Defaults sized from the SF-054 receipt cost: a float of 50 receipts, 200 per day. */
export const DEFAULT_LIMITS: Limits = {
  floatCap: BigInt(60_000_000),
  periodMs: BigInt(86_400_000),
  periodAllowance: BigInt(250_000_000),
  maxDrawsPerPeriod: BigInt(6),
  minDrawIntervalMs: BigInt(600_000),
};

/**
 * The reserve given to a newly enrolled device's own account: two period
 * allowances, at most what the budget holds after the float. Devices draw
 * only from their reserve; the owner refills or reclaims it.
 */
export function defaultReserve(limits: Limits, available: bigint, floatMist: bigint): bigint {
  const rest = available > floatMist ? available - floatMist : BigInt(0);
  const wanted = limits.periodAllowance * BigInt(2);
  return wanted < rest ? wanted : rest;
}

export function validateLimits(limits: Limits): Limits {
  if (limits.periodMs <= BigInt(0)) throw new Error("The period must be positive");
  if (limits.maxDrawsPerPeriod <= BigInt(0)) throw new Error("Allow at least one draw per period");
  if (limits.floatCap > limits.periodAllowance && limits.periodAllowance > BigInt(0)) throw new Error("The float cap should not exceed the period allowance");
  const ceiling = BigInt(1_000_000_000_000);
  if (limits.floatCap > ceiling || limits.periodAllowance > ceiling) throw new Error("Limits above 1,000 SUI are refused");
  return limits;
}

function limitsArg(tx: Transaction, limits: Limits) {
  validateLimits(limits);
  return calls.fleet_budget_new_limits(tx, PACKAGE_ID, limits.floatCap, limits.periodMs, limits.periodAllowance,
    limits.maxDrawsPerPeriod, limits.minDrawIntervalMs);
}

// ── Owner transactions ───────────────────────────────────────────────────────

/** Create and fund a budget; its owner cap goes to `sender`. */
export function buildCreateBudgetTx(depositMist: bigint, limits: Limits, lowBalanceMist: bigint, sender: string): Transaction {
  requireBudget();
  const tx = new Transaction();
  const [deposit] = tx.splitCoins(tx.gas, [tx.pure.u64(depositMist)]);
  const [cap] = calls.fleet_budget_create(tx, PACKAGE_ID, REGISTRY_ID, deposit, limitsArg(tx, limits), lowBalanceMist);
  tx.transferObjects([cap], tx.pure.address(sender));
  return tx;
}

export function buildDepositTx(budgetId: string, capId: string, amountMist: bigint): Transaction {
  requireBudget();
  if (amountMist <= BigInt(0)) throw new Error("Deposit a positive amount");
  const tx = new Transaction();
  const [coin] = tx.splitCoins(tx.gas, [tx.pure.u64(amountMist)]);
  calls.fleet_budget_deposit(tx, PACKAGE_ID, budgetId, capId, coin);
  return tx;
}

export function buildWithdrawTx(budgetId: string, capId: string, amountMist: bigint, sender: string): Transaction {
  requireBudget();
  if (amountMist <= BigInt(0)) throw new Error("Withdraw a positive amount");
  const tx = new Transaction();
  const [coin] = calls.fleet_budget_withdraw(tx, PACKAGE_ID, budgetId, capId, amountMist);
  tx.transferObjects([coin], tx.pure.address(sender));
  return tx;
}

export function buildSetPausedTx(budgetId: string, capId: string, paused: boolean): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_set_paused(tx, PACKAGE_ID, budgetId, capId, paused);
  return tx;
}

export function buildConfigureTx(budgetId: string, capId: string, limits: Limits, lowBalanceMist: bigint): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_configure(tx, PACKAGE_ID, budgetId, capId, limitsArg(tx, limits), lowBalanceMist);
  return tx;
}

export function buildSetDeviceSuspendedTx(budgetId: string, capId: string, accountId: string, suspended: boolean): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_set_device_suspended(tx, PACKAGE_ID, budgetId, capId, accountId, suspended);
  return tx;
}

export function buildSetDeviceLimitsTx(budgetId: string, capId: string, accountId: string, limits: Limits): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_set_device_limits(tx, PACKAGE_ID, budgetId, capId, accountId, limitsArg(tx, limits));
  return tx;
}

/** Owner-paid gas straight to a device too empty to pay for its own draw. */
export function buildFundDeviceTx(budgetId: string, capId: string, accountId: string, amountMist: bigint): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_fund_device(tx, PACKAGE_ID, budgetId, capId, REGISTRY_ID, accountId, amountMist);
  return tx;
}

/** Move budget funds into a device's reserve, which its draws come from. */
export function buildFundReserveTx(budgetId: string, capId: string, accountId: string, amountMist: bigint): Transaction {
  requireBudget();
  if (amountMist <= BigInt(0)) throw new Error("Refill a positive amount");
  const tx = new Transaction();
  calls.fleet_budget_fund_reserve(tx, PACKAGE_ID, budgetId, capId, accountId, amountMist);
  return tx;
}

/** Move a device's unused reserve back into the budget. */
export function buildReclaimReserveTx(budgetId: string, capId: string, accountId: string, amountMist: bigint): Transaction {
  requireBudget();
  if (amountMist <= BigInt(0)) throw new Error("Nothing to reclaim");
  const tx = new Transaction();
  calls.fleet_budget_reclaim_reserve(tx, PACKAGE_ID, budgetId, capId, accountId, amountMist);
  return tx;
}

/** Close a device's account for good; its reserve returns to the budget. */
export function buildRemoveDeviceTx(budgetId: string, capId: string, accountId: string): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_remove_device(tx, PACKAGE_ID, budgetId, capId, accountId);
  return tx;
}

/** Enroll an already activated device the owner holds (its owner cap). */
export function buildEnrollDeviceTx(budgetId: string, capId: string, ownerCapId: string, floatMist: bigint, reserveMist: bigint): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_enroll_device(tx, PACKAGE_ID, budgetId, capId, REGISTRY_ID, ownerCapId, floatMist, reserveMist, CLOCK);
  return tx;
}

/**
 * Register, issue the device cap, activate and (with a budget) enroll the
 * device with its own gas account and reserve and send it its float, in one
 * atomic PTB. The owner cap goes to `sender`.
 */
export function buildRegisterAndEnrollTx(name: string, deviceAddress: string, arch: string, sender: string,
  budget?: { budgetId: string; capId: string; floatMist: bigint; reserveMist: bigint }): Transaction {
  requireManifest();
  const tx = new Transaction();
  const [device] = calls.device_registry_register_device(tx, PACKAGE_ID, REGISTRY_ID, name, deviceAddress, arch, CLOCK);
  calls.device_cap_issue_cap(tx, PACKAGE_ID, device);
  const [owner] = calls.device_registry_activate_device(tx, PACKAGE_ID, REGISTRY_ID, device);
  if (budget) {
    requireBudget();
    calls.fleet_budget_enroll_device(tx, PACKAGE_ID, budget.budgetId, budget.capId, REGISTRY_ID, owner, budget.floatMist, budget.reserveMist, CLOCK);
  }
  tx.transferObjects([owner], tx.pure.address(sender));
  return tx;
}

// ── Reads (fleet_budget interface version 1) ─────────────────────────────────

export interface BudgetView {
  id: string;
  capId: string;
  version: bigint;
  balance: bigint;
  paused: boolean;
  defaultLimits: Limits;
  lowBalanceThreshold: bigint;
  deviceCount: bigint;
  totalDeposited: bigint;
  totalWithdrawn: bigint;
  /** Floats and owner refills paid straight to devices. */
  totalPaid: bigint;
  totalAllocated: bigint;
  totalReclaimed: bigint;
}

export interface AccountView {
  /** The device's own shared GasAccount. */
  accountId: string;
  deviceId: string;
  ownerCapId: string;
  /** What the device may still draw. */
  reserve: bigint;
  limits: Limits;
  suspended: boolean;
  periodStartMs: bigint;
  drawnInPeriod: bigint;
  drawsInPeriod: bigint;
  lastDrawMs: bigint;
  totalDrawn: bigint;
  enrolledAtMs: bigint;
}

function limitsOf(value: ReturnType<typeof fleet_budget_GasAccountBcs.parse>["limits"]): Limits {
  return { floatCap: BigInt(value.float_cap), periodMs: BigInt(value.period_ms), periodAllowance: BigInt(value.period_allowance),
    maxDrawsPerPeriod: BigInt(value.max_draws_per_period), minDrawIntervalMs: BigInt(value.min_draw_interval_ms) };
}

/** Budgets whose owner cap this address holds, each checked against its cap. */
export async function fetchBudgets(client: SuiGrpcClient, owner: string): Promise<BudgetView[]> {
  await verifyDeployment(client);
  if (!budgetSupported()) return [];
  const caps = (await allOwned(client, owner, moveType("fleet_budget::BudgetOwnerCap")))
    .map((object) => ({ capId: object.objectId, budgetId: fleet_budget_BudgetOwnerCapBcs.parse(object.content).budget_id }));
  if (!caps.length) return [];
  const { objects } = await client.getObjects({ objectIds: caps.map((cap) => cap.budgetId), include: { content: true } });
  return objects.flatMap((object, index) => {
    if (object instanceof ObjectError && (object.reason === "notFound" || object.reason === "deleted")) return [];
    if (object instanceof Error) throw object;
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("fleet_budget::FleetBudget"))) throw new Error("Unexpected FleetBudget type");
    const fields = fleet_budget_FleetBudgetBcs.parse(object.content);
    // A cap for another budget (or a replaced cap) must not claim this one.
    if (normalizeSuiAddress(fields.owner_cap_id) !== normalizeSuiAddress(caps[index].capId)) return [];
    if (normalizeSuiAddress(fields.registry_id) !== normalizeSuiAddress(REGISTRY_ID)) throw new Error("The budget belongs to another registry");
    return [{ id: object.objectId, capId: caps[index].capId, version: BigInt(fields.version), balance: BigInt(fields.balance.value),
      paused: fields.paused, defaultLimits: limitsOf(fields.default_limits), lowBalanceThreshold: BigInt(fields.low_balance_threshold),
      deviceCount: BigInt(fields.device_count), totalDeposited: BigInt(fields.total_deposited),
      totalWithdrawn: BigInt(fields.total_withdrawn), totalPaid: BigInt(fields.total_paid),
      totalAllocated: BigInt(fields.total_allocated), totalReclaimed: BigInt(fields.total_reclaimed) }];
  });
}

/**
 * A device's current account in a budget, or undefined if it is not
 * enrolled: the budget indexes the account's ID, and the shared GasAccount
 * must name this budget and device.
 */
export async function fetchAccount(client: Pick<SuiGrpcClient, "getDynamicField" | "getObject">, budgetId: string, deviceId: string): Promise<AccountView | undefined> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: budgetId,
      name: { type: moveType("fleet_budget::DeviceKey"), bcs: fleet_budget_DeviceKeyBcs.serialize({ device_id: deviceId }).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag("0x2::object::ID")) throw new Error("Unexpected device index type");
  const accountId = bcs.Address.parse(field.dynamicField.value.bcs);
  const { object } = await client.getObject({ objectId: accountId, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("fleet_budget::GasAccount"))) throw new Error("Unexpected GasAccount type");
  const value = fleet_budget_GasAccountBcs.parse(object.content);
  if (normalizeSuiAddress(value.budget_id) !== normalizeSuiAddress(budgetId) || normalizeSuiAddress(value.device_id) !== normalizeSuiAddress(deviceId)) {
    throw new Error("The gas account belongs to another budget or device");
  }
  return { accountId, deviceId, ownerCapId: value.owner_cap_id, reserve: BigInt(value.reserve.value), limits: limitsOf(value.limits),
    suspended: value.suspended, periodStartMs: BigInt(value.period_start_ms), drawnInPeriod: BigInt(value.drawn_in_period),
    drawsInPeriod: BigInt(value.draws_in_period), lastDrawMs: BigInt(value.last_draw_ms),
    totalDrawn: BigInt(value.total_drawn), enrolledAtMs: BigInt(value.enrolled_at_ms) };
}

export interface DrawEvent {
  deviceId: string;
  amount: bigint;
  /** The device's reserve after the draw. */
  reserve: bigint;
  atLimit: boolean;
  checkpoint: string | null;
  digest: string;
}

/** Every draw from this budget, newest first. */
export async function fetchDraws(client: SuiGrpcClient, budgetId: string): Promise<DrawEvent[]> {
  const events = await allEvents(client, moveType("fleet_budget::GasDrawn"));
  return events.flatMap((event) => {
    const value = fleet_budget_GasDrawnBcs.parse(event.bcs);
    if (normalizeSuiAddress(value.budget_id) !== normalizeSuiAddress(budgetId)) return [];
    return [{ deviceId: value.device_id, amount: BigInt(value.amount), reserve: BigInt(value.reserve), atLimit: value.at_limit,
      checkpoint: event.checkpoint, digest: event.transactionDigest }];
  });
}

// ── Alerts and warnings ──────────────────────────────────────────────────────

export interface BudgetAlert {
  severity: "warning" | "critical";
  deviceId?: string;
  text: string;
}

/** The account's usage in its current period, as the contract would roll it at `nowMs`. */
export function periodUsage(account: AccountView, nowMs: bigint) {
  const expired = nowMs >= account.periodStartMs + account.limits.periodMs;
  return { drawn: expired ? BigInt(0) : account.drawnInPeriod, draws: expired ? BigInt(0) : account.drawsInPeriod };
}

/**
 * Devices drawing at their limits or far above the fleet's typical device,
 * device reserves running out, and a budget near exhaustion. Signals for the
 * owner to act (suspend, revoke, pause, refill, deposit); the limits
 * themselves are enforced on chain.
 */
export function budgetAlerts(budget: BudgetView, accounts: AccountView[], nowMs: bigint, runway: number | null): BudgetAlert[] {
  const alerts: BudgetAlert[] = [];
  if (budget.paused) alerts.push({ severity: "warning", text: "The budget is paused: no device can draw gas or upload storage until you resume it." });
  if (budget.balance === BigInt(0)) alerts.push({ severity: accounts.length ? "warning" : "critical", text: "The budget holds no unallocated funds: devices draw only from their reserves, and you cannot refill them until you deposit." });
  else if (budget.balance < budget.lowBalanceThreshold) alerts.push({ severity: "warning", text: "The budget is below your low-balance threshold. Deposit before device reserves need refilling." });
  if (runway !== null && runway < 7) alerts.push({ severity: runway < 2 ? "critical" : "warning", text: `At the recent draw rate the budget lasts about ${runway.toFixed(1)} days.` });
  const usage = accounts.map((account) => ({ account, ...periodUsage(account, nowMs) }));
  const drawn = usage.map((u) => u.drawn).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const median = drawn.length ? drawn[Math.floor((drawn.length - 1) / 2)] : BigInt(0);
  for (const { account, drawn, draws } of usage) {
    if (account.suspended) continue;
    if (account.reserve === BigInt(0)) {
      alerts.push({ severity: "critical", deviceId: account.deviceId, text: "Its gas reserve is empty: it cannot top up, and its receipts queue until you refill the reserve." });
    } else if (account.reserve < account.limits.periodAllowance) {
      alerts.push({ severity: "warning", deviceId: account.deviceId, text: "Its gas reserve is below one period allowance. Refill it from the budget." });
    }
    if (drawn >= account.limits.periodAllowance || draws >= account.limits.maxDrawsPerPeriod) {
      alerts.push({ severity: "warning", deviceId: account.deviceId, text: "Drawing at its period limit. If this is unexpected, suspend it or revoke its key." });
    } else if (accounts.length >= 3 && drawn > BigInt(0) && drawn >= median * BigInt(3) && draws >= BigInt(2)) {
      alerts.push({ severity: "warning", deviceId: account.deviceId, text: "Drawing far more than the fleet's typical device this period. Check it for compromise." });
    }
  }
  return alerts;
}

// ── Owner-prepaid Walrus storage (fleet_storage package) ─────────────────────

/** Pinned Walrus Testnet objects (MystenLabs/walrus testnet-v1.59.0 c68b530e, setup/client_config_testnet.yaml; system version 4). */
export const PINNED_WALRUS = {
  testnet: {
    walrusPackage: "0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611",
    walrusOrigin: "0xd84704c17fc870b8764832c535aa6b11f21a95cd6f5bb38a9b07d2cf42220c66",
    system: "0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af",
    walType: "0x8270feb7375eee355e64fdb69c50abb6b5f9393a722883c1cf45f8e26048810a::wal::WAL",
    /** Walrus's Testnet SUI→WAL exchange: a fixed-rate route, no market slippage. */
    exchange: {
      package: "0x82593828ed3fcb8c6a235eac9abd0adbe9c5f9bbffa9b1e7a45cdd884481ef9f",
      objects: [
        "0xf4d164ea2def5fe07dc573992a029e010dba09b1a8dcbc44c5c2e79567f39073",
        "0x19825121c52080bb1073662231cfea5c0e4d905fd13e95f21e9a018f2ef41862",
        "0x83b454e524c71f30803f4d6c302a86fb6a39e96cdfb873c2d1e93bc1c26a3bc5",
        "0x8d63209cf8589ce7aef8f262437163c67577ed09f3e636a9d8e0813843fb8bf1",
      ],
    },
  },
} as const;

export interface StorageConfig {
  /** The fleet_storage package built against the pinned Walrus contracts. */
  packageId: string;
  walrus: (typeof PINNED_WALRUS)["testnet"];
}

/** Storage support for this build: only where a pinned, verified Walrus build exists. */
export function storageConfig(network = MANIFEST?.network, packageId = process.env.NEXT_PUBLIC_FLEET_STORAGE_PACKAGE): { config?: StorageConfig; reason?: string } {
  if (!packageId) return { reason: "Prepaid Walrus storage is not configured for this build (no fleet_storage package)." };
  if (!/^0x[0-9a-fA-F]{1,64}$/.test(packageId)) return { reason: "The configured fleet_storage package ID is invalid." };
  if (network !== "testnet") return { reason: `Prepaid Walrus storage is built only against the pinned Walrus Testnet contracts; none is verified for ${network ?? "this network"}.` };
  return { config: { packageId: normalizeSuiAddress(packageId), walrus: PINNED_WALRUS.testnet } };
}

/** Why the deposit cannot swap SUI for WAL on this network, if it cannot. */
export function swapRoute(network = MANIFEST?.network): { route?: "testnet-exchange"; explanation: string } {
  if (network === "testnet") {
    return { route: "testnet-exchange", explanation: "Fleet storage can be funded in SUI only: its deposit swaps SUI for WAL through Walrus's pinned Testnet exchange at its posted fixed rate, deposits the WAL and can buy the storage, all in one transaction that aborts if fewer WAL than the minimum arrive." };
  }
  return { explanation: "No DEX route is pinned and verified for this network, so the deposit cannot swap SUI for WAL safely in one transaction. Buy WAL directly (an exchange, or a card on-ramp that sells WAL) and deposit it." };
}

export interface Quota {
  periodMs: bigint;
  /** Walrus encoded bytes, which storage is reserved and paid in. */
  encodedBytesPerPeriod: bigint;
  uploadsPerPeriod: bigint;
  maxBlobSize: bigint;
  /** WAL FROST. */
  writeFeesPerPeriod: bigint;
}

/** Per-device upload limits for new fleet storage: a day's period. */
export const DEFAULT_QUOTA: Quota = {
  periodMs: BigInt(86_400_000),
  encodedBytesPerPeriod: BigInt(512 * 1024 * 1024),
  uploadsPerPeriod: BigInt(24),
  maxBlobSize: BigInt(8 * 1024 * 1024),
  writeFeesPerPeriod: BigInt(100_000_000),
};

/** Walrus prices storage per 1 MiB unit per epoch. */
export const WALRUS_UNIT_BYTES = BigInt(1024 * 1024);

/** WAL FROST Walrus charges for `encodedBytes` over `epochs` at `pricePerUnit`. */
export function storageCost(encodedBytes: bigint, epochs: number, pricePerUnit: bigint): bigint {
  const units = (encodedBytes + WALRUS_UNIT_BYTES - BigInt(1)) / WALRUS_UNIT_BYTES;
  return units * pricePerUnit * BigInt(epochs);
}

export function buildCreateStorageTx(config: StorageConfig, budgetId: string, capId: string, quota: Quota): Transaction {
  requireBudget();
  if (quota.periodMs <= BigInt(0) || quota.uploadsPerPeriod <= BigInt(0)) throw new Error("The quota needs a positive period and upload count");
  const tx = new Transaction();
  const [q] = tx.moveCall({ target: `${config.packageId}::fleet_storage::new_quota`, arguments: [tx.pure.u64(quota.periodMs),
    tx.pure.u64(quota.encodedBytesPerPeriod), tx.pure.u64(quota.uploadsPerPeriod), tx.pure.u64(quota.maxBlobSize), tx.pure.u64(quota.writeFeesPerPeriod)] });
  tx.moveCall({ target: `${config.packageId}::fleet_storage::create`, arguments: [tx.object(budgetId), tx.object(capId), q] });
  return tx;
}

/** A pool refill: add `addBytes` and keep the pool until `endEpoch`. */
export interface Refill { addBytes: bigint; endEpoch: number }

/**
 * What `fleet_storage::refill_pool` does at `currentEpoch`: the pool's
 * elapsed epochs are dropped (an expired pool entirely), the rest is kept at
 * its size until `endEpoch` and `addBytes` are added over the same range.
 * `endEpoch` is raised to the pool's end, which a refill cannot shorten.
 * `cost` is the WAL FROST at `pricePerUnit`.
 */
export function planRefill(storage: Pick<StorageView, "poolSize" | "startEpoch" | "endEpoch"> | undefined, currentEpoch: number,
  addBytes: bigint, epochs: number, pricePerUnit: bigint) {
  if (addBytes < BigInt(0) || !Number.isInteger(epochs) || epochs <= 0) throw new Error("Keep the storage for at least one epoch");
  const live = storage?.endEpoch !== undefined && storage.endEpoch > currentEpoch;
  const kept = live ? storage!.poolSize : BigInt(0);
  const start = live ? Math.max(storage!.startEpoch ?? currentEpoch, currentEpoch) : currentEpoch;
  const endEpoch = live ? Math.max(currentEpoch + epochs, storage!.endEpoch!) : currentEpoch + epochs;
  if (kept + addBytes <= BigInt(0)) throw new Error("Add storage: the pool has none left to keep");
  const cost = (live ? storageCost(kept, endEpoch - storage!.endEpoch!, pricePerUnit) : BigInt(0))
    + (addBytes > BigInt(0) ? storageCost(addBytes, endEpoch - start, pricePerUnit) : BigInt(0));
  return { endEpoch, poolSize: kept + addBytes, startEpoch: start, droppedBytes: live ? BigInt(0) : storage?.poolSize ?? BigInt(0), cost };
}

function refillPool(tx: Transaction, config: StorageConfig, storageId: string, budgetId: string, capId: string, refill: Refill) {
  if (refill.addBytes < BigInt(0) || !Number.isInteger(refill.endEpoch) || refill.endEpoch <= 0) throw new Error("Refill a non-negative size until a later epoch");
  tx.moveCall({ target: `${config.packageId}::fleet_storage::refill_pool`, arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId),
    tx.object(config.walrus.system), tx.pure.u64(refill.addBytes), tx.pure.u32(refill.endEpoch)] });
}

/**
 * Top up the pool and keep it until `endEpoch` with the fleet's own WAL, in
 * one call that works at any epoch (also after the pool expired): no WAL
 * leaves the fleet object. See `planRefill`.
 */
export function buildRefillPoolTx(config: StorageConfig, storageId: string, budgetId: string, capId: string, refill: Refill): Transaction {
  requireBudget();
  const tx = new Transaction();
  refillPool(tx, config, storageId, budgetId, capId, refill);
  return tx;
}

/** Deposit WAL from the wallet (bought directly, for example by card) into fleet storage. */
export function buildDepositWalTx(config: StorageConfig, storageId: string, budgetId: string, capId: string, amountFrost: bigint): Transaction {
  requireBudget();
  if (amountFrost <= BigInt(0)) throw new Error("Deposit a positive amount");
  const tx = new Transaction();
  const wal = tx.add(coinWithBalance({ type: config.walrus.walType, balance: amountFrost }));
  tx.moveCall({ target: `${config.packageId}::fleet_storage::deposit_wal`, arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId), wal] });
  return tx;
}

export function buildWithdrawWalTx(config: StorageConfig, storageId: string, budgetId: string, capId: string, amountFrost: bigint, sender: string): Transaction {
  requireBudget();
  if (amountFrost <= BigInt(0)) throw new Error("Withdraw a positive amount");
  const tx = new Transaction();
  const [wal] = tx.moveCall({ target: `${config.packageId}::fleet_storage::withdraw_wal`, arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId), tx.pure.u64(amountFrost)] });
  tx.transferObjects([wal], tx.pure.address(sender));
  return tx;
}

/**
 * Pay in SUI only: swap SUI for WAL through the pinned Testnet exchange and
 * deposit it, aborting the whole transaction unless at least `minWal`
 * arrives; optionally refill the pool with it in the same transaction.
 */
export function buildSwapAndDepositWalTx(config: StorageConfig, exchangeId: string, storageId: string, budgetId: string, capId: string,
  suiMist: bigint, minWal: bigint, refill?: Refill): Transaction {
  requireBudget();
  if (!config.walrus.exchange.objects.some((id) => normalizeSuiAddress(id) === normalizeSuiAddress(exchangeId))) throw new Error("Not a pinned exchange");
  if (suiMist <= BigInt(0) || minWal <= BigInt(0)) throw new Error("Swap a positive amount with a positive minimum");
  const tx = new Transaction();
  const [sui] = tx.splitCoins(tx.gas, [tx.pure.u64(suiMist)]);
  const [wal] = tx.moveCall({ target: `${config.walrus.exchange.package}::wal_exchange::exchange_all_for_wal`, arguments: [tx.object(exchangeId), sui] });
  tx.moveCall({ target: `${config.packageId}::fleet_storage::deposit_wal_at_least`,
    arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId), wal, tx.pure.u64(minWal)] });
  if (refill) refillPool(tx, config, storageId, budgetId, capId, refill);
  return tx;
}

// fleet_storage::FleetStorage, with walrus::storage_resource::Storage as pinned (testnet-v1.59.0).
const StorageBcs = bcs.struct("Storage", { id: bcs.Address, start_epoch: bcs.u32(), end_epoch: bcs.u32(), storage_size: bcs.u64() });
const QuotaBcs = bcs.struct("Quota", { period_ms: bcs.u64(), encoded_bytes_per_period: bcs.u64(), uploads_per_period: bcs.u64(),
  max_blob_size: bcs.u64(), write_fees_per_period: bcs.u64() });
const FleetStorageBcs = bcs.struct("FleetStorage", { id: bcs.Address, version: bcs.u64(), budget_id: bcs.Address, pool: bcs.option(StorageBcs),
  wal: bcs.struct("Balance", { value: bcs.u64() }), default_quota: QuotaBcs, blob_count: bcs.u64(), total_encoded_bytes: bcs.u64(), total_write_fees: bcs.u64() });
const StorageCreatedBcs = bcs.struct("StorageCreated", { storage_id: bcs.Address, budget_id: bcs.Address });

export interface StorageView {
  id: string;
  version: bigint;
  /** Unused prepaid storage, in encoded bytes. */
  poolSize: bigint;
  /** The pool's Walrus epochs; uploads end with the pool. */
  startEpoch?: number;
  endEpoch?: number;
  walBalance: bigint;
  defaultQuota: Quota;
  blobCount: bigint;
  totalEncodedBytes: bigint;
  totalWriteFees: bigint;
}

/** The fleet storage created for `budgetId` (the newest, if several), checked against its type and budget. */
export async function fetchStorage(client: SuiGrpcClient, config: StorageConfig, budgetId: string): Promise<StorageView | undefined> {
  const created = (await allEvents(client, `${config.packageId}::fleet_storage::StorageCreated`))
    .map((event) => StorageCreatedBcs.parse(event.bcs))
    .filter((event) => normalizeSuiAddress(event.budget_id) === normalizeSuiAddress(budgetId));
  for (const { storage_id } of created) {
    let object;
    try { ({ object } = await client.getObject({ objectId: storage_id, include: { content: true } })); }
    catch (error) { if (error instanceof ObjectError && (error.reason === "notFound" || error.reason === "deleted")) continue; throw error; }
    if (normalizeStructTag(object.type) !== normalizeStructTag(`${config.packageId}::fleet_storage::FleetStorage`)) throw new Error("Unexpected FleetStorage type");
    const fields = FleetStorageBcs.parse(object.content);
    if (normalizeSuiAddress(fields.budget_id) !== normalizeSuiAddress(budgetId)) throw new Error("The fleet storage belongs to another budget");
    const q = fields.default_quota;
    return { id: object.objectId, version: BigInt(fields.version), poolSize: fields.pool ? BigInt(fields.pool.storage_size) : BigInt(0),
      startEpoch: fields.pool?.start_epoch, endEpoch: fields.pool?.end_epoch, walBalance: BigInt(fields.wal.value),
      defaultQuota: { periodMs: BigInt(q.period_ms), encodedBytesPerPeriod: BigInt(q.encoded_bytes_per_period), uploadsPerPeriod: BigInt(q.uploads_per_period),
        maxBlobSize: BigInt(q.max_blob_size), writeFeesPerPeriod: BigInt(q.write_fees_per_period) },
      blobCount: BigInt(fields.blob_count), totalEncodedBytes: BigInt(fields.total_encoded_bytes), totalWriteFees: BigInt(fields.total_write_fees) };
  }
  return undefined;
}

/** Warnings about fleet storage running out or expiring. */
export function storageAlerts(storage: StorageView, currentEpoch: number | undefined): BudgetAlert[] {
  const alerts: BudgetAlert[] = [];
  if (storage.poolSize === BigInt(0)) alerts.push({ severity: "warning", text: "Fleet storage holds no prepaid space: fleet-paid uploads fail until you buy storage." });
  if (storage.walBalance === BigInt(0)) alerts.push({ severity: "warning", text: "Fleet storage holds no WAL for write fees: fleet-paid uploads fail until you deposit WAL." });
  if (storage.endEpoch !== undefined && currentEpoch !== undefined && storage.endEpoch <= currentEpoch) {
    alerts.push({ severity: "critical", text: `Prepaid storage expired at Walrus epoch ${storage.endEpoch}: fleet-paid uploads fail until you refill the pool.` });
  } else if (storage.endEpoch !== undefined && currentEpoch !== undefined && storage.endEpoch - currentEpoch <= 1) {
    alerts.push({ severity: "critical", text: `Prepaid storage ends at Walrus epoch ${storage.endEpoch}: refill the pool to keep it longer before new uploads would expire at once.` });
  }
  return alerts;
}

/** WAL out for `suiMist`, at the exchange's posted rate, less `slippageBps`. */
export function minWalOut(suiMist: bigint, rate: { wal: bigint; sui: bigint }, slippageBps: number): bigint {
  if (rate.sui <= BigInt(0) || slippageBps < 0 || slippageBps > 10_000) throw new Error("Invalid rate or slippage");
  const expected = (suiMist * rate.wal) / rate.sui;
  return (expected * BigInt(10_000 - slippageBps)) / BigInt(10_000);
}

/** Read and verify a pinned exchange: its type and posted rate. */
export async function readExchange(client: SuiGrpcClient, config: StorageConfig, exchangeId: string) {
  if (!config.walrus.exchange.objects.some((id) => normalizeSuiAddress(id) === normalizeSuiAddress(exchangeId))) throw new Error("Not a pinned exchange");
  const { object } = await client.getObject({ objectId: exchangeId, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(`${config.walrus.exchange.package}::wal_exchange::Exchange`)) throw new Error("Exchange object has an unexpected type");
  // wal_exchange::Exchange { id, wal: Balance<WAL>, sui: Balance<SUI>, rate, admin }.
  const Exchange = bcs.struct("Exchange", { id: bcs.Address, wal: bcs.u64(), sui: bcs.u64(),
    rate: bcs.struct("ExchangeRate", { wal: bcs.u64(), sui: bcs.u64() }), admin: bcs.Address });
  const value = Exchange.parse(object.content);
  return { walAvailable: BigInt(value.wal), rate: { wal: BigInt(value.rate.wal), sui: BigInt(value.rate.sui) } };
}
