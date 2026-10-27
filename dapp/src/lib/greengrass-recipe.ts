// SF-037: AWS Greengrass V2 recipe translation into native catalog manifests.
//
// The supported subset, every rejection reason and the reported differences
// are specified in docs/GREENGRASS_COMPATIBILITY.md and pinned by
// interfaces/compat/aws-recipe-cases.json; scripts/greengrass_compat.py holds
// the matching reference model. A recipe is translated only when each field
// keeps its AWS meaning on the native daemon; anything else is rejected with
// every reason, never approximated.
import { parseAllDocuments, isMap, isPair, isScalar, visit, type Document } from "yaml";
import { CONFIGURATION_LIMITS, type ComponentRecipe } from "./recipe-schema";
import { MANAGED_CATALOG } from "./greengrass-managed";

export type ArtifactBinding = { artifactBlobId: string; artifactSha256: string };
export type NativeManifest = { os: string; arch: string; recipe: ComponentRecipe };
/** SF-038 classification of a dependency on an AWS-managed component. */
export type ManagedClassification = {
  outcome: string; reason?: string; tickets?: string[]; adapter?: string; requires?: string[]; implementation_ticket?: string;
};
/** Operator IPC ACL equivalent of AWS accessControl; never applied by a recipe. */
export type IpcAclSuggestion = { operations: string[]; publish: string[]; subscribe: string[]; unmapped: string[] };
export type Translation = {
  outcome: "translate" | "reject";
  reasons: string[];
  manifests?: NativeManifest[];
  omitted_manifests?: number[];
  informational?: string[];
  managed_dependencies?: Record<string, ManagedClassification>;
  ipc_acl?: IpcAclSuggestion;
};

const NAME = /^[A-Za-z0-9._-]{1,128}$/;
const NUM = "(0|[1-9][0-9]*)";
const IDENT = "(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)";
const CORE = `${NUM}\\.${NUM}\\.${NUM}(-${IDENT}(\\.${IDENT})*)?`;
const SEMVER = new RegExp(`^${CORE}(\\+[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?$`);
const COMPARATOR = `(>=|<=|>|<|=|\\^|~)?${CORE}`;
// Same grammar as interfaces/recipe.schema.json componentDependencies.
const RANGE = new RegExp(`^(\\*|${COMPARATOR}( ${COMPARATOR})*)$`);
const ONE_COMPARATOR = new RegExp(`^(>=|<=|>|<|=|\\^|~)?(${CORE})$`);
const TOP_KEYS = new Set(["RecipeFormatVersion", "ComponentName", "ComponentVersion", "ComponentDescription",
  "ComponentPublisher", "ComponentConfiguration", "ComponentDependencies", "ComponentType", "ComponentSource", "Manifests", "Lifecycle"]);
