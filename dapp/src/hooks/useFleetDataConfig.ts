"use client";
import { DATA_CONFIG, configuredDataFleets } from "@/lib/fleet-data";
import { normalizeId } from "@/lib/deployment-manifest";
import { useFleetSelection } from "./useFleetSelection";
const configured = configuredDataFleets();
/** Configuration is only a locator. readFleetData rechecks chain, registry,
 * budget and storage bindings on chain before using any report/archive. */
export function useFleetDataConfig() {
  const selection = useFleetSelection();
  if (!selection.supported) return DATA_CONFIG;
  if (!selection.fleet) return undefined;
  const matches = configured.filter(c => normalizeId(c.budgetId) === normalizeId(selection.fleet!.budgetId));
  if (matches.length > 1) throw Error("Multiple fleet-data configurations for one budget");
  return matches[0];
}
