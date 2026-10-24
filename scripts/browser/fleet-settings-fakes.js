// Test-only dependencies for the real Fleet settings page (scripts/test_fleet_settings_browser.cjs).
// The wallet, chain reads, fleet budget and the SF-130 settings transaction are
// controlled; the page's own pricing, estimate and deposit logic are real.
// The scenario comes from the URL: ?scenario=short|funded|unsupported.
const scenario = new URLSearchParams(location.search).get("scenario") ?? "short";
const control = { scenario, built: [], signed: [], gasReads: 0, walrusReads: 0 };
window.__control = control;
const SUI = BigInt(1_000_000_000);

const account = { address: "0x" + "a".repeat(64), chains: ["sui:testnet"] };
const client = {
  getReferenceGasPrice: async () => { control.gasReads++; return { referenceGasPrice: "1000" }; },
  $extend: () => ({ walrus: { systemState: async () => { control.walrusReads++;
    return { storage_price_per_unit_size: "11000", write_price_per_unit_size: "20000", committee: { epoch: 7 } }; } } }),
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
const budget = { id: "0xb1", capId: "0xc1", balance: funded ? BigInt(100_000) * SUI : SUI / BigInt(100) };
const storage = { id: "0x51", walBalance: funded ? BigInt(100_000) * SUI : BigInt(0) };
export const useFleetStorage = () => ({ budget, storage, config: { packageId: "0x81" } });
export const useBudgets = () => ({ data: [budget], isLoading: false });

export const useFleetTransaction = () => ({
  isPending: false,
  mutateAsync: async ({ transaction }) => { control.signed.push(transaction.settings); return { digest: "FAKEDIGEST" + control.signed.length }; },
});

export function settingsSaver() {
  if (scenario === "unsupported") return { reason: "This deployment's contracts have no on-chain fleet settings yet (SF-130)." };
  return { build: (settings) => { control.built.push(settings); return { settings, setSender() {} }; } };
}