// AWS matches lifecycle step names, step keys and artifact keys
// case-insensitively (Install/install, URI/Uri); these are the canonical spellings.
const STEP_KEYS = ["Script", "RequiresPrivilege", "Skipif", "Timeout", "Setenv", "BootstrapOnRollback"];
const MAPPED_STEPS = new Set(["install", "run", "shutdown"]);
const UNSUPPORTED_STEPS = new Set(["bootstrap", "startup", "recover", "uninstall"]);
const LIFECYCLE_KEYS = [...MAPPED_STEPS, ...UNSUPPORTED_STEPS, "Setenv"];
const ARTIFACT_KEYS = ["Uri", "Unarchive", "Permission", "Digest", "Algorithm"];
const PERMISSION_KEYS = ["Read", "Execute"];
const OS_MAP: Record<string, string> = { linux: "linux", "*": "*" };
const ARCH_MAP: Record<string, string> = { amd64: "x86_64", aarch64: "aarch64", arm: "arm", "*": "*" };
const OMITTED_OS = new Set(["windows", "darwin"]);
const OMITTED_ARCH = new Set(["x86"]);
// Literal text the native daemon would substitute (shell-quoted).
const NATIVE_PLACEHOLDERS = ["{artifact}", "{artifacts}", "{work}", "{pid}", "{configuration}"];
// Environment the Greengrass nucleus provides; the daemon clears the environment.
const AWS_ENVIRONMENT = /\$\{?(SVCUID|AWS_GG_[A-Z0-9_]*|AWS_IOT_THING_NAME|AWS_REGION|AWS_DEFAULT_REGION|GG_ROOT_CA_PATH|AWS_CONTAINER_CREDENTIALS_FULL_URI|AWS_CONTAINER_AUTHORIZATION_TOKEN)(?![A-Za-z0-9_])/;
const MAX_MANIFESTS = 16;
const MANAGED_PREFIXES = ["aws.greengrass.", "aws.iot.", "variant."];
const PUBLISH = "aws.greengrass#PublishToTopic";
const SUBSCRIBE = "aws.greengrass#SubscribeToTopic";

/** Migration guidance for every rejection reason and reported difference. */
export const GUIDANCE: Record<string, string> = {
  "invalid-recipe": "The document must be one JSON object or YAML mapping.",
  "invalid-yaml": "Fix the YAML syntax error; the recipe is never guessed from a partial parse.",
  "duplicate-key": "Remove the duplicated key: which value Greengrass would use is not guessed.",
  "multiple-documents": "Supply exactly one YAML document per recipe.",
  "yaml-ambiguous-scalar": "A plain YAML scalar means different things under YAML 1.1 (the Greengrass parser) and YAML 1.2, for example yes/no/on/off, 0755, 1:20 or a << merge key. Quote it, or write the value you mean explicitly.",
  "yaml-non-json-value": "Use only JSON-compatible YAML: string keys, finite numbers, no tags, directives or anchors that change meaning.",
  "case-duplicate-key": "Lifecycle step names, step keys and artifact keys are matched case-insensitively, as in AWS, so Install and install (or URI and Uri) are the same key. Keep only one of them.",
  "unknown-field": "Remove the field, or correct its spelling: Greengrass field names are matched exactly and unknown fields are never ignored.",
  "unsupported-format-version": "Set RecipeFormatVersion to '2020-01-25' (quoted in YAML).",
  "unsupported-component-type": "Only aws.greengrass.generic components translate. Repackage Lambda functions or plugins as a generic component with a run command (not Lambda compatibility; see docs/GREENGRASS_MANAGED_COMPONENTS.md).",
  "lambda-source": "ComponentSource (Lambda) has no native runtime; repackage the function as a generic component.",
  "global-lifecycle-selections": "Move lifecycle steps into each manifest's Lifecycle; global Lifecycle and Selections are not translated.",
  "invalid-component-name": "Use 1-128 characters of A-Z a-z 0-9 . _ - for ComponentName.",
  "invalid-component-version": "Use a SemVer 2.0.0 ComponentVersion of at most 128 characters.",
  "invalid-description": "ComponentDescription must be a string.",
  "invalid-configuration": "ComponentConfiguration may contain only a DefaultConfiguration object.",
  "configuration-limits": `Keep DefaultConfiguration within the native limits: at most ${CONFIGURATION_LIMITS.values} values and depth ${CONFIGURATION_LIMITS.depth}.`,
  "configuration-key-case-collision": "The Greengrass nucleus configuration store looks keys up case-insensitively, while native keys are case-sensitive: rename keys that differ only in case.",
  "configuration-without-command": "Native configuration is delivered only to install or run commands; add one to every manifest or remove DefaultConfiguration.",
  "configuration-variable": "Native commands receive the whole configuration as the JSON file {configuration} (and $SUIFLEET_CONFIGURATION for run/shutdown), never inline values. Read the value from that file, for example with jq or your program, then remove {configuration:...}.",
  "ipc-access-control": "accessControl authorizes the AWS IPC protocol, which SuiFleet does not implement. Port the component to the native IPC SDK (docs/COMPONENT_IPC.md), run it as a separately provisioned Unix user and have the operator add the suggested [ipc.components.<name>] ACL. Recipes never grant IPC privileges.",
  "invalid-dependency": "Each ComponentDependencies entry needs a valid name other than the component itself, with only VersionRequirement and DependencyType HARD or SOFT.",
  "unsupported-version-range": "Use the native comparator grammar (for example >=1.0.0 <2.0.0, ^1.2.0, ~1.2.0 or *); x-ranges, hyphen ranges and || are not translated.",
  "aws-managed-dependency": "This AWS-managed component has no translatable native equivalent yet: see managed_dependencies and docs/GREENGRASS_MANAGED_COMPONENTS.md for the replacement, adapter ticket or roadmap gap, then depend on your native component instead.",
  "unsupported-manifest-key": "Manifests may contain only Name, Platform, Lifecycle and Artifacts.",
  "unsupported-platform-key": "Platform may use only os and architecture; architecture.detail, runtime and custom keys have no native selector.",
  "platform-regex": "Platform values must be exact names or *; regular expressions are not translated.",
  "unsupported-platform-value": "Use os linux or * and architecture amd64, aarch64, arm or *.",
  "duplicate-platform": "Each translated manifest needs a distinct os/architecture pair; native selection is first match.",
  "no-manifests": "Add at least one manifest.",
  "no-supported-platform": "Add a Linux x86_64, aarch64 or arm manifest; Windows and macOS have no native runtime, and 32-bit x86 is outside the supported import scope.",
  "artifact-count": "Each manifest needs exactly one artifact (and a recipe at most 16 manifests); combine files into one archive unpacked by install.",
  "unsupported-artifact-key": "Artifacts may contain only URI, Unarchive, Permission, Digest and Algorithm, in any letter case as in AWS (URI or Uri).",
  "unsupported-unarchive": "Use Unarchive NONE and unpack the archive in install, for example tar -xf {artifacts:path}/<file>.",
  "unsupported-artifact-permission": "Use the default Permission (Read OWNER, Execute NONE); run the artifact through an interpreter or chmod it in install.",
  "unbound-artifact": "Bind every artifact URI to an authenticated Walrus upload (blob ID and SHA-256); s3:// URIs are never fetched.",
  "artifact-digest-mismatch": "The recipe Digest must be the SHA-256 (Algorithm SHA-256) of exactly the bound upload.",
  "no-lifecycle-command": "Add an install or run step. A native recipe without either hands its artifact to the platform updater, which an AWS component never does.",
  "invalid-step": "A lifecycle step must be a script string or an object with a Script string.",
  "empty-step": "A lifecycle script must contain a command. Remove the empty step; a native recipe without install or run hands its artifact to the platform updater.",
  "unsupported-step-key": "Remove Skipif and BootstrapOnRollback; express conditions inside the script.",
  "privileged-step": "RequiresPrivilege other than false has no native equivalent: components run as the daemon user. Move privileged work into a platform installer (docs/PLATFORM_INSTALLERS.md).",
  "step-timeout-or-env": "Remove Timeout and Setenv: native step timeouts are operator configuration and the environment is cleared. Set variables inside the script.",
  "unsupported-lifecycle-step": "bootstrap, startup, recover and uninstall have no native equivalent: use install for setup, run for the long-running process (the supervisor restarts it), and platform tooling for reboots and removal.",
  "unknown-lifecycle-step": "Lifecycle step names are install, run or shutdown, in any letter case as in AWS (Install, Run, Shutdown); the translated manifest uses lowercase.",
  "shutdown-without-run": "shutdown applies only to a supervised run command.",
  "native-placeholder-collision": "The script contains {artifact}, {artifacts}, {work}, {pid} or {configuration} literally, which the native daemon would substitute. Rename that text.",
  "quoted-variable": "Native substitutions are shell-quoted, so recipe variables inside quotes or next to backslashes would change meaning. Move them outside quotes.",
  "unmappable-artifact-path": "Only {artifacts:path}/<this manifest's artifact file name> translates (to {artifact}). Unpack archives yourself and avoid {artifacts:decompressedPath} or other files.",
  "work-path-in-install": "{work:path} is available only to run and shutdown; install runs in the version's artifact directory.",
  "dependency-variable": "{<component>:...} dependency variables have no native equivalent; pass paths through configuration instead.",
  "aws-runtime-variable": "{iot:thingName} and {kernel:rootPath} have no native equivalent; the device identity is a Sui object, not an AWS thing.",
  "aws-environment-variable": "The script reads an environment variable the Greengrass nucleus sets (SVCUID, AWS_GG_*, AWS_IOT_THING_NAME, AWS_REGION, GG_ROOT_CA_PATH, AWS container credentials). The daemon clears the environment; use configuration or SUIFLEET_* variables.",
  "unknown-variable": "Unknown recipe variable namespace; only artifacts:path and work:path translate.",
  "ComponentPublisher": "Publisher is not part of the native recipe; the catalog's owner identifies the publisher.",
  "Lifecycle.install": "install runs once per deployment in the version's artifact directory (temporary for install-only components), not the AWS work folder ($PWD), as the daemon user with a cleared environment, and is bounded by the operator's apply.timeout_seconds rather than the AWS 120 s default.",
  "Lifecycle.run": "run is supervised in the component work directory ({work:path}, as in AWS) with no timeout: restart on failure with backoff, Broken after 5 restarts in 10 minutes, replacing AWS recover and step timeouts. It runs as the daemon user with a cleared environment.",
  "Lifecycle.shutdown": "shutdown is bounded by the operator's component stop timeout rather than the AWS 15 s default.",
  "ComponentDependencies": "Every dependency becomes a native HARD dependency, also for AWS SOFT: AWS requires both types to be present and started first, while a native SOFT dependency may be left out. The native resolver therefore fails the deployment when no version satisfies a dependency. Supervised dependencies start, and become ready, first at deployment and daemon start, but a dependent still starts if a dependency fails to become ready, and there is no AWS HARD restart coupling: a dependent is not restarted or stopped when its dependency restarts, errors or is replaced. Make the component wait for and tolerate its dependency itself (for example with a readiness check), or deploy them together.",
  "ComponentConfiguration.DefaultConfiguration": "Becomes native defaults, delivered as the {configuration} file (and $SUIFLEET_CONFIGURATION to run/shutdown) instead of the IPC GetConfiguration call. On a later version, device values are kept and new defaults fill only missing keys; the AWS guide does not specify how a changed default combines with an existing value, so check upgrades that change defaults. Per-deployment reset and merge are native configuration deployments (reset before merge, as in AWS).",
};

const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const has = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);
// Own data property: plain assignment of "__proto__" would set the prototype and drop the key.
const define = <T>(target: Record<string, T>, key: string, value: T) =>
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
const BLANK = /^[ \t\n\r]*$/;

