"use client";

import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { fetchGroups, type DeviceGroup } from "@/lib/sui";

export function useGroups() {
  const client = useCurrentClient();
  const account = useCurrentAccount();

  return useQuery<DeviceGroup[]>({
    queryKey: ["groups", NETWORK, account?.address],
    queryFn: () => fetchGroups(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: 15_000,
  });
}
