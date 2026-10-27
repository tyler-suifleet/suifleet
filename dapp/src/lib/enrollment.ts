import type { NamedFleet } from "./customer-fleets";
import { customer_FleetBcs } from "./fleet-protocol";
// Address-free bulk enrollment (SF-100) on the SF-021 claim foundation.
//
// An operator reserves aliases and serial numbers, generates one single-use
// claim key per reservation and registers the claim addresses in an
// EnrollmentPolicy. Each device then enrolls itself (`suifleet --enroll`):
// its own key sends, proving possession, and the claim key sponsors. Only an
// on-chain DeviceEnrolled event for the reservation's claim, whose device the
// connected operator holds the owner cap for, binds the alias, serial and
// group to a device. A name alone never creates or authorizes an identity.
//
// Jobs contain public data only (claim addresses, names, digests). Claim keys
// exist in memory just long enough to be written into the downloaded
// credential archive; they are never stored, logged or put in a URL.
import { Transaction } from "@mysten/sui/transactions";
import { Ed25519Keypair } from "@mysten/sui/keypairs/ed25519";
import { decodeSuiPrivateKey } from "@mysten/sui/cryptography";
import { bcs } from "@mysten/sui/bcs";
import { normalizeStructTag, toHex } from "@mysten/sui/utils";
import { ObjectError } from "@mysten/sui/client";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { calls, enrollment_DeviceEnrolledBcs, enrollment_EnrollmentPolicyBcs, enrollment_PolicyCreatedBcs } from "./fleet-protocol";
import { moveType, normalizeId, requireManifest, verifyDeployment } from "./deployment-manifest";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { ALIAS_META_KEY, SERIAL_META_KEY, allEvents, fetchDevices, fetchFleetDevices, fetchGroups, objectsInBatches, type DeviceGroup, type DeviceObject } from "./sui";

// ── Limits ────────────────────────────────────────────────────────────────────

/**
 * Entries per transaction. Sui caps a PTB at 1,024 commands, 512 arguments per
 * command, 16 KiB per pure argument and 128 KiB in total. A claim chunk is one
 * `add_claims` (250 addresses = 8 KiB), one split and 250 transfers; a bind
 * chunk is at most 5 commands per device (borrow, 2× set_meta, add_device,
 * return). These are conservative; measured fleet-scale limits are SF-090.
 */
export const LIMITS = { claimsPerTx: 250, bindsPerTx: 100, revokesPerTx: 250, transfersPerTx: 250 } as const;
/** Default enrollment gas each claim sponsors (the daemon caps one enrollment at 0.05 SUI). */
export const DEFAULT_CLAIM_FUND_MIST = BigInt(20_000_000);
/** Larger batches: generate credentials offline with scripts/claims.py and import its roster. */
export const BROWSER_CREDENTIAL_LIMIT = 5_000;
export const MAX_JOB_ENTRIES = 100_000;
// Safe in TOML strings, file names and FAT boot media; case-insensitively unique.
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

// ── Rosters ───────────────────────────────────────────────────────────────────

export interface RosterRow { line: number; serial: string; alias: string; group?: string }
export interface RosterIssue { line: number; serial?: string; message: string }

/** Minimal RFC 4180 CSV: quoted fields, doubled quotes, CRLF. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"' && field === "") quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (quoted) throw new Error("CSV ends inside a quoted field");
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((value) => value.trim() !== ""));
}

/**
 * `{n}` / `{n:4}`: the row's position (from `start`), zero-padded to a width;
 * `{serial}`: its serial number. Example: `pi-{n:5}` → pi-00001.
 */
export function expandTemplate(template: string, index: number, serial: string): string {
  return template.replace(/\{(n(?::(\d{1,2}))?|serial)\}/g, (_, token: string, width?: string) =>
    token === "serial" ? serial : String(index).padStart(Number(width ?? 0), "0"));
}

/**
 * Rows from CSV with a header naming `serial` and optionally `alias` (or
 * `name`) and `group`. A blank alias is filled from the naming template.
 */
export function parseRoster(text: string, template = "", start = 1): { rows: RosterRow[]; issues: RosterIssue[] } {
  const [header, ...body] = parseCsv(text);
  if (!header) return { rows: [], issues: [{ line: 1, message: "empty roster" }] };
  const columns = header.map((h) => h.trim().toLowerCase());
  const at = (name: string) => columns.indexOf(name);
  const serialAt = at("serial"), aliasAt = at("alias") >= 0 ? at("alias") : at("name"), groupAt = at("group");
  if (serialAt < 0) return { rows: [], issues: [{ line: 1, message: "the header must name a serial column" }] };
  if (body.length > MAX_JOB_ENTRIES) return { rows: [], issues: [{ line: 1, message: `at most ${MAX_JOB_ENTRIES} rows per roster` }] };
  const rows: RosterRow[] = [], issues: RosterIssue[] = [];
  body.forEach((cells, i) => {
    const line = i + 2, serial = (cells[serialAt] ?? "").trim();
    let alias = aliasAt >= 0 ? (cells[aliasAt] ?? "").trim() : "";
    if (!alias && template) alias = expandTemplate(template, start + i, serial);
    if (!alias) return issues.push({ line, serial, message: "no alias and no naming template" });
    const group = groupAt >= 0 ? (cells[groupAt] ?? "").trim() || undefined : undefined;
    rows.push({ line, serial, alias, group });
  });
  return { rows, issues };
}

