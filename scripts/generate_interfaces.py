#!/usr/bin/env python3
"""Generate both client interfaces from a reviewed ABI and recipe schema.

--capture records the compiled Move ABI for deliberate review; normal runs
never accept contract changes automatically. --check checks both the compiled
ABI and every generated file, including field order, reference mutability,
generic arguments, return types and struct abilities.
"""
import argparse
import difflib
import json
from pathlib import Path
import re
import tempfile

from check_contract_interfaces import ROOT, summarize, read_summary

CATALOG = ROOT / "interfaces/fleet-v1.json"
RECIPE = ROOT / "interfaces/recipe.schema.json"
MANAGED = ROOT / "interfaces/compat/managed-components.json"


def managed_ts(header):
    """The SF-038 managed-component fields the SF-037 recipe translator classifies by."""
    managed = json.loads(MANAGED.read_text())
    components = [{key: row[key] for key in ("name", "disposition", "tickets", "adapter", "discontinued") if key in row}
                  for row in managed["components"]]
    adapters = [{key: row[key] for key in ("id", "specification", "implementation_ticket", "replaces", "depends_on")}
                for row in managed["adapters"]]
    catalog = {"components": components, "adapters": adapters}
    return header + "export const MANAGED_CATALOG = " + json.dumps(catalog, indent=2) + " as const;\n"


def move_type(value):
    if isinstance(value, str):
        return value
    if "NamedTypeParameter" in value:
        return value["NamedTypeParameter"]
    if "Reference" in value:
        mutable, inner = value["Reference"]
        return ("&mut " if mutable else "&") + move_type(inner)
    if "vector" in value:
        return "vector<" + move_type(value["vector"]) + ">"
    if "Datatype" not in value:
        raise ValueError(f"unsupported compiled Move type: {value!r}")
    datatype = value["Datatype"]
    module = datatype["module"]
    name = f'{module["address"]}::{module["name"]}::{datatype["name"]}'
    args = datatype["type_arguments"]
    return name + ("<" + ",".join(move_type(x["argument"]) for x in args) + ">" if args else "")


def normalize(modules):
    result = {}
    for module, summary in sorted(modules.items()):
        functions = {}
        for name, function in sorted(summary["functions"].items()):
            if function["visibility"] != "Public" and not function["entry"]:
                continue
            functions[name] = {
                "visibility": function["visibility"], "entry": function["entry"],
                "type_parameters": function["type_parameters"],
                "parameters": [move_type(p["type_"]) for p in function["parameters"]],
                "returns": [move_type(t) for t in function["return_"]],
            }
        structs = {}
        for name, struct in sorted(summary["structs"].items()):
            fields = struct["fields"]["fields"]
            structs[name] = {
                "abilities": sorted(struct["abilities"]),
                "type_parameters": struct["type_parameters"],
                "fields": [[name, move_type(field["type_"])]
                           for name, field in sorted(fields.items(), key=lambda x: x[1]["index"])],
            }
        if summary["enums"]:
            raise ValueError("enum layouts require explicit generator support")
        result[module] = {"functions": functions, "structs": structs}
    return result


def diff(expected, actual, label):
    if expected == actual:
        return []
    a, b = [json.dumps(v, indent=2, sort_keys=True).splitlines() for v in (expected, actual)]
    return [label + " drift:\n" + "\n".join(difflib.unified_diff(a, b, fromfile="reviewed", tofile="actual"))]


def generic(value):
    match = re.fullmatch(r"([^<]+)<(.+)>", value)
    if not match:
        return value, []
    base, body = match.groups()
    depth, args, start = 0, [], 0
    for index, char in enumerate(body):
        depth += (char == "<") - (char == ">")
        if char == "," and depth == 0:
            args.append(body[start:index]); start = index + 1
    return base, args + [body[start:]]


