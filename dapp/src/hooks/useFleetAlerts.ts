"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient, useCurrentAccount } from "@mysten/dapp-kit-react";
import { NETWORK } from "@/lib/constants";
import { ALERTS_REFRESH_MS, currentAlerts, type AlertsResult } from "@/lib/alerts";
import { fetchChainAlerts } from "@/lib/chain-alerts";

/** Fleet alerts for the connected owner's devices (SF-020). */
export function useFleetAlerts(): AlertsResult {
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);
  const query = useQuery<AlertsResult>({
    queryKey: ["alerts", NETWORK, account?.address],
    queryFn: () => fetchChainAlerts(client, account!.address),
    enabled: !!account?.address,
    refetchInterval: ALERTS_REFRESH_MS,
  });
  return currentAlerts(query.data, query.dataUpdatedAt, query.isError, Math.max(now, query.dataUpdatedAt));
}
