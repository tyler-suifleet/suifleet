import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress, normalizeTypeTag } from "@mysten/sui/utils";
import { APPROVAL_TYPES } from "./protocol-version";
import { MANIFEST, moveType, verifyDeployment } from "./deployment-manifest";
import { PACKAGE_ID } from "./constants";
import { customer_CustomerBcs, security_policy_ActionBcs, security_policy_FactorConfigurationBcs } from "./fleet-protocol";
import { buildSecurityPolicyTx, fetchOrganizationSecurity, validateSecurityRequirements } from "./security-policy";

const vectors = bcs.vector(bcs.vector(bcs.u8()));
export const secondPersonSupported = () => !!MANIFEST && APPROVAL_TYPES.every(t => t in MANIFEST!.type_origins);
function txCall(name: string, args: (tx: Transaction) => Parameters<Transaction["moveCall"]>[0]["arguments"]) {
  if (!secondPersonSupported()) throw Error("This deployment has no second-person verifier");
  const tx = new Transaction(); tx.moveCall({ target: `${PACKAGE_ID}::${name}`, arguments: args(tx) }); return tx;
}
export function configureTx(customer: string, consent: string) {
  return txCall("customer::configure_second_person", tx => [tx.object(customer), tx.object(consent), tx.object("0x6")]);
}
export function decisionTx(customer: string, action: string, decision: "approve" | "reject" | "cancel") {
  return txCall(decision === "cancel" ? "customer::cancel_disclosed_security_action" : `customer::${decision}_action`, tx => [tx.object(customer), tx.object(action), ...(decision === "approve" ? [tx.object("0x6")] : [])]);
}
export interface ApprovalRequest { id: string; proposer: string; expires: bigint; generation: bigint; approved: boolean; arguments: number[][]; operation: string }
export function decodeRequest(id: string, customer: string, content: Uint8Array, bytes: Uint8Array): ApprovalRequest {
  const a = security_policy_ActionBcs.parse(content);
  if (normalizeSuiAddress(a.customer_id) !== normalizeSuiAddress(customer)) throw Error("Proposal belongs to another organization");
  const args = vectors.parse(bytes);
  if (!args.length || bytes.length > 16384) throw Error("Unsupported proposal arguments");
  // BCS parsing rejects trailing or malformed operation names. All bytes remain
  // visible; the operation label alone never authorizes execution.
  const operation = bcs.string().parse(Uint8Array.from(args[0]));
  return { id, proposer: a.proposer, expires: BigInt(a.expires_ms), generation: BigInt(a.generation), approved: (a.attested & 4) !== 0, arguments: args, operation };
}
async function typedObject(client: SuiGrpcClient, id: string, type: Parameters<typeof moveType>[0]) {
  const { object } = await client.getObject({ objectId: id, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType(type))) throw Error(`Unexpected ${type} object`);
  return object.content;
}
export interface UnavailableApprovalRequest { id: string; error: string }
export async function fetchRequests(client: SuiGrpcClient, customer: string) {
  await verifyDeployment(client);
  if (!secondPersonSupported()) throw Error("This deployment has no second-person verifier");
  // This public getter returns an empty vector for old all-off organizations;
  // avoid treating arbitrary RPC errors as an empty pending queue.
  const key = moveType("security_policy::FactorKey");
  let cursor: string | null | undefined;
  const seen = new Set<string>();
  let exists = false;
  for (;;) {
    const fields = await client.listDynamicFields({ parentId: customer, cursor, limit: 100 });
    if (fields.dynamicFields.some(f => normalizeStructTag(f.name.type) === normalizeStructTag(key) && f.name.bcs[0] === 0)) { exists = true; break; }
    if (!fields.hasNextPage) break;
    if (!fields.cursor || seen.has(fields.cursor)) throw Error("Pending index pagination made no progress");
    seen.add(fields.cursor); cursor = fields.cursor;
    if (seen.size >= 200) throw Error("Pending index lookup is incomplete");
  }
  if (!exists) return [];
  const { dynamicField } = await client.getDynamicField({ parentId: customer, name: { type: key, bcs: bcs.u8().serialize(0).toBytes() } });
  const indexType = normalizeTypeTag(dynamicField.value.type).replace(/0x[0-9a-fA-F]+(?=::)/g, address => normalizeSuiAddress(address));
  if (indexType !== `vector<${normalizeStructTag("0x2::object::ID")}>`) throw Error("Unexpected pending index type");
  const ids = bcs.vector(bcs.Address).parse(dynamicField.value.bcs);
  return Promise.all(ids.map(async id => {
    try {
      const content = await typedObject(client, id, "security_policy::Action");
      const { dynamicField: disclosed } = await client.getDynamicField({ parentId: id, name: { type: moveType("security_policy::FactorKey"), bcs: bcs.u8().serialize(0).toBytes() } });
      if (normalizeStructTag(disclosed.value.type) !== normalizeStructTag(moveType("security_policy::FactorConfiguration"))) throw Error("Unexpected proposal arguments type");
      return decodeRequest(id, customer, content, Uint8Array.from(security_policy_FactorConfigurationBcs.parse(disclosed.value.bcs).binding));
    } catch (error) {
      return { id, error: error instanceof Error ? error.message : "Request could not be read" } satisfies UnavailableApprovalRequest;
    }
  }));
}
export async function proposeEnrollmentTx(client: SuiGrpcClient, customer: string, expires: bigint) {
  await verifyDeployment(client);
  const content = await typedObject(client, customer, "customer::Customer");
  const bytes = vectors.serialize([Array.from(bcs.string().serialize("configure_second_person").toBytes()), Array.from(bcs.Address.serialize(customer).toBytes())]).toBytes();
  return txCall("customer::propose_approval", tx => [tx.object(customer), tx.pure.vector("u8", bytes), tx.pure.u64(expires), tx.object("0x6")]);
}
export async function proposePolicyTx(client: SuiGrpcClient, customer: string, requirements: number[], expires: bigint) {
  await verifyDeployment(client);
  const values = validateSecurityRequirements(requirements);
  const content = await typedObject(client, customer, "customer::Customer");
  const bytes = vectors.serialize([Array.from(bcs.string().serialize("set_security").toBytes()), Array.from(bcs.Address.serialize(customer).toBytes()), Array.from(bcs.vector(bcs.u8()).serialize(values).toBytes())]).toBytes();
  return txCall("customer::propose_approval", tx => [tx.object(customer), tx.pure.vector("u8", bytes), tx.pure.u64(expires), tx.object("0x6")]);
}
export function policyValues(request: ApprovalRequest): number[] | undefined {
  if (request.operation !== "set_security" || request.arguments.length !== 3) return undefined;
  bcs.Address.parse(Uint8Array.from(request.arguments[1]));
  return validateSecurityRequirements(bcs.vector(bcs.u8()).parse(Uint8Array.from(request.arguments[2])));
}
export async function executePolicyTx(client: SuiGrpcClient, customer: string, request: ApprovalRequest) {
  await verifyDeployment(client);
  const policy = await fetchOrganizationSecurity(client, customer);
  if (!policy || request.generation !== policy.generation || !request.approved) throw Error("Approval is missing or stale");
  const enrollment = request.operation === "configure_second_person" && request.arguments.length === 2;
  const delay = request.operation === "configure_security_delays" && request.arguments.length === 3;
  const values = delay ? bcs.vector(bcs.u64()).parse(Uint8Array.from(request.arguments[2])) : enrollment ? undefined : policyValues(request);
  if (!enrollment && !values) throw Error("Use the exact secured operation builder for this proposal");
  await typedObject(client, customer, "customer::Customer");
  const current = bcs.Address.serialize(customer).toBytes();
  if (current.length !== request.arguments[1].length || current.some((b, i) => b !== request.arguments[1][i])) throw Error("Organization changed; propose again");
  if (enrollment) return configureTx(customer, request.id);
  return txCall(delay ? "customer::configure_security_delays_secured" : "customer::set_security_secured", tx => [tx.object(customer), delay ? tx.pure(bcs.vector(bcs.u64()).serialize(values!).toBytes()) : tx.pure.vector("u8", values!.map(Number)), tx.object(request.id), tx.object("0x6")]);
}

// Each factor editor owns only its bit; retain every other current requirement.
export function approvalRequirements(current: number[], selected: number[]) {
  const values = validateSecurityRequirements(current);
  validateSecurityRequirements(selected);
  return values.map((v, i) => (v & ~4) | (selected[i] & 4));
}
export async function policyChangeTx(client: SuiGrpcClient, customer: string, requirements: number[]) {
  const policy = await fetchOrganizationSecurity(client, customer);
  if (!policy) throw Error("Migrate the organization first");
  const values = validateSecurityRequirements(requirements);
  // Match security_policy::update: action 4 plus every weakened action
  // must satisfy its current factors, including independent approval and cancellation delay.
  const needsQueue = policy.requirements.some((current, i) =>
    (current & (4 | 8)) !== 0 && (i === 4 || (current & values[i]) !== current));
  return needsQueue
    ? proposePolicyTx(client, customer, values, BigInt(Date.now() + 604800000))
    : buildSecurityPolicyTx(customer, values);
}
