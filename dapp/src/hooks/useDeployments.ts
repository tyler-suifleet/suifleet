"use client";

import { useFleetSelection } from "./useFleetSelection";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { fetchDeploymentEvents, type DeploymentEvent } from "@/lib/sui";

export function useDeployments() {
  const selection = useFleetSelection();
  const client = useCurrentClient();
  const account = useCurrentAccount();

  return useQuery<DeploymentEvent[]>({
    queryKey: ["deployments", NETWORK, account?.address, selection.identity],
    queryFn: () => fetchDeploymentEvents(client, selection.supported ? selection.fleet!.id : account!.address),
    enabled: !!account?.address && (!selection.supported || !!selection.fleet),
    refetchInterval: 60_000,
  });
}