/** A row's group: an object ID or the name of one of the operator's groups. */
function resolveGroup(value: string | undefined, groups: DeviceGroup[]): { id?: string; error?: string } {
  if (!value) return {};
  const byId = /^0x[0-9a-fA-F]{1,64}$/.test(value) ? groups.find((g) => g.objectId === normalizeId(value)) : undefined;
  const byName = groups.filter((g) => g.name === value);
  if (byId) return { id: byId.objectId };
  if (byName.length === 1) return { id: byName[0].objectId };
  return { error: byName.length ? `group name "${value}" is ambiguous; use its object ID` : `group "${value}" is not one of your groups` };
}

export interface ReservationCheck {
  /** Accepted rows, each with the `line` of the input row it came from. */
  accepted: { line: number; serial: string; alias: string; groupId?: string }[];
  issues: RosterIssue[];
}

/**
 * Reject malformed, duplicate and conflicting reservations. Aliases and
 * serials are unique (case-insensitively) across the roster, the operator's
 * devices and every reservation already in a job.
 */
export function checkReservations(rows: RosterRow[], context: {
  devices: DeviceObject[]; groups: DeviceGroup[]; reserved: { serial: string; alias: string }[]; defaultGroupId?: string;
}): ReservationCheck {
  const key = (value: string) => value.toLowerCase();
  const takenAlias = new Map<string, string>(), takenSerial = new Map<string, string>();
  for (const d of context.devices) {
    takenAlias.set(key(d.name), `device ${d.name}`);
    if (d.serial) takenSerial.set(key(d.serial), `device ${d.name}`);
  }
  for (const r of context.reserved) {
    takenAlias.set(key(r.alias), `reservation ${r.serial}`);
    takenSerial.set(key(r.serial), `reservation ${r.alias}`);
  }
  const accepted: ReservationCheck["accepted"] = [], issues: RosterIssue[] = [];
  for (const row of rows) {
    const problem = (message: string) => issues.push({ line: row.line, serial: row.serial, message });
    if (!NAME.test(row.serial)) { problem("serial must be 1-64 letters, digits, '.', '_' or '-', starting alphanumeric"); continue; }
    if (!NAME.test(row.alias)) { problem(`alias "${row.alias}" must be 1-64 letters, digits, '.', '_' or '-', starting alphanumeric`); continue; }
    const serialClash = takenSerial.get(key(row.serial)), aliasClash = takenAlias.get(key(row.alias));
    if (serialClash) { problem(`serial ${row.serial} is already used by ${serialClash}`); continue; }
    if (aliasClash) { problem(`alias ${row.alias} is already used by ${aliasClash}`); continue; }
    const group = row.group ? resolveGroup(row.group, context.groups) : { id: context.defaultGroupId };
    if (group.error) { problem(group.error); continue; }
    takenSerial.set(key(row.serial), `row ${row.line}`);
    takenAlias.set(key(row.alias), `row ${row.line}`);
    accepted.push({ line: row.line, serial: row.serial, alias: row.alias, groupId: group.id });
  }
  return { accepted, issues };
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

export interface JobEntry {
  serial: string;
  alias: string;
  groupId?: string;
  /** An imported roster's unresolved group name or ID; resolved on import. */
  group?: string;
  /** Address of this reservation's single-use claim key. */
  claim: string;
  revoked?: boolean;
  /**
   * Set before its registration is first submitted. A claim later missing from
   * the policy (revoked elsewhere, or an uncertain submission) is then never
   * re-registered without an explicit operator decision.
   */
  submitted?: boolean;
  /** Wallet returned successful effects; node confirmation may still be pending. */
  registrationDigest?: string;
  /** Persisted only after a chain snapshot actually contained this claim. */
  observedRegistered?: boolean;
  /** Policy version at the last positive observation; older reads cannot prove withdrawal. */
  observedPolicyVersion?: string;
  /** Last failure affecting this entry; cleared by the next success. */
  error?: string;
}

export interface JobEvent { at: number; action: string; count: number; digest?: string; error?: string }

export interface EnrollmentJob {
  schema: 1;
  network: string;
  registry: string;
  operator: string;
  policyId: string;
  /** Enrollment gas each claim sponsors, in MIST (decimal string). */
  claimFundMist: string;
  entries: JobEntry[];
  log: JobEvent[];
}

export function newJob(operator: string, policyId: string, claimFundMist = DEFAULT_CLAIM_FUND_MIST): EnrollmentJob {
  const m = requireManifest();
  return { schema: 1, network: m.network, registry: normalizeId(REGISTRY_ID), operator: normalizeId(operator),
    policyId: normalizeId(policyId), claimFundMist: claimFundMist.toString(), entries: [], log: [] };
}

/**
 * Parse an exported job or a scripts/claims.py roster. Only known public
 * fields are kept: anything resembling key material is refused outright.
 */
export function parseJob(text: string, expected: { operator: string; policyId: string }): EnrollmentJob {
  if (/suiprivkey|"seed|"secret|"private/i.test(text)) throw new Error("This file contains key material; import the public roster/job file only");
  const raw = JSON.parse(text);
  const m = requireManifest();
  if (raw?.schema !== 1 || !Array.isArray(raw.entries)) throw new Error("Unsupported enrollment job or roster file");
  if (raw.network !== undefined && raw.network !== m.network) throw new Error(`File is for ${raw.network}, not ${m.network}`);
  if (raw.registry !== undefined && normalizeId(raw.registry) !== normalizeId(REGISTRY_ID)) throw new Error("File is for another fleet registry");
  if (raw.operator !== undefined && normalizeId(raw.operator) !== normalizeId(expected.operator)) throw new Error("File belongs to another operator");
  if (raw.policyId !== undefined && normalizeId(raw.policyId) !== normalizeId(expected.policyId)) throw new Error("File belongs to another enrollment policy");
  if (raw.entries.length > MAX_JOB_ENTRIES) throw new Error(`At most ${MAX_JOB_ENTRIES} entries per job`);
  const job = newJob(expected.operator, expected.policyId, BigInt(raw.claimFundMist ?? DEFAULT_CLAIM_FUND_MIST));
  const claims = new Set<string>();
  for (const e of raw.entries) {
    if (typeof e?.serial !== "string" || typeof e?.alias !== "string" || !NAME.test(e.serial) || !NAME.test(e.alias)) throw new Error("Invalid entry serial/alias");
    const claim = normalizeId(e.claim);
    if (claims.has(claim)) throw new Error(`Duplicate claim ${claim}`);
    claims.add(claim);
    const entry: JobEntry = { serial: e.serial, alias: e.alias, claim, groupId: e.groupId ? normalizeId(e.groupId) : undefined,
      group: typeof e.group === "string" && e.group ? e.group : undefined,
      revoked: e.revoked === true || undefined, submitted: e.submitted === true || undefined,
      registrationDigest: typeof e.registrationDigest === "string" ? e.registrationDigest : undefined,
      observedPolicyVersion: typeof e.observedPolicyVersion === "string" && /^[1-9][0-9]*$/.test(e.observedPolicyVersion) ? e.observedPolicyVersion : undefined,
      observedRegistered: e.observedRegistered === true || undefined, error: typeof e.error === "string" ? e.error : undefined };
    job.entries.push(Object.fromEntries(Object.entries(entry).filter(([, value]) => value !== undefined)) as JobEntry);
  }
  if (Array.isArray(raw.log)) job.log = raw.log.filter((l: JobEvent) => typeof l?.action === "string").slice(-200);
  return job;
}

const storageKey = (job: Pick<EnrollmentJob, "network" | "policyId">) => `suifleet.enrollment.${job.network}.${job.policyId}`;
export function saveJob(storage: Storage, job: EnrollmentJob) { storage.setItem(storageKey(job), JSON.stringify(job)); }
export function loadJob(storage: Storage, operator: string, policyId: string): EnrollmentJob | undefined {
  const text = storage.getItem(storageKey({ network: requireManifest().network, policyId: normalizeId(policyId) }));
  return text ? parseJob(text, { operator, policyId }) : undefined;
}
/** Reservations in every stored job of this network, for conflict checks. */
export function storedReservations(storage: Storage, operator?: string): { serial: string; alias: string }[] {
  const prefix = `suifleet.enrollment.${requireManifest().network}.`;
  const out: { serial: string; alias: string }[] = [];
  for (let i = 0; i < storage.length; i++) {
    const name = storage.key(i);
    if (!name?.startsWith(prefix)) continue;
    try {
      const job = JSON.parse(storage.getItem(name) ?? "{}");
      if (operator && job.operator !== normalizeId(operator)) continue;
      for (const e of job.entries ?? []) if (!e.revoked) out.push({ serial: e.serial, alias: e.alias });
    }
    catch { /* an unreadable job is reported when opened */ }
  }
  return out;
}

// ── Claim credentials ─────────────────────────────────────────────────────────

export interface ClaimCredential { serial: string; alias: string; claim: string; seedHex: string }

/** One fresh claim key per accepted reservation. Keys stay in memory only. */
export function generateCredentials(reservations: ReservationCheck["accepted"]): { entries: JobEntry[]; credentials: ClaimCredential[] } {
  if (reservations.length > BROWSER_CREDENTIAL_LIMIT) throw new Error(`Generate at most ${BROWSER_CREDENTIAL_LIMIT} credentials in the browser; use scripts/claims.py for larger batches`);
  const entries: JobEntry[] = [], credentials: ClaimCredential[] = [];
  for (const r of reservations) {
    const key = Ed25519Keypair.generate();
    const claim = normalizeId(key.toSuiAddress());
    credentials.push({ serial: r.serial, alias: r.alias, claim, seedHex: toHex(decodeSuiPrivateKey(key.getSecretKey()).secretKey) });
    entries.push({ serial: r.serial, alias: r.alias, groupId: r.groupId, claim });
  }
  return { entries, credentials };
}

/** `enroll.toml` for `suifleet --enroll` on the device's provisioning media. */
export function enrollToml(c: Pick<ClaimCredential, "serial" | "alias">, policyId: string, keypairPath = "/etc/suifleet/device.key", fleet?: NamedFleet): string {
  return [
    `# SuiFleet enrollment for ${c.alias} (serial ${c.serial}).`,
    "# claim.key is a single-use enrollment credential: keep this media private",
    "# and erase it after enrollment. The device generates its own identity key.",
    `manifest_path = "deployment.json"`,
    `policy_id = "${normalizeId(policyId)}"`,
    `claim_key_path = "claim.key"`,
    `name = "${c.alias}"`,
    `keypair_path = "${keypairPath.replace(/["\\]/g, "")}"`,
    ...(fleet ? ["[managed]", `customer_id = "${normalizeId(fleet.customerId)}"`, `fleet_id = "${normalizeId(fleet.id)}"`, `budget_id = "${normalizeId(fleet.budgetId)}"`] : []),
    "",
  ].join("\n");
}

/**
 * A POSIX ustar archive: `enrollment/<serial>/{enroll.toml,claim.key,deployment.json}`
 * per device, with private (0600/0700) modes.
 */
export function credentialArchive(credentials: ClaimCredential[], policyId: string, manifestJson: string, keypairPath?: string, fleet?: NamedFleet): Uint8Array {
  const encoder = new TextEncoder(), blocks: Uint8Array[] = [];
  const mtime = Math.floor(Date.now() / 1000);
  const octal = (value: number, width: number) => value.toString(8).padStart(width - 1, "0") + "\0";
  function entry(name: string, body: Uint8Array | null, mode: number) {
    const header = new Uint8Array(512);
    const put = (text: string, offset: number) => header.set(encoder.encode(text), offset);
    if (encoder.encode(name).length > 99) throw new Error(`archive path too long: ${name}`);
    put(name, 0); put(octal(mode, 8), 100); put(octal(0, 8), 108); put(octal(0, 8), 116);
    put(octal(body?.length ?? 0, 12), 124); put(octal(mtime, 12), 136); put("        ", 148);
    put(body ? "0" : "5", 156); put("ustar\0", 257); put("00", 263);
    put(octal(header.reduce((sum, b) => sum + b, 0), 7) + " ", 148);
    blocks.push(header);
    if (body) { blocks.push(body); if (body.length % 512) blocks.push(new Uint8Array(512 - (body.length % 512))); }
  }
  entry("enrollment/", null, 0o700);
  const manifest = encoder.encode(manifestJson.endsWith("\n") ? manifestJson : manifestJson + "\n");
  for (const c of credentials) {
    entry(`enrollment/${c.serial}/`, null, 0o700);
    entry(`enrollment/${c.serial}/enroll.toml`, encoder.encode(enrollToml(c, policyId, keypairPath, fleet)), 0o600);
    entry(`enrollment/${c.serial}/claim.key`, encoder.encode(c.seedHex + "\n"), 0o600);
    entry(`enrollment/${c.serial}/deployment.json`, manifest, 0o644);
  }
  blocks.push(new Uint8Array(1024));
  const out = new Uint8Array(blocks.reduce((n, b) => n + b.length, 0));
  let offset = 0;
  for (const b of blocks) { out.set(b, offset); offset += b.length; }
  return out;
}

// ── Chain reads ───────────────────────────────────────────────────────────────

export interface PolicyState {
  version?: string;
  fleet?: NamedFleet;
  id: string;
  operator: string;
  registry: string;
  claimsTable: string;
  unusedClaims: number;
  remaining: bigint;
  expiresAtMs: bigint;
  open: boolean;
  enrolled: bigint;
}

// PolicyCreated was introduced with EnrollmentPolicy in the same upgrade, so
// they share a type origin (the manifest records the policy's).
const policyCreatedType = () => `${normalizeId(requireManifest().type_origins["enrollment::EnrollmentPolicy"])}::enrollment::PolicyCreated`;

function parsePolicy(object: { objectId: string; type: string; content: Uint8Array; version?: string }): PolicyState {
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("enrollment::EnrollmentPolicy"))) throw new Error("Not an EnrollmentPolicy");
  const p = enrollment_EnrollmentPolicyBcs.parse(object.content);
  return { id: object.objectId, version: object.version, operator: p.operator, registry: p.registry, claimsTable: p.claims.id, unusedClaims: Number(p.claims.size),
    remaining: BigInt(p.remaining), expiresAtMs: BigInt(p.expires_at_ms), open: p.open, enrolled: BigInt(p.enrolled) };
}

