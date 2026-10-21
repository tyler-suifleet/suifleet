// The delivery choice for one release (SF-022). Confidential is the default;
// public needs an explicit acknowledgement and is never chosen automatically.
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import type { AccessPolicy } from "./access-policy";
import { sealNamespace } from "./access-policy";
import { newIdentity, sealer, SEAL_SETTINGS, type SealSettings } from "./seal";
import type { Protection } from "./walrus";

export interface Delivery {
  mode: "confidential" | "public";
  policyId: string;
  /** The uploader confirmed the content may be published unencrypted. */
  publicAcknowledged: boolean;
}

export const DEFAULT_DELIVERY: Delivery = { mode: "confidential", policyId: "", publicAcknowledged: false };

/**
 * One release's protection: a single fresh identity under the policy's
 * current key epoch, shared by its artifact and recipe so a device fetches
 * one key. Throws instead of degrading to public delivery.
 */
export function releaseProtection(
  client: SuiGrpcClient,
  delivery: Delivery,
  policies: AccessPolicy[],
  settings: SealSettings | null = SEAL_SETTINGS ?? null,
): Protection {
  if (delivery.mode === "public") {
    if (!delivery.publicAcknowledged) throw new Error("Confirm that this content may be stored publicly, or choose confidential delivery");
    return { kind: "public" };
  }
  if (!settings) throw new Error("Confidential delivery needs Seal key servers (NEXT_PUBLIC_SEAL_KEY_SERVERS); nothing was uploaded");
  const policy = policies.find(p => p.objectId === delivery.policyId);
  if (!policy) throw new Error("Choose an access policy you administer for confidential delivery");
  return { kind: "confidential", seal: sealer(client, settings, sealNamespace(), newIdentity(policy.objectId, policy.keyEpoch)) };
}