/**
 * Match keys case-insensitively, as AWS does: the values under their canonical
 * names, in document order, and the keys that match none. Keys that differ
 * only in case are rejected, never resolved by picking one.
 */
function fold(value: Record<string, unknown>, names: readonly string[], reasons: Set<string>): [Record<string, unknown>, string[]] {
  const known: Record<string, unknown> = {}, unknown: string[] = [];
  for (const [key, item] of Object.entries(value)) {
    const canonical = names.find(n => n.toLowerCase() === key.toLowerCase());
    if (canonical === undefined) unknown.push(key);
    else if (has(known, canonical)) reasons.add("case-duplicate-key");
    else define(known, canonical, item);
  }
  return [known, unknown];
}

/** SemVer 2.0.0 precedence key, as scripts/greengrass_managed.py `_key`. */
function precedence(version: string): [number, number, number, number, [number, number, string][]] {
  const dash = version.indexOf("-");
  const core = dash < 0 ? version : version.slice(0, dash);
  const [major, minor, patch] = core.split(".").map(Number);
  if (dash < 0) return [major, minor, patch, 1, []];
  return [major, minor, patch, 0, version.slice(dash + 1).split(".").map(p => /^[0-9]+$/.test(p) ? [0, Number(p), ""] : [1, 0, p])];
}
function compare(a: ReturnType<typeof precedence>, b: ReturnType<typeof precedence>): number {
  for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return (a[i] as number) < (b[i] as number) ? -1 : 1;
  const [x, y] = [a[4], b[4]];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    for (let j = 0; j < 3; j++) if (x[i][j] !== y[i][j]) return x[i][j] < y[i][j] ? -1 : 1;
  }
  return x.length === y.length ? 0 : x.length < y.length ? -1 : 1;
}
/** True when a version satisfies a native-grammar range (space means AND). */
export function satisfies(version: string, requirement: string): boolean {
  if (!RANGE.test(requirement)) throw new Error(`unsupported range ${requirement}`);
  if (requirement === "*") return true;
  const value = precedence(version);
  return requirement.split(" ").every(comparator => {
    const [, op = "", bound] = ONE_COMPARATOR.exec(comparator)!;
    const low = precedence(bound);
    const [major, minor, patch] = low;
    const at = (h: [number, number, number]) => compare(value, [...h, 0, []]) < 0;
    const cmp = compare(value, low);
    if (op === "^") return cmp >= 0 && at(major ? [major + 1, 0, 0] : minor ? [0, minor + 1, 0] : [0, 0, patch + 1]);
    if (op === "~") return cmp >= 0 && at([major, minor + 1, 0]);
    return { ">=": cmp >= 0, "<=": cmp <= 0, ">": cmp > 0, "<": cmp < 0 }[op] ?? cmp === 0;
  });
}