def bcs_type(value):
    base, args = generic(value)
    if base == "vector":
        return f"bcs.vector({bcs_type(args[0])})"
    if base == "std::option::Option":
        return f"bcs.option({bcs_type(args[0])})"
    if base in ["address", "sui::object::ID", "sui::object::UID"]:
        return "bcs.Address"
    if base == "std::string::String":
        return "bcs.string()"
    if base == "sui::vec_map::VecMap":
        return f'bcs.struct("VecMap", {{ contents: bcs.vector(bcs.struct("Entry", {{ key: {bcs_type(args[0])}, value: {bcs_type(args[1])} }})) }})'
    if base == "sui::table::Table":
        # Entries live in dynamic fields; the struct itself is its UID and size.
        return 'bcs.struct("Table", { id: bcs.Address, size: bcs.u64() })'
    if base == "sui::balance::Balance":
        return 'bcs.struct("Balance", { value: bcs.u64() })'
    if base == "std::option::Option":
        return f"bcs.option({bcs_type(args[0])})"
    if base in ["bool", "u8", "u16", "u32", "u64", "u128", "u256"]:
        return f"bcs.{base}()"
    if base.startswith("sui_edge::"):
        return base.split("::")[1] + "_" + base.split("::")[2] + "Bcs"
    raise ValueError(f"unsupported BCS type {value}")


def rust_type(value):
    base, args = generic(value)
    if base == "vector":
        return f"Vec<{rust_type(args[0])}>"
    if base == "std::option::Option":
        return f"Option<{rust_type(args[0])}>"
    if base in ["address", "sui::object::ID", "sui::object::UID"]:
        return "[u8; 32]"
    if base == "std::string::String":
        return "String"
    if base == "sui::vec_map::VecMap":
        return f"super::VecMap<{rust_type(args[0])}, {rust_type(args[1])}>"
    if base == "sui::table::Table":
        return "super::Table"
    if base == "sui::balance::Balance":
        return "super::Balance"
    if base == "std::option::Option":
        return f"Option<{rust_type(args[0])}>"
    if base in ["bool", "u8", "u16", "u32", "u64", "u128"]:
        return base
    if base.startswith("sui_edge::"):
        return "super::" + "::".join(base.split("::")[1:])
    raise ValueError(f"unsupported Rust wire type {value}")


def recipe_ts_type(schema):
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if isinstance(schema["type"], list):
        return " | ".join("null" if t == "null" else t for t in schema["type"])
    if schema["type"] in ("integer", "number"):
        return "number"
    if schema["type"] == "boolean":
        return "boolean"
    if schema["type"] == "string":
        return "string"
    if schema["type"] == "array":
        return f"{recipe_ts_type(schema['items'])}[]"
    if schema["type"] == "object":
        if schema.get("additionalProperties") is True:
            return "Record<string, unknown>"
        if isinstance(schema.get("additionalProperties"), dict):
            return f"Record<string, {recipe_ts_type(schema['additionalProperties'])}>"
        required = schema.get("required", [])
        return "{ " + " ".join(f"{name}{'' if name in required else '?'}: {recipe_ts_type(s)};" for name, s in schema.get("properties", {}).items()) + " }"
    raise ValueError("unsupported recipe field")


def recipe_rust_fields(schema, top=False):
    result = []
    for name, field in schema.get("properties", {}).items():
        rust_name = re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()
        if name == "description":
            rust_name = "_description"
        required = name in schema.get("required", [])
        if name == "services":
            datatype = "crate::services::Services"
        elif name == "componentDependencies":
            datatype = "std::collections::BTreeMap<String, ComponentDependency>"
        elif field["type"] == "object" and field.get("additionalProperties") is True:
            datatype = "serde_json::Map<String, serde_json::Value>"
        elif field["type"] == "object":
            datatype = {"configuration": "Configuration", "lifecycle": "Lifecycle"}[name]
            # serde would also read these structs from a JSON array.
            result += ['    #[serde(deserialize_with = "object")]']
        elif field["type"] == "array" and field["items"]["type"] == "string":
            datatype = "Vec<String>"
        elif field["type"] == "string" or field["type"] == ["string", "null"]:
            datatype = "String"
        else:
            raise ValueError("unsupported recipe field")
        result += [f'    #[serde(rename = "{name}")]']
        if not required:
            # As in the schema, only a nullable field may be null when present.
            nullable = isinstance(field["type"], list) and "null" in field["type"]
            result += ["    #[serde(default)]" if nullable else '    #[serde(default, deserialize_with = "present")]']
            datatype = f"Option<{datatype}>"
        result += [f"    pub {rust_name}: {datatype},"]
    return result


