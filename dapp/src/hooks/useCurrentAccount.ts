"use client";

import { useSyncExternalStore } from "react";
import { useCurrentAccount as useWalletAccount } from "@mysten/dapp-kit-react";

const subscribe = () => () => {};
const clientSnapshot = () => true;
const serverSnapshot = () => false;

/** A saved wallet can reconnect before React hydrates a static route. Keep the
 * server and hydration render disconnected, then expose the live SDK account.
 * This preserves SSR and account-change subscriptions without trusting storage
 * as authority or delaying transaction-time wallet/network verification. */
export function useCurrentAccount(...args: Parameters<typeof useWalletAccount>) {
  const account = useWalletAccount(...args);
  return useHydrated() ? account : null;
}

/** False during the server and hydration renders, true afterwards. */
export function useHydrated() {
  return useSyncExternalStore(subscribe, clientSnapshot, serverSnapshot);
}