/** SF-038 classification of one dependency, as scripts/greengrass_managed.py `classify_dependency`. */
export function classifyManagedDependency(name: unknown, spec: unknown): ManagedClassification {
  if (typeof name !== "string" || !isObject(spec) || Object.keys(spec).some(k => k !== "VersionRequirement" && k !== "DependencyType")) return { outcome: "reject", reason: "invalid-dependency" };
  if (!["HARD", "SOFT"].includes((has(spec, "DependencyType") ? spec.DependencyType : "HARD") as string)) return { outcome: "reject", reason: "invalid-dependency" };
  const requirement = has(spec, "VersionRequirement") ? spec.VersionRequirement : "*";
  if (typeof requirement !== "string" || !RANGE.test(requirement)) return { outcome: "reject", reason: "unsupported-version-range" };
  const row = MANAGED_CATALOG.components.find(c => c.name === name) as { disposition: string; tickets: readonly string[]; adapter?: string; discontinued?: string } | undefined;
  if (!row) return MANAGED_PREFIXES.some(p => name.startsWith(p)) ? { outcome: "reject", reason: "unknown-managed-component" } : { outcome: "not-managed" };
  if (row.discontinued) return { outcome: "reject", reason: "discontinued-aws-service" };
  if (row.disposition === "roadmap") return { outcome: "reject", reason: "roadmap-gap", tickets: [...row.tickets] };
  if (row.disposition === "not-applicable") return { outcome: "reject", reason: "not-applicable" };
  if (row.disposition === "native") return { outcome: "replace", tickets: [...row.tickets] };
  if (row.disposition === "native-component") return { outcome: "package-natively", tickets: [...row.tickets] };
  const adapter = MANAGED_CATALOG.adapters.find(a => a.id === row.adapter)!;
  if (adapter.specification !== "complete") return { outcome: "reject", reason: "adapter-specification-incomplete", adapter: adapter.id, implementation_ticket: adapter.implementation_ticket };
  if (!satisfies((adapter.replaces as Record<string, string>)[name], requirement)) return { outcome: "reject", reason: "unsupported-aws-version", adapter: adapter.id };
  return { outcome: "defer", adapter: adapter.id, requires: [...adapter.depends_on].sort(), implementation_ticket: adapter.implementation_ticket };
}

/** True when YAML could make the document mean something JSON cannot: non-string keys, unsafe numbers, tags, aliases, directives. */
function yamlNonJson(document: Document): boolean {
  let bad = false;
  visit(document, {
    Pair(_, pair) { if (isPair(pair) && (!isScalar(pair.key) || typeof pair.key.value !== "string")) bad = true; },
    Scalar(_, node) {
      const value = node.value;
      if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) bad = true;
      if (typeof value === "bigint" || (node.tag && !node.tag.startsWith("tag:yaml.org,2002:"))) bad = true;
    },
    Map(_, node) { if (node.tag && node.tag !== "tag:yaml.org,2002:map") bad = true; },
    Seq(_, node) { if (node.tag && node.tag !== "tag:yaml.org,2002:seq") bad = true; },
    Alias() { bad = true; },
  });
  return bad || !isMap(document.contents) || document.directives?.yaml.explicit === true;
}

