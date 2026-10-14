"use client";

import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useSuiClient, useCurrentAccount } from "@mysten/dapp-kit";
import { fetchDeploymentEvents, type DeploymentEvent } from "@/lib/sui";

export function useDeployments() {
  const client = useSuiClient();
  const account = useCurrentAccount();

  return useQuery<DeploymentEvent[]>({
    queryKey: ["deployments", NETWORK, account?.address],
    queryFn: () => fetchDeploymentEvents(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: 10_000,
  });
}
