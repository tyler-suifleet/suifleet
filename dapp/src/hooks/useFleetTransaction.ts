"use client";

import { getSession, isEnokiWallet } from "@mysten/enoki";
import { useMutation } from "@tanstack/react-query";
import { useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import type { Transaction } from "@mysten/sui/transactions";
import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";

export type TransactionPhase = "validating" | "wallet" | "confirming";

export function useFleetTransaction() {
  const client = useCurrentClient();
  const wallet = useDAppKit();
  return useMutation({
    mutationFn: async ({ transaction, onProgress }: { transaction: Transaction; onProgress?: (phase: TransactionPhase) => void }) => {
      onProgress?.("validating");
      const manifest = requireManifest();
      const account = wallet.stores.$connection.get().account;
      if (!account) throw new Error("Connect a wallet before submitting a transaction");
      const address = account.address;
      const chains = JSON.stringify([...new Set(account.chains)].sort());
      if (!account.chains.includes(`sui:${manifest.network}`)) throw new Error(`Wallet account does not support sui:${manifest.network}`);
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network does not match the deployment manifest");
      await verifyDeployment(client);
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
      onProgress?.("wallet");
      const result = await wallet.signAndExecuteTransaction({ transaction, account: currentAccount, network: manifest.network });
      if (result.FailedTransaction || !result.Transaction.status.success) {
        throw new Error(result.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      // Confirm against the verified fleet node, independently of wallet effects.
      onProgress?.("confirming");
      const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { effects: true } });
      if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) {
        throw new Error(confirmed.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      return { digest: confirmed.Transaction.digest };
    },
  });
}