TS_VALIDATOR = '''
type Schema = { $schema?: string; $id?: string; type: string | string[]; const?: string | number | null; minimum?: number; maximum?: number; enum?: string[]; pattern?: string; maxLength?: number; items?: Schema; maxItems?: number; required?: string[]; properties?: Record<string, Schema>; propertyNames?: Schema; additionalProperties?: boolean | Schema };
function validate(schema: Schema, value: unknown, path: string): void {
  const type = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
  if (schema.type === "integer" && (!Number.isSafeInteger(value))) throw new Error(`${path}: expected integer`);
  if (schema.minimum !== undefined && (value as number) < schema.minimum) throw new Error(`${path}: below minimum`);
  if (schema.maximum !== undefined && (value as number) > schema.maximum) throw new Error(`${path}: above maximum`);
  if (!(Array.isArray(schema.type) ? schema.type : [schema.type]).includes(schema.type === "integer" && type === "number" ? "integer" : type)) throw new Error(`${path}: invalid type`);
  if (Object.prototype.hasOwnProperty.call(schema, "const") && value !== schema.const) throw new Error(`${path}: unsupported version`);
  if (schema.enum && !schema.enum.includes(value as string)) throw new Error(`${path}: unsupported value`);
  if (schema.pattern && (typeof value !== "string" || new RegExp(schema.pattern).exec(value)?.[0] !== value)) throw new Error(`${path}: invalid encoding`);
  if (schema.maxLength !== undefined && (value as string).length > schema.maxLength) throw new Error(`${path}: too long`);
  if (schema.type === "array") {
    const items = value as unknown[];
    if (schema.maxItems !== undefined && items.length > schema.maxItems) throw new Error(`${path}: too many items`);
    items.forEach((item, index) => validate(schema.items as Schema, item, `${path}[${index}]`));
  }
  if (schema.type === "object") {
    const object = value as Record<string, unknown>;
    for (const name of schema.required || []) if (!(name in object)) throw new Error(`${path}.${name}: required`);
    for (const [name, child] of Object.entries(object)) {
      if (schema.propertyNames) validate(schema.propertyNames, name, `${path}[${JSON.stringify(name)}]`);
      const property = schema.properties?.[name];
      if (property) {
        if (child !== undefined || (schema.required || []).includes(name)) validate(property, child, `${path}.${name}`);
      } else if (schema.additionalProperties === false) throw new Error(`${path}.${name}: unknown field`);
      else if (typeof schema.additionalProperties === "object") validate(schema.additionalProperties, child, `${path}.${name}`);
    }
  }
}
export function assertComponentRecipe(value: unknown): asserts value is ComponentRecipe {
  validate(RECIPE_SCHEMA, value, "recipe");
  // Dependencies were introduced by schema 1.1; a 1.0 consumer would reject the field.
  const recipe = value as ComponentRecipe;
  if (recipe.schemaVersion === "1.0" && recipe.componentDependencies !== undefined) throw new Error("recipe.componentDependencies: requires schemaVersion 1.1");
  if (recipe.services !== undefined && recipe.schemaVersion !== "1.3") throw new Error("recipe.services: requires schemaVersion 1.3");
  assertRecipeConfiguration(recipe);
}
/** Configuration rules shared with the daemon (suifleet-edge/src/configuration.rs). */
export const CONFIGURATION_LIMITS = { depth: 32, values: 4096 } as const;
function assertRecipeConfiguration(recipe: ComponentRecipe): void {
  const configuration = recipe.configuration as { defaults?: Record<string, unknown>; merge?: Record<string, unknown>; reset?: string[]; confidential?: string[] };
  const used = Object.keys(configuration).length > 0 || (recipe.lifecycle as { validate?: unknown }).validate !== undefined;
  // Older daemons reject these fields: they need a 1.2 consumer.
  if (used && !["1.2", "1.3"].includes(recipe.schemaVersion)) throw new Error("recipe.configuration: requires schemaVersion 1.2");
  if (used && !recipe.lifecycle.install && !recipe.lifecycle.run) throw new Error("recipe.configuration: needs an install or run command to receive it");
  for (const name of ["defaults", "merge"] as const) {
    const document = configuration[name];
    if (document === undefined) continue;
    let values = 0;
    const walk = (value: unknown, depth: number): void => {
      if (++values > CONFIGURATION_LIMITS.values || depth > CONFIGURATION_LIMITS.depth) throw new Error(`recipe.configuration.${name}: exceeds ${CONFIGURATION_LIMITS.values} values or depth ${CONFIGURATION_LIMITS.depth}`);
      if (value !== null && typeof value === "object") for (const child of Object.values(value)) walk(child, depth + 1);
    };
    walk(document, 1);
    // Recipes are public: a confidential value only ever comes from the device.
    for (const pointer of configuration.confidential || []) {
      let node: unknown = document;
      for (const token of pointer.split("/").slice(1).map(t => t.replace(/~1/g, "/").replace(/~0/g, "~"))) {
        if (node === null || typeof node !== "object" || Array.isArray(node)) throw new Error(`recipe.configuration.${name}: confidential ${pointer} is inside a non-object value`);
        if (!Object.prototype.hasOwnProperty.call(node, token)) { node = undefined; break; }
        node = (node as Record<string, unknown>)[token];
      }
      if (node !== undefined) throw new Error(`recipe.configuration.${name}: confidential ${pointer} must not have a value in a public recipe`);
    }
  }
}
'''


