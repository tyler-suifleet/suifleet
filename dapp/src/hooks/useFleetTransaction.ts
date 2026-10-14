"use client";

import { useMutation } from "@tanstack/react-query";
import { useSuiClient, useSignAndExecuteTransaction } from "@mysten/dapp-kit";
import type { Transaction } from "@mysten/sui/transactions";

import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";

// A wallet returning a digest does not mean the Move transaction succeeded.
export function useFleetTransaction() {
  const client = useSuiClient();
  const wallet = useSignAndExecuteTransaction();
  return useMutation({
    mutationFn: async ({ transaction }: { transaction: Transaction }) => {
      const manifest = requireManifest();
      await verifyDeployment(client);
      const result = await wallet.mutateAsync({ transaction, chain: `sui:${manifest.network}` });
      const confirmed = await client.waitForTransaction({ digest: result.digest, options: { showEffects: true } });
      if (confirmed.effects?.status.status !== "success") {
        throw new Error(confirmed.effects?.status.error ?? "Transaction execution was not successful");
      }
      return result;
    },
  });
}
