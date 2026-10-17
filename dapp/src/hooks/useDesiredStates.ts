"use client";

import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { fetchDesiredStates, type DesiredState } from "@/lib/desired-state";

/** Current desired state of each group (SF-013), keyed by group ID. */
export function useDesiredStates(groupIds: string[]) {
  const client = useCurrentClient();
  return useQuery<Record<string, DesiredState>>({
    queryKey: ["desired", NETWORK, ...groupIds],
    queryFn: () => fetchDesiredStates(client, groupIds),
    enabled: groupIds.length > 0,
    refetchInterval: 15_000,
  });
}
