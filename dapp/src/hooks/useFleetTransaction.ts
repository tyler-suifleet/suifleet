"use client";

import { checkDeviceCapacity } from "@/lib/customer-entitlements";
import { getSession, isEnokiWallet } from "@mysten/enoki";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import { useRef } from "react";
import { useFleetSelection } from "./useFleetSelection";
import { fetchCustomerFleets, selectionKey, withManagedBudgetCap } from "@/lib/customer-fleets";
import { fetchManagedBudget } from "@/lib/fleet-budget";
import { verifySupportDiagnostics } from "@/lib/fleet-support";
import type { FleetDataConfig } from "@/lib/fleet-data";
import type { Transaction } from "@mysten/sui/transactions";
import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";

export type TransactionPhase = "validating" | "wallet" | "confirming";

export function useFleetTransaction() {
  const selection = useFleetSelection();
  const cache = useQueryClient();
  const latest = useRef(selection); latest.current = selection;
  const client = useCurrentClient();
  const wallet = useDAppKit();
  return useMutation({
    mutationFn: async ({ transaction, onProgress, onSubmitted, onDefiniteFailure, support }: { transaction: Transaction; onProgress?: (phase: TransactionPhase) => void; onSubmitted?: (digest: string) => void; onDefiniteFailure?: () => void; support?: { config: FleetDataConfig; deviceId: string } }) => {
      const captured = selection;
      onProgress?.("validating");
      const manifest = requireManifest();
      const account = wallet.stores.$connection.get().account;
      if (!account) throw new Error("Connect a wallet before submitting a transaction");
      const address = account.address;
      const chains = JSON.stringify([...new Set(account.chains)].sort());
      if (!account.chains.includes(`sui:${manifest.network}`)) throw new Error(`Wallet account does not support sui:${manifest.network}`);
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network does not match the deployment manifest");
      await verifyDeployment(client);
      if (captured.supported && captured.fleet) {
        const fleets = await fetchCustomerFleets(client, address);
        const current = fleets.find(f => f.id === captured.fleet!.id && f.customerId === captured.fleet!.customerId);
        if (!current) throw Error("Fleet access was revoked");
        if (support) await verifySupportDiagnostics(client, current, support.config, support.deviceId, address, transaction);
        else {
          if (!current.administrator) throw Error("Fleet access was revoked or is read-only");
          const budget = await fetchManagedBudget(client, current.budgetId);
          transaction = withManagedBudgetCap(transaction, current, budget.capId);
        }
      }
      await checkDeviceCapacity(client, transaction);
      const connectedWallet = wallet.stores.$connection.get().wallet;
      if (connectedWallet && isEnokiWallet(connectedWallet)) {
        const session = await getSession(connectedWallet);
        const { response: info } = await client.ledgerService.getServiceInfo({});
        if (!session || !session.jwt || !Number.isFinite(session.expiresAt) || Date.now() >= session.expiresAt ||
            !Number.isSafeInteger(session.maxEpoch) || info.epoch === undefined || info.epoch > BigInt(session.maxEpoch)) {
          throw new Error("Google sign-in session expired or could not be verified. Disconnect and sign in again before retrying.");
        }
      }
      // Pass the selected account explicitly; never silently sign with an
      // account selected while the asynchronous network proof was in flight.
      const currentAccount = wallet.stores.$connection.get().account;
      if (!currentAccount || currentAccount.address !== address || JSON.stringify([...new Set(currentAccount.chains)].sort()) !== chains) throw new Error("Wallet account changed; retry the transaction");
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network changed; retry the transaction");
      if (captured.supported && (latest.current.identity !== captured.identity || (captured.fleet && localStorage.getItem(selectionKey(address)) && localStorage.getItem(selectionKey(address)) !== captured.fleet.id))) throw Error("Fleet selection changed; retry the transaction");
      onProgress?.("wallet");
      const result = await wallet.signAndExecuteTransaction({ transaction, account: currentAccount, network: manifest.network }).catch((error: unknown) => {
        // A transport error can hide an executed transaction. Only explicit
        // wallet rejection permits the caller to retry automatically.
        const rejected = error as { code?: number; name?: string; message?: string } | null;
        if (rejected?.code === 4001 || rejected?.name === "UserRejectedRequestError" || rejected?.message === "User rejected the request") onDefiniteFailure?.();
        throw error;
      });
      if (result.FailedTransaction || !result.Transaction.status.success) {
        onDefiniteFailure?.();
        throw new Error(result.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      // Confirm against the verified fleet node, independently of wallet effects.
      onProgress?.("confirming");
      onSubmitted?.(result.Transaction.digest);
      const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { effects: true, events: true } });
      if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) {
        throw new Error(confirmed.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      void cache.invalidateQueries({ queryKey: ["customer-fleets"] });
      // Events from the verified node's confirmed effects, not the wallet's.
      return { digest: confirmed.Transaction.digest, events: confirmed.Transaction.events ?? [] };
    },
  });
}
