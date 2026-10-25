import type { SuiGrpcClient } from "@mysten/sui/grpc";
import type { Transaction } from "@mysten/sui/transactions";
import { normalizeId } from "./deployment-manifest";
import { readFleetData, readDeviceData, type FleetDataConfig } from "./fleet-data";
import type { NamedFleet } from "./customer-fleets";

/** A support grant authorizes this one diagnostic request, not arbitrary
 * customer administration or a transaction carrying extra commands. */
export async function verifySupportDiagnostics(client: SuiGrpcClient, fleet: NamedFleet, config: FleetDataConfig,
  deviceId: string, wallet: string, transaction: Transaction): Promise<void> {
  if (normalizeId(fleet.budgetId) !== normalizeId(config.budgetId) || !fleet.devices.includes(normalizeId(deviceId))) throw Error("Diagnostic target is outside the selected fleet");
  const [data, writer] = await Promise.all([readFleetData(client, config), readDeviceData(client, config, deviceId)]);
  if (!data.support.some(address => normalizeId(address) === normalizeId(wallet)) || !writer) throw Error("Support access was revoked or the device is unavailable");
  const tx = transaction.getData(), call = tx.commands.length === 1 ? tx.commands[0].MoveCall : undefined;
  if (!call || tx.inputs.length !== 3 || normalizeId(call.package) !== normalizeId(config.packageId) || call.module !== "fleet_data"
    || call.function !== "request_diagnostics_support" || call.typeArguments.length || call.arguments.length !== 3) throw Error("Support access only authorizes a single diagnostic request");
  const expected = [config.fleetId, writer.id, "0x6"].map(normalizeId);
  call.arguments.forEach((arg, index) => {
    const input = arg.$kind === "Input" ? tx.inputs[arg.Input] : undefined;
    const id = input?.UnresolvedObject?.objectId ?? input?.Object?.SharedObject?.objectId;
    if (!id || normalizeId(id) !== expected[index]) throw Error("Diagnostic request object binding changed");
  });
}
