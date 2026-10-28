"use client";

import { useFleetSelection } from "./useFleetSelection";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { fetchGroups, type DeviceGroup } from "@/lib/sui";

export function useGroups() {
  const selection = useFleetSelection();
  const client = useCurrentClient();
  const account = useCurrentAccount();

  return useQuery<DeviceGroup[]>({
    queryKey: ["groups", NETWORK, account?.address, selection.identity],
    queryFn: async () => {
      const groups = await fetchGroups(client, selection.supported ? selection.fleet!.id : account!.address);
      return selection.supported ? groups.filter(g => selection.fleet!.groups.includes(g.objectId)) : groups;
    },
    enabled: !!account?.address && (!selection.supported || !!selection.fleet),
    refetchInterval: 60_000,
  });
}
