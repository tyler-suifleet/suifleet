import { useSyncExternalStore } from "react";
const id = n => `0x${n.toString(16).padStart(64, "0")}`;
window.accessFixture = { connected: false, state: "empty", recipients: "ready", fail: false, pending: false };
window.accessCalls = [];
let listeners = new Set();
window.updateAccess = changes => { window.accessFixture = { ...window.accessFixture, ...changes }; listeners.forEach(l => l()); };
function useFixture() { return useSyncExternalStore(cb => { listeners.add(cb); return () => listeners.delete(cb); }, () => window.accessFixture); }
export function useCurrentAccount() { return useFixture().connected ? { address: id(10) } : null; }
// This page fixture supplies reads explicitly; an unexpected chain call must fail.
const client = new Proxy({}, { get: (_target, key) => { throw Error(`Unexpected chain access in access fixture: ${String(key)}`); } });
export const useCurrentClient = () => client;
export const useCurrentNetwork = () => 'testnet';
export const POLICIES_ENABLED = ACCESS_ENABLED;
export function useFleetSelection() { return { fleet: undefined }; }
const managedFleet = administrator => ({ id: id(70), customerId: id(71), administrator, devices: [id(33)], policies: [] });
export function useAccessPolicies() {
  const f = useFixture();
  return { isPending: f.state === "loading", isFetching: false, error: f.state === "error" ? new Error("fixture RPC unavailable") : null,
    refetch: async () => { if (f.state === "error") window.updateAccess({ state: "empty" }); },
    data: f.state === "policy" ? [{ fleet: f.managed ? managedFleet(f.managed === "admin") : undefined, objectId: id(20), capId: id(21), name: "Production sensors", version: "1", keyEpoch: "0", devices: [id(31)], groups: [], accounts: [] }] : [] };
}
export function useDevices() {
  const f = useFixture();
  return { data: f.recipients === "empty" ? [] : [{ objectId: id(30), ownerCapId: id(40), name: "Factory sensor" }, { objectId: id(31), ownerCapId: id(41), name: "Granted sensor" }, { objectId: id(32), name: "Legacy unactivated" }, ...(f.managed ? [{ objectId: id(33), managed: true, name: "Customer sensor" }] : [])],
    isPending: f.recipients === "loading", error: f.recipients === "error" ? new Error("device read failed") : null, refetch: async () => window.updateAccess({ recipients: "ready" }) };
}
export function useGroups() {
  const f = useFixture();
  return { data: f.recipients === "empty" ? [] : [{ objectId: id(50), name: "Factory floor" }],
    isPending: f.recipients === "loading", error: f.recipients === "error" ? new Error("group read failed") : null, refetch: async () => window.updateAccess({ recipients: "ready" }) };
}
export function useFleetTransaction() {
  const f = useFixture();
  return { isPending: f.pending, mutateAsync: async ({ transaction }) => {
    window.accessCalls.push(transaction.getData());
    if (f.fail) throw new Error("Wallet rejected the transaction");
    return { digest: "fixture-effects-success" };
  } };
}
