"use client";
import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { useCurrentAccount, useCurrentClient, useCurrentNetwork } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { NETWORK, REGISTRY_ID } from "@/lib/constants";
import { MANIFEST } from "@/lib/deployment-manifest";
import { customersSupported, fetchCustomerFleets, selectionKey, selectFleet, type NamedFleet } from "@/lib/customer-fleets";

const listeners = new Set<() => void>();
let revision = 0;
export function notifyFleetSelection() { revision++; for (const listener of listeners) listener(); }
function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => { listeners.delete(listener); window.removeEventListener("storage", listener); };
}
interface Selection {
  supported: boolean; stale: boolean; fleets: NamedFleet[]; fleet?: NamedFleet; loading: boolean; error?: Error | null;
  warnings?: string[]; identity: string; select: (id: string) => void; refresh: () => void;
}
const Context = createContext<Selection>({ supported: false, stale: false, fleets: [], loading: false, identity: "", select() {}, refresh() {} });
export function FleetSelectionProvider({ children }: { children: ReactNode }) {
  const account = useCurrentAccount(), client = useCurrentClient(), network = useCurrentNetwork();
  const supported = customersSupported();
  const identity = `${MANIFEST?.chain_identifier}:${REGISTRY_ID}:${network}:${account?.address ?? ""}`;
  const epoch = useRef({ identity, value: 0 });
  if (epoch.current.identity !== identity) epoch.current = { identity, value: epoch.current.value + 1 };
  const tick = useSyncExternalStore(subscribe, () => revision, () => 0);
  const [preference, setPreference] = useState<{ identity: string; id: string | null }>();
  useEffect(() => {
    const read = () => { setPreference({ identity, id: account ? localStorage.getItem(selectionKey(account.address)) : null }); };
    read(); window.addEventListener("storage", read);
    return () => window.removeEventListener("storage", read);
  }, [identity, account?.address, tick]);
  const query = useQuery({ queryKey: ["customer-fleets", identity, epoch.current.value],
    enabled: supported && !!account && network === NETWORK,
    queryFn: async () => {
      const warnings: string[] = [];
      const fleets = await fetchCustomerFleets(client, account!.address, message => warnings.push(message));
      return { fleets, warnings };
    }, refetchInterval: 15_000 });
  const valid = !!account && network === NETWORK && preference?.identity === identity && !query.isError;
  const fleets = valid ? query.data?.fleets ?? [] : [];
  const fleet = valid ? selectFleet(fleets, preference.id) : undefined;
  useEffect(() => {
    if (account && fleet && preference?.identity === identity && !preference.id) {
      localStorage.setItem(selectionKey(account.address), fleet.id);
      setPreference({ identity, id: fleet.id });
    }
  }, [account?.address, fleet, identity, preference]);
  const value: Selection = { warnings: valid ? query.data?.warnings : [], supported, stale: valid && query.isSuccess && !!preference.id && !fleet, fleets, fleet, identity: `${identity}:${fleet?.customerId ?? "unresolved"}:${fleet?.id ?? "unresolved"}:${fleet?.administrator ? "admin" : fleet?.support ? "support" : "read"}`,
    loading: supported && !!account && (query.isLoading || preference?.identity !== identity), error: account && network !== NETWORK ? new Error("Wallet network does not match this deployment") : query.error,
    select(id) {
      if (!account || !fleets.some(f => f.id === id)) return;
      localStorage.setItem(selectionKey(account.address), id); setPreference({ identity, id }); notifyFleetSelection();
    }, refresh() { void query.refetch(); } };
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useFleetSelection() { return useContext(Context); }

/** Remount local forms, decrypted plaintext and sessions on every identity
 * change; unresolved/revoked selection never renders prior fleet content. */
export function FleetScope({ children, allowWithoutFleet = false }: { children: ReactNode; allowWithoutFleet?: boolean }) {
  const selection = useFleetSelection();
  if (allowWithoutFleet) return <div key={selection.identity}>{children}</div>;
  const warnings = selection.warnings?.map(message => <p role="status" key={message}>{message}</p>);
  if (selection.supported && !selection.fleet) return <>{warnings}<p role="status">{selection.loading ? "Verifying fleet access…" : "Select or create a fleet to continue."}</p></>;
  return <div key={selection.identity}>{warnings}{children}</div>;
}
