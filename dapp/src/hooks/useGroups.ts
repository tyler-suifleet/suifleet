"use client";

import { useQuery } from "@tanstack/react-query";
import { useSuiClient, useCurrentAccount } from "@mysten/dapp-kit";
import { fetchGroups, type DeviceGroup } from "@/lib/sui";

export function useGroups() {
  const client = useSuiClient();
  const account = useCurrentAccount();

  return useQuery<DeviceGroup[]>({
    queryKey: ["groups", account?.address],
    queryFn: () => fetchGroups(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: 15_000,
  });
}
