// Test-only dependencies for the real Fleet archives page (scripts/test_data_session_browser.cjs).
// Chain reads, Seal and Walrus are controlled so a read can be held open while
// the signed Seal session expires; window.__control drives it from Playwright.
const operator = "0x000000000000000000000000000000000000000000000000000000000000000a";
const device = "0x00000000000000000000000000000000000000000000000000000000000000d1";
const control = { keys: [], reads: [] };
window.__control = control;

const account = { address: operator, chains: ["sui:localnet"] };
const client = { $extend: () => ({ walrus: { systemState: async () => ({committee:{epoch:0}}) } }) };
const wallet = {
  stores: { $connection: { get: () => ({ account }) }, $currentNetwork: { get: () => "localnet" } },
  signPersonalMessage: async () => ({ signature: "signed" }),
};
export const useCurrentAccount = () => account;
export const useCurrentClient = () => client;
export const useDAppKit = () => wallet;

export class SessionKey {
  static async create() { const key = new SessionKey(); control.keys.push(key); return key; }
  constructor() { this.expired = false; }
  getPersonalMessage() { return new Uint8Array(1); }
  async setPersonalMessageSignature() {}
  isExpired() { return this.expired; }
}

export const useDevices = () => ({ data: [] });
export const useFleetTransaction = () => ({ mutateAsync: async () => {}, isPending: false });

export const DATA_CONFIG = { budgetId: "0xb1", typeOrigin: "0x81", packageId: "0x81", fleetId: "0xf1" };
export const readFleetData = async () => ({ support: [operator] });
export const readDeviceData = async () => ({ id: "0xe1", anchor_count: "1" });
export const listFleetDeviceIds = async () => ({ devices: [device], cursor: null, hasNextPage: false });
export const readAnchorEntries = async () => ({ entries: [{ index: 0, anchor: { kind: 0, at_ms: "1", end_epoch: 100 } }], next: 1 });
export const fetchBudgets = async () => [];
export const archiveDecryptor = async () => () => {};
// Each archive read waits until the test resolves it.
export const fetchVerifiedArchive = () => new Promise((resolve) => control.reads.push(resolve));
export const requireManifest = () => ({ network: "localnet" });
export const verifyDeployment = async () => {};
export const SEAL_SETTINGS = {};
export const WALRUS_AGGREGATOR = "";
export const walrus = () => ({});
export const TESTNET_WALRUS_PACKAGE_CONFIG = {};

export const liveArchiveEntries = async (_client,_config,_device,entries) => entries;

export const parseCompactHead = () => { throw Error('This session fixture has no compact history'); };

// The policy panel has its own authority/async tests; this fixture isolates sessions.
export const StreamRetentionPanel = () => null;

// Usage metering is covered by test_data_metering.cjs; isolate Seal session lifetime here.
export const DataUsage = () => null;

// Generic browsing has its own real-browser harness; isolate archive sessions here.
export const FleetDataBrowser = () => null;
