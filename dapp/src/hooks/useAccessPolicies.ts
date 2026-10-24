"use client";

import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { NETWORK } from "@/lib/constants";
import { fetchAdministeredPolicies, type AccessPolicy } from "@/lib/access-policy";
import { MANIFEST, SEAL_POLICY_TYPES } from "@/lib/deployment-manifest";

/** Whether this deployment's package includes Seal access policies. */
export const POLICIES_ENABLED = !!MANIFEST && SEAL_POLICY_TYPES.every(name => !!MANIFEST?.type_origins[name]);

export function useAccessPolicies() {
  const client = useCurrentClient();
  const account = useCurrentAccount();
  return useQuery<AccessPolicy[]>({
    queryKey: ["access-policies", NETWORK, account?.address],
    queryFn: () => fetchAdministeredPolicies(client, account!.address),
    enabled: !!account?.address && POLICIES_ENABLED,
    refetchInterval: 60_000,
  });
}