function documents(text: string, version: "1.1" | "1.2") {
  return parseAllDocuments(text, version === "1.2" ? { version, merge: false, uniqueKeys: true } :
    { version, schema: "yaml-1.1", merge: true, uniqueKeys: true, customTags: tags => tags.filter(t => typeof t === "string" || t.tag !== "tag:yaml.org,2002:timestamp") });
}

/**
 * Parse a recipe document, JSON or YAML, into its JSON value. Valid JSON is
 * read as JSON. Greengrass reads YAML with a YAML 1.1 parser; YAML is accepted
 * only when YAML 1.1 and YAML 1.2 give the same JSON value, so nothing is
 * silently reinterpreted. Duplicate keys are rejected in both.
 */
export function parseGreengrassRecipe(text: string): { recipe: unknown } | Translation {
  const reject = (reason: string): Translation => ({ outcome: "reject", reasons: [reason] });
  let json = true;
  try { JSON.parse(text); } catch { json = false; }
  // YAML 1.2 is a JSON superset: the same parser checks keys and numbers for both.
  const modern = documents(text, "1.2");
  if (!Array.isArray(modern)) return reject(json ? "invalid-recipe" : "invalid-yaml");
  if (modern.length !== 1) return reject(modern.length ? "multiple-documents" : "invalid-recipe");
  const [document] = modern;
  if (document.errors.some(e => e.code === "DUPLICATE_KEY")) return reject("duplicate-key");
  if (document.errors.length) return reject("invalid-yaml");
  if (document.warnings.length || yamlNonJson(document)) return reject(json ? "invalid-recipe" : "yaml-non-json-value");
  const value = document.toJS({ maxAliasCount: 0 });
  if (!json) {
    const legacy = documents(text, "1.1");
    if (!Array.isArray(legacy) || legacy.length !== 1 || legacy[0].errors.length
      || JSON.stringify(value) !== JSON.stringify(legacy[0].toJS({ maxAliasCount: 0 }))) return reject("yaml-ambiguous-scalar");
  }
  return { recipe: value };
}