/** The connected operator's policies for this fleet's registry, newest first. */
export async function fetchPolicies(client: SuiGrpcClient, operator: string, include: string[] = [], fleet?: NamedFleet): Promise<PolicyState[]> {
  await verifyDeployment(client);
  if (fleet) {
    const { object } = await client.getObject({ objectId: fleet.id, include: { content: true } });
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Fleet"))) throw Error("Unexpected fleet inventory");
    const current = customer_FleetBcs.parse(object.content);
    if (current.customer_id !== fleet.customerId) throw Error("Fleet customer mismatch");
    const scope = { ...fleet, enrollmentPolicies: current.enrollment_policies };
    return Promise.all(scope.enrollmentPolicies.map(id => readPolicy(client, id, scope)));
  }
  const ids = (await allEvents(client, policyCreatedType(), operator)).map((e) => enrollment_PolicyCreatedBcs.parse(e.bcs))
    .filter((e) => e.operator === normalizeId(operator) && e.registry === normalizeId(REGISTRY_ID)).map((e) => e.policy_id);
  // `include`: e.g. a policy just created, before its event is indexed.
  return (await objectsInBatches(client, [...new Set([...include.map(normalizeId), ...ids])])).map(parsePolicy)
    .filter((p) => p.operator === normalizeId(operator) && p.registry === normalizeId(REGISTRY_ID));
}

