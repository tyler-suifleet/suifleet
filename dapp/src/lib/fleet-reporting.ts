import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress as id } from "@mysten/sui/utils";
import { MANIFEST, verifyDeployment } from "./deployment-manifest";
import { UpgradeCapBcs } from "./move-bcs";
import { configuredDataFleets, FleetDataBcs, readFleetData, readDeviceData, type FleetDataConfig } from "./fleet-data";
import { DEFAULT_QUOTA, FleetStorageBcs, fetchAccount, fetchManagedBudget } from "./fleet-budget";
import type { NamedFleet } from "./customer-fleets";

/** One reviewed package release for all customers, never a per-fleet build list. */
export interface ReportingRelease { packageId: string; upgradeCapId: string; storagePackageId: string; storageUpgradeCapId: string }
export function reportingRelease(): ReportingRelease | undefined {
  const raw = process.env.NEXT_PUBLIC_FLEET_REPORTING_RELEASE;
  if (!raw) return undefined;
  const value = JSON.parse(raw);
  const keys = ["packageId", "upgradeCapId", "storagePackageId", "storageUpgradeCapId"];
  if (!value || Object.keys(value).sort().join() !== keys.sort().join()) throw Error("Invalid reporting release");
  for (const key of keys) value[key] = id(value[key]);
  return value;
}
const Locator = bcs.Address;
export { Locator as ReportingLocatorBcs };

async function releaseOrigins(client: SuiGrpcClient, release: ReportingRelease) {
  async function check(packageId: string, capId: string, module: string, names: string[]) {
    const [{ object }, { object: cap }] = await Promise.all([
      client.getObject({ objectId: packageId, include: { objectBcs: true } }),
      client.getObject({ objectId: capId, include: { content: true } }),
    ]);
    const pkg = bcs.Object.parse(object.objectBcs).data.Package;
    if (!pkg || id(pkg.id) !== packageId || normalizeStructTag(cap.type) !== normalizeStructTag("0x2::package::UpgradeCap")) throw Error("Invalid reporting package proof");
    const upgrade = UpgradeCapBcs.parse(cap.content);
    if (id(upgrade.package) !== packageId || upgrade.version !== pkg.version) throw Error("Reporting release has been upgraded; update the reviewed release");
    return Object.fromEntries(names.map(name => {
      const entries = pkg.typeOriginTable.filter(t => t.moduleName === module && t.datatypeName === name);
      if (entries.length !== 1) throw Error(`Missing reporting type ${name}`);
      return [name, id(entries[0].package)];
    }));
  }
  const [data, storage] = await Promise.all([
    check(release.packageId, release.upgradeCapId, "fleet_data", ["FleetData", "DeviceData", "DeviceKey", "CompactHeadKey", "StreamRetentionKey", "LocatorKey"]),
    check(release.storagePackageId, release.storageUpgradeCapId, "fleet_storage", ["FleetStorage"]),
  ]);
  if (data.DeviceData !== data.FleetData || data.DeviceKey !== data.FleetData) throw Error("Incompatible reporting origins");
  return { data, storage };
}

async function verifyStorage(client: SuiGrpcClient, config: FleetDataConfig, origin: string) {
  const { object } = await client.getObject({ objectId: config.storageId, include: { content: true } });
  if (object.owner.$kind !== "Shared" || normalizeStructTag(object.type) !== normalizeStructTag(`${origin}::fleet_storage::FleetStorage`)) throw Error("Unexpected reporting storage type/ownership");
  const storage = FleetStorageBcs.parse(object.content);
  if (id(storage.id) !== config.storageId || id(storage.budget_id) !== config.budgetId || !["1", "2", "3"].includes(storage.version)) throw Error("Reporting storage binding/version mismatch");
}

/** Only a field on the verified selected budget is authoritative. Absence alone
 * permits explicit legacy fallback; RPC errors and invalid locators fail closed. */
