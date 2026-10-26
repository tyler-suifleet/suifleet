// Live browser acceptance (SF-010): the real dApp Kit provider, connect UI,
// useFleetTransaction hook, transaction builders, queries and Walrus upload
// code against a live network. The wallet is a scripted Wallet Standard
// wallet signing with a throwaway test key (funded for this run, swept back
// afterwards); it is the one stand-in for a browser extension.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider, useCurrentAccount } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { Ed25519Keypair } from "@mysten/sui/keypairs/ed25519";
import { Transaction } from "@mysten/sui/transactions";
import { fromHex, toBase64 } from "@mysten/sui/utils";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import * as sui from "@/lib/sui";
import { buildCleanupStep } from "./receipt-cleanup";
import * as walrus from "@/lib/walrus";

const m = LIVE_MANIFEST;
const keypair = Ed25519Keypair.fromSecretKey(fromHex(LIVE_SEED));
const chain = `sui:${m.network}`;
const client = new SuiGrpcClient({ network: m.network, baseUrl: m.rpc_url });
let reject = false;
let lastDigest = null; // Last transaction this wallet executed (success or abort).
const PUBLIC = { kind: "public" }; // SF-022 requires an explicit delivery choice per upload.
const listeners = new Set();
const account = { address: keypair.getPublicKey().toSuiAddress(), publicKey: keypair.getPublicKey().toSuiBytes(), chains: [chain],
  features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const wallet = {
  version: "1.0.0", name: "SuiFleet Live Test Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [chain], accounts: [account],
  features: {
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => listeners.forEach((fn) => fn({ accounts: [] })) },
    "standard:events": { version: "1.0.0", on: (_, fn) => { listeners.add(fn); return () => listeners.delete(fn); } },
    "sui:signTransaction": { version: "2.0.0", signTransaction: async ({ transaction }) => {
      if (reject) throw new Error("User rejected signing");
      return keypair.signTransaction(await Transaction.from(await transaction.toJSON()).build({ client }));
    } },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async ({ transaction, chain: requested }) => {
      if (requested !== chain) throw new Error("Wrong signing chain");
      if (reject) throw new Error("User rejected signing");
      const parsed = Transaction.from(await transaction.toJSON());
      const bytes = await parsed.build({ client });
      const result = await keypair.signAndExecuteTransaction({ transaction: parsed, client });
      const tx = result.Transaction ?? result.FailedTransaction;
      lastDigest = tx.digest;
      return { bytes: toBase64(bytes), signature: tx.signatures[0], digest: tx.digest, effects: toBase64(tx.effects.bcs) };
    } },
  },
};
const kit = createDAppKit({ networks: [m.network], createClient: () => client, autoConnect: false, slushWalletConfig: null, storage: null });
window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));

window.live = {
  address: account.address, reject: (value) => { reject = value; },
  wallets: () => kit.stores.$wallets.get().map((w) => w.name),
  unsupportedChain: () => { account.chains = ["sui:mainnet"]; listeners.forEach((fn) => fn({ accounts: [account] })); },
  restoreChain: () => { account.chains = [chain]; listeners.forEach((fn) => fn({ accounts: [account] })); },
  devices: () => sui.fetchDevices(client, account.address),
  groups: () => sui.fetchGroups(client, account.address),
  deployments: () => sui.fetchDeploymentEvents(client, account.address),
  storage: (recipeBlobId) => walrus.probeDeploymentStorage(recipeBlobId),
  // An explicitly public test component: the gate's daemon has no Seal access.
  async upload(name, version, script) {
    const artifact = await walrus.uploadArtifact(new File([script], `${name}-${version}`), account.address, PUBLIC);
    const recipe = await walrus.uploadRecipe({ schemaVersion: "1.0", componentName: name, componentVersion: version,
      artifactBlobId: artifact.blobId, artifactSha256: artifact.sha256, lifecycle: {}, configuration: {} }, account.address, PUBLIC);
    return { artifact, recipe };
  },
  // Sweep everything left back to the funder, directly with the test key.
  async sweep(to) {
    const tx = new Transaction(); tx.transferObjects([tx.gas], to);
    const result = await keypair.signAndExecuteTransaction({ transaction: tx, client });
    return (result.Transaction ?? result.FailedTransaction).digest;
  },
  lastDigest: () => lastDigest, resetDigest: () => { lastDigest = null; },
  // Execution status read back from the network, independently of the hook.
  async executed(digest) {
    const result = await client.core.waitForTransaction({ digest, include: { effects: true } });
    const tx = result.Transaction ?? result.FailedTransaction;
    return { success: Boolean(tx.status.success), error: tx.status.error?.message ?? null };
  },
  builders: sui,
};

function App() {
  const connected = useCurrentAccount(), mutation = useFleetTransaction();
  React.useEffect(() => {
    // `submit(builder, ...args)` runs a dApp builder through the dApp's hook.
    window.live.submit = async (builder, ...args) => {
      const transaction = typeof builder === "function" ? builder() : sui[builder](...args);
      return mutation.mutateAsync({ transaction });
    };
    window.live.deleteDeploymentStep = async (deployment, fleet) => {
      const transaction = await buildCleanupStep(client, deployment, fleet);
      if (!transaction) return { done: true };
      return { done: false, ...await mutation.mutateAsync({ transaction }) };
    };
    // A transaction that executes and aborts on chain. The SDK simulates any
    // transaction that still needs resolution and refuses an abort before
    // signing, so resolve the dApp builder's commands without execution checks
    // and pay gas explicitly: the fully resolved transaction is then signed,
    // executed and judged by the hook like any other.
    window.live.submitAborting = async (builder, ...args) => {
      const built = sui[builder](...args);
      built.setSender(account.address);
      const transaction = Transaction.fromKind(await built.build({ client, onlyTransactionKind: true }));
      const [{ referenceGasPrice }, { objects }] = await Promise.all([client.core.getReferenceGasPrice(),
        client.core.listCoins({ owner: account.address })]);
      const coin = objects.reduce((a, b) => (BigInt(b.balance) > BigInt(a.balance) ? b : a));
      transaction.setSender(account.address);
      transaction.setGasPrice(referenceGasPrice);
      transaction.setGasBudget(20_000_000);
      transaction.setGasPayment([{ objectId: coin.objectId, version: coin.version, digest: coin.digest }]);
      lastDigest = null;
      return mutation.mutateAsync({ transaction });
    };
    window.live.ready = true;
  }, [mutation]);
  return <><ConnectButton /><div id="account">{connected?.address ?? "disconnected"}</div></>;
}
createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><App /></DAppKitProvider></QueryClientProvider>);
