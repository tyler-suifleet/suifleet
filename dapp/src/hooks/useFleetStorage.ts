"use client";

import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { formatStorageQueryError } from "@/lib/storage-errors";
import { useState } from "react";
import { fetchRetention, retentionOrigin } from "@/lib/retention";
import { parseToken } from "@/lib/network-costs";
import { useQuery } from "@tanstack/react-query";
import { useFleetSelection } from "./useFleetSelection";
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
  const { config, reason } = storageConfig();
  const storage = useQuery<StorageView | null>({
    queryKey: ["fleet-storage", NETWORK, budget?.id, config?.packageId],
    enabled: !!budget && !!config,
    refetchInterval: 60_000,
    // An absent optional record is successful data; Query rejects undefined.
    queryFn: async () => (await fetchStorage(client, config!, budget!.id)) ?? null,
  });
  const target: FleetStorageTarget | undefined = config && budget && storage.data
    ? { config, storageId: storage.data.id, budgetId: budget.id, capId: budget.capId, managed: selection.fleet ? { customerId: selection.fleet.customerId, fleetId: selection.fleet.id } : undefined } : undefined;
  const why = reason ?? (budgets.error ? formatStorageQueryError(budgets.error, "Fleet budget")
    : storage.error ? formatStorageQueryError(storage.error)
    : !budget ? (budgets.isLoading ? "Loading your fleet budget…" : "Create a fleet budget on the Network budget page first.")
    : !storage.data ? (storage.isLoading ? "Loading fleet storage…" : "Create and fund fleet storage on the Network budget page first.")
    : undefined);
  return { budget, storage: storage.data ?? undefined, config, target, reason: why, refetch: storage.refetch };
}

/** A BlobStore that pays uploads from the fleet's prepaid storage, when the owner has one. */
export function useFleetBlobStore() {
  const client = useCurrentClient();
  const mutation = useFleetTransaction();
  const { target, storage, reason } = useFleetStorage();
  const [renewalTarget, setRenewalTarget] = useState(0), [maxFee, setMaxFee] = useState('');
  const origin = retentionOrigin();
  const policy = useQuery({ queryKey: ['retention-upload', target?.storageId, origin], enabled: !!target && !!origin,
    refetchInterval: 60_000,
    queryFn: () => fetchRetention(client, target!.storageId, origin!, target!.config.storageOrigin ?? target!.config.packageId) });
  const retention = policy.data?.policy ? { target: renewalTarget, maxFee, setTarget: setRenewalTarget, setMaxFee } : undefined;
  if (!target) return { reason, retention };
  if (policy.error) return { reason: formatStorageQueryError(policy.error, "Storage retention"), retention };
  const store: BlobStore = (body, progress) => uploadToFleetStorage(client, { ...target,
    retention: retention ? { target: renewalTarget, maxFee: parseToken(maxFee) } : undefined }, body,
    (transaction) => mutation.mutateAsync({ transaction }), progress);
  return { store, storage, retention };
}