export async function readPolicy(client: SuiGrpcClient, policyId: string, fleet?: NamedFleet): Promise<PolicyState> {
  const { object } = await client.getObject({ objectId: normalizeId(policyId), include: { content: true } });
  const policy = parsePolicy(object);
  if (fleet) {
    if (!fleet.enrollmentPolicies.includes(policyId) || policy.operator !== fleet.id || policy.registry !== normalizeId(REGISTRY_ID)) throw Error("Enrollment policy belongs to another fleet");
    const { dynamicField } = await client.getDynamicField({ parentId: policyId, name: { type: moveType("enrollment::ManagedKey"), bcs: bcs.bool().serialize(false).toBytes() } });
    if (dynamicField.value.type !== "bool") throw Error("Unexpected enrollment authority state");
    policy.open = bcs.bool().parse(dynamicField.value.bcs); policy.fleet = fleet;
  }
  return policy;
}

export function policyAccepting(policy: PolicyState, nowMs = Date.now()): string | undefined {
  if (!policy.open) return "closed";
  if (policy.expiresAtMs !== BigInt(0) && BigInt(nowMs) > policy.expiresAtMs) return "expired";
  if (policy.remaining === BigInt(0)) return "quota exhausted";
  return undefined;
}

export interface Enrollment { policyId: string; deviceId: string; deviceAddress: string; claim: string; operator: string; ownerCap: string }