export async function resolveFleetReporting(client: SuiGrpcClient, fleet: NamedFleet, release = reportingRelease()): Promise<FleetDataConfig | undefined> {
  await verifyDeployment(client);
  await fetchManagedBudget(client, fleet.budgetId);
  const origins = release ? await releaseOrigins(client, release) : undefined;
  const keyOrigin = origins?.data.LocatorKey;
  if (keyOrigin && origins) {
    let field;
    try { field = await client.getDynamicField({ parentId: fleet.budgetId, name: {
      type: `${keyOrigin}::fleet_data::LocatorKey`, bcs: bcs.bool().serialize(false).toBytes(),
    } }); } catch (error) {
      if (!(error instanceof ObjectError && error.reason === "notFound")) throw error;
    }
    if (field) {
      if (!release) throw Error("Reporting locator exists but this app has no reviewed reporting release");
      if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag("0x2::object::ID")) throw Error("Unexpected reporting locator type");
      const dataId = id(Locator.parse(field.dynamicField.value.bcs));
      const { object } = await client.getObject({ objectId: dataId, include: { content: true } });
      if (object.owner.$kind !== "Shared" || normalizeStructTag(object.type) !== normalizeStructTag(`${origins.data.FleetData}::fleet_data::FleetData`)) throw Error("Unexpected located data type/ownership");
      const content = FleetDataBcs.parse(object.content);
      const config: FleetDataConfig = { schema: 1, chainIdentifier: MANIFEST!.chain_identifier,
        registryId: id(MANIFEST!.shared_objects.registry.id), packageId: release.packageId, typeOrigin: origins.data.FleetData,
        fleetId: dataId, budgetId: id(fleet.budgetId), storageId: id(content.storage_id),
        compactHistoryOrigin: origins.data.CompactHeadKey, streamRetentionOrigin: origins.data.StreamRetentionKey };
      await readFleetData(client, config);
      await verifyStorage(client, config, origins.storage.FleetStorage);
      return config;
    }
  }
  const legacy = configuredDataFleets().filter(c => id(c.budgetId) === id(fleet.budgetId));
  if (legacy.length > 1) throw Error("Multiple legacy reporting locators for one budget");
  if (legacy[0]) {
    await readFleetData(client, legacy[0]);
    // Legacy storage origin remains an explicit, reviewed migration setting.
    const origin = process.env.NEXT_PUBLIC_FLEET_STORAGE_ORIGIN || process.env.NEXT_PUBLIC_FLEET_STORAGE_PACKAGE;
    if (!origin) throw Error("Legacy reporting requires its verified storage origin");
    await verifyStorage(client, legacy[0], id(origin));
  }
  return legacy[0];
}

export async function buildProvisionReporting(client: SuiGrpcClient, fleet: NamedFleet, release = reportingRelease()) {
  if (!fleet.administrator) throw Error("Only a current fleet administrator can set up reporting");
  if (!release || !MANIFEST) throw Error("This deployment needs the reporting setup release");
  if (await resolveFleetReporting(client, fleet, release)) throw Error("Reporting is already set up; refresh to recover the verified configuration");
  await releaseOrigins(client, release);
  const budget = await fetchManagedBudget(client, fleet.budgetId);
  const tx = new Transaction(), q = DEFAULT_QUOTA;
  const [quota] = tx.moveCall({ target: `${release.storagePackageId}::fleet_storage::new_quota`, arguments:
    [q.periodMs, q.encodedBytesPerPeriod, q.uploadsPerPeriod, q.maxBlobSize, q.writeFeesPerPeriod].map(n => tx.pure.u64(n)) });
  tx.moveCall({ target: `${release.packageId}::fleet_data::provision`, arguments: [tx.object(fleet.budgetId), tx.object(budget.capId), quota, tx.pure.string(MANIFEST.chain_identifier)] });
  return tx;
}

export async function buildEnrollReporting(client: SuiGrpcClient, fleet: NamedFleet, config: FleetDataConfig, device: string) {
  if (!fleet.administrator || !fleet.devices.some(d => id(d) === id(device)) || id(config.budgetId) !== id(fleet.budgetId)) throw Error("Device is outside this administrator's fleet");
  const current = await resolveFleetReporting(client, fleet);
  if (!current || current.fleetId !== config.fleetId) throw Error("Reporting configuration changed; refresh");
  const account = await fetchAccount(client, fleet.budgetId, device);
  if (!account) throw Error("Enroll this device in the network budget first");
  if (await readDeviceData(client, config, device)) throw Error("Device reporting already enrolled; refresh to download settings");
  const budget = await fetchManagedBudget(client, fleet.budgetId), tx = new Transaction();
  tx.moveCall({ target: `${config.packageId}::fleet_data::enroll`, arguments: [tx.object(config.fleetId), tx.object(fleet.budgetId), tx.object(budget.capId), tx.object(account.accountId)] });
  return tx;
}

/** Download an additive TOML fragment, never replace keys/journals/base config. */
export async function reportingClientSettings(client: SuiGrpcClient, fleet: NamedFleet, device: string) {
  if (!fleet.devices.some(d => id(d) === id(device))) throw Error("Device is outside the selected fleet");
  const config = await resolveFleetReporting(client, fleet);
  if (!config) throw Error("Set up fleet reporting first");
  const [writer, account] = await Promise.all([readDeviceData(client, config, device), fetchAccount(client, fleet.budgetId, device)]);
  if (!writer || !account || id(writer.account_id) !== id(account.accountId)) throw Error("Enroll device reporting with its current network account before downloading");
  const values = { package_id: config.packageId, type_origin: config.typeOrigin, fleet_id: config.fleetId,
    budget_id: config.budgetId, storage_id: config.storageId, writer_id: writer.id, account_id: account.accountId };
  return '# Merge into /etc/suifleet/config.toml; preserve device keys and journals.\n# Requires this deployment.json and the existing registered device identity.\n# Merge budget_id into any existing [budget] section; retain its draw settings.\n' +
    `[budget]\nbudget_id = "${id(config.budgetId)}"\n\n[fleet_data]\n` +
    Object.entries(values).map(([key, value]) => `${key} = "${id(value)}"`).join("\n") +
    `\n\n[fleet_data.managed]\ncustomer_id = "${id(fleet.customerId)}"\nfleet_id = "${id(fleet.id)}"\n`;
}
