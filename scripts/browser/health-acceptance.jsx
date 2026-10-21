// Test-only browser harness for device health (SF-019): the real Devices
// page, dApp Kit and telemetry verification. The wallet and chain reads are
// fixtures; the health service is a real HTTP server serving signed reports.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { normalizeSuiAddress } from "@mysten/sui/utils";
import DevicesPage from "@/app/devices/page";
import { DeviceBcs, DeviceOwnerCapBcs } from "@/lib/move-bcs";
import { device_registry_IdentityBcs as IdentityBcs } from "@/lib/fleet-protocol";
import { manifestClient } from "../fixtures/manifest-client.ts";

const m = ACCEPTANCE_MANIFEST, fixture = HEALTH_FIXTURE;
const account = { address: fixture.operator, publicKey: new Uint8Array(32), chains: [`sui:${m.network}`], features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const wallet = {
  version: "1.0.0", name: "SuiFleet Acceptance Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [`sui:${m.network}`], accounts: [account],
  features: {
    "sui:signTransaction": { version: "2.0.0", signTransaction: async () => { throw new Error("Not used"); } },
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => {} },
    "standard:events": { version: "1.0.0", on: () => () => {} },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async () => { throw new Error("Not used"); } },
  },
};
const type = (name) => `${m.type_origins[name]}::${name}`;
const client = new SuiGrpcClient({ network: m.network, baseUrl: "http://127.0.0.1:1" });
const responses = manifestClient(m);
client.ledgerService.getServiceInfo = responses.ledgerService.getServiceInfo;
client.getObject = responses.getObject;
// Device detail history uses these hooks too; this health-only fixture has no deployments/groups.
client.listEvents = async () => ({ events: [], hasNextPage: false, endCursor: null });
const device = (d) => ({ objectId: d.id, type: type("device_registry::Device"), content: DeviceBcs.serialize({
  id: d.id, name: d.name, device_address: fixture.signer, arch: "aarch64", meta: { contents: [] }, registered_at: 1, schema_version: 1 }).toBytes() });
client.listOwnedObjects = async ({ type: wanted }) => {
  const objects = wanted === type("device_registry::DeviceOwnerCap")
    ? fixture.devices.filter((d) => d.activated).map((d) => ({ objectId: d.cap, content: DeviceOwnerCapBcs.serialize({ id: d.cap, device_id: d.id }).toBytes() }))
    : fixture.devices.filter((d) => !d.activated).map(device);
  return { objects, hasNextPage: false };
};
client.getObjects = async ({ objectIds }) => ({ objects: objectIds.map((id) => device(fixture.devices.find((d) => d.id === id))) });
client.getDynamicField = async ({ name }) => {
  const id = normalizeSuiAddress([...name.bcs].map((b) => b.toString(16).padStart(2, "0")).join(""));
  const found = fixture.devices.find((d) => d.id === id && d.activated);
  if (!found) throw new ObjectError("missing", "not found", { reason: "notFound" });
  return { dynamicField: { value: { type: type("device_registry::Identity"),
    bcs: IdentityBcs.serialize({ signer: fixture.signer, revoked: false, owner_cap: found.cap }).toBytes() } } };
};
const kit = createDAppKit({ networks: [m.network], createClient: () => client, autoConnect: false, slushWalletConfig: null, storage: null });
window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><ConnectButton /><DevicesPage /></DAppKitProvider></QueryClientProvider>);
