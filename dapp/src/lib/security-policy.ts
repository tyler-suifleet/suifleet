import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { MANIFEST, SECURITY_TYPES, moveType, verifyDeployment } from "./deployment-manifest";
import { delaySupported } from "./security-delay";
import { PACKAGE_ID } from "./constants";
import { security_policy_PolicyBcs, security_policy_PolicyKeyBcs } from "./fleet-protocol";

export const SECURITY_ACTIONS = ["Publish releases", "Deploy and advance rollouts", "Change access policies", "Withdraw activity or storage funds", "Change administrators and security", "Hand over fleets", "Other administration"] as const;
export const SECURITY_FACTORS = [
  { bit: 1, name: "Multisig administrators", description: "Require the configured wallet signing threshold, using independently controlled keys.", ticket: "SF-201" },
  { bit: 2, name: "Passkey co-signature", description: "Require an enrolled passkey as well as wallet authorization.", ticket: "SF-198" },
  { bit: 4, name: "Second-person approval", description: "Require approval from another authorized credential. The proposer cannot approve their own request.", ticket: "SF-199" },
  { bit: 8, name: "Delay and cancellation", description: "Wait through an on-chain cancellation window before executing. A current administrator can cancel.", ticket: "SF-200" },
] as const;
export const APPROVAL_ORIGINS = ["customer::SecondPersonConfiguration"] as const;
export const AVAILABLE_SECURITY_FACTORS = 9 | (MANIFEST && APPROVAL_ORIGINS.every(t => t in MANIFEST!.type_origins) ? 4 : 0);
export function securitySupported() { return !!MANIFEST && SECURITY_TYPES.every(t => t in MANIFEST!.type_origins); }
export interface OrganizationSecurity { generation: bigint; requirements: number[] }
export function validateSecurityRequirements(values: readonly number[], available = AVAILABLE_SECURITY_FACTORS): number[] {
  if (values.length !== SECURITY_ACTIONS.length || values.some(v => !Number.isInteger(v) || v < 0 || v > 15 || (v & available) !== v)) {
    throw Error("The requested protection is unavailable in this deployment");
  }
  return [...values];
}
export async function fetchOrganizationSecurity(client: SuiGrpcClient, customerId: string): Promise<OrganizationSecurity | undefined> {
  await verifyDeployment(client);
  if (!securitySupported()) return undefined;
  // A missing policy on a legacy customer is not an all-off protected policy.
  const { object } = await client.getObject({ objectId: customerId, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Customer"))) throw Error("Unexpected organization type");
  const { customer_CustomerBcs } = await import("./fleet-protocol");
  const customer = customer_CustomerBcs.parse(object.content);
  if (customer.version === "1" || customer.version === "196") return undefined;
  if (!["197", "199", "201"].includes(customer.version)) throw Error("Unsupported organization authority version");
  const field = await client.getDynamicField({ parentId: customerId, name: {
    type: moveType("security_policy::PolicyKey"), bcs: security_policy_PolicyKeyBcs.serialize({ dummy_field: false }).toBytes(),
  } });
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("security_policy::Policy"))) throw Error("Unexpected organization policy type");
  const p = security_policy_PolicyBcs.parse(field.dynamicField.value.bcs);
  if (p.schema !== "1" || p.requirements.length !== SECURITY_ACTIONS.length || p.requirements.some(v => v > 15)) throw Error("Unsupported organization policy");
  return { generation: BigInt(p.generation), requirements: p.requirements };
}
export function buildSecurityPolicyTx(customerId: string, requirements: readonly number[]) {
  if (!securitySupported()) throw Error("Deploy the organization security framework first");
  const values = validateSecurityRequirements(requirements, delaySupported() ? AVAILABLE_SECURITY_FACTORS : 0);
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::set_security`, arguments: [tx.object(customerId), tx.pure.vector("u8", values)] });
  return tx;
}
