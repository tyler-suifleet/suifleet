"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { currentHealth, fetchFleetHealth, HEALTH_REFRESH_MS, TELEMETRY_URL, type DeviceHealth } from "@/lib/telemetry";

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

/** The browser clock, re-read every few seconds so stale health expires on screen. */
function useNow(periodMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), periodMs);
    return () => clearInterval(timer);
  }, [periodMs]);
  return now;
}

/**
 * Verified health of each device (SF-019), keyed by device ID. A failed or
 * stalled refresh turns the previous result into `unavailable`
 * (`currentHealth`) instead of leaving it on screen.
 */
export function useFleetHealth(deviceIds: string[]) {
  const client = useCurrentClient();
  const account = useCurrentAccount();
  const token = useTelemetryToken();
  const now = useNow(5_000);
  const query = useQuery<Record<string, DeviceHealth>>({
    queryKey: ["health", NETWORK, TELEMETRY_URL, account?.address, token, ...deviceIds],
    queryFn: () => fetchFleetHealth(client, deviceIds, account!.address, TELEMETRY_URL, token),
    enabled: !!account?.address && deviceIds.length > 0,
    refetchInterval: HEALTH_REFRESH_MS,
  });
  return { data: currentHealth(query.data, query.dataUpdatedAt, query.isError, Math.max(now, query.dataUpdatedAt)), error: query.error };
}
