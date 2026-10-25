// SF-131: live prices for the cost estimate. The connected network's reference
// gas price and Walrus storage/write prices are read over gRPC; USD spot
// prices are fetched from CoinGecko only when the owner asks. Anything not
// read live keeps the recorded Mainnet snapshot, and each part keeps its own
// source and date so the page never shows a stale price as current.
import { MAINNET_QUOTE, type CostQuote } from "@/lib/network-costs";

export const USD_QUOTE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=sui,walrus-2&vs_currencies=usd&include_last_updated_at=true";

export interface LiveParts {
  gas?: { referenceGasPrice: bigint; observedAt: number };
  walrus?: { storagePrice: bigint; writePrice: bigint; epoch: number; observedAt: number };
  usd?: { SUI: number; WAL: number; observedAt: number };
}

/** The recorded Mainnet quote with whichever parts were read live on `network`. */
export function combineQuote(network: string, live: LiveParts, base: CostQuote = MAINNET_QUOTE): CostQuote {
  const quote: CostQuote = { ...base, network };
  if (live.gas) {
    if (live.gas.referenceGasPrice <= BigInt(0)) throw new Error("reference gas price must be positive");
    Object.assign(quote, { referenceGasPrice: live.gas.referenceGasPrice, gasSource: `${network} reference gas price (live)`, gasObservedAt: live.gas.observedAt });
  }
  if (live.walrus) {
    if (live.walrus.storagePrice <= BigInt(0) || live.walrus.writePrice < BigInt(0)) throw new Error("Walrus prices must be positive");
    Object.assign(quote, { walrusStoragePrice: live.walrus.storagePrice, walrusWritePrice: live.walrus.writePrice,
      walrusSource: `${network} Walrus epoch ${live.walrus.epoch} (live)`, walrusObservedAt: live.walrus.observedAt });
  }
  if (live.usd) Object.assign(quote, { usd: { SUI: live.usd.SUI, WAL: live.usd.WAL }, usdSource: "CoinGecko spot (live)", usdObservedAt: live.usd.observedAt });
  return quote;
}

/** CoinGecko SUI and WAL spot prices, dated by CoinGecko's own update time. */
export async function fetchUsdQuote(fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<NonNullable<LiveParts["usd"]>> {
  const response = await fetcher(USD_QUOTE_URL, { signal, credentials: "omit", referrerPolicy: "no-referrer" });
  if (!response.ok) throw new Error(`USD quote unavailable (HTTP ${response.status})`);
  const body = await response.json() as Record<string, { usd?: unknown; last_updated_at?: unknown }>;
  const read = (id: string) => {
    const usd = body?.[id]?.usd, at = body?.[id]?.last_updated_at;
    if (typeof usd !== "number" || !Number.isFinite(usd) || usd <= 0 || typeof at !== "number" || !Number.isFinite(at)) throw new Error(`USD quote for ${id} is malformed`);
    return { usd, at: at * 1000 };
  };
  const sui = read("sui"), wal = read("walrus-2");
  return { SUI: sui.usd, WAL: wal.usd, observedAt: Math.min(sui.at, wal.at) };
}

/** "2026-10-23 21:22 UTC" */
export function quoteDate(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
