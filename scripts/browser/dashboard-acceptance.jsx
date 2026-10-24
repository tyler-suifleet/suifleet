// Test-only browser harness for SF-084 dashboard views: the real Device
// services, Rollouts and Logs pages with the real dApp Kit, SF-082 chain
// reader, signature verification and alert rules. The wallet and fullnode are
// fixtures serving exact BCS; health reports are signed with Mysten's SDK.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createDAppKit, DAppKitProvider } from "@mysten/dapp-kit-react";
import { ConnectButton } from "@mysten/dapp-kit-react/ui";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { bcs, TypeTagSerializer } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import { deriveDynamicFieldID, normalizeStructTag, normalizeSuiAddress as id } from "@mysten/sui/utils";
import ServicesPage from "@/app/services/page";
import RolloutsPage from "@/app/rollouts/page";
import DataPage from "@/app/data/page";
import { ArchiveView, AccessRecord } from "@/components/ArchiveSearch";
import { ShadowDocuments } from "@/components/ShadowDocuments";
import { verifyArchive } from "@/lib/fleet-bulk";
import { shadowDocumentsView } from "@/lib/shadow-documents";
import { DeviceBcs, DeviceOwnerCapBcs, DeploymentBcs, DeploymentCreatedBcs, ReceiptsBcs } from "@/lib/move-bcs";
import { device_registry_IdentityBcs as IdentityBcs, deployment_manager_RolloutBcs as RolloutBcs,
  deployment_manager_RolloutDecisionBcs as DecisionBcs } from "@/lib/fleet-protocol";
import { FleetDataBcs, DeviceDataBcs, DATA_CONFIG as config } from "@/lib/fleet-data";
import { manifestClient } from "../fixtures/manifest-client.ts";