/** DeviceEnrolled events of one policy, by claim address. */
export async function fetchEnrollments(client: SuiGrpcClient, policyId: string): Promise<Map<string, Enrollment>> {
  const out = new Map<string, Enrollment>();
  for (const e of await allEvents(client, moveType("enrollment::DeviceEnrolled"))) {
    const d = enrollment_DeviceEnrolledBcs.parse(e.bcs);
    if (d.policy_id !== normalizeId(policyId)) continue;
    out.set(d.claim, { policyId: d.policy_id, deviceId: d.device_id, deviceAddress: d.device_address, claim: d.claim, operator: d.operator, ownerCap: d.owner_cap });
  }
  return out;
}

async function mapLimit<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i]); }
  }));
  return out;
}

/** Which claims are currently unused in the policy's claim table. */
export async function registeredClaims(client: Pick<SuiGrpcClient, "getDynamicField">, policy: PolicyState, claims: string[]): Promise<Set<string>> {
  const present = await mapLimit(claims, 16, async (claim) => {
    try {
      const field = await client.getDynamicField({ parentId: policy.claimsTable, name: { type: "address", bcs: bcs.Address.serialize(claim).toBytes() } });
      return field.dynamicField.value.type === "bool";
    } catch (error) {
      if (error instanceof ObjectError && error.reason === "notFound") return false;
      throw error;
    }
  });
  return new Set(claims.filter((_, i) => present[i]));
}

