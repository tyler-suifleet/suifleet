"use client";

import { useQuery } from "@tanstack/react-query";
import { useSuiClient, useCurrentAccount } from "@mysten/dapp-kit";
import { fetchDevices, type DeviceObject } from "@/lib/sui";

export function useDevices() {
  const client = useSuiClient();
  const account = useCurrentAccount();

  return useQuery<DeviceObject[]>({
    queryKey: ["devices", account?.address],
    queryFn: () => fetchDevices(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: 15_000,
  });
}