SETTINGS_FIELDS = ["check_interval_ms", "rollout_check_interval_ms", "health_interval_ms", "archive_interval_ms", "archive_retention_epochs"]


def settings_rust(settings):
    """Shared SF-130 defaults and inclusive bounds; Move enforces the same values."""
    lines = ["/// SF-130 per-fleet cost settings: defaults and inclusive bounds (Move field units).",
             "pub mod fleet_settings_values {"]
    for field in SETTINGS_FIELDS:
        kind = "u32" if field == "archive_retention_epochs" else "u64"
        low, high = settings["bounds"][field]
        lines += [f"    pub const DEFAULT_{field.upper()}: {kind} = {settings['defaults'][field]};",
                  f"    pub const MIN_{field.upper()}: {kind} = {low};",
                  f"    pub const MAX_{field.upper()}: {kind} = {high};"]
    return lines + ["}"]


def settings_move(settings):
    """Move acceptance of the client defaults and bounds; each bound must be accepted."""
    defaults = [settings["defaults"][f] for f in SETTINGS_FIELDS]
    lines = ["    #[test]", "    fun fleet_settings_values_match_clients() {",
             "        let d = sui_edge::fleet_settings::defaults();",
             *[f"        assert!(sui_edge::fleet_settings::{field}(&d) == {value}, {index});" for index, (field, value) in enumerate(zip(SETTINGS_FIELDS, defaults))]]
    for index, field in enumerate(SETTINGS_FIELDS):
        for bound in settings["bounds"][field]:
            values = list(defaults)
            values[index] = bound
            if field == "check_interval_ms":
                # A fast check never exceeds the routine check.
                values[1] = min(values[1], bound)
            lines.append(f"        sui_edge::fleet_settings::new({', '.join(str(v) for v in values)});")
    if settings["rollout_check_off"] is not None:
        values = list(defaults); values[1] = settings["rollout_check_off"]
        lines.append(f"        sui_edge::fleet_settings::new({', '.join(str(v) for v in values)});")
    return lines + ["    }"]


