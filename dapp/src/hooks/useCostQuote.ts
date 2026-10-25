"use client";

import { useMemo, useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { NETWORK } from "@/lib/constants";
import { combineQuote, fetchUsdQuote, type LiveParts } from "@/lib/cost-quote";

/**
 * The estimate's prices: the connected network's live reference gas price and
 * Walrus prices (Mainnet and Testnet have a Walrus system), and USD spot
 * prices once the owner fetches them. Failed reads fall back to the recorded
 * Mainnet snapshot, labelled as such.
 */
export function useCostQuote() {
  const client = useCurrentClient();
  const gas = useQuery({
    queryKey: ["cost-quote-gas", NETWORK],
    refetchInterval: 300_000,
    queryFn: async () => ({ referenceGasPrice: BigInt((await client.getReferenceGasPrice()).referenceGasPrice), observedAt: Date.now() }),
  });
  const walrus = useQuery({
    queryKey: ["cost-quote-walrus", NETWORK],
    enabled: NETWORK === "mainnet" || NETWORK === "testnet",
    refetchInterval: 300_000,
    queryFn: async () => {
      const { walrus: extension, MAINNET_WALRUS_PACKAGE_CONFIG, TESTNET_WALRUS_PACKAGE_CONFIG } = await import("@mysten/walrus");
      const packageConfig = NETWORK === "mainnet" ? MAINNET_WALRUS_PACKAGE_CONFIG : TESTNET_WALRUS_PACKAGE_CONFIG;
      const state = await client.$extend(extension({ packageConfig })).walrus.systemState();
      return { storagePrice: BigInt(state.storage_price_per_unit_size), writePrice: BigInt(state.write_price_per_unit_size),
        epoch: state.committee.epoch, observedAt: Date.now() };
    },
  });
  const [usd, setUsd] = useState<LiveParts["usd"]>();
  const [usdError, setUsdError] = useState<string>();
  const [usdLoading, setUsdLoading] = useState(false);
  async function refreshUsd() {
    setUsdError(undefined); setUsdLoading(true);
    try { setUsd(await fetchUsdQuote(fetch, AbortSignal.timeout(10_000))); }
    catch (error) { setUsdError(error instanceof Error ? error.message : String(error)); }
    finally { setUsdLoading(false); }
  }
  // Stable between renders: estimates (and the budget page's effects) recompute only when a price changes.
  const { quote, invalid } = useMemo(() => {
    try { return { quote: combineQuote(NETWORK, { gas: gas.data, walrus: walrus.data, usd }) }; }
    catch (error) { return { quote: combineQuote(NETWORK, { usd }), invalid: (error as Error).message }; }
  }, [gas.data, walrus.data, usd]);
  return { quote, gasError: gas.error?.message ?? invalid, walrusError: walrus.error?.message, usdError, usdLoading, refreshUsd };
}
