"use client";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { DATA_CONFIG } from "@/lib/fleet-data";
import { resolveFleetReporting } from "@/lib/fleet-reporting";
import { useFleetSelection } from "./useFleetSelection";

export function useFleetReporting() {
  const selection = useFleetSelection(), client = useCurrentClient();
  return useQuery({ queryKey: ["fleet-reporting", selection.identity], enabled: !!selection.fleet,
    queryFn: () => resolveFleetReporting(client, selection.fleet!), retry: false, refetchInterval: 60_000 });
}
/** Only expose the current selection's freshly verified locator. */
export function useFleetDataConfig() {
  const selection = useFleetSelection(), query = useFleetReporting();
  if (!selection.supported) return DATA_CONFIG;
  return query.isSuccess && !query.isRefetchError ? query.data : undefined;
}
