import { useSyncExternalStore } from "react";
const listeners = new Set<() => void>();
let version = 0;
export const A = "0x" + "a".repeat(64), B = "0x" + "b".repeat(64);
export const state = { account: A, network: "localnet", revoked: false, warning: false, fail: false, delayed: false,
  created: false, transactions: [] as unknown[], pending: [] as (() => void)[] };
function emit() { version++; for (const listener of listeners) listener(); }
function useVersion() { useSyncExternalStore(fn => { listeners.add(fn); return () => { listeners.delete(fn); }; }, () => version, () => 0); }
export function useCurrentAccount() { useVersion(); return state.account ? { address: state.account, chains: ["sui:localnet"] } : null; }
export function useCurrentNetwork() { useVersion(); return state.network; }
export function useCurrentClient() { return client; }
import { device_registry_DeviceBcs } from "../../dapp/src/lib/fleet-protocol";
import { MANIFEST } from "../../dapp/src/lib/deployment-manifest";
import { F, G, H } from "./customer-discovery-browser";
const D = "0x" + "d".repeat(64), E = "0x" + "e".repeat(64), J = "0x" + "1".repeat(64);
export const deviceIds = [D, E, J];
const client = { getObjects: async ({ objectIds }: { objectIds: string[] }) => ({ objects: objectIds.map(objectId => ({
  objectId, type: `${MANIFEST!.type_origins["device_registry::Device"]}::device_registry::Device`,
  content: device_registry_DeviceBcs.serialize({ id: objectId, name: objectId === D ? "Alpha device" : objectId === E ? "Beta device" : "Separate customer device", device_address: A,
    arch: "x86_64", meta: { contents: [] }, registered_at: 1, schema_version: 1 }).toBytes(),
})) }) };
export const control = {
 wallet(account: string, network = "localnet") { state.account = account; state.network = network; emit(); },
 revoke(value = true) { state.revoked = value; emit(); },
 warn(value = true) { state.warning = value; emit(); },
 fail(value = true) { state.fail = value; emit(); },
 delay(value = true) { state.delayed = value; },
 release() { state.delayed = false; for (const fn of state.pending.splice(0)) fn(); },
 state,
};