export async function balances(client: Pick<SuiGrpcClient, "getBalance">, owners: string[]): Promise<Map<string, bigint>> {
  const values = await mapLimit(owners, 16, async (owner) => BigInt((await client.getBalance({ owner })).balance.balance));
  return new Map(owners.map((owner, i) => [owner, values[i]]));
}

// ── Status ────────────────────────────────────────────────────────────────────

export type EntryStatus =
  | "unregistered"      // claim not yet in the policy
  | "unfunded"          // registered, enrollment gas below the job's amount
  | "awaiting-device"   // registered and funded; the device has not enrolled
  | "revoked"           // claim withdrawn before use
  | "confirming"        // submission outcome or policy visibility still uncertain
  | "withdrawn"         // observed earlier, now absent, or policy closed
  | "binding"           // enrolled; alias/serial/group still to apply
  | "complete"
  | "conflict"          // enrolled, but its alias/serial/group cannot be applied
  | "untrusted";        // enrolled, but not a device this wallet controls

export interface EntryView {
  entry: JobEntry;
  status: EntryStatus;
  detail?: string;
  device?: DeviceObject;
  enrollment?: Enrollment;
  needsMeta?: boolean;
  needsGroup?: boolean;
}

export interface ChainSnapshot {
  policy: PolicyState;
  registered: Set<string>;
  balances: Map<string, bigint>;
  enrollments: Map<string, Enrollment>;
  devices: DeviceObject[];
  groups: DeviceGroup[];
}

/**
 * Read everything the job's statuses depend on, fresh from the chain,
 * including the operator's devices and groups: a plan built from stale
 * membership would re-add devices and abort.
 */
export async function snapshot(client: SuiGrpcClient, job: EnrollmentJob, fleet?: NamedFleet): Promise<ChainSnapshot> {
  await verifyDeployment(client);
  if (fleet) {
    const { object } = await client.getObject({ objectId: fleet.id, include: { content: true } });
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Fleet"))) throw Error("Unexpected fleet inventory");
    const current = customer_FleetBcs.parse(object.content);
    if (current.customer_id !== fleet.customerId) throw Error("Fleet customer mismatch");
    fleet = { ...fleet, devices: current.devices, groups: current.groups, enrollmentPolicies: current.enrollment_policies };
  }
  const [policy, devices, groups] = await Promise.all([readPolicy(client, job.policyId, fleet), fleet ? fetchFleetDevices(client, fleet.devices) : fetchDevices(client, job.operator), fetchGroups(client, job.operator)]);
  if (policy.operator !== job.operator) throw new Error("This policy belongs to another operator");
  if (policy.registry !== normalizeId(REGISTRY_ID)) throw new Error("This policy belongs to another fleet registry");
  const enrollments = await fetchEnrollments(client, job.policyId);
  const pending = job.entries.filter((e) => !enrollments.has(e.claim)).map((e) => e.claim);
  const registered = await registeredClaims(client, policy, pending);
  return { policy, registered, enrollments, devices, groups, balances: await balances(client, pending.filter((c) => registered.has(c))) };
}

/** Remember positive observations only; a missing field is never proof of registration. */
export function observeRegistrations(job: EnrollmentJob, chain: ChainSnapshot): EnrollmentJob {
  let changed = false;
  const entries = job.entries.map(entry => {
    if (!chain.registered.has(entry.claim)) return entry;
    const newerObservation = chain.policy.version && BigInt(chain.policy.version) > BigInt(entry.observedPolicyVersion ?? 0);
    if (entry.observedRegistered && !newerObservation) return entry;
    changed = true;
    return { ...entry, observedRegistered: true, observedPolicyVersion: chain.policy.version };
  });
  return changed ? { ...job, entries } : job;
}

/**
 * Reconcile chain state with persisted submission/observation evidence, so
 * an interrupted or repeated run never treats an uncertain absence as failure. A device is bound only through its
 * claim's DeviceEnrolled event under this job's policy and operator, and only
 * if the connected wallet holds that enrollment's owner cap.
 */
