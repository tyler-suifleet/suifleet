"use client";

import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { fetchDevices, type DeviceObject } from "@/lib/sui";

export function useDevices() {
  const client = useCurrentClient();
  const account = useCurrentAccount();

  return useQuery<DeviceObject[]>({
    queryKey: ["devices", NETWORK, account?.address],
    queryFn: () => fetchDevices(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: 15_000,
  });
}