/** Every artifact URI (any letter case) in a recipe's manifests, in order, for binding. */
export function artifactUris(recipe: unknown): string[] {
  const uris: string[] = [];
  const manifests = isObject(recipe) && Array.isArray(recipe.Manifests) ? recipe.Manifests : [];
  for (const manifest of manifests) {
    for (const artifact of isObject(manifest) && Array.isArray(manifest.Artifacts) ? manifest.Artifacts : []) {
      const uri = isObject(artifact) ? fold(artifact, ARTIFACT_KEYS, new Set())[0].Uri : undefined;
      if (typeof uri === "string" && !uris.includes(uri)) uris.push(uri);
    }
  }
  return uris;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Rewrite recipe variables to native substitutions where their meaning is identical. */
function script(step: string, text: string, basename: string | null, reasons: Set<string>): string {
  if (NATIVE_PLACEHOLDERS.some(p => text.includes(p))) reasons.add("native-placeholder-collision");
  if (AWS_ENVIRONMENT.test(text)) reasons.add("aws-environment-variable");
  const variables = [...text.matchAll(/\{([^{}]*)\}/g)].map(m => m[1]).filter(body => body.includes(":"));
  // Native substitutions are shell-quoted; inside quotes they change meaning.
  if (variables.length && /['"\\]/.test(text)) reasons.add("quoted-variable");
  if (basename) text = text.replace(new RegExp(`\\{artifacts:path\\}/${escapeRegExp(basename)}(?![A-Za-z0-9._-])`, "g"), "{artifact}");
  if (step !== "install") text = text.split("{work:path}").join("{work}");
  for (const [, body] of text.matchAll(/\{([^{}]*)\}/g)) {
    if (["artifact", "artifacts", "work", "pid", "configuration"].includes(body)) continue;
    const parts = body.split(":");
    if (parts.length === 3) reasons.add("dependency-variable");
    else if (parts.length !== 2) continue; // not a recipe variable (for example shell ${VAR} or JSON)
    else if (parts[0] === "configuration") reasons.add("configuration-variable");
    else if (parts[0] === "artifacts") reasons.add("unmappable-artifact-path");
    else if (parts[0] === "work") reasons.add("work-path-in-install");
    else if (parts[0] === "iot" || parts[0] === "kernel") reasons.add("aws-runtime-variable");
    else reasons.add("unknown-variable");
  }
  return text;
}

function step(name: string, value: unknown, basename: string | null, reasons: Set<string>): string | null {
  let text: unknown = value;
  if (isObject(value)) {
    const [keys, unknown] = fold(value, STEP_KEYS, reasons);
    if (unknown.length || has(keys, "Skipif") || has(keys, "BootstrapOnRollback")) reasons.add("unsupported-step-key");
    // Only an explicit false keeps AWS semantics; Greengrass may coerce strings such as "true".
    if (has(keys, "RequiresPrivilege") && keys.RequiresPrivilege !== false) reasons.add("privileged-step");
    if (has(keys, "Timeout") || has(keys, "Setenv")) reasons.add("step-timeout-or-env");
    text = keys.Script;
  }
  if (typeof text !== "string") {
    reasons.add("invalid-step");
    return null;
  }
  // The native validators accept only a nonempty install or run command.
  if (BLANK.test(text)) {
    reasons.add("empty-step");
    return null;
  }
  return script(name, text, basename, reasons);
}

/** Map an AWS Platform to [os, arch]; "omit" for platforms without a native runtime. */
function platform(value: unknown, reasons: Set<string>): [string, string] | "omit" | null {
  if (value === undefined) return ["*", "*"];
  if (!isObject(value) || Object.keys(value).some(k => k !== "os" && k !== "architecture")) {
    reasons.add("unsupported-platform-key");
    return null;
  }
  const mapped: string[] = [];
  for (const [key, mapping, omitted] of [["os", OS_MAP, OMITTED_OS], ["architecture", ARCH_MAP, OMITTED_ARCH]] as const) {
    const item = has(value, key) ? value[key] : "*";
    if (typeof item !== "string" || (item.length > 1 && item.startsWith("/") && item.endsWith("/"))) {
      reasons.add("platform-regex");
      return null;
    }
    if (omitted.has(item)) return "omit";
    if (!has(mapping, item)) {
      reasons.add("unsupported-platform-value");
      return null;
    }
    mapped.push(mapping[item]);
  }
  return [mapped[0], mapped[1]];
}

function base64Hex(text: unknown): string | null {
  if (typeof text !== "string" || !/^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text)) return null;
  const bytes = atob(text);
  return Array.from(bytes, c => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
}

function artifact(manifest: Record<string, unknown>, bindings: Record<string, ArtifactBinding>, reasons: Set<string>): [ArtifactBinding | null, string | null] {
  const artifacts = has(manifest, "Artifacts") ? manifest.Artifacts : [];
  if (!Array.isArray(artifacts) || artifacts.length !== 1) {
    reasons.add("artifact-count");
    return [null, null];
  }
  if (!isObject(artifacts[0])) {
    reasons.add("unsupported-artifact-key");
    return [null, null];
  }
  // An unknown key is reported with every other finding for this artifact.
  const [entry, unknown] = fold(artifacts[0], ARTIFACT_KEYS, reasons);
  if (unknown.length) reasons.add("unsupported-artifact-key");
  const uri = entry.Uri;
  const basename = typeof uri === "string" ? uri.replace(/\/+$/, "").split("/").pop()! : null;
  if ((has(entry, "Unarchive") ? entry.Unarchive : "NONE") !== "NONE") reasons.add("unsupported-unarchive");
  const declared = has(entry, "Permission") ? entry.Permission : {};
  const [permission, other] = isObject(declared) ? fold(declared, PERMISSION_KEYS, reasons) : [{}, [""]];
  if (other.length || (has(permission, "Read") ? permission.Read : "OWNER") !== "OWNER" || (has(permission, "Execute") ? permission.Execute : "NONE") !== "NONE") {
    reasons.add("unsupported-artifact-permission");
  }
  const binding = typeof uri === "string" && has(bindings, uri) ? bindings[uri] : null;
  if (!binding) {
    reasons.add("unbound-artifact");
    return [null, basename];
  }
  if (has(entry, "Digest") || has(entry, "Algorithm")) {
    if (entry.Algorithm !== "SHA-256" || base64Hex(has(entry, "Digest") ? entry.Digest : "") !== binding.artifactSha256.toLowerCase()) reasons.add("artifact-digest-mismatch");
  }
  return [binding, basename];
}

/** Native configuration limits (CONFIGURATION_LIMITS), counted as the native validators do. */
function withinLimits(document: Record<string, unknown>): boolean {
  let values = 0;
  const walk = (value: unknown, depth: number): boolean => {
    if (++values > CONFIGURATION_LIMITS.values || depth > CONFIGURATION_LIMITS.depth) return false;
    return value === null || typeof value !== "object" || Object.values(value).every(child => walk(child, depth + 1));
  };
  return walk(document, 1);
}

function caseCollision(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(caseCollision);
  if (!isObject(value)) return false;
  const keys = Object.keys(value).map(k => k.toLowerCase());
  return new Set(keys).size !== keys.length || Object.values(value).some(caseCollision);
}

/** The operator ACL equivalent of AWS pub/sub accessControl; everything else is listed unmapped. */
function ipcAcl(policies: unknown): IpcAclSuggestion {
  // Greengrass lets every component read its own configuration and report its state.
  const acl = { operations: new Set(["config", "health"]), publish: new Set<string>(), subscribe: new Set<string>(), unmapped: new Set<string>() };
  for (const [service, byId] of Object.entries(isObject(policies) ? policies : { accessControl: null })) {
    for (const [id, policy] of Object.entries(isObject(byId) ? byId : { "": null })) {
      const operations = isObject(policy) && Array.isArray(policy.operations) ? policy.operations : null;
      const resources = isObject(policy) && Array.isArray(policy.resources) ? policy.resources : null;
      if (service !== "aws.greengrass.ipc.pubsub" || !operations || !resources) {
        acl.unmapped.add(`${service}/${id}`);
        continue;
      }
      for (const operation of operations) {
        const targets = operation === "*" ? [acl.publish, acl.subscribe] : operation === PUBLISH ? [acl.publish] : operation === SUBSCRIBE ? [acl.subscribe] : [];
        if (!targets.length) acl.unmapped.add(`${service}/${id}/${String(operation)}`);
        for (const resource of resources) {
          // Native topics are exact strings: an AWS * glob has no equivalent.
          if (typeof resource !== "string" || !resource || resource.includes("*")) {
            acl.unmapped.add(`${service}/${id}/${String(resource)}`);
            continue;
          }
          for (const target of targets) target.add(resource);
        }
      }
    }
  }
  if (acl.publish.size) acl.operations.add("publish");
  if (acl.subscribe.size) acl.operations.add("receive");
  const sorted = (s: Set<string>) => [...s].sort();
  return { operations: sorted(acl.operations), publish: sorted(acl.publish), subscribe: sorted(acl.subscribe), unmapped: sorted(acl.unmapped) };
}

/** Translate one parsed AWS recipe with its operator-supplied artifact bindings. */
export function translateGreengrassRecipe(recipe: unknown, bindings: Record<string, ArtifactBinding>): Translation {
  const reasons = new Set<string>();
  const informational: string[] = [];
  const note = (field: string) => { if (!informational.includes(field)) informational.push(field); };
  if (!isObject(recipe)) return { outcome: "reject", reasons: ["invalid-recipe"] };
  if (Object.keys(recipe).some(k => !TOP_KEYS.has(k))) reasons.add("unknown-field");
  if (recipe.RecipeFormatVersion !== "2020-01-25") reasons.add("unsupported-format-version");
  if ((has(recipe, "ComponentType") ? recipe.ComponentType : "aws.greengrass.generic") !== "aws.greengrass.generic") reasons.add("unsupported-component-type");
  if (has(recipe, "ComponentSource")) reasons.add("lambda-source");
  if (has(recipe, "Lifecycle")) reasons.add("global-lifecycle-selections");
  const name = recipe.ComponentName, version = recipe.ComponentVersion;
  if (typeof name !== "string" || !NAME.test(name)) reasons.add("invalid-component-name");
  if (typeof version !== "string" || !SEMVER.test(version) || version.length > 128) reasons.add("invalid-component-version");
  if (has(recipe, "ComponentDescription") && typeof recipe.ComponentDescription !== "string") reasons.add("invalid-description");
  if (has(recipe, "ComponentPublisher")) note("ComponentPublisher");

  let defaults: Record<string, unknown> | undefined;
  let acl: IpcAclSuggestion | undefined;
  const config = has(recipe, "ComponentConfiguration") ? recipe.ComponentConfiguration : {};
  const declared = isObject(config) && has(config, "DefaultConfiguration") ? config.DefaultConfiguration : {};
  if (!isObject(config) || Object.keys(config).some(k => k !== "DefaultConfiguration") || !isObject(declared)) {
    reasons.add("invalid-configuration");
  } else if (has(declared, "accessControl")) {
    reasons.add("ipc-access-control");
    acl = ipcAcl(declared.accessControl);
  } else if (Object.keys(declared).length) {
    if (!withinLimits(declared)) reasons.add("configuration-limits");
    if (caseCollision(declared)) reasons.add("configuration-key-case-collision");
    defaults = declared;
    note("ComponentConfiguration.DefaultConfiguration");
  }

  const dependencies: Record<string, { versionRequirement: string }> = {};
  const managed: Record<string, ManagedClassification> = {};
  const required = has(recipe, "ComponentDependencies") ? recipe.ComponentDependencies : {};
  for (const [dependency, spec] of Object.entries(isObject(required) ? required : (reasons.add("invalid-dependency"), {}))) {
    if (!NAME.test(dependency) || dependency === name) { reasons.add("invalid-dependency"); continue; }
    if (MANAGED_PREFIXES.some(p => dependency.startsWith(p))) {
      const classification = classifyManagedDependency(dependency, spec);
      if (classification.reason === "invalid-dependency" || classification.reason === "unsupported-version-range") { reasons.add(classification.reason); continue; }
      define(managed, dependency, classification);
      // A native replacement is the daemon itself: the dependency is dropped and reported.
      if (classification.outcome === "replace") note(`ComponentDependencies.${dependency}`);
      else reasons.add("aws-managed-dependency");
      continue;
    }
    if (!isObject(spec) || Object.keys(spec).some(k => k !== "VersionRequirement" && k !== "DependencyType")) { reasons.add("invalid-dependency"); continue; }
    const requirement = spec.VersionRequirement;
    if (typeof requirement !== "string" || !RANGE.test(requirement)) { reasons.add("unsupported-version-range"); continue; }
    if (has(spec, "DependencyType") && spec.DependencyType !== "HARD" && spec.DependencyType !== "SOFT") { reasons.add("invalid-dependency"); continue; }
    // AWS installs and starts every dependency first, HARD or SOFT (SOFT only
    // drops restart coupling). Native SOFT may omit a dependency, so every
    // translated dependency is native HARD (the default); see the guidance.
    define(dependencies, dependency, { versionRequirement: requirement });
  }
  const dependent = Object.keys(dependencies).length > 0;
  if (dependent) note("ComponentDependencies");

  const manifests: NativeManifest[] = [];
  const omitted: number[] = [];
  const seen = new Set<string>();
  let raw = recipe.Manifests;
  if (!Array.isArray(raw) || !raw.length) {
    reasons.add("no-manifests");
    raw = [];
  }
  // Every manifest is checked in full, so one check reports every finding.
  (raw as unknown[]).forEach((manifest, index) => {
    if (!isObject(manifest)) {
      reasons.add("unsupported-manifest-key");
      return;
    }
    if (Object.keys(manifest).some(k => !["Name", "Platform", "Lifecycle", "Selections", "Artifacts"].includes(k))) reasons.add("unsupported-manifest-key");
    if (has(manifest, "Selections")) reasons.add("global-lifecycle-selections");
    const selected = platform(manifest.Platform, reasons);
    if (selected === "omit") { omitted.push(index); return; }
    const distinct = selected !== null && !seen.has(selected.join("/"));
    if (selected !== null && !distinct) reasons.add("duplicate-platform");
    if (distinct) seen.add(selected.join("/"));
    const [binding, basename] = artifact(manifest, bindings, reasons);
    let declared = has(manifest, "Lifecycle") ? manifest.Lifecycle : {};
    if (!isObject(declared)) { reasons.add("invalid-step"); declared = {}; }
    const [lifecycle, unknownSteps] = fold(declared as Record<string, unknown>, LIFECYCLE_KEYS, reasons);
    if (unknownSteps.length) reasons.add("unknown-lifecycle-step");
    const commands: Record<string, string> = {};
    for (const [stepName, value] of Object.entries(lifecycle)) {
      if (stepName === "Setenv") reasons.add("step-timeout-or-env");
      else if (MAPPED_STEPS.has(stepName)) {
        const command = step(stepName, value, basename, reasons);
        if (command !== null) commands[stepName] = command;
      } else {
        step(stepName, value, basename, reasons);
        reasons.add("unsupported-lifecycle-step");
      }
    }
    if (has(commands, "shutdown") && !has(commands, "run")) reasons.add("shutdown-without-run");
    // Without install or run the daemon hands the artifact to the platform updater.
    if (!has(commands, "install") && !has(commands, "run")) reasons.add(defaults ? "configuration-without-command" : "no-lifecycle-command");
    for (const stepName of ["install", "run", "shutdown"]) if (has(commands, stepName)) note(`Lifecycle.${stepName}`);
    if (!binding || selected === null || !distinct) return;
    const native: ComponentRecipe = {
      schemaVersion: defaults ? "1.2" : dependent ? "1.1" : "1.0",
      componentName: name as string,
      componentVersion: version as string,
      artifactBlobId: binding.artifactBlobId,
      artifactSha256: binding.artifactSha256,
      lifecycle: commands,
      configuration: defaults ? { defaults } : {},
    };
    if (has(recipe, "ComponentDescription")) native.description = recipe.ComponentDescription as string;
    if (dependent) native.componentDependencies = dependencies;
    manifests.push({ os: selected[0], arch: selected[1], recipe: native });
  });
  if ((raw as unknown[]).length && !manifests.length && !reasons.size) reasons.add("no-supported-platform");
  if (manifests.length > MAX_MANIFESTS) reasons.add("artifact-count");
  const extra = { ...(Object.keys(managed).length ? { managed_dependencies: managed } : {}), ...(acl ? { ipc_acl: acl } : {}) };
  if (reasons.size) return { outcome: "reject", reasons: [...reasons].sort(), ...extra };
  return { outcome: "translate", reasons: [], manifests, omitted_manifests: omitted, informational, ...extra };
}

/** Parse then translate a JSON or YAML recipe document. */
export function translateGreengrassText(text: string, bindings: Record<string, ArtifactBinding>): Translation {
  const parsed = parseGreengrassRecipe(text);
  return "recipe" in parsed ? translateGreengrassRecipe(parsed.recipe, bindings) : parsed;
}

/** Operator TOML for the suggested native IPC ACL (docs/COMPONENT_IPC.md); UID left to the platform. */
export function ipcAclToml(component: string, acl: IpcAclSuggestion): string {
  const list = (items: string[]) => `[${items.map(item => JSON.stringify(item)).join(", ")}]`;
  return [`[ipc.components.${JSON.stringify(component)}]`, "uid = 0 # set to the component's dedicated non-root Unix UID",
    `operations = ${list(acl.operations)}`, `publish = ${list(acl.publish)}`, `subscribe = ${list(acl.subscribe)}`].join("\n");
}