export function entryStatuses(job: EnrollmentJob, chain: ChainSnapshot): EntryView[] {
  const fund = BigInt(job.claimFundMist);
  const byId = new Map(chain.devices.map((d) => [d.objectId, d]));
  const key = (v: string) => v.toLowerCase();
  return job.entries.map((entry): EntryView => {
    const enrollment = chain.enrollments.get(entry.claim);
    if (!enrollment) {
      if (chain.registered.has(entry.claim)) {
        return (chain.balances.get(entry.claim) ?? BigInt(0)) < fund ? { entry, status: "unfunded" } : { entry, status: "awaiting-device" };
      }
      if (entry.revoked) return { entry, status: "revoked" };
      if (entry.submitted || entry.observedRegistered || entry.registrationDigest) {
        if (chain.policy.open === false) return { entry, status: "withdrawn", detail: "the policy is closed and the claim is absent" };
        if (entry.observedRegistered && entry.observedPolicyVersion && chain.policy.version && BigInt(chain.policy.version) > BigInt(entry.observedPolicyVersion)) {
          return { entry, status: "withdrawn", detail: "previously observed in the policy; absent after a later policy mutation (withdrawn elsewhere)" };
        }
        return { entry, status: "confirming", detail: entry.registrationDigest
          ? `registration ${entry.registrationDigest}: waiting for the claim to appear in policy reads; do not submit again`
          : entry.observedRegistered ? "waiting for a policy read newer than the last observed registration"
          : "submission interrupted; outcome unknown. Refresh to reconcile before taking further action" };
      }
      return { entry, status: "unregistered" };
    }
    if (enrollment.operator !== job.operator || enrollment.policyId !== job.policyId) {
      return { entry, enrollment, status: "untrusted", detail: "enrolled under another operator or policy" };
    }
    const device = byId.get(enrollment.deviceId);
    if (!device || (!device.managed && device.ownerCapId !== enrollment.ownerCap)) {
      return { entry, enrollment, status: "untrusted", detail: "this wallet does not hold the device's owner cap (transferred or deregistered)" };
    }
    const other = chain.devices.find((d) => d.objectId !== device.objectId &&
      (key(d.name) === key(entry.alias) || (d.serial && key(d.serial) === key(entry.serial))));
    if (other) return { entry, enrollment, device, status: "conflict", detail: `alias or serial already used by ${other.name}` };
    const needsMeta = device.meta[ALIAS_META_KEY] !== entry.alias || device.meta[SERIAL_META_KEY] !== entry.serial;
    let needsGroup = false;
    if (entry.groupId) {
      const group = chain.groups.find((g) => g.objectId === entry.groupId);
      if (!group) return { entry, enrollment, device, status: "conflict", detail: "its group no longer exists or is not yours" };
      needsGroup = !group.deviceIds.includes(device.objectId);
    }
    return { entry, enrollment, device, needsMeta, needsGroup, status: needsMeta || needsGroup ? "binding" : "complete" };
  });
}

export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

// ── Transactions ──────────────────────────────────────────────────────────────

/** quota: enrollments allowed; expiresAtMs: clock milliseconds, 0 = never. */
export function buildCreatePolicyTx(quota: bigint | number, expiresAtMs: bigint | number, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::create_enrollment_policy`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(REGISTRY_ID), tx.pure.u64(quota), tx.pure.u64(expiresAtMs)] });
  else calls.enrollment_create_policy(tx, PACKAGE_ID, REGISTRY_ID, quota, expiresAtMs);
  return tx;
}

export function buildSetLimitsTx(policyId: string, quota: bigint | number, expiresAtMs: bigint | number, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::configure_enrollment`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policyId), tx.pure.u64(quota), tx.pure.u64(expiresAtMs), tx.pure.bool(false)] });
  else calls.enrollment_set_limits(tx, PACKAGE_ID, policyId, quota, expiresAtMs);
  return tx;
}

export function buildClosePolicyTx(policyId: string, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::configure_enrollment`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policyId), tx.pure.u64(0), tx.pure.u64(0), tx.pure.bool(true)] });
  else calls.enrollment_close(tx, PACKAGE_ID, policyId);
  return tx;
}

/**
 * Register claims (duplicates are ignored on chain) and, separately chosen per
 * claim, send each its enrollment gas: `register` and `fund` may differ, so a
 * resumed run tops up only underfunded claims. This sponsorship pays the
 * enrollment transaction only; ongoing device gas is `buildTransferTx`.
 */
export function buildClaimsTx(policyId: string, register: string[], fund: { claim: string; mist: bigint }[], fleet?: NamedFleet): Transaction {
  requireManifest();
  if (register.length > LIMITS.claimsPerTx || fund.length > LIMITS.transfersPerTx) throw new Error("Too many claims for one transaction");
  const tx = new Transaction();
  if (register.length) {
    if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::add_claims`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policyId), tx.pure.vector("address", register)] });
    else calls.enrollment_add_claims(tx, PACKAGE_ID, policyId, register);
  }
  if (fund.length) {
    const coins = tx.splitCoins(tx.gas, fund.map((f) => tx.pure.u64(f.mist)));
    fund.forEach((f, i) => tx.transferObjects([coins[i]], tx.pure.address(f.claim)));
  }
  return tx;
}