def generate(catalog, recipe):
    abi = catalog["modules"]
    header = "// Generated by scripts/generate_interfaces.py; edit interfaces/, then regenerate.\n"
    metadata = [header,
          f'export const PROTOCOL_VERSION = {catalog["protocol_version"]} as const;',
          f'export const MANIFEST_SCHEMA_VERSION = {catalog["manifest_schema_version"]} as const;',
          f'export const DEPLOYMENT_SCHEMA_VERSION = {catalog["deployment_schema_version"]} as const;',
          f'export const SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS = {json.dumps(catalog["supported_deployment_schema_versions"])} as const;',
          f'export const CATALOG_TYPES = {json.dumps(catalog["optional_type_origins"]["component_catalog"])} as const;',
          f'export const HANDOVER_TYPES = {json.dumps(catalog["optional_type_origins"]["fleet_handover"])} as const;',
          f'export const SUBSCRIPTION_TYPES = {json.dumps(catalog["optional_type_origins"]["subscription"])} as const;',
          f'export const GROUP_BATCH_TYPES = {json.dumps(catalog["optional_type_origins"]["group_batch"])} as const;',
          f'export const SEAL_POLICY_TYPES = {json.dumps(catalog["optional_type_origins"]["seal_policy"])} as const;',
          f'export const BUDGET_TYPES = {json.dumps(catalog["optional_type_origins"]["fleet_budget"])} as const;',
          f'export const CUSTOMER_TYPES = {json.dumps(catalog["optional_type_origins"]["customer"])} as const;',
          f'export const INBOX_TYPES = {json.dumps(catalog["optional_type_origins"]["fleet_inbox"])} as const;',
          f'export const ROLLOUT_HEALTH_TYPES = {json.dumps(catalog["optional_type_origins"]["rollout_health"])} as const;',
          f'export const SETTINGS_TYPES = {json.dumps(catalog["optional_type_origins"]["fleet_settings"])} as const;',
          f'export const SHARD_TYPES = {json.dumps(catalog["optional_type_origins"]["receipt_shards"])} as const;',
          '/** SF-130 per-fleet cost settings: defaults and inclusive bounds, in Move field units. */',
          f'export const FLEET_SETTINGS = {json.dumps(catalog["fleet_settings"])} as const;',
          f'export const STATUS = {json.dumps(catalog["statuses"])} as const;',
          f'export const RECIPE_VERSION = {json.dumps(recipe["properties"]["schemaVersion"]["enum"][0])} as const;',
          f'export const RECIPE_VERSIONS = {json.dumps(recipe["properties"]["schemaVersion"]["enum"])} as const;',
          f'export const REQUIRED_TYPES = {json.dumps(catalog["required_type_origins"])} as const;']
    ts = [header, 'import { bcs } from "@mysten/sui/bcs";', 'import { Transaction } from "@mysten/sui/transactions";',
          'export * from "./protocol-version";',
          ]
    rust = [header, "#![allow(dead_code)]", "use serde::{Deserialize, Serialize};",
            f'pub const PROTOCOL_VERSION: u64 = {catalog["protocol_version"]};',
            f'pub const MANIFEST_SCHEMA_VERSION: u64 = {catalog["manifest_schema_version"]};',
            f'pub const DEPLOYMENT_SCHEMA_VERSION: u64 = {catalog["deployment_schema_version"]};',
            f'pub const SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS: &[u64] = &{json.dumps(catalog["supported_deployment_schema_versions"])};',
            'pub const SUBSCRIPTION_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["subscription"]) + '];',
            'pub const CATALOG_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["component_catalog"]) + '];',
            'pub const SEAL_POLICY_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["seal_policy"]) + '];',
            'pub const BUDGET_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["fleet_budget"]) + '];',
            'pub const CUSTOMER_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["customer"]) + '];',
            'pub const INBOX_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["fleet_inbox"]) + '];',
            'pub const ROLLOUT_HEALTH_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["rollout_health"]) + '];',
            'pub const SETTINGS_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["fleet_settings"]) + '];',
            'pub const SHARD_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["optional_type_origins"]["receipt_shards"]) + '];',
            *settings_rust(catalog["fleet_settings"]),
            f'pub const RECIPE_VERSION: &str = {json.dumps(recipe["properties"]["schemaVersion"]["enum"][0])};',
            'pub const RECIPE_VERSIONS: &[&str] = &[' + ', '.join(json.dumps(v) for v in recipe["properties"]["schemaVersion"]["enum"]) + '];',
            *[f"pub const STATUS_{name.upper()}: u8 = {value};" for name, value in catalog["statuses"].items()],
            'pub const REQUIRED_TYPES: &[&str] = &[' + ', '.join(json.dumps(t) for t in catalog["required_type_origins"]) + '];',
            'pub const MOVE_ABI_JSON: &str = include_str!("../../interfaces/fleet-v1.json");',
            '#[derive(Debug, Clone, Deserialize, Serialize)]\npub struct Entry<K, V> { pub key: K, pub value: V }',
            '#[derive(Debug, Clone, Deserialize, Serialize)]\npub struct VecMap<K, V> { pub contents: Vec<Entry<K, V>> }',
            '/// sui::table::Table: entries are dynamic fields; the struct is its UID and size.',
            '#[derive(Debug, Clone, Deserialize, Serialize)]\npub struct Table { pub id: [u8; 32], pub size: u64 }',
            '/// sui::balance::Balance<T>: its value.',
            '#[derive(Debug, Clone, Deserialize, Serialize)]\npub struct Balance { pub value: u64 }']
    calls = []
    move_types = []
    for module, contents in abi.items():
        for name, function in contents["functions"].items():
            # Generic witness values are constructed by their defining Move
            # module, not serialized as browser pure inputs. Keep their exact
            # type constraints in the reviewed ABI; omit unusable JS builders.
            if function["type_parameters"]:
                continue
            if any(t.startswith("&") for t in function["returns"]):
                continue
            params = [p for p in function["parameters"] if p not in ["&sui::tx_context::TxContext", "&mut sui::tx_context::TxContext"]]
            arguments, types = [], []
            for index, datatype in enumerate(params):
                arg = f"a{index}"
                base = datatype.removeprefix("&mut ").removeprefix("&")
                if base.startswith("sui_edge::") or base == "sui::clock::Clock" or base.startswith("sui::coin::Coin<"):
                    types += [f'{arg}: Parameters<Transaction["object"]>[0]']
                    arguments += [f"tx.object({arg})"]
                elif base == "std::string::String":
                    types += [f"{arg}: string"]; arguments += [f"tx.pure.string({arg})"]
                elif base == "std::option::Option<std::string::String>":
                    types += [f"{arg}: string | null"]; arguments += [f'tx.pure.option("string", {arg})']
                elif base in ["address", "sui::object::ID"]:
                    types += [f"{arg}: string"]; arguments += [f"tx.pure.address({arg})"]
                elif base == "vector<std::string::String>":
                    types += [f"{arg}: string[]"]; arguments += [f"tx.pure(bcs.vector(bcs.string()).serialize({arg}))"]
                elif base in ["vector<sui::object::ID>", "vector<address>"]:
                    types += [f"{arg}: string[]"]; arguments += [f"tx.pure(bcs.vector(bcs.Address).serialize({arg}))"]
                elif base == "vector<u64>":
                    types += [f"{arg}: (number | bigint | string)[]"]; arguments += [f"tx.pure(bcs.vector(bcs.u64()).serialize({arg}))"]
                elif base == "u8":
                    types += [f"{arg}: number"]; arguments += [f"tx.pure.u8({arg})"]
                elif base == "u32":
                    types += [f"{arg}: number"]; arguments += [f"tx.pure.u32({arg})"]
                elif base == "bool":
                    types += [f"{arg}: boolean"]; arguments += [f"tx.pure.bool({arg})"]
                elif base == "vector<u8>":
                    types += [f"{arg}: Uint8Array | number[]"]; arguments += [f'tx.pure.vector("u8", {arg})']
                elif base == "u64":
                    types += [f"{arg}: number | bigint | string"]; arguments += [f"tx.pure.u64({arg})"]
                else:
                    raise ValueError(f"unsupported transaction parameter {datatype}")
            calls += [f'  {module}_{name}: (tx: Transaction, packageId: string{", " if types else ""}{", ".join(types)}) => tx.moveCall({{ target: `${{packageId}}::{module}::{name}`, arguments: [{", ".join(arguments)}] }}),']
        rust += [f"pub mod {module} {{", "    use super::*;", f'    pub const MODULE: &str = "{module}";']
        rust += [f'    pub const {name.upper()}: &str = "{name}";' for name in contents["functions"]]
        # BCS constants are evaluated eagerly. Emit nested local structs before
        # their users even when the reviewed ABI is alphabetically ordered.
        ordered = []
        visiting = set()
        def emit_struct(name):
            if name in ordered:
                return
            if name in visiting:
                raise ValueError("recursive struct layout")
            visiting.add(name)
            for _, datatype in contents["structs"][name]["fields"]:
                for dependency in re.findall(r"sui_edge::" + module + r"::([A-Za-z_][A-Za-z_0-9]*)", datatype):
                    emit_struct(dependency)
            visiting.remove(name)
            ordered.append(name)
        for name in contents["structs"]:
            emit_struct(name)
        for name in ordered:
            struct = contents["structs"][name]
            # Phantom coin parameters do not affect BCS; Balance<T> stores only u64.
            if any(not parameter["phantom"] for parameter in struct["type_parameters"]):
                raise ValueError("nonphantom generic contract structs require explicit support")
            fields = ", ".join(f"{name}: {bcs_type(t)}" for name, t in struct["fields"])
            ts += [f'export const {module}_{name}Bcs = bcs.struct("{name}", {{ {fields} }});']
            move_types += [f'  {module}_{name}: "{module}::{name}",']
            rust += ["    #[derive(Debug, Clone, Deserialize, Serialize)]", f"    pub struct {name} {{",
                     *[f"        pub {field}: {rust_type(t)}," for field, t in struct["fields"]], "    }"]
            rust += [f'    impl {name} {{ pub const MOVE_TYPE: &\'static str = "{module}::{name}"; }}']
        rust += ["}"]
    ts += ["export const calls = {", *calls, "};", "export const TYPES = {", *move_types, "} as const;"]
    # JSON event used by the existing daemon; wire structs above serve BCS transports.
    event = abi["deployment_manager"]["structs"]["DeploymentCreated"]
    rust += ["#[derive(Debug, Clone, Deserialize)]", "pub struct DeploymentCreatedEvent {"]
    for name, datatype in event["fields"]:
        t = "Vec<String>" if datatype.startswith("vector<") else "String" if datatype in ["address", "sui::object::ID", "std::string::String"] else rust_type(datatype)
        rust += [f"    pub {name}: {t},"]
    rust += ["}", "/// A present optional field; null is rejected like any other wrong type.",
             "fn present<'de, D: serde::Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<Option<T>, D::Error> { T::deserialize(d).map(Some) }",
             "/// A JSON object only, as the schema says; never an array of fields.",
             "fn object<'de, D: serde::Deserializer<'de>, T: serde::de::DeserializeOwned>(d: D) -> Result<T, D::Error> {",
             "    let value = serde_json::Value::deserialize(d)?;",
             "    if !value.is_object() { return Err(serde::de::Error::custom(\"expected a JSON object\")); }",
             "    serde_json::from_value(value).map_err(serde::de::Error::custom)",
             "}",
             "#[derive(Debug, Deserialize)]", "#[serde(deny_unknown_fields)]", "pub struct Recipe {", *recipe_rust_fields(recipe), "}",
             "#[derive(Debug, Default, Deserialize)]", "#[serde(deny_unknown_fields)]", "pub struct Lifecycle {", *recipe_rust_fields(recipe["properties"]["lifecycle"]), "}",
             "#[derive(Debug, Clone, Default, Deserialize)]", "#[serde(deny_unknown_fields)]", "pub struct Configuration {", *recipe_rust_fields(recipe["properties"]["configuration"]), "}",
             "#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]", "#[serde(deny_unknown_fields)]", "pub struct ComponentDependency {",
             *recipe_rust_fields(recipe["properties"]["componentDependencies"]["additionalProperties"]), "}"]
    recipe_ts = [header, f"export type ComponentRecipe = {recipe_ts_type(recipe)};",
                 "const RECIPE_SCHEMA: Schema = " + json.dumps(recipe, indent=2) + ";", TS_VALIDATOR]
    move = ["// Generated protocol-value acceptance; not a production entry point.", "#[test_only]", "module sui_edge::protocol_value_tests {",
            "    #[test]", "    fun status_values_match_clients() {", *[f"        assert!(sui_edge::deployment_manager::status_{name.lower()}() == {value}, {index});" for index, (name,value) in enumerate(catalog["statuses"].items())], "    }",
            *settings_move(catalog["fleet_settings"]), "}"]
    return {
        ROOT / "dapp/src/lib/fleet-abi.ts": header + "export const MOVE_ABI = " + json.dumps(abi, indent=2) + " as const;\n",
        ROOT / "dapp/src/lib/fleet-protocol.ts": "\n".join(ts) + "\n",
        ROOT / "dapp/src/lib/recipe-schema.ts": "\n".join(recipe_ts).rstrip() + "\n",
        ROOT / "dapp/src/lib/protocol-version.ts": "\n".join(metadata) + "\n",
        ROOT / "dapp/src/lib/greengrass-managed.ts": managed_ts(header),
        ROOT / "suifleet-edge/src/protocol.rs": "\n".join(rust) + "\n",
        ROOT / "contracts/tests/protocol_value_tests.move": "\n".join(move) + "\n",
    }


