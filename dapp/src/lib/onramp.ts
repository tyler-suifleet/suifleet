// Card funding without a SuiFleet server (SF-083): open a fiat on-ramp's own
// hosted checkout, pre-filled with the wallet address, asset and amount. KYC,
// payment, fees and regional availability stay inside the provider's flow.
// SuiFleet receives no webhook and calls no provider API: funds count only
// once they arrive in the wallet and the user's own deposit transaction is
// confirmed on chain (see purchaseStatus).

export type OnrampAsset = "WAL" | "SUI";

export interface OnrampProvider {
  id: "onramp-money";
  name: string;
  /** Public partner app ID, compiled into the build (never a secret). */
  appId: string;
}

/**
 * OnRamp.money's hosted buy page, configured with a public app ID
 * (NEXT_PUBLIC_ONRAMP_APP_ID). Coinbase Onramp is not offered: it now needs a
 * session token minted server-side with a secret CDP key, which this
 * serverless app does not have.
 */
export function onrampProvider(appId = process.env.NEXT_PUBLIC_ONRAMP_APP_ID): { provider?: OnrampProvider; reason?: string } {
  if (!appId) return { reason: "Card funding is not configured for this build (no public on-ramp app ID). Fund the wallet from an exchange or another wallet." };
  if (!/^\d{1,12}$/.test(appId)) return { reason: "The configured on-ramp app ID is not a public numeric ID; card funding is disabled." };
  return { provider: { id: "onramp-money", name: "OnRamp.money", appId } };
}

/** The provider's hosted checkout URL; opened in a new tab, never embedded with credentials. */
export function checkoutUrl(provider: OnrampProvider, walletAddress: string, asset: OnrampAsset, amount: string): string {
  if (!/^0x[0-9a-f]{64}$/.test(walletAddress)) throw new Error("A full Sui wallet address is required");
  if (!/^\d+(\.\d+)?$/.test(amount) || Number(amount) <= 0) throw new Error("Enter a positive amount");
  const url = new URL("https://onramp.money/main/buy/");
  url.searchParams.set("appId", provider.appId);
  url.searchParams.set("walletAddress", walletAddress);
  url.searchParams.set("coinCode", asset.toLowerCase());
  url.searchParams.set("network", "sui");
  url.searchParams.set("coinAmount", amount);
  return url.toString();
}

/** A purchase the user started, kept in this browser only. */
export interface PendingPurchase {
  address: string;
  asset: OnrampAsset;
  amount: string;
  /** Wallet balance of `asset` (base units) when checkout opened. */
  startBalance: string;
  startedAt: number;
}

/** The browser storage the pending-purchase record lives in (localStorage in the app). */
export type PurchaseStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/**
 * One record per network, wallet and asset: the SUI and WAL checkouts are
 * tracked separately, so neither is ever compared against the other's balance
 * or overwritten or dismissed by the other.
 */
export function purchaseKey(network: string, address: string, asset: OnrampAsset): string {
  return `suifleet:onramp:${network}:${address}:${asset}`;
}

/** The saved purchase of this asset, or null if none or it does not match exactly. */
export function loadPurchase(storage: PurchaseStorage, network: string, address: string, asset: OnrampAsset): PendingPurchase | null {
  const saved = storage.getItem(purchaseKey(network, address, asset));
  if (!saved) return null;
  try {
    const record = JSON.parse(saved) as Partial<PendingPurchase>;
    if (record.address === address && record.asset === asset && typeof record.amount === "string"
      && typeof record.startBalance === "string" && /^\d+$/.test(record.startBalance)
      && typeof record.startedAt === "number" && Number.isFinite(record.startedAt)) return record as PendingPurchase;
  } catch { /* unreadable: treated as no record */ }
  return null;
}

export function savePurchase(storage: PurchaseStorage, network: string, record: PendingPurchase): void {
  storage.setItem(purchaseKey(network, record.address, record.asset), JSON.stringify(record));
}

export function clearPurchase(storage: PurchaseStorage, network: string, address: string, asset: OnrampAsset): void {
  storage.removeItem(purchaseKey(network, address, asset));
}

export type PurchaseState =
  | { state: "arrived"; received: bigint }
  | { state: "waiting" }
  | { state: "unconfirmed" };

/** Purchases still unseen after this long are shown as unconfirmed. */
export const PURCHASE_TIMEOUT_MS = 2 * 60 * 60 * 1000;

/**
 * What the wallet balance says about a purchase. Only an on-chain balance
 * increase counts, and even then nothing is credited to the budget until the
 * user's deposit transaction confirms. A failed, cancelled or slow purchase
 * is reported as such, separately from budget exhaustion; it never changes
 * existing fleet state, storage or devices.
 */
export function purchaseStatus(pending: PendingPurchase, balance: bigint, now: number): PurchaseState {
  const received = balance - BigInt(pending.startBalance);
  if (received > BigInt(0)) return { state: "arrived", received };
  return now - pending.startedAt > PURCHASE_TIMEOUT_MS ? { state: "unconfirmed" } : { state: "waiting" };
}

/** What the estimate shows about fees before approval, kept separate. */
export const FEE_NOTES = {
  onramp: "Card and on-ramp fees, exchange rates, KYC and the countries served are set by the provider and shown in its checkout before you pay. SuiFleet does not know or add them.",
  regions: "Availability depends on your country and payment method; the provider may decline or ask for identity verification.",
  service: "SuiFleet adds no service fee: the deposit covers network costs only (an open business decision, see docs/FLEET_BUDGET.md).",
} as const;
