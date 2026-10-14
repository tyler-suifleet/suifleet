"use client";

import { useMutation } from "@tanstack/react-query";
import { useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import type { Transaction } from "@mysten/sui/transactions";
import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";

export function useFleetTransaction() {
  const client = useCurrentClient();
  const wallet = useDAppKit();
  return useMutation({
    mutationFn: async ({ transaction }: { transaction: Transaction }) => {
      const manifest = requireManifest();
      const account = wallet.stores.$connection.get().account;
      if (!account) throw new Error("Connect a wallet before submitting a transaction");
      if (!account.chains.includes(`sui:${manifest.network}`)) throw new Error(`Wallet account does not support sui:${manifest.network}`);
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network does not match the deployment manifest");
      await verifyDeployment(client);
      // Pass the selected account explicitly; never silently sign with an
      // account selected while the asynchronous network proof was in flight.
      if (wallet.stores.$connection.get().account !== account) throw new Error("Wallet account changed; retry the transaction");
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network changed; retry the transaction");
      const result = await wallet.signAndExecuteTransaction({ transaction, account, network: manifest.network });
      if (result.FailedTransaction || !result.Transaction.status.success) {
        throw new Error(result.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      // Confirm against the verified fleet node, independently of wallet effects.
      const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { effects: true } });
      if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) {
        throw new Error(confirmed.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful");
      }
      return { digest: confirmed.Transaction.digest };
    },
  });
}