export function buildRevokeClaimsTx(policyId: string, claims: string[], fleet?: NamedFleet): Transaction {
  requireManifest();
  if (claims.length > LIMITS.revokesPerTx) throw new Error("Too many claims for one transaction");
  const tx = new Transaction();
  for (const claim of claims) {
    if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::revoke_claim`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policyId), tx.pure.address(claim)] });
    else calls.enrollment_revoke_claim(tx, PACKAGE_ID, policyId, claim);
  }
  return tx;
}

/** Bind enrolled devices: write alias and serial metadata and join their groups. */
export function buildBindTx(views: EntryView[], fleet?: NamedFleet): Transaction {
  requireManifest();
  if (views.length > LIMITS.bindsPerTx) throw new Error("Too many devices for one transaction");
  const tx = new Transaction();
  for (const v of views) {
    if (v.status !== "binding" || (!v.device?.ownerCapId && !v.device?.managed)) throw new Error(`${v.entry.alias} is not ready to bind`);
    if (fleet) {
      if (v.needsMeta) tx.moveCall({ target: `${PACKAGE_ID}::customer::bind_device_metadata`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(REGISTRY_ID), tx.pure.id(v.device!.objectId), tx.pure.string(v.entry.alias), tx.pure.string(v.entry.serial)] });
      if (v.needsGroup && v.entry.groupId) tx.moveCall({ target: `${PACKAGE_ID}::customer::add_to_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(v.entry.groupId), tx.pure.id(v.device!.objectId)] });
      continue;
    }
    const [device, loan] = calls.device_registry_borrow_device(tx, PACKAGE_ID, REGISTRY_ID, v.device!.ownerCapId!);
    if (v.needsMeta) {
      calls.device_registry_set_meta(tx, PACKAGE_ID, device, ALIAS_META_KEY, v.entry.alias);
      calls.device_registry_set_meta(tx, PACKAGE_ID, device, SERIAL_META_KEY, v.entry.serial);
    }
    if (v.needsGroup && v.entry.groupId) calls.device_group_add_device(tx, PACKAGE_ID, v.entry.groupId, device);
    calls.device_registry_return_device(tx, PACKAGE_ID, REGISTRY_ID, device, loan);
  }
  return tx;
}

/** Plain SUI transfers, e.g. ongoing gas for enrolled devices' status reports. */
export function buildTransferTx(recipients: string[], mist: bigint): Transaction {
  requireManifest();
  if (recipients.length > LIMITS.transfersPerTx) throw new Error("Too many recipients for one transaction");
  const tx = new Transaction();
  const coins = tx.splitCoins(tx.gas, recipients.map(() => tx.pure.u64(mist)));
  recipients.forEach((r, i) => tx.transferObjects([coins[i]], tx.pure.address(r)));
  return tx;
}

/** The policy a create-policy transaction made, from its own events. */
export async function createdPolicy(client: SuiGrpcClient, digest: string): Promise<string> {
  const result = await client.getTransaction({ digest, include: { events: true } });
  const tx = result.Transaction ?? result.FailedTransaction;
  const event = tx?.events?.find((e) => normalizeStructTag(e.eventType) === normalizeStructTag(policyCreatedType()));
  if (!event) throw new Error("The transaction created no enrollment policy");
  return enrollment_PolicyCreatedBcs.parse(event.bcs).policy_id;
}

// ── Plans ─────────────────────────────────────────────────────────────────────

export interface Step {
  kind: "claims" | "bind";
  label: string;
  build: () => Transaction;
  entries: JobEntry[];
  /** Entries this step registers for the first time. */
  registers?: JobEntry[];
}

/** The remaining work, in transaction-sized steps, recomputed from statuses. */
export function plan(job: EnrollmentJob, views: EntryView[], chain: Pick<ChainSnapshot, "balances">, fleet?: NamedFleet): Step[] {
  const fund = BigInt(job.claimFundMist);
  const pending = views.filter((v) => v.status === "unregistered" || v.status === "unfunded");
  const steps: Step[] = chunk(pending, LIMITS.claimsPerTx).map((part, i, all) => ({
    kind: "claims", label: `Register and fund claims ${i + 1}/${all.length}`, entries: part.map((v) => v.entry),
    registers: part.filter((v) => v.status === "unregistered").map((v) => v.entry),
    build: () => buildClaimsTx(job.policyId,
      part.filter((v) => v.status === "unregistered").map((v) => v.entry.claim),
      part.map((v) => ({ claim: v.entry.claim, mist: fund - (chain.balances.get(v.entry.claim) ?? BigInt(0)) })).filter((f) => f.mist > BigInt(0)), fleet),
  }));
  const binding = views.filter((v) => v.status === "binding");
  steps.push(...chunk(binding, LIMITS.bindsPerTx).map((part, i, all) => ({
    kind: "bind" as const, label: `Name and group devices ${i + 1}/${all.length}`, entries: part.map((v) => v.entry), build: () => buildBindTx(part, fleet) })));
  return steps;
}
