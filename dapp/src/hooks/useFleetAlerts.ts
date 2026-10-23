"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { NETWORK } from "@/lib/constants";
import { TELEMETRY_URL } from "@/lib/telemetry";
import { ALERTS_REFRESH_MS, currentAlerts, fetchAlerts, type AlertsResult } from "@/lib/alerts";
import { DATA_CONFIG } from "@/lib/fleet-data";
import { fetchChainAlerts } from "@/lib/chain-alerts";
import { useTelemetryToken } from "@/hooks/useFleetHealth";

/** Fleet alerts for the connected owner's devices (SF-020). */
export function useFleetAlerts(): AlertsResult {
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const token = useTelemetryToken();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);
  const query = useQuery<AlertsResult>({
    queryKey: ["alerts", NETWORK, TELEMETRY_URL, account?.address, token],
    queryFn: () => DATA_CONFIG ? fetchChainAlerts(client, account!.address) : fetchAlerts(account!.address, TELEMETRY_URL, token),
    enabled: !!account?.address,
    refetchInterval: ALERTS_REFRESH_MS,
  });
  return currentAlerts(query.data, query.dataUpdatedAt, query.isError, Math.max(now, query.dataUpdatedAt));
}
