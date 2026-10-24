// SF-131: saving fleet settings on chain through SF-130's owner-only
// fleet_settings::set. Saving is offered only when this deployment's manifest
// records the settings type origins and the wallet holds a fleet budget's
// owner cap; otherwise the page explains why and still prices the choices.
import type { Transaction } from "@mysten/sui/transactions";
import type { BudgetView } from "@/lib/fleet-budget";
import { MANIFEST } from "@/lib/deployment-manifest";
import { buildSetFleetSettingsTx, settingsSupported, type FleetSettings } from "@/lib/fleet-settings";

export interface SettingsSaver {
  build?: (settings: FleetSettings) => Transaction | Promise<Transaction>;
  /** Why saving is unavailable, when it is. */
  reason?: string;
}

export function settingsSaver(budget: Pick<BudgetView, "id" | "capId"> | undefined, manifest = MANIFEST): SettingsSaver {
  if (!settingsSupported(manifest)) {
    return { reason: "This deployment's contracts have no on-chain fleet settings (SF-130); upgrade the contracts to save. Prices and the deposit below still follow your choices." };
  }
  if (!budget) return { reason: "Settings are stored on your fleet budget: create one on the Network budget page first." };
  return { build: (settings) => buildSetFleetSettingsTx(budget.id, budget.capId, settings) };
}
