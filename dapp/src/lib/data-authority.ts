import type { NamedFleet } from './customer-fleets';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';

/** Resolve against fresh discovery, never a cached administrator flag. */
export function selectedDataAuthority(fleets: NamedFleet[], selected: NamedFleet | undefined, budgetId: string) {
  const fleet = fleets.find(f => f.id === selected?.id && f.customerId === selected?.customerId && id(f.budgetId) === id(budgetId));
  if (!fleet) throw Error('Fleet access was revoked');
  if (fleet.administrator) return { kind: 'customer' as const, customerId: fleet.customerId, fleetId: fleet.id };
  return { kind: 'reader' as const };
}