def check(catalog, recipe, modules):
    errors = diff(catalog["modules"], normalize(modules), "compiled Move ABI")
    for name in catalog["required_type_origins"]:
        module, struct = name.split("::")
        if struct not in modules.get(module, {}).get("structs", {}):
            errors.append(f"required type origin missing from compiled ABI: {name}")
    for path, text in generate(catalog, recipe).items():
        if not path.exists() or path.read_text() != text:
            errors.append(f"generated client drift: {path.relative_to(ROOT)}; run make generate-interfaces")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--summary-dir", type=Path)
    args = parser.parse_args()
    if args.summary_dir:
        modules, _ = read_summary(args.summary_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="fleet-abi-") as directory:
            modules, _ = summarize(ROOT / "contracts", Path(directory))
    if args.capture:
        if args.check:
            parser.error("--capture and --check are mutually exclusive")
        if not CATALOG.exists():
            parser.error("bootstrap version/compatibility metadata in interfaces/fleet-v1.json before capturing ABI")
        catalog = json.loads(CATALOG.read_text())
        catalog["modules"] = normalize(modules)
        CATALOG.write_text(json.dumps(catalog, indent=2) + "\n")
    catalog, recipe = json.loads(CATALOG.read_text()), json.loads(RECIPE.read_text())
    if catalog["interface_version"] != 1:
        parser.error("unsupported interface catalog version")
    errors = diff(catalog["modules"], normalize(modules), "compiled Move ABI")
    if not args.check and not errors:
        for path, text in generate(catalog, recipe).items():
            path.write_text(text)
    else:
        errors += check(catalog, recipe, modules) if not errors else []
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"PASS: {len(modules)} compiled Move modules and generated TypeScript/Rust/recipe/status interfaces")


if __name__ == "__main__":
    main()