const m = ACCEPTANCE_MANIFEST, f = DASHBOARD_FIXTURE;
const account = { address: f.operator, publicKey: new Uint8Array(32), chains: [`sui:${m.network}`], features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
const refuse = async (input) => {
  if (location.hash === "#onchain") {
    const transaction = Transaction.from(await input.transaction.toJSON());
    window.rolloutTransaction = transaction.getData();
    throw new Error("Fixture wallet stopped before signing");
  }
  throw new Error("This read-only acceptance must not sign");
};
const wallet = {
  version: "1.0.0", name: "SuiFleet Acceptance Wallet", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: [`sui:${m.network}`], accounts: [account],
  features: {
    "sui:signTransaction": { version: "2.0.0", signTransaction: refuse },
    "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: refuse },
    "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
    "standard:disconnect": { version: "1.0.0", disconnect: async () => {} },
    "standard:events": { version: "1.0.0", on: () => () => {} },
  },
};
const type = (name) => normalizeStructTag(`${m.type_origins[name]}::${name}`);
const dataType = (name) => normalizeStructTag(`${config.typeOrigin}::fleet_data::${name}`);
const notFound = () => new ObjectError("missing", "not found", { reason: "notFound" });
const forged = location.hash === "#forged";
const fleet = { id: config.fleetId, version: "1", budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
  chain_identifier: config.chainIdentifier, key_epoch: "0", support: [], health_interval_ms: "86400000", batch_interval_ms: "86400000",
  emergency_interval_ms: "300000", device_count: String(f.devices.length) };
const writers = Object.fromEntries(f.devices.map((d) => [id(d.writer), d]));
// #far-future: u64 grant and decision times beyond the Date range are shown raw.
const farFuture = location.hash === "#far-future", u64max = "18446744073709551615";
const rollout = farFuture ? { ...f.rollout, record: { ...f.rollout.record, admitted_at: [u64max, ...f.rollout.record.admitted_at.slice(1)] },
  decisions: f.rollout.decisions.map((d) => d.revision === "3" ? { ...d, at_ms: u64max } : d) } : location.hash === "#onchain" ? { ...f.rollout, record: { ...f.rollout.record, state: 0 } } : f.rollout;
const deployment = { id: rollout.id, creator: f.operator, component_name: "app", version: "2.0.0", recipe_blob_id: "blob", artifact_sha256: "a".repeat(64), created_at: "1", schema_version: "3" };

const client = new SuiGrpcClient({ network: m.network, baseUrl: "http://127.0.0.1:1" });
const responses = manifestClient(m);
client.ledgerService.getServiceInfo = responses.ledgerService.getServiceInfo;
client.getObject = async ({ objectId }) => {
  objectId = id(objectId);
  if (objectId === rollout.id) return { object: { objectId, type: type("deployment_manager::DeploymentRecord"), content: DeploymentBcs.serialize(deployment).toBytes() } };
  if (location.hash === "#onchain" && (objectId === config.fleetId || writers[objectId])) throw Error("Rollout must not read periodic fleet health");
  if (objectId === config.fleetId) return { object: { objectId, owner: { $kind: "Shared" }, type: dataType("FleetData"), content: FleetDataBcs.serialize(fleet).toBytes() } };
  const d = writers[objectId];
  if (d) {
    const report = forged && d.forgeable ? d.forged : d.report;
    return { object: { objectId, owner: { $kind: "Shared" }, type: dataType("DeviceData"), content: DeviceDataBcs.serialize({
      id: d.writer, version: "1", fleet_id: config.fleetId, device_id: d.id, account_id: d.writer, health_seq: String(report.seq), health_at_ms: String(report.atMs),
      message: report.message, signature: report.signature, batch_seq: "0", batch_at_ms: null, emergency_at_ms: null, anchor_count: "0" }).toBytes() } };
  }
  return responses.getObject({ objectId });
};
const deviceObject = (d) => ({ objectId: d.id, type: type("device_registry::Device"), content: DeviceBcs.serialize({
  id: d.id, name: d.name, device_address: f.signer, arch: "aarch64", meta: { contents: [] }, registered_at: 1, schema_version: 1 }).toBytes() });
client.listOwnedObjects = async ({ type: wanted }) => ({ hasNextPage: false, objects: normalizeStructTag(wanted) === type("device_registry::DeviceOwnerCap")
  ? f.devices.map((d) => ({ objectId: d.cap, content: DeviceOwnerCapBcs.serialize({ id: d.cap, device_id: d.id }).toBytes() })) : [] });
// Match the production batch reader's complete Field type, ID and key bytes.
const fields = new Map();
function field(parent, keyType, Key, name, valueType, Value, value) {
  const fieldId = deriveDynamicFieldID(parent, TypeTagSerializer.parseFromStr(keyType), Key.serialize(name).toBytes());
  fields.set(fieldId, { objectId: fieldId, type: `0x2::dynamic_field::Field<${keyType},${valueType}>`,
    content: bcs.struct("Field", {id:bcs.Address,name:Key,value:Value}).serialize({id:fieldId,name,value}).toBytes() });
}
for (const d of f.devices) {
  field(m.shared_objects.registry.id, type("device_registry::IdentityKey"), bcs.Address, d.id,
    type("device_registry::Identity"), IdentityBcs, {signer:f.signer,revoked:false,owner_cap:d.cap});
  field(config.fleetId, dataType("DeviceKey"), bcs.Address, d.id, "0x2::object::ID", bcs.Address, d.writer);
}
for (const [name, value] of [["TargetGroupsKey", []], ["TargetDevicesKey", rollout.members]]) {
  field(rollout.id, type(`deployment_manager::${name}`), bcs.bool(), false,
    "vector<0x2::object::ID>", bcs.vector(bcs.Address), value);
}
field(rollout.id, type("deployment_manager::ReceiptsKey"), bcs.bool(), false,
  type("deployment_manager::Receipts"), ReceiptsBcs, {version:"2",statuses:{contents:rollout.receipts}});
client.getObjects = async ({ objectIds }) => ({ objects: await Promise.all(objectIds.map(async (objectId) => {
  if (fields.has(id(objectId))) return fields.get(id(objectId));
  if (writers[id(objectId)]) return (await client.getObject({objectId})).object;
  if (id(objectId) === id(rollout.id)) return { objectId, type: type("deployment_manager::DeploymentRecord"), content: DeploymentBcs.serialize(deployment).toBytes() };
  const d = f.devices.find((d) => d.id === id(objectId));
  return d ? deviceObject(d) : notFound();
})) });
client.listEvents = async ({ filter }) => {
  const wanted = normalizeStructTag(filter.eventType);
  const events = wanted === type("deployment_manager::DeploymentCreated") ? [{ bcs: DeploymentCreatedBcs.serialize({ deployment_id: rollout.id,
      component_name: "app", version: "2.0.0", recipe_blob_id: "blob", artifact_sha256: "a".repeat(64), target_groups: [], target_devices: rollout.members, creator: f.operator }).toBytes() }]
    : wanted === type("deployment_manager::RolloutDecision") ? rollout.decisions.map((d) => ({ bcs: DecisionBcs.serialize({ deployment_id: rollout.id, ...d }).toBytes() })) : [];
  return { events, hasNextPage: false, endCursor: null };
};
responses.listEvents = client.listEvents;
client.ledgerService.listEvents = responses.ledgerService.listEvents;
client.listDynamicFields = async ({ parentId }) => {
  if (id(parentId) !== config.fleetId) throw notFound();
  return { hasNextPage: false, cursor: null, dynamicFields: f.devices.map((d) => ({ $kind: "DynamicField", name: { type: dataType("DeviceKey"), bcs: bcs.Address.serialize(d.id).toBytes() }, valueType: "0x2::object::ID" })) };
};
client.getDynamicField = async ({ parentId, name }) => {
  const key = normalizeStructTag(name.type);
  if (key === dataType("DeviceKey")) {
    const d = f.devices.find((d) => d.id === id(bcs.Address.parse(name.bcs)));
    if (!d) throw notFound();
    return { dynamicField: { value: { type: "0x2::object::ID", bcs: bcs.Address.serialize(d.writer).toBytes() } } };
  }
  if (key === type("device_registry::IdentityKey")) {
    const d = f.devices.find((d) => d.id === id(bcs.Address.parse(name.bcs)));
    if (!d) throw notFound();
    return { dynamicField: { value: { type: type("device_registry::Identity"), bcs: IdentityBcs.serialize({ signer: f.signer, revoked: false, owner_cap: d.cap }).toBytes() } } };
  }
  if (id(parentId) === id(rollout.id) && key === type("deployment_manager::ReceiptsKey")) {
    return { dynamicField: { value: { type: type("deployment_manager::Receipts"), bcs: ReceiptsBcs.serialize({ version: "2",
      statuses: { contents: rollout.receipts } }).toBytes() } } };
  }
  if (id(parentId) === id(rollout.id) && key === type("deployment_manager::RolloutKey")) {
    return { dynamicField: { value: { type: type("deployment_manager::Rollout"), bcs: RolloutBcs.serialize(rollout.record).toBytes() } } };
  }
  throw notFound();
};

function ArchiveFixture() {
  const [query, setQuery] = useState({ text: "", source: "", caseSensitive: false });
  const [open, setOpen] = useState(true);
  return <>{open ? <ArchiveView archive={f.archive} index={3} query={query} setQuery={setQuery} close={() => setOpen(false)} /> : <p>Archive closed.</p>}
    <AccessRecord entries={f.access} /></>;
}

// Shadow documents from a signed archive, through the real verifier. Decryption
// itself is covered by scripts/test_shadow_documents.cjs (real Seal SDK).
function ShadowFixture() {
  const [state, setState] = useState({});
  React.useEffect(() => {
    const envelope = location.hash === "#tampered" ? f.tamperedEnvelope : f.shadowEnvelope;
    verifyArchive(new TextEncoder().encode(envelope), config, f.devices[0].id, f.shadowAnchor)
      .then((archive) => setState({ archive }), (error) => setState({ error: error.message }));
  }, []);
  const index = location.hash === "#history" ? 2 : 4;
  return state.error ? <p role="status">Archive unavailable: {state.error}</p>
    : state.archive ? <ShadowDocuments view={shadowDocumentsView(state.archive, index, 5)} index={index} /> : <p>Verifying…</p>;
}

const kit = createDAppKit({ networks: [m.network], createClient: () => client, autoConnect: false, slushWalletConfig: null, storage: null });
window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
const Page = { "/services": ServicesPage, "/rollouts": RolloutsPage, "/data": DataPage, "/archive": ArchiveFixture, "/shadows": ShadowFixture }[location.pathname];
createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={new QueryClient()}><DAppKitProvider dAppKit={kit}><ConnectButton /><Page /></DAppKitProvider></QueryClientProvider>);
