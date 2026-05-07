"use client";

import { useQuery } from "@tanstack/react-query";
import { useSuiClient } from "@mysten/dapp-kit";
import { fetchDeploymentEvents, type DeploymentEvent } from "@/lib/sui";

export function useDeployments() {
  const client = useSuiClient();

  return useQuery<DeploymentEvent[]>({
    queryKey: ["deployments"],
    queryFn: () => fetchDeploymentEvents(client),
    refetchInterval: 10_000,
  });
}
