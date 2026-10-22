// Prepaid fleet network costs (SF-083): transaction builders and reads for the
// on-chain fleet budget (sui_edge::fleet_budget) and, when configured, the
// owner-prepaid Walrus storage (fleet_storage). See docs/FLEET_BUDGET.md.
import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { Transaction, coinWithBalance } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { BUDGET_TYPES, MANIFEST, moveType, requireManifest, verifyDeployment } from "./deployment-manifest";
import { calls, fleet_budget_BudgetOwnerCapBcs, fleet_budget_DeviceAccountBcs, fleet_budget_DeviceKeyBcs,
  fleet_budget_FleetBudgetBcs, fleet_budget_GasDrawnBcs } from "./fleet-protocol";
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

export function buildSetDeviceSuspendedTx(budgetId: string, capId: string, deviceId: string, suspended: boolean): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_set_device_suspended(tx, PACKAGE_ID, budgetId, capId, deviceId, suspended);
  return tx;
}

export function buildSetDeviceLimitsTx(budgetId: string, capId: string, deviceId: string, limits: Limits): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_set_device_limits(tx, PACKAGE_ID, budgetId, capId, deviceId, limitsArg(tx, limits));
  return tx;
}

export function buildFundDeviceTx(budgetId: string, capId: string, deviceId: string, amountMist: bigint): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_fund_device(tx, PACKAGE_ID, budgetId, capId, REGISTRY_ID, deviceId, amountMist);
  return tx;
}

export function buildRemoveDeviceTx(budgetId: string, capId: string, deviceId: string): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_remove_device(tx, PACKAGE_ID, budgetId, capId, deviceId);
  return tx;
}

/** Enroll an already activated device the owner holds (its owner cap). */
export function buildEnrollDeviceTx(budgetId: string, capId: string, ownerCapId: string, floatMist: bigint): Transaction {
  requireBudget();
  const tx = new Transaction();
  calls.fleet_budget_enroll_device(tx, PACKAGE_ID, budgetId, capId, REGISTRY_ID, ownerCapId, floatMist, CLOCK);
  return tx;
}

/**
 * Register, issue the device cap, activate and (with a budget) enroll and
 * send the device its float, in one atomic PTB. The owner cap goes to `sender`.
 */
export function buildRegisterAndEnrollTx(name: string, deviceAddress: string, arch: string, sender: string,
  budget?: { budgetId: string; capId: string; floatMist: bigint }): Transaction {
  requireManifest();
  const tx = new Transaction();
  const [device] = calls.device_registry_register_device(tx, PACKAGE_ID, REGISTRY_ID, name, deviceAddress, arch, CLOCK);
  calls.device_cap_issue_cap(tx, PACKAGE_ID, device);
  const [owner] = calls.device_registry_activate_device(tx, PACKAGE_ID, REGISTRY_ID, device);
  if (budget) {
    requireBudget();
    calls.fleet_budget_enroll_device(tx, PACKAGE_ID, budget.budgetId, budget.capId, REGISTRY_ID, owner, budget.floatMist, CLOCK);
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
  totalDrawn: bigint;
}

export interface AccountView {
  deviceId: string;
  ownerCapId: string;
  limits: Limits;
  suspended: boolean;
  periodStartMs: bigint;
  drawnInPeriod: bigint;
  drawsInPeriod: bigint;
  lastDrawMs: bigint;
  totalDrawn: bigint;
  enrolledAtMs: bigint;
}

function limitsOf(value: ReturnType<typeof fleet_budget_DeviceAccountBcs.parse>["limits"]): Limits {
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
      totalWithdrawn: BigInt(fields.total_withdrawn), totalDrawn: BigInt(fields.total_drawn) }];
  });
}

/** A device's account in a budget, or undefined if it is not enrolled. */
export async function fetchAccount(client: Pick<SuiGrpcClient, "getDynamicField">, budgetId: string, deviceId: string): Promise<AccountView | undefined> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: budgetId,
      name: { type: moveType("fleet_budget::DeviceKey"), bcs: fleet_budget_DeviceKeyBcs.serialize({ device_id: deviceId }).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("fleet_budget::DeviceAccount"))) throw new Error("Unexpected DeviceAccount type");
  const value = fleet_budget_DeviceAccountBcs.parse(field.dynamicField.value.bcs);
  return { deviceId, ownerCapId: value.owner_cap_id, limits: limitsOf(value.limits), suspended: value.suspended,
    periodStartMs: BigInt(value.period_start_ms), drawnInPeriod: BigInt(value.drawn_in_period),
    drawsInPeriod: BigInt(value.draws_in_period), lastDrawMs: BigInt(value.last_draw_ms),
    totalDrawn: BigInt(value.total_drawn), enrolledAtMs: BigInt(value.enrolled_at_ms) };
}

