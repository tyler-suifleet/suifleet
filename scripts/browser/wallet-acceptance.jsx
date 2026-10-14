// Test-only browser harness: actual dApp Kit/Wallet Standard/React hook.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider, useCurrentAccount } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeSuiAddress, fromHex, toBase58 } from "@mysten/sui/utils";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { UpgradeCapBcs } from "@/lib/move-bcs";

const m = ACCEPTANCE_MANIFEST, address = normalizeSuiAddress("0xa"), digest = toBase58(new Uint8Array(32));
let reject = false, failEffects = false, chain = m.chain_identifier, signCalls = 0;
const account = { address, publicKey: new Uint8Array(32), chains: [`sui:${m.network}`], features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const listeners = new Set();
const wallet = {
  version: "1.0.0", name: "SuiFleet Acceptance Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [`sui:${m.network}`], accounts: [account],
  features: {
    "sui:signTransaction": { version: "2.0.0", signTransaction: async () => { throw new Error("Not used in sign-and-execute acceptance"); } },
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => { listeners.forEach((fn) => fn({ accounts: [] })); } },
    "standard:events": { version: "1.0.0", on: (_, fn) => { listeners.add(fn); return () => listeners.delete(fn); } },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async (input) => {
      signCalls++;
      if (input.chain !== `sui:${m.network}`) throw new Error("Wrong signing chain");
      if (reject) throw new Error("User rejected signing");
      const tx = Transaction.from(await input.transaction.toJSON());
      const effects = bcs.TransactionEffects.serialize({ V1: {
        status: failEffects ? { Failure: { InsufficientGas: true } } : { Success: true }, executedEpoch: "1",
        gasUsed: { computationCost: "1", storageCost: "1", storageRebate: "0", nonRefundableStorageFee: "0" },
        modifiedAtVersions: [], sharedObjects: [], transactionDigest: digest, created: [], mutated: [], unwrapped: [],
        deleted: [], unwrappedThenDeleted: [], wrapped: [], gasObject: [{ objectId: "0x1", version: "1", digest }, { AddressOwner: address }],
        eventsDigest: null, dependencies: [],
      } }).toBase64();
      return { digest, bytes: BufferlessBase64(await tx.build()), signature: "fixture-signature", effects };
    } },
  },
};
function BufferlessBase64(bytes) { return btoa(String.fromCharCode(...bytes)); }
const client = new SuiGrpcClient({ network: m.network, baseUrl: "http://127.0.0.1:1" });
// Only network responses are fixtures: dApp Kit and effect decoding are real.
client.ledgerService.getServiceInfo = (async () => ({ response: { chainId: toBase58(fromHex(chain + "00".repeat(28))) } }));
client.getObject = (async ({ objectId }) => {
  if (objectId === m.call_package_id) return { object: { objectBcs: bcs.Object.serialize({ data: { Package: {
    id: m.call_package_id, version: m.package_version, moduleMap: new Map(), linkageTable: new Map(),
    typeOriginTable: Object.entries(m.type_origins).map(([name, origin]) => ({ moduleName: name.split("::")[0], datatypeName: name.split("::")[1], package: origin })),
  } }, owner: { Immutable: true }, previousTransaction: digest, storageRebate: 0 }).toBytes() } };
  if (objectId === m.upgrade_cap_id) return { object: { type: "0x2::package::UpgradeCap", content: UpgradeCapBcs.serialize({ id: objectId, package: m.call_package_id, version: m.package_version, policy: 0 }).toBytes() } };
  return { object: { type: `${m.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`, owner: { $kind: "Shared", Shared: { initialSharedVersion: "1" } } } };
});
client.waitForTransaction = (async () => ({ $kind: "Transaction", Transaction: { digest, status: { success: true } } }));
const kit = createDAppKit({ networks: [m.network], createClient: () => client, autoConnect: false, slushWalletConfig: null, storage: null });
window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
Object.assign(window, { acceptance: {
  reject: () => { reject = true; }, effectsFail: () => { reject = false; failEffects = true; },
  reset: () => { reject = false; failEffects = false; chain = m.chain_identifier; }, wrongChain: () => { chain = "deadbeef"; },
  unsupportedChain: () => { account.chains = ["sui:mainnet"]; listeners.forEach((fn) => fn({ accounts: [account] })); },
  wallets: () => kit.stores.$wallets.get().map((w) => w.name), signCalls: () => signCalls, disconnect: () => kit.disconnectWallet(),
} });
function App() {
  const connected = useCurrentAccount(), mutation = useFleetTransaction();
  const [outcome, setOutcome] = React.useState("idle");
  return <><ConnectButton /><div id="account">{connected?.address ?? "disconnected"}</div><button id="sign" onClick={async () => {
    setOutcome("pending");
    const tx = new Transaction(); tx.setGasPayment([{ objectId: "0x1", version: "1", digest }]); tx.setGasPrice(1); tx.setGasBudget(1000000); tx.transferObjects([tx.gas], "0xb");
    try { await mutation.mutateAsync({ transaction: tx }); setOutcome("success"); } catch (error) { setOutcome(`error: ${String(error)}`); }
  }}>Sign test transaction</button><div id="outcome">{outcome}</div></>;
}
createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><App /></DAppKitProvider></QueryClientProvider>);
