"use client";

import { useQuery } from "@tanstack/react-query";
import { probeDeploymentStorage, type DeploymentStorage } from "@/lib/walrus";

/** Whether a deployment's recipe and artifact can still be downloaded by devices. */
export function useDeploymentStorage(recipeBlobId: string) {
  return useQuery<DeploymentStorage>({
    queryKey: ["deployment-storage", recipeBlobId],
    queryFn: () => probeDeploymentStorage(recipeBlobId),
    enabled: !!recipeBlobId,
    staleTime: 5 * 60_000,
    refetchInterval: 15 * 60_000,
  });
}
