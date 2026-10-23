// Test-only browser harness: actual dApp Kit/Wallet Standard/React hook.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider, useCurrentAccount } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeSuiAddress, toBase58 } from "@mysten/sui/utils";
import { dAppKit as kit } from "@/lib/dapp-kit";
import { FirstRun } from "@/components/FirstRun";
import { DeviceBcs } from "@/lib/move-bcs";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { manifestClient } from "../fixtures/manifest-client.ts";

const m = ACCEPTANCE_MANIFEST, address = normalizeSuiAddress("0xa"), digest = toBase58(new Uint8Array(32));
let reject = false, failEffects = false, chain = m.chain_identifier, signCalls = 0, gasBalance = "0", expired = false, epoch = 1n;
let hold = null, balanceFail = false, inventoryFail = false, registerOnSuccess = null;
const inventory = [];
const account = { address, publicKey: new Uint8Array(32), chains: [`sui:${m.network}`], features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const listeners = new Set();
const wallet = {
  version: "1.0.0", name: "SuiFleet Acceptance Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [`sui:${m.network}`], accounts: [account],
  features: {
    "enoki:getMetadata": { version: "1.0.0", getMetadata: () => ({ provider: "google" }) },
    "enoki:getSession": { version: "1.0.0", getSession: async () => ({ jwt: "fixture", maxEpoch: 1, expiresAt: expired ? 0 : Date.now() + 60000 }) },
    "sui:signTransaction": { version: "2.0.0", signTransaction: async () => { throw new Error("Not used in sign-and-execute acceptance"); } },
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => { listeners.forEach((fn) => fn({ accounts: [] })); } },
    "standard:events": { version: "1.0.0", on: (_, fn) => { listeners.add(fn); return () => listeners.delete(fn); } },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async (input) => {
      signCalls++;
      if (input.chain !== `sui:${m.network}`) throw new Error("Wrong signing chain");
      if (reject) throw new Error("User rejected signing");
      if (hold) await hold.promise;
      const tx = Transaction.from(await input.transaction.toJSON());
      const effects = bcs.TransactionEffects.serialize({ V1: {
        status: failEffects ? { Failure: { InsufficientGas: true } } : { Success: true }, executedEpoch: "1",
        gasUsed: { computationCost: "1", storageCost: "1", storageRebate: "0", nonRefundableStorageFee: "0" },
        modifiedAtVersions: [], sharedObjects: [], transactionDigest: digest, created: [], mutated: [], unwrapped: [],
        deleted: [], unwrappedThenDeleted: [], wrapped: [], gasObject: [{ objectId: "0x1", version: "1", digest }, { AddressOwner: address }],
        eventsDigest: null, dependencies: [],
      } }).toBase64();
      const output = new Transaction(); output.setSender(address); output.setGasPayment([{ objectId: "0x1", version: "1", digest }]); output.setGasPrice(1); output.setGasBudget(1000000); output.transferObjects([output.gas], "0xb");
      if (!failEffects && registerOnSuccess) { inventory.push(registerOnSuccess); registerOnSuccess = null; }
      return { digest, bytes: BufferlessBase64(await output.build()), signature: "fixture-signature", effects };
    } },
  },
};
function BufferlessBase64(bytes) { return btoa(String.fromCharCode(...bytes)); }
const client = kit.getClient();
// Only network responses are fixtures: dApp Kit and effect decoding are real.
const responses = manifestClient(m, () => chain);
client.ledgerService.getServiceInfo = async () => { const result = await responses.ledgerService.getServiceInfo(); result.response.epoch = epoch; return result; };
client.getBalance = async () => { if (balanceFail) throw new Error("Balance service unavailable"); return { balance: { balance: gasBalance } }; };
const deviceType = `${m.type_origins["device_registry::Device"]}::device_registry::Device`;
client.listOwnedObjects = async ({ type }) => {
  if (inventoryFail) throw new Error("Inventory service unavailable");
  const objects = type === deviceType ? inventory.map((d) => ({ objectId: d.id, type: deviceType, content: DeviceBcs.serialize({
    id: d.id, name: d.name, device_address: d.id, arch: "aarch64", meta: { contents: [] }, registered_at: 1, schema_version: 1 }).toBytes() })) : [];
  return { objects, hasNextPage: false };
};
client.getObject = responses.getObject;
client.waitForTransaction = (async () => ({ $kind: "Transaction", Transaction: { digest, status: { success: true } } }));

window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
Object.assign(window, { acceptance: {
  fund: () => { gasBalance = "1000000000"; }, expire: () => { expired = true; }, advanceEpoch: () => { epoch = 2n; }, reject: () => { reject = true; }, effectsFail: () => { reject = false; failEffects = true; },
  reset: () => { reject = false; failEffects = false; chain = m.chain_identifier; expired = false; epoch = 1n; balanceFail = false; inventoryFail = false; },
  hold: () => { let release; const promise = new Promise((resolve) => { release = resolve; }); hold = { promise, release }; },
  release: () => { hold?.release(); hold = null; },
  registerAs: (name) => { registerOnSuccess = { id: normalizeSuiAddress("0x" + "3".repeat(64)), name }; },
  balanceFail: () => { balanceFail = true; }, inventoryFail: () => { inventoryFail = true; }, wrongChain: () => { chain = "deadbeef"; },
  unsupportedChain: () => { account.chains = ["sui:mainnet"]; listeners.forEach((fn) => fn({ accounts: [account] })); },
  wallets: () => kit.stores.$wallets.get().map((w) => w.name), signCalls: () => signCalls, disconnect: () => kit.disconnectWallet(),
} });
function App() { return <FirstRun connect={<ConnectButton />} />; }
createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><App /></DAppKitProvider></QueryClientProvider>);