export interface DrawEvent {
  deviceId: string;
  amount: bigint;
  balance: bigint;
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
    return [{ deviceId: value.device_id, amount: BigInt(value.amount), balance: BigInt(value.balance), atLimit: value.at_limit,
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
 * and a budget near exhaustion. Signals for the owner to act (suspend,
 * revoke, pause, deposit); the limits themselves are enforced on chain.
 */
export function budgetAlerts(budget: BudgetView, accounts: AccountView[], nowMs: bigint, runway: number | null): BudgetAlert[] {
  const alerts: BudgetAlert[] = [];
  if (budget.paused) alerts.push({ severity: "warning", text: "The budget is paused: no device can draw gas or upload storage until you resume it." });
  if (budget.balance === BigInt(0)) alerts.push({ severity: "critical", text: "The budget is empty. Devices cannot top up; their receipts queue until you deposit." });
  else if (budget.balance < budget.lowBalanceThreshold) alerts.push({ severity: "warning", text: "The budget is below your low-balance threshold. Deposit before it runs out." });
  if (runway !== null && runway < 7) alerts.push({ severity: runway < 2 ? "critical" : "warning", text: `At the recent draw rate the budget lasts about ${runway.toFixed(1)} days.` });
  const usage = accounts.map((account) => ({ account, ...periodUsage(account, nowMs) }));
  const drawn = usage.map((u) => u.drawn).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const median = drawn.length ? drawn[Math.floor((drawn.length - 1) / 2)] : BigInt(0);
  for (const { account, drawn, draws } of usage) {
    if (account.suspended) continue;
    if (drawn >= account.limits.periodAllowance || draws >= account.limits.maxDrawsPerPeriod) {
      alerts.push({ severity: "warning", deviceId: account.deviceId, text: "Drawing at its period limit. If this is unexpected, suspend it or revoke its key." });
    } else if (accounts.length >= 3 && drawn > BigInt(0) && drawn >= median * BigInt(3) && draws >= BigInt(2)) {
      alerts.push({ severity: "warning", deviceId: account.deviceId, text: "Drawing far more than the fleet's typical device this period. Check it for compromise." });
    }
  }
  return alerts;
}

// ── Owner-prepaid Walrus storage (fleet_storage package) ─────────────────────

/** Pinned Walrus Testnet objects (MystenLabs/walrus testnet-v1.39.1, setup/client_config_testnet.yaml). */
export const PINNED_WALRUS = {
  testnet: {
    walrusPackage: "0xa998b8719ca1c0a6dc4e24a859bbb39f5477417f71885fbf2967a6510f699144",
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
    return { route: "testnet-exchange", explanation: "The deposit swaps SUI for WAL through Walrus's pinned Testnet exchange at its posted fixed rate, in the same transaction, and aborts if fewer WAL than the minimum arrive." };
  }
  return { explanation: "No DEX route is pinned and verified for this network, so the deposit cannot swap SUI for WAL safely in one transaction. Buy WAL directly (an exchange, or a card on-ramp that sells WAL) and deposit it." };
}

export function buildCreateStorageTx(config: StorageConfig, budgetId: string, capId: string, quota: {
  periodMs: bigint; encodedBytesPerPeriod: bigint; uploadsPerPeriod: bigint; maxBlobSize: bigint; writeFeesPerPeriod: bigint }): Transaction {
  requireBudget();
  const tx = new Transaction();
  const [q] = tx.moveCall({ target: `${config.packageId}::fleet_storage::new_quota`, arguments: [tx.pure.u64(quota.periodMs),
    tx.pure.u64(quota.encodedBytesPerPeriod), tx.pure.u64(quota.uploadsPerPeriod), tx.pure.u64(quota.maxBlobSize), tx.pure.u64(quota.writeFeesPerPeriod)] });
  tx.moveCall({ target: `${config.packageId}::fleet_storage::create`, arguments: [tx.object(budgetId), tx.object(capId), q] });
  return tx;
}

/** Buy `encodedBytes` of Walrus storage for `epochs` with the owner's WAL and hold it for the fleet. */
export function buildBuyStorageTx(config: StorageConfig, storageId: string, budgetId: string, capId: string,
  encodedBytes: bigint, epochs: number, maxPaymentFrost: bigint, sender: string): Transaction {
  requireBudget();
  const tx = new Transaction();
  const payment = tx.add(coinWithBalance({ type: config.walrus.walType, balance: maxPaymentFrost }));
  const [storage] = tx.moveCall({ target: `${config.walrus.walrusPackage}::system::reserve_space`,
    arguments: [tx.object(config.walrus.system), tx.pure.u64(encodedBytes), tx.pure.u32(epochs), payment] });
  tx.moveCall({ target: `${config.packageId}::fleet_storage::deposit_storage`, arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId), storage] });
  // Unspent WAL goes back to the owner.
  tx.transferObjects([payment], tx.pure.address(sender));
  return tx;
}

/**
 * Deposit write-fee WAL bought from SUI in the same transaction, through the
 * pinned Testnet exchange; aborts unless at least `minWal` arrives.
 */
export function buildSwapAndDepositWalTx(config: StorageConfig, exchangeId: string, storageId: string, budgetId: string, capId: string,
  suiMist: bigint, minWal: bigint): Transaction {
  requireBudget();
  if (!config.walrus.exchange.objects.some((id) => normalizeSuiAddress(id) === normalizeSuiAddress(exchangeId))) throw new Error("Not a pinned exchange");
  const tx = new Transaction();
  const [sui] = tx.splitCoins(tx.gas, [tx.pure.u64(suiMist)]);
  const [wal] = tx.moveCall({ target: `${config.walrus.exchange.package}::wal_exchange::exchange_all_for_wal`, arguments: [tx.object(exchangeId), sui] });
  tx.moveCall({ target: `${config.packageId}::fleet_storage::deposit_wal_at_least`,
    arguments: [tx.object(storageId), tx.object(budgetId), tx.object(capId), wal, tx.pure.u64(minWal)] });
  return tx;
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
