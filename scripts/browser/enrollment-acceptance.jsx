// Test-only browser harness for bulk enrollment (SF-100): the real Enrollment
// and Devices pages, dApp Kit, useFleetTransaction and transaction builders
// against a disposable localnet (reached through the page server, same
// origin). The wallet is a scripted Wallet Standard wallet signing with the
// test operator key; it is the one stand-in for a browser extension.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { Ed25519Keypair } from "@mysten/sui/keypairs/ed25519";
import { Transaction } from "@mysten/sui/transactions";
import { fromHex, toBase64 } from "@mysten/sui/utils";
import EnrollmentPage from "@/app/enrollment/page";
import DevicesPage from "@/app/devices/page";

const m = LIVE_MANIFEST;
const keypair = Ed25519Keypair.fromSecretKey(fromHex(OPERATOR_SEED));
const chain = `sui:${m.network}`;
const client = new SuiGrpcClient({ network: m.network, baseUrl: window.location.origin });
let reject = false, calls = 0;
const digests = [];
const account = { address: keypair.getPublicKey().toSuiAddress(), publicKey: keypair.getPublicKey().toSuiBytes(), chains: [chain],
  features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const wallet = {
  version: "1.0.0", name: "SuiFleet Enrollment Test Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [chain], accounts: [account],
  features: {
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => {} },
    "standard:events": { version: "1.0.0", on: () => () => {} },
    "sui:signTransaction": { version: "2.0.0", signTransaction: async () => { throw new Error("Not used"); } },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async ({ transaction, chain: requested }) => {
      calls++;
      if (requested !== chain) throw new Error("Wrong signing chain");
      if (reject) throw new Error("User rejected the request");
      const parsed = Transaction.from(await transaction.toJSON());
      const bytes = await parsed.build({ client });
      const result = await keypair.signAndExecuteTransaction({ transaction: parsed, client });
      const tx = result.Transaction ?? result.FailedTransaction;
      digests.push(tx.digest);
      return { bytes: toBase64(bytes), signature: tx.signatures[0], digest: tx.digest, effects: toBase64(tx.effects.bcs) };
    } },
  },
};
const kit = createDAppKit({ networks: [m.network], createClient: () => client, autoConnect: false, slushWalletConfig: null, storage: null });
window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
window.enrollment = { address: account.address, reject: (value) => { reject = value; }, calls: () => calls, digests: () => [...digests] };

function App() {
  const [page, setPage] = useState("enrollment");
  return <>
    <ConnectButton />
    <nav><button onClick={() => setPage("enrollment")}>Enrollment page</button> <button onClick={() => setPage("devices")}>Devices page</button></nav>
    {page === "enrollment" ? <EnrollmentPage /> : <DevicesPage />}
  </>;
}
createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><App /></DAppKitProvider></QueryClientProvider>);
