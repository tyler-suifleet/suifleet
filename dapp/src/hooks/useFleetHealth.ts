"use client";

import { useSyncExternalStore } from "react";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { fetchFleetHealth, TELEMETRY_URL, type DeviceHealth } from "@/lib/telemetry";

// The indexer read token stays in this browser tab only (never in the build).
const TOKEN_KEY = "suifleet.telemetryToken";
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function setTelemetryToken(token: string) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  listeners.forEach((listener) => listener());
}

export function useTelemetryToken(): string {
  return useSyncExternalStore(subscribe, () => sessionStorage.getItem(TOKEN_KEY) ?? "", () => "");
}

/** Verified health of each device (SF-019), keyed by device ID. */
export function useFleetHealth(deviceIds: string[]) {
  const client = useCurrentClient();
  const account = useCurrentAccount();
  const token = useTelemetryToken();
  return useQuery<Record<string, DeviceHealth>>({
    queryKey: ["health", NETWORK, TELEMETRY_URL, account?.address, token, ...deviceIds],
    queryFn: () => fetchFleetHealth(client, deviceIds, account!.address, TELEMETRY_URL, token),
    enabled: !!account?.address && deviceIds.length > 0,
    refetchInterval: 15_000,
  });
}
