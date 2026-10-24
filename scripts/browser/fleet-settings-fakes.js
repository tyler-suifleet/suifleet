// Test-only dependencies for the real Fleet settings page (scripts/test_fleet_settings_browser.cjs).
// The wallet, chain reads and fleet budget are controlled; the page's pricing,
// estimate, deposit logic and the SF-130 settings transaction it builds are
// real. The "wallet" records the transaction it is asked to sign, decoded.
// The scenario comes from the URL: ?scenario=short|funded|saved|rejected|unsupported.
import { bcs } from "@mysten/sui/bcs";
import { ObjectError } from "@mysten/sui/client";
import { fleet_settings_SettingsBcs } from "@/lib/fleet-protocol";

const scenario = new URLSearchParams(location.search).get("scenario") ?? "short";
const control = { scenario, signed: [], settingsReads: [], gasReads: 0, walrusReads: 0 };
window.__control = control;
const SUI = BigInt(1_000_000_000);

const account = { address: "0x" + "a".repeat(64), chains: ["sui:testnet"] };
const client = {
  getReferenceGasPrice: async () => { control.gasReads++; return { referenceGasPrice: "1000" }; },
  $extend: () => ({ walrus: { systemState: async () => { control.walrusReads++;
    return { storage_price_per_unit_size: "11000", write_price_per_unit_size: "20000", committee: { epoch: 7 } }; } } }),
  // The fleet's saved settings record: present only in the "saved" scenario.
  getDynamicField: async ({ parentId }) => {
    control.settingsReads.push(parentId);
    if (scenario !== "saved") throw new ObjectError("notFound", "missing field", { reason: "notFound" });
    const bytes = fleet_settings_SettingsBcs.serialize({ version: 1, check_interval_ms: 900_000, rollout_check_interval_ms: 0,
      health_interval_ms: 3_600_000, archive_interval_ms: 86_400_000, archive_retention_epochs: 42 }).toBytes();
    return { dynamicField: { value: { type: `${window.__SF_MANIFEST_OBJECT.call_package_id}::fleet_settings::Settings`, bcs: bytes } } };
  },
};
export const useCurrentAccount = () => account;
export const useCurrentClient = () => client;
// Used only by budget enrollment, which this page does not render.
export const useDAppKit = () => ({});

// @mysten/walrus, as the quote hook uses it.
export const walrus = () => ({});
export const MAINNET_WALRUS_PACKAGE_CONFIG = { name: "mainnet" };
export const TESTNET_WALRUS_PACKAGE_CONFIG = { name: "testnet" };

const devices = [1, 2, 3].map((n) => ({ objectId: `0xd${n}`, name: `pi-${n}` }));
export const useDevices = () => ({ data: devices, isSuccess: true });

const funded = scenario === "funded";
const budget = { id: "0x" + "b1".repeat(32), capId: "0x" + "c1".repeat(32), balance: funded ? BigInt(100_000) * SUI : SUI / BigInt(100) };
const storage = { id: "0x51", walBalance: funded ? BigInt(100_000) * SUI : BigInt(0) };
export const useFleetStorage = () => ({ budget, storage, config: { packageId: "0x81" } });
export const useBudgets = () => ({ data: [budget], isLoading: false });

// The signed transaction, decoded: its Move call, object inputs and pure values.
function decode(transaction) {
  const data = transaction.getData();
  const calls = data.commands.filter((c) => c.$kind === "MoveCall").map(({ MoveCall: c }) => {
    const inputs = c.arguments.map((a) => data.inputs[a.Input]);
    const pure = inputs.filter((i) => i.Pure).map((i) => Uint8Array.from(atob(i.Pure.bytes), (ch) => ch.charCodeAt(0)));
    return { target: `${c.package}::${c.module}::${c.function}`, objects: inputs.filter((i) => i.UnresolvedObject).map((i) => i.UnresolvedObject.objectId),
      values: pure.map((bytes) => bytes.length === 8 ? bcs.u64().parse(bytes) : bcs.u32().parse(bytes)) };
  });
  return { sender: data.sender, calls };
}

export const useFleetTransaction = () => ({
  isPending: false,
  mutateAsync: async ({ transaction }) => {
    control.signed.push(decode(transaction));
    if (scenario === "rejected") throw new Error("User rejected the request");
    return { digest: "FAKEDIGEST" + control.signed.length };
  },
});
