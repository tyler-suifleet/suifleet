// SF-131: saving fleet settings on chain. The Move API is SF-130's (#265) and
// has not merged, so no deployment can save yet; guessing its function names
// would sign a transaction against an interface that does not exist. When
// SF-130 merges, build its generated settings call here and enable saving
// only for deployments whose manifest records that type origin.
import type { Transaction } from "@mysten/sui/transactions";
import type { FleetSettings } from "@/lib/fleet-settings";

export interface SettingsSaver {
  build?: (settings: FleetSettings) => Transaction | Promise<Transaction>;
  /** Why saving is unavailable, when it is. */
  reason?: string;
}

export function settingsSaver(): SettingsSaver {
  return { reason: "This deployment's contracts have no on-chain fleet settings yet (SF-130). Prices and the deposit below still follow your choices." };
}
