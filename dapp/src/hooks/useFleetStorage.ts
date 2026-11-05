"use client";

import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { formatStorageQueryError } from "@/lib/storage-errors";
import { useState } from "react";
import { fetchRetention, retentionOrigin } from "@/lib/retention";
import { parseToken } from "@/lib/network-costs";
import { useQuery } from "@tanstack/react-query";
import { useFleetSelection } from "./useFleetSelection";
import { resolveFleetStorageRelease } from "@/lib/fleet-reporting";
import { NETWORK } from "@/lib/constants";
import { budgetSupported, fetchBudgets, fetchManagedBudget, fetchStorage, storageConfig, type BudgetView, type StorageView } from "@/lib/fleet-budget";
import { uploadToFleetStorage, type FleetStorageTarget } from "@/lib/fleet-upload";
import type { BlobStore } from "@/lib/walrus";
import { useFleetTransaction } from "./useFleetTransaction";

/** Budgets whose owner cap the connected account holds (shared with the budget page). */
export function useBudgets() {
  const selection = useFleetSelection();
  const account = useCurrentAccount(), client = useCurrentClient();
  return useQuery<BudgetView[]>({
    queryKey: ["fleet-budgets", NETWORK, account?.address, selection.identity],
    enabled: !!account && budgetSupported() && (!selection.supported || !!selection.fleet),
    refetchInterval: 60_000,
    queryFn: async () => selection.supported ? [await fetchManagedBudget(client, selection.fleet!.budgetId)] : fetchBudgets(client, account!.address),
  });
}

/** The connected owner's budget, its fleet storage if any, and why fleet storage is unavailable otherwise. */
export function useFleetStorage() {
  const selection = useFleetSelection();
  const client = useCurrentClient();
  const budgets = useBudgets();
  const budget = budgets.data?.[0];
  const storage = useQuery({
    queryKey: ["fleet-storage", NETWORK, budget?.id, selection.identity],
    enabled: !!budget,
    refetchInterval: 60_000,
    // An absent optional record is successful data; Query rejects undefined.
    queryFn: async () => {
      const resolved = selection.fleet ? await resolveFleetStorageRelease(client, selection.fleet) : { ...storageConfig(), storageId: undefined };
      const value = resolved.config ? (await fetchStorage(client, resolved.config, budget!.id)) ?? null : null;
      if (resolved.storageId && value?.id !== resolved.storageId) throw Error("Reporting storage discovery does not match its verified locator");
      return { ...resolved, value };
    },
  });
  const config = storage.isSuccess && !storage.isRefetchError ? storage.data.config : undefined;
  const value = config ? storage.data?.value : undefined;
  const reason = storage.data?.reason;
  const target: FleetStorageTarget | undefined = config && budget && value
    ? { config, storageId: value.id, budgetId: budget.id, capId: budget.capId, managed: selection.fleet ? { customerId: selection.fleet.customerId, fleetId: selection.fleet.id } : undefined } : undefined;
  const why = reason ?? (budgets.error ? formatStorageQueryError(budgets.error, "Fleet budget")
    : storage.error ? formatStorageQueryError(storage.error)
    : !budget ? (budgets.isLoading ? "Loading your fleet budget…" : "Create a fleet budget on the Network budget page first.")
    : !value ? (storage.isLoading ? "Loading fleet storage…" : "Create and fund fleet storage on the Network budget page first.")
    : undefined);
  return { budget, loading: budgets.isLoading || storage.isLoading, budgetError: budgets.error, storage: value ?? undefined, config, target, reason: why, refetch: storage.refetch };
}

/** A BlobStore that pays uploads from the fleet's prepaid storage, when the owner has one. */
export function useFleetBlobStore() {
  const client = useCurrentClient();
  const mutation = useFleetTransaction();
  const { target, storage, reason } = useFleetStorage();
  const [renewalTarget, setRenewalTarget] = useState(0), [maxFee, setMaxFee] = useState('');
  const origin = retentionOrigin(target?.config);
  const policy = useQuery({ queryKey: ['retention-upload', target?.storageId, origin], enabled: !!target && !!origin,
    refetchInterval: 60_000,
    queryFn: () => fetchRetention(client, target!.storageId, origin!, target!.config.storageOrigin ?? target!.config.packageId) });
  const retention = policy.data?.policy ? { target: renewalTarget, maxFee, setTarget: setRenewalTarget, setMaxFee } : undefined;
  if (!target) return { reason, retention, configured: false };
  if (origin && policy.isLoading) return { reason: "Loading retention authorization…", retention, configured: true };
  if (policy.error) return { reason: formatStorageQueryError(policy.error, "Storage retention"), retention, configured: true };
  const store: BlobStore = (body, progress) => uploadToFleetStorage(client, { ...target,
    retention: retention ? { target: renewalTarget, maxFee: parseToken(maxFee) } : undefined }, body,
    (transaction) => mutation.mutateAsync({ transaction }), progress);
  return { store, storage, retention, target, configured: true };
}
