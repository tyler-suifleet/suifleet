"use client";

import { useFleetSelection } from "./useFleetSelection";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { fetchDevices, fetchFleetDevices, type DeviceObject } from "@/lib/sui";

export function useDevices() {
  const selection = useFleetSelection();
  const client = useCurrentClient();
  const account = useCurrentAccount();

  return useQuery<DeviceObject[]>({
    queryKey: ["devices", NETWORK, account?.address, selection.identity],
    queryFn: () => selection.supported ? fetchFleetDevices(client, selection.fleet!.devices) : fetchDevices(client, account!.address),
    enabled: !!account?.address && (!selection.supported || !!selection.fleet),
    refetchInterval: 60_000,
  });
}
