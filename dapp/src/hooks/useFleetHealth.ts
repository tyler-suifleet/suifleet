"use client";

import { useEffect, useState } from "react";
import { useFleetSelection } from "./useFleetSelection";
import { useFleetDataConfig } from "./useFleetDataConfig";
import { NETWORK } from "@/lib/constants";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { currentHealth, fetchFleetHealth, HEALTH_REFRESH_MS, type DeviceHealth } from "@/lib/telemetry";

/** The browser clock, re-read every few seconds so stale health expires on screen. */
export function useNow(periodMs: number): number {
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
  const selection = useFleetSelection(), config = useFleetDataConfig();
  const client = useCurrentClient();
  const account = useCurrentAccount();
  const now = useNow(5_000);
  const scopedIds = selection.supported ? deviceIds.filter(id => selection.fleet?.devices.includes(id)) : deviceIds;
  const query = useQuery<Record<string, DeviceHealth>>({
    queryKey: ["health", NETWORK, account?.address, selection.identity, ...scopedIds],
    queryFn: () => fetchFleetHealth(client, scopedIds, account!.address, undefined, undefined, undefined, selection.supported ? { config } : undefined),
    enabled: !!account?.address && scopedIds.length > 0 && (!selection.supported || !!selection.fleet),
    refetchInterval: HEALTH_REFRESH_MS,
  });
  return { data: currentHealth(query.data, query.dataUpdatedAt, query.isError, Math.max(now, query.dataUpdatedAt)), error: query.error };
}
