#!/usr/bin/env python3
"""SF-028 Greengrass compatibility specification checker.

Validates interfaces/compat/greengrass-compat.json and the SF-038 managed
component catalog (scripts/greengrass_managed.py), and evaluates every
fixture case against the *specification reference models* below. These models
define the proposed adapter semantics precisely enough to make the fixtures
executable and self-consistent. They are not the daemon, the SF-037 recipe
translator or any IPC/runtime adapter, and passing them proves no runtime
conformance. Runtime execution of each suite stays "pending" in the matrix
until the owning implementation runs the same fixtures.

Usage: python3 scripts/greengrass_compat.py [--root DIR]
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import greengrass_managed as managed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPAT = Path("interfaces/compat")

# ---------------------------------------------------------------------------
# AWS Greengrass V2 recipe -> native SuiFleet catalog manifests: the SF-037
# subset against the merged native recipe contract, including the SF-014
# supervisor, SF-015 configuration defaults and the SF-038 managed-dependency
# classification. The SF-037 translator (dapp/src/lib/greengrass-recipe.ts)
# implements the same rules; scripts/test_greengrass_compat.cjs executes these
# fixtures against it. Outcomes: translate or reject (every reason).
# ---------------------------------------------------------------------------
NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
NUM = r"(0|[1-9][0-9]*)"
IDENT = r"(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(rf"^{NUM}\.{NUM}\.{NUM}(-{IDENT}(\.{IDENT})*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")
CORE = rf"{NUM}\.{NUM}\.{NUM}(-{IDENT}(\.{IDENT})*)?"
COMPARATOR = rf"(>=|<=|>|<|=|\^|~)?{CORE}"
# Same grammar as interfaces/recipe.schema.json componentDependencies.
RANGE = re.compile(rf"^(\*|{COMPARATOR}( {COMPARATOR})*)$")
TOP_KEYS = {
    "RecipeFormatVersion", "ComponentName", "ComponentVersion", "ComponentDescription",
    "ComponentPublisher", "ComponentConfiguration", "ComponentDependencies",
    "ComponentType", "ComponentSource", "Manifests", "Lifecycle",
}
STEP_KEYS = {"Script", "RequiresPrivilege", "Skipif", "Timeout", "Setenv", "BootstrapOnRollback"}
# Native SF-014 lifecycle commands that keep their meaning.
MAPPED_STEPS = ("install", "run", "shutdown")
# No native equivalent on the merged base.
UNSUPPORTED_STEPS = {"bootstrap", "startup", "recover", "uninstall"}
OS_MAP = {"linux": "linux", "*": "*"}
OMITTED_OS = {"windows", "darwin"}
ARCH_MAP = {"amd64": "x86_64", "aarch64": "aarch64", "arm": "arm", "*": "*"}
OMITTED_ARCH = {"x86"}
VARIABLE = re.compile(r"\{([^{}]*)\}")
# Literal text the native daemon would substitute (shell-quoted).
NATIVE_PLACEHOLDERS = ("{artifact}", "{artifacts}", "{work}", "{pid}", "{configuration}")
# Environment the Greengrass nucleus sets; the native daemon clears the environment.
AWS_ENVIRONMENT = re.compile(r"\$\{?(SVCUID|AWS_GG_[A-Z0-9_]*|AWS_IOT_THING_NAME|AWS_REGION|AWS_DEFAULT_REGION|GG_ROOT_CA_PATH"
                             r"|AWS_CONTAINER_CREDENTIALS_FULL_URI|AWS_CONTAINER_AUTHORIZATION_TOKEN)(?![A-Za-z0-9_])")
MAX_MANIFESTS = 16
# Native configuration limits (suifleet-edge/src/configuration.rs).
CONFIG_VALUES, CONFIG_DEPTH = 4096, 32
PUBLISH, SUBSCRIBE = "aws.greengrass#PublishToTopic", "aws.greengrass#SubscribeToTopic"
# Reasons decided while parsing JSON/YAML text, before translation.
PARSE_REASONS = {"invalid-recipe", "invalid-yaml", "duplicate-key", "multiple-documents", "yaml-ambiguous-scalar", "yaml-non-json-value"}


def _script(step_name: str, script: str, basename: str, reasons: set) -> str:
    """Rewrite AWS recipe variables to native substitutions where meaning is identical."""
    if any(p in script for p in NATIVE_PLACEHOLDERS):
        reasons.add("native-placeholder-collision")
    if AWS_ENVIRONMENT.search(script):
        reasons.add("aws-environment-variable")
    variables = [body for body in VARIABLE.findall(script) if ":" in body]
    if variables and set(script) & {"'", '"', "\\"}:
        # Native substitutions are shell-quoted; inside quotes they change meaning.
        reasons.add("quoted-variable")
    exact = re.compile(r"\{artifacts:path\}/" + re.escape(basename) + r"(?![A-Za-z0-9._-])") if basename else None
    if exact:
        script = exact.sub("{artifact}", script)
    if step_name != "install":
        script = script.replace("{work:path}", "{work}")
    for body in VARIABLE.findall(script):
        if body in ("artifact", "artifacts", "work", "pid", "configuration"):
            continue
        parts = body.split(":")
        if len(parts) == 3:
            reasons.add("dependency-variable")
        elif len(parts) != 2:
            continue  # not a recipe variable (for example shell ${VAR} or JSON)
        elif parts[0] == "configuration":
            reasons.add("configuration-variable")
        elif parts[0] == "artifacts":
            reasons.add("unmappable-artifact-path")
        elif parts[0] == "work":
            reasons.add("work-path-in-install")
        elif parts[0] in ("iot", "kernel"):
            reasons.add("aws-runtime-variable")
        else:
            reasons.add("unknown-variable")
    return script


def _step(step_name: str, step, basename: str, reasons: set):
    """Return the translated command for one lifecycle step, or None."""
    if isinstance(step, str):
        script = step
    elif isinstance(step, dict):
        if set(step) - STEP_KEYS or "Skipif" in step or "BootstrapOnRollback" in step:
            reasons.add("unsupported-step-key")
        if "RequiresPrivilege" in step and step["RequiresPrivilege"] is not False:
            reasons.add("privileged-step")
        if "Timeout" in step or "Setenv" in step:
            reasons.add("step-timeout-or-env")
        script = step.get("Script")
        if not isinstance(script, str):
            reasons.add("invalid-step")
            return None
    else:
        reasons.add("invalid-step")
        return None
    # The native validators accept only a nonempty install or run command.
    if not script.strip(" \t\n\r"):
        reasons.add("empty-step")
        return None
    return _script(step_name, script, basename, reasons)


def _platform(platform, reasons: set):
    """Map an AWS Platform to (os, arch); None when omitted as unsupported."""
    if platform is None:
        return ("*", "*")
    if not isinstance(platform, dict) or set(platform) - {"os", "architecture"}:
        reasons.add("unsupported-platform-key")
        return None
    values = []
    for key, mapping, omitted in (("os", OS_MAP, OMITTED_OS), ("architecture", ARCH_MAP, OMITTED_ARCH)):
        value = platform.get(key, "*")
        if not isinstance(value, str) or (value.startswith("/") and value.endswith("/") and len(value) > 1):
            reasons.add("platform-regex")
            return None
        if value in omitted:
            return "omit"
        if value not in mapping:
            reasons.add("unsupported-platform-value")
            return None
        values.append(mapping[value])
    return tuple(values)


def _artifact(manifest: dict, bindings: dict, reasons: set):
    artifacts = manifest.get("Artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        reasons.add("artifact-count")
        return None, None
    artifact = artifacts[0]
    if not isinstance(artifact, dict) or set(artifact) - {"Uri", "Unarchive", "Permission", "Digest", "Algorithm"}:
        reasons.add("unsupported-artifact-key")
        return None, None
    uri = artifact.get("Uri")
    basename = uri.rstrip("/").rsplit("/", 1)[-1] if isinstance(uri, str) else None
    if artifact.get("Unarchive", "NONE") != "NONE":
        reasons.add("unsupported-unarchive")
    permission = artifact.get("Permission", {})
    if not isinstance(permission, dict) or set(permission) - {"Read", "Execute"} or permission.get("Read", "OWNER") != "OWNER" or permission.get("Execute", "NONE") != "NONE":
        reasons.add("unsupported-artifact-permission")
    binding = bindings.get(uri) if isinstance(uri, str) else None
    if binding is None:
        reasons.add("unbound-artifact")
        return None, basename
    if "Digest" in artifact or "Algorithm" in artifact:
        try:
            digest = base64.b64decode(artifact.get("Digest", ""), validate=True).hex()
        except (ValueError, TypeError):
            digest = None
        if artifact.get("Algorithm") != "SHA-256" or digest != binding["artifactSha256"].lower():
            reasons.add("artifact-digest-mismatch")
    return binding, basename


def _within_limits(document) -> bool:
    count = 0

    def walk(value, depth):
        nonlocal count
        count += 1
        if count > CONFIG_VALUES or depth > CONFIG_DEPTH:
            return False
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        return all(walk(child, depth + 1) for child in children)
    return walk(document, 1)


def _case_collision(value) -> bool:
    """Greengrass configuration keys are case-insensitive; native keys are not."""
    if isinstance(value, list):
        return any(_case_collision(item) for item in value)
    if not isinstance(value, dict):
        return False
    keys = [key.lower() for key in value]
    return len(set(keys)) != len(keys) or any(_case_collision(child) for child in value.values())


def _ipc_acl(policies) -> dict:
    """Operator IPC ACL equivalent of AWS pub/sub accessControl (never applied by a recipe)."""
    operations, publish, subscribe, unmapped = {"config", "health"}, set(), set(), set()
    for service, by_id in (policies.items() if isinstance(policies, dict) else [("accessControl", None)]):
        for policy_id, policy in (by_id.items() if isinstance(by_id, dict) else [("", None)]):
            ops = policy.get("operations") if isinstance(policy, dict) else None
            resources = policy.get("resources") if isinstance(policy, dict) else None
            if service != "aws.greengrass.ipc.pubsub" or not isinstance(ops, list) or not isinstance(resources, list):
                unmapped.add(f"{service}/{policy_id}")
                continue
            for operation in ops:
                targets = [publish, subscribe] if operation == "*" else [publish] if operation == PUBLISH else [subscribe] if operation == SUBSCRIBE else []
                if not targets:
                    unmapped.add(f"{service}/{policy_id}/{operation}")
                for resource in resources:
                    # Native topics are exact strings: an AWS * glob has no equivalent.
                    if not isinstance(resource, str) or not resource or "*" in resource:
                        unmapped.add(f"{service}/{policy_id}/{resource}")
                        continue
                    for target in targets:
                        target.add(resource)
    if publish:
        operations.add("publish")
    if subscribe:
        operations.add("receive")
    return {"operations": sorted(operations), "publish": sorted(publish), "subscribe": sorted(subscribe), "unmapped": sorted(unmapped)}


def translate_recipe(recipe, bindings: dict, catalog: dict) -> dict:
    """Classify/translate one parsed AWS recipe (JSON or YAML text parses to the same value)."""
    reasons: set = set()
    informational: list = []

    def note(field):
        if field not in informational:
            informational.append(field)
    if not isinstance(recipe, dict):
        return {"outcome": "reject", "reasons": ["invalid-recipe"]}
    if set(recipe) - TOP_KEYS:
        reasons.add("unknown-field")
    if recipe.get("RecipeFormatVersion") != "2020-01-25":
        reasons.add("unsupported-format-version")
    if recipe.get("ComponentType", "aws.greengrass.generic") != "aws.greengrass.generic":
        reasons.add("unsupported-component-type")
    if "ComponentSource" in recipe:
        reasons.add("lambda-source")
    if "Lifecycle" in recipe:
        reasons.add("global-lifecycle-selections")
    name, version = recipe.get("ComponentName"), recipe.get("ComponentVersion")
    if not isinstance(name, str) or not NAME.match(name):
        reasons.add("invalid-component-name")
    if not isinstance(version, str) or not SEMVER.match(version) or len(version) > 128:
        reasons.add("invalid-component-version")
    if "ComponentDescription" in recipe and not isinstance(recipe["ComponentDescription"], str):
        reasons.add("invalid-description")
    if "ComponentPublisher" in recipe:
        note("ComponentPublisher")
    defaults, acl = None, None
    config = recipe.get("ComponentConfiguration", {})
    default = config.get("DefaultConfiguration", {}) if isinstance(config, dict) else None
    if not isinstance(config, dict) or set(config) - {"DefaultConfiguration"} or not isinstance(default, dict):
        reasons.add("invalid-configuration")
    elif "accessControl" in default:
        reasons.add("ipc-access-control")
        acl = _ipc_acl(default["accessControl"])
    elif default:
        if not _within_limits(default):
            reasons.add("configuration-limits")
        if _case_collision(default):
            reasons.add("configuration-key-case-collision")
        defaults = default
        note("ComponentConfiguration.DefaultConfiguration")
    dependencies, managed_dependencies = {}, {}
    declared = recipe.get("ComponentDependencies", {})
    if not isinstance(declared, dict):
        reasons.add("invalid-dependency"); declared = {}
    for dep, spec in declared.items():
        if not NAME.match(dep) or dep == name:
            reasons.add("invalid-dependency"); continue
        if dep.startswith(managed.MANAGED_PREFIXES):
            classification = managed.classify_dependency(catalog, dep, spec)
            if classification.get("reason") in ("invalid-dependency", "unsupported-version-range"):
                reasons.add(classification["reason"]); continue
            managed_dependencies[dep] = classification
            # A native replacement is the daemon itself: dropped and reported.
            if classification["outcome"] == "replace":
                note(f"ComponentDependencies.{dep}")
            else:
                reasons.add("aws-managed-dependency")
            continue
        if not isinstance(spec, dict) or set(spec) - {"VersionRequirement", "DependencyType"}:
            reasons.add("invalid-dependency"); continue
        requirement = spec.get("VersionRequirement")
        if not isinstance(requirement, str) or not RANGE.match(requirement):
            reasons.add("unsupported-version-range"); continue
        if "DependencyType" in spec and spec["DependencyType"] not in ("HARD", "SOFT"):
            reasons.add("invalid-dependency"); continue
        # AWS SOFT still requires the dependency at start; native SOFT may omit
        # it, so every translated dependency is native HARD (the default).
        dependencies[dep] = {"versionRequirement": requirement}
    if dependencies:
        note("ComponentDependencies")
    manifests, omitted, seen = [], [], set()
    raw = recipe.get("Manifests")
    if not isinstance(raw, list) or not raw:
        reasons.add("no-manifests")
        raw = []
    for index, manifest in enumerate(raw):
        if not isinstance(manifest, dict) or set(manifest) - {"Name", "Platform", "Lifecycle", "Selections", "Artifacts"}:
            reasons.add("unsupported-manifest-key"); continue
        if "Selections" in manifest:
            reasons.add("global-lifecycle-selections")
        platform = _platform(manifest.get("Platform"), reasons)
        if platform == "omit":
            omitted.append(index); continue
        if platform is None:
            continue
        if platform in seen:
            reasons.add("duplicate-platform"); continue
        seen.add(platform)
        binding, basename = _artifact(manifest, bindings, reasons)
        lifecycle = manifest.get("Lifecycle", {})
        commands = {}
        if not isinstance(lifecycle, dict):
            reasons.add("invalid-step"); lifecycle = {}
        for step_name, step in lifecycle.items():
            if step_name == "Setenv":
                reasons.add("step-timeout-or-env")
            elif step_name in MAPPED_STEPS:
                command = _step(step_name, step, basename, reasons)
                if command is not None:
                    commands[step_name] = command
            elif step_name in UNSUPPORTED_STEPS:
                _step(step_name, step, basename, reasons)
                reasons.add("unsupported-lifecycle-step")
            else:
                reasons.add("unknown-lifecycle-step")
        if "shutdown" in commands and "run" not in commands:
            reasons.add("shutdown-without-run")
        # Without install or run the daemon hands the artifact to the platform updater.
        if "install" not in commands and "run" not in commands:
            reasons.add("configuration-without-command" if defaults else "no-lifecycle-command")
        for step_name in MAPPED_STEPS:
            if step_name in commands:
                note(f"Lifecycle.{step_name}")
        if binding is None:
            continue
        native = {
            "schemaVersion": "1.2" if defaults else "1.1" if dependencies else "1.0",
            "componentName": name,
            "componentVersion": version,
            "artifactBlobId": binding["artifactBlobId"],
            "artifactSha256": binding["artifactSha256"],
            "lifecycle": commands,
            "configuration": {"defaults": defaults} if defaults else {},
        }
        if "ComponentDescription" in recipe:
            native["description"] = recipe["ComponentDescription"]
        if dependencies:
            native["componentDependencies"] = dependencies
        manifests.append({"os": platform[0], "arch": platform[1], "recipe": native})
    if raw and not manifests and not reasons:
        reasons.add("no-supported-platform")
    if len(manifests) > MAX_MANIFESTS:
        reasons.add("artifact-count")
    extra = ({"managed_dependencies": managed_dependencies} if managed_dependencies else {}) | ({"ipc_acl": acl} if acl else {})
    if reasons:
        return {"outcome": "reject", "reasons": sorted(reasons)} | extra
    return {"outcome": "translate", "reasons": [], "manifests": manifests,
            "omitted_manifests": omitted, "informational": informational} | extra


# ---------------------------------------------------------------------------
# MQTT topics (MQTT 3.1.1 section 4.7 / MQTT 5 section 4.7) and the
# proposed AWS-compatible local pub/sub authorization semantics.
# ---------------------------------------------------------------------------
def valid_topic(topic: str) -> bool:
    return isinstance(topic, str) and 0 < len(topic.encode()) <= 65535 and not set(topic) & {"+", "#", "\0"}


def valid_filter(topic_filter: str) -> bool:
    if not isinstance(topic_filter, str) or not topic_filter or "\0" in topic_filter or len(topic_filter.encode()) > 65535:
        return False
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


def filter_matches(topic_filter: str, topic: str) -> bool:
    if not valid_filter(topic_filter) or not valid_topic(topic):
        raise ValueError("invalid filter or topic")
    if topic.startswith("$") and topic_filter[0] in "+#":
        return False
    filters, levels = topic_filter.split("/"), topic.split("/")
    for index, part in enumerate(filters):
        if part == "#":
            return True
        if index >= len(levels) or (part != "+" and part != levels[index]):
            return False
    return len(filters) == len(levels)


def glob_matches(pattern: str, value: str) -> bool:
    """`*` matches any characters (including '/'); everything else, including + and #, is literal."""
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, re.DOTALL) is not None


def pubsub_authorized(policies: list, operation: str, topic: str) -> bool:
    if operation not in ("PublishToTopic", "SubscribeToTopic"):
        raise ValueError("unknown operation")
    for policy in policies:
        operations = policy["operations"]
        if "*" in operations or f"aws.greengrass#{operation}" in operations:
            if any(glob_matches(resource, topic) for resource in policy["resources"]):
                return True
    return False


# ---------------------------------------------------------------------------
# Device shadows (AWS IoT Device Shadow document semantics, proposed local
# persistence semantics).
# ---------------------------------------------------------------------------
SHADOW_MAX_BYTES = 8192
SHADOW_MAX_DEPTH = 8
CLIENT_TOKEN_MAX = 64
SHADOW_NAME = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")


def _depth(value) -> int:
    """Nesting levels; the section object is level 1 and every object or array adds one."""
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def json_equal(a, b) -> bool:
    """JSON value equality: unlike Python ``==``, booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) or isinstance(b, dict):
        return (isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys()
                and all(json_equal(a[k], b[k]) for k in a))
    if isinstance(a, list) or isinstance(b, list):
        return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                and all(json_equal(x, y) for x, y in zip(a, b)))
    return a == b


def _valid_version(value) -> bool:
    """A document version is a JSON integer >= 1; booleans are not versions."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _contains_null(value) -> bool:
    if isinstance(value, list):
        return any(_contains_null(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_null(v) for v in value.values())
    return value is None


def _has_null_in_array(value) -> bool:
    """A null anywhere under an array, including in objects nested in it."""
    if isinstance(value, list):
        return any(_contains_null(v) for v in value)
    if isinstance(value, dict):
        return any(_has_null_in_array(v) for v in value.values())
    return False


def _merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
            if not target[key]:
                target.pop(key)
        elif isinstance(value, dict):
            nested = {}
            _merge(nested, value)
            if nested:
                target[key] = nested
            else:
                target.pop(key, None)
        else:
            target[key] = copy.deepcopy(value)
    return target


def shadow_delta(desired, reported):
    delta = {}
    for key, value in (desired or {}).items():
        other = (reported or {}).get(key)
        if isinstance(value, dict) and isinstance(other, dict):
            nested = shadow_delta(value, other)
            if nested:
                delta[key] = nested
        elif not json_equal(value, other):
            delta[key] = copy.deepcopy(value)
    return delta


class ShadowStore:
    """Named shadows for one thing; versions survive deletion (proposed)."""

    def __init__(self):
        self.documents: dict = {}
        self.versions: dict = {}

    def _view(self, name):
        state = self.documents[name]
        view = {key: copy.deepcopy(state[key]) for key in ("desired", "reported") if key in state}
        delta = shadow_delta(state.get("desired"), state.get("reported"))
        if delta:
            view["delta"] = delta
        return {"state": view, "version": self.versions[name]}

    def apply(self, op: dict) -> dict:
        name = op.get("shadow", "")
        if name and not SHADOW_NAME.match(name):
            return {"error": "invalid"}
        kind = op["op"]
        if kind == "get":
            return self._view(name) if name in self.documents else {"error": "not-found"}
        if kind == "delete":
            if "version" in op and not _valid_version(op["version"]):
                return {"error": "invalid"}
            if name not in self.documents:
                return {"error": "not-found"}
            if "version" in op and op["version"] != self.versions[name]:
                return {"error": "conflict"}
            del self.documents[name]
            self.versions[name] += 1
            return {"version": self.versions[name]}
        if kind != "update":
            raise ValueError("unknown shadow op")
        request = op["request"]
        token = request.get("clientToken")
        state = request.get("state")
        if token is not None and (not isinstance(token, str) or len(token.encode()) > CLIENT_TOKEN_MAX):
            return {"error": "invalid"}
        if set(request) - {"state", "version", "clientToken"} or not isinstance(state, dict) or set(state) - {"desired", "reported"}:
            return {"error": "invalid"}
        if any(not (v is None or isinstance(v, dict)) for v in state.values()):
            return {"error": "invalid"}
        if any(_depth(v) > SHADOW_MAX_DEPTH for v in state.values() if v) or _has_null_in_array(state):
            return {"error": "invalid"}
        if "version" in request:
            if not _valid_version(request["version"]):
                return {"error": "invalid"}
            if request["version"] != self.versions.get(name, 0) or name not in self.documents:
                return {"error": "conflict"}
        current = copy.deepcopy(self.documents.get(name, {}))
        for section, patch in state.items():
            if patch is None:
                current.pop(section, None)
            else:
                merged = _merge(current.get(section, {}), patch)
                if merged:
                    current[section] = merged
                else:
                    current.pop(section, None)
        if len(json.dumps(current, separators=(",", ":")).encode()) > SHADOW_MAX_BYTES:
            return {"error": "too-large"}
        self.documents[name] = current
        self.versions[name] = self.versions.get(name, 0) + 1
        result = self._view(name)
        if token is not None:
            result["clientToken"] = token
        return result


# ---------------------------------------------------------------------------
# Durable streams (proposed SuiFleet semantics, modelled on AWS Stream Manager).
# ---------------------------------------------------------------------------
STREAM_NAME = re.compile(r"^[A-Za-z0-9 ,._-]{1,255}$")
STREAM_MESSAGE_OVERHEAD = 64  # charged per message against max_size (header and index entry)


class StreamStore:
    def __init__(self):
        self.streams: dict = {}

    def apply(self, op: dict) -> dict:
        kind = op["op"]
        if kind == "restart":
            for name in [n for n, s in self.streams.items() if s["persistence"] == "Memory"]:
                stream = self.streams[name]
                stream["messages"].clear()
                stream["oldest"] = stream["next"]
            return {}
        name = op["stream"]
        if kind == "create":
            if name in self.streams:
                return {"error": "exists"}
            if not STREAM_NAME.match(name) or op["strategy_on_full"] not in ("RejectNewData", "OverwriteOldestData") \
                    or op.get("persistence", "File") not in ("File", "Memory") or not 1024 <= op["max_size"]:
                return {"error": "invalid"}
            self.streams[name] = {"max_size": op["max_size"], "strategy": op["strategy_on_full"],
                                  "persistence": op.get("persistence", "File"), "messages": [],
                                  "next": 0, "oldest": 0, "exported": -1}
            return {}
        stream = self.streams.get(name)
        if stream is None:
            return {"error": "not-found"}
        messages = stream["messages"]
        if kind == "append":
            size = op["size"]
            cost = size + STREAM_MESSAGE_OVERHEAD
            if cost > stream["max_size"] or size < 1:
                return {"error": "invalid"}
            while sum(m[1] for m in messages) + cost > stream["max_size"]:
                if stream["strategy"] == "RejectNewData":
                    return {"error": "full"}
                messages.pop(0)
                stream["oldest"] = messages[0][0] if messages else stream["next"]
            sequence = stream["next"]
            messages.append((sequence, cost))
            stream["next"] += 1
            if len(messages) == 1:
                stream["oldest"] = sequence
            return {"sequence": sequence}
        if kind == "read":
            start, minimum, maximum = op.get("start", 0), op.get("min", 1), op.get("max", 1)
            if minimum < 1 or maximum < minimum:
                return {"error": "invalid"}
            if start < stream["oldest"]:
                return {"error": "evicted", "oldest": stream["oldest"]}
            found = [s for s, _ in messages if s >= start][:maximum]
            if len(found) < minimum:
                return {"error": "not-enough-messages"}
            return {"sequences": found}
        if kind == "export-ack":
            sequence = op["sequence"]
            if sequence >= stream["next"]:
                return {"error": "invalid"}
            stream["exported"] = max(stream["exported"], sequence)
            return {"exported": stream["exported"]}
        if kind == "describe":
            return {"oldest": stream["oldest"], "newest": stream["next"] - 1, "exported": stream["exported"],
                    "bytes": sum(m[1] for m in messages)}
        raise ValueError("unknown stream op")


# ---------------------------------------------------------------------------
# Client-device authorization (aws.greengrass.clientdevices.Auth 2.5.x policy
# semantics as the declared reference; proposed SuiFleet identity states).
# ---------------------------------------------------------------------------
CDA_OPERATIONS = {"mqtt:connect": "mqtt:clientId:", "mqtt:publish": "mqtt:topic:", "mqtt:subscribe": "mqtt:topicfilter:"}


def selection_matches(rule: str, thing: str) -> bool:
    for term in rule.split(" OR "):
        key, _, pattern = term.partition(":")
        if key.strip() != "thingName":
            raise ValueError("unsupported selector")
        pattern = pattern.strip().replace("\\:", ":")
        inner = pattern.strip("*")
        if "*" in inner:
            raise ValueError("wildcard only at start or end")
        if pattern == "*":
            return True
        prefix, suffix = pattern.startswith("*"), pattern.endswith("*")
        if (prefix and suffix and inner in thing) or (prefix and not suffix and thing.endswith(inner)) \
                or (suffix and not prefix and thing.startswith(inner)) or (not prefix and not suffix and thing == inner):
            return True
    return False


def client_authorized(config: dict, request: dict) -> bool:
    identity = config["identities"].get(request["thing"])
    if identity is None or identity["state"] != "active" or request["now"] >= identity["expires"]:
        return False
    operation, resource = request["operation"], request["resource"]
    if operation not in CDA_OPERATIONS or not resource.startswith(CDA_OPERATIONS[operation]):
        return False
    if operation == "mqtt:connect" and resource != CDA_OPERATIONS[operation] + request["thing"] and not config.get("allow_foreign_client_ids"):
        return False
    groups = config["deviceGroups"]["definitions"].values()
    for group in groups:
        if not selection_matches(group["selectionRule"], request["thing"]):
            continue
        for statement in config["deviceGroups"]["policies"][group["policyName"]].values():
            if ("*" in statement["operations"] or operation in statement["operations"]) and \
                    any(r == "*" or glob_matches(r, resource) for r in statement["resources"]):
                return True
    return False


# ---------------------------------------------------------------------------
# AWS IoT Core connection (SF-061 aws-iot-core-connection, matching
# aws.greengrass.clientdevices.mqtt.Bridge 2.3.3 IotCore routes and
# aws.greengrass.ShadowManager 2.3.15 cloud synchronization).
# ---------------------------------------------------------------------------
AWS_IOT_FIELDS = {"endpoint", "port", "alpn", "thing_name", "client_id", "ca_file", "certificate_file", "private_key_file",
                  "keep_alive_seconds", "credential_check_seconds", "reject_denied_publications"}
AWS_NAME = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
AWS_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def aws_connection(config: dict) -> dict:
    """The connection settings: valid, and the ALPN and client id used."""
    if set(config) - AWS_IOT_FIELDS or not {"endpoint", "thing_name", "ca_file", "certificate_file", "private_key_file"} <= set(config):
        return {"valid": False}
    port, alpn = config.get("port", 8883), config.get("alpn")
    client_id = config.get("client_id", config["thing_name"])
    endpoint = config["endpoint"]
    valid = (0 < len(endpoint) <= 253 and all(AWS_LABEL.match(label) for label in endpoint.split("."))
             and 0 < port < 65536 and (alpn is None or 0 < len(alpn) <= 255)
             and bool(AWS_NAME.match(config["thing_name"])) and bool(AWS_NAME.match(client_id))
             and 30 <= config.get("keep_alive_seconds", 60) <= 1200 and 5 <= config.get("credential_check_seconds", 30) <= 3600
             and isinstance(config.get("reject_denied_publications", False), bool)
             and all(config[k].startswith("/") for k in ("ca_file", "certificate_file", "private_key_file")))
    if not valid:
        return {"valid": False}
    return {"valid": True, "alpn": alpn if alpn is not None else ("x-amzn-mqtt-ca" if port == 443 else None), "client_id": client_id}


def aws_within_limits(topic: str) -> bool:
    """AWS IoT Core: 256 bytes and 7 slashes, without a $aws/rules/<rule>/ prefix."""
    if topic.startswith("$aws/rules/") and "/" in topic[len("$aws/rules/"):]:
        topic = topic[len("$aws/rules/"):].split("/", 1)[1]
    return len(topic.encode()) <= 256 and topic.count("/") <= 7


def filters_overlap(a: str, b: str) -> bool:
    x, y = a.split("/"), b.split("/")
    for index in range(max(len(x), len(y))):
        p, q = (x[index] if index < len(x) else None), (y[index] if index < len(y) else None)
        if p == "#" or q == "#":
            return True
        if p is None or q is None:
            return False
        if not (p == "+" or q == "+" or p == q):
            return False
    return True


def reaches_aws_shadows(topic_filter: str) -> bool:
    return topic_filter.startswith("$") and filters_overlap(topic_filter, "$aws/things/+/shadow/#")


def aws_mapping_error(aws_iot: bool, broker, mappings: list):
    """None when the bridge mappings are acceptable, else the refusal."""
    uses = lambda link: any(link in (m["source"], m["target"]) for m in mappings)
    if broker is None and uses("LocalMqtt"):
        return "need bridge.broker"
    if not (aws_iot or mappings) or len(mappings) > 64:
        return "1-64 mappings"
    if not aws_iot and uses("IotCore"):
        return "need the [aws_iot] connection"
    for m in mappings:
        prefix = m.get("target_topic_prefix", "")
        if m["source"] == m["target"]:
            return "must differ"
        if "Pubsub" not in (m["source"], m["target"]):
            return "through Pubsub"
        if not valid_filter(m["topic"]) or len(m["topic"].encode()) > 256:
            return "invalid bridge mapping topic filter"
        if "IotCore" in (m["source"], m["target"]):
            produced = prefix + m["topic"]
            cloud_side = m["topic"] if m["source"] == "IotCore" else produced
            if reaches_aws_shadows(cloud_side) or reaches_aws_shadows(produced):
                return "reaches AWS shadow topics"
            if not aws_within_limits(cloud_side):
                return "exceeds AWS IoT Core topic limits"
    for out in (m for m in mappings if m["source"] == "Pubsub"):
        produced = out.get("target_topic_prefix", "") + out["topic"]
        for inbound in (m for m in mappings if m["source"] == out["target"]):
            if valid_filter(produced) and filters_overlap(produced, inbound["topic"]):
                return "bridge loop"
    return None


def aws_patch(old: dict, new: dict) -> dict:
    """The AWS shadow update state turning `old` into `new` (merge semantics)."""
    out = {key: None for key in old if key not in new}
    for key, value in new.items():
        if isinstance(old.get(key), dict) and isinstance(value, dict):
            inner = aws_patch(old[key], value)
            if inner:
                out[key] = inner
        elif key not in old or not json_equal(old[key], value):
            out[key] = value
    return out


def aws_shadow_call(case: dict):
    """(request, outcome) of one synchronizer call against an AWS answer."""
    answer, body = case["answer"], case["answer"]["body"]
    code = body.get("code")
    if case["call"] == "fetch":
        request = {"op": "get", "body": {}}
        if not answer["accepted"]:
            return request, ({"version": 0, "document": None} if code == 404 else {"error": f"rejected: {code}"})
        version = body.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return request, {"error": "no version"}
        document = {}
        for section in ("desired", "reported"):
            value = body.get("state", {}).get(section)
            if value is None:
                continue
            if not isinstance(value, dict):
                return request, {"error": "not an object"}
            document[section] = value
        return request, {"version": version, "document": document}
    expected = case["expected_version"]
    if case["document"] is None:
        request = {"op": "delete", "body": {}}
        if answer["accepted"]:
            return request, {"stored": 0}
        return request, ({"conflict": True} if code == 404 else {"error": f"rejected: {code}"})
    request = {"op": "update", "body": {"state": aws_patch(case["cloud"] or {}, case["document"])}}
    if expected > 0:
        request["body"]["version"] = expected
    if not answer["accepted"]:
        return request, ({"conflict": True} if code == 409 else {"error": f"rejected: {code}"})
    version = body.get("version")
    if not isinstance(version, int) or version <= expected:
        return request, {"error": "did not advance"}
    return request, {"stored": version}


def aws_denied_publication(events: list, reject: bool) -> list:
    """A bridged message the endpoint closes the connection on soon after it
    was sent in that session, three times in a row, is held: kept in the
    spool and retried. With reject_denied_publications it is rejected
    instead. A PUBACK delivers it and resets the count; other closes and
    transport errors neither count nor reset."""
    closes, outcomes = 0, []
    for event in events:
        if event == "puback":
            closes = 0
            outcomes.append("delivered")
        elif event == "closed-soon":
            closes += 1
            if closes < 3:
                outcomes.append("retry")
            elif reject:
                outcomes.append("rejected")
                closes = 0
            else:
                outcomes.append("held")
        elif event in ("closed-late", "closed-before-send", "error"):
            outcomes.append("held" if closes >= 3 else "retry")
        else:
            raise ValueError(event)
    return outcomes


def check_aws_iot(cases) -> int:
    count = 0
    for case in cases["connection"]:
        if aws_connection(case["config"]) != case["expected"]:
            raise AssertionError(f"aws-iot connection case {case['id']}: {aws_connection(case['config'])!r}")
        count += 1
    for case in cases["topics"]:
        if aws_within_limits(case["topic"]) != case["within_limits"]:
            raise AssertionError(f"aws-iot topic case {case['id']}")
        count += 1
    for case in cases["mappings"]:
        error = aws_mapping_error(case["aws_iot"], case.get("broker"), case["mappings"])
        if (error is None) != ("ok" in case["expected"]) or (error is not None and error != case["expected"]["error"]):
            raise AssertionError(f"aws-iot mapping case {case['id']}: {error!r}")
        count += 1
    for case in cases["shadow_patches"]:
        if not json_equal(aws_patch(case["from"], case["to"]), case["patch"]):
            raise AssertionError(f"aws-iot patch case {case['id']}: {aws_patch(case['from'], case['to'])!r}")
        count += 1
    for case in cases["shadow_answers"]:
        request, outcome = aws_shadow_call(case)
        if not json_equal(request, case["request"]) or not json_equal(outcome, case["expected"]):
            raise AssertionError(f"aws-iot shadow answer case {case['id']}: {request!r} {outcome!r}")
        count += 1
    for case in cases["denied_publication"]:
        actual = aws_denied_publication([step["event"] for step in case["steps"]], case["reject_denied_publications"])
        if actual != [step["expect"] for step in case["steps"]]:
            raise AssertionError(f"aws-iot denied publication case {case['id']}: {actual!r}")
        count += len(case["steps"])
    return count


# ---------------------------------------------------------------------------
# Fixture evaluation and matrix checks.
# ---------------------------------------------------------------------------
def _load(root: Path, name: str):
    return json.loads((root / COMPAT / name).read_text())


def _strict_json(text: str):
    def pairs(items):
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate-key")
        return dict(items)
    return json.loads(text, object_pairs_hook=pairs)


def check_recipes(cases, catalog: dict) -> int:
    ids = set()
    for case in cases:
        if case["id"] in ids:
            raise AssertionError(f"recipe case {case['id']}: duplicate id")
        ids.add(case["id"])
        expected = case["expected"]
        if "recipe" not in case:
            # Text the parser must reject; the translator test executes it.
            if not isinstance(case.get("recipe_text"), str) or expected.get("outcome") != "reject" \
                    or len(expected.get("reasons", [])) != 1 or expected["reasons"][0] not in PARSE_REASONS:
                raise AssertionError(f"recipe case {case['id']}: needs a recipe, or recipe_text with one parse rejection")
            continue
        if "recipe_text" in case and case.get("format") == "json":
            if _strict_json(case["recipe_text"]) != case["recipe"]:
                raise AssertionError(f"recipe case {case['id']}: recipe_text does not parse to recipe")
        actual = translate_recipe(case["recipe"], case.get("bindings", {}), catalog)
        for key in expected:
            if actual.get(key) != expected[key]:
                raise AssertionError(f"recipe case {case['id']}: {key} {actual.get(key)!r} != {expected[key]!r}")
        for key in ("managed_dependencies", "ipc_acl"):
            if key in actual and key not in expected:
                raise AssertionError(f"recipe case {case['id']}: {key} must be pinned")
        if actual["outcome"] == "translate" and "manifests" not in expected:
            raise AssertionError(f"recipe case {case['id']}: translate cases must pin manifests")
    return len(cases)


def check_pubsub(cases) -> int:
    count = 0
    for case in cases["filters"]:
        if valid_filter(case["filter"]) != case["valid"]:
            raise AssertionError(f"filter case {case['id']}")
        count += 1
    for case in cases["topics"]:
        if valid_topic(case["topic"]) != case["valid"]:
            raise AssertionError(f"topic case {case['id']}")
        count += 1
    for case in cases["matches"]:
        if filter_matches(case["filter"], case["topic"]) != case["matches"]:
            raise AssertionError(f"match case {case['id']}")
        count += 1
    for case in cases["authorization"]:
        if pubsub_authorized(case["policies"], case["operation"], case["topic"]) != case["allowed"]:
            raise AssertionError(f"pubsub authorization case {case['id']}")
        count += 1
    return count


def _run_sequences(cases, factory, label) -> int:
    count = 0
    for case in cases:
        store = factory()
        for index, step in enumerate(case["steps"]):
            actual = store.apply(step["op"])
            if not json_equal(actual, step["expect"]):
                raise AssertionError(f"{label} case {case['id']} step {index}: {actual!r} != {step['expect']!r}")
            count += 1
    return count


def check_client_devices(cases) -> int:
    count = 0
    for case in cases:
        for request in case["requests"]:
            if client_authorized(case["config"], request) != request["allowed"]:
                raise AssertionError(f"client-device case {case['id']}: {request}")
            count += 1
    return count


def _managed_catalog(root: Path) -> dict:
    return json.loads((root / managed.CATALOG).read_text())


# Runners take (cases, root); root locates the SF-038 managed catalog.
SUITE_RUNNERS = {
    "aws-recipe-cases.json": lambda c, root: check_recipes(c, _managed_catalog(root)),
    "pubsub-cases.json": lambda c, _: check_pubsub(c),
    "shadow-cases.json": lambda c, _: _run_sequences(c, ShadowStore, "shadow"),
    "stream-cases.json": lambda c, _: _run_sequences(c, StreamStore, "stream"),
    "client-device-cases.json": lambda c, _: check_client_devices(c),
    "managed-dependency-cases.json": lambda c, root: managed.check_dependency_cases(c, _managed_catalog(root)),
    "credential-provider-cases.json": lambda c, _: _run_sequences(c, managed.CredentialProvider, "credential-provider"),
    "cloudwatch-metrics-cases.json": lambda c, _: _run_sequences(c, managed.MetricsExport, "cloudwatch-metrics"),
    "cloudwatch-logs-cases.json": lambda c, _: _run_sequences(c, managed.LogsExport, "cloudwatch-logs"),
    "aws-iot-cases.json": lambda c, _: check_aws_iot(c),
}
LEVELS = ("functional", "aws_api", "aws_recipe")
STATUSES = {"implemented", "partial", "proposed", "excluded", "undecided", "roadmap", "not-applicable"}
EXECUTIONS = {"pending", "passed"}


def check_matrix(root: Path, matrix: dict, backlog_keys: set) -> list:
    errors = []
    if matrix.get("format_version") != 1:
        errors.append("format_version must be 1")
    if matrix.get("parity_claim") is not False:
        errors.append("parity_claim must be false")
    suites = matrix.get("suites", {})
    ids = set()
    for row in matrix.get("capabilities", []):
        rid = row.get("id")
        if rid in ids:
            errors.append(f"{rid}: duplicate capability")
        ids.add(rid)
        for ticket in row.get("tickets", []):
            if ticket not in backlog_keys:
                errors.append(f"{rid}: unknown ticket {ticket}")
        if not row.get("tickets") and any(row.get(level) not in ("excluded", "not-applicable") for level in LEVELS):
            errors.append(f"{rid}: needs an owning ticket")
        for level in LEVELS:
            status = row.get(level)
            if status not in STATUSES:
                errors.append(f"{rid}: {level} status {status!r}")
            if status in ("implemented", "partial"):
                evidence = row.get("current_evidence") or []
                if not evidence:
                    errors.append(f"{rid}: {level}={status} requires current_evidence")
                for path in evidence:
                    if not (root / path).exists():
                        errors.append(f"{rid}: missing evidence {path}")
        if row.get("aws_api") in ("implemented", "partial") or row.get("aws_recipe") in ("implemented", "partial"):
            if not any(suites.get(s, {}).get("runtime_execution") == "passed" for s in row.get("suites", [])):
                errors.append(f"{rid}: AWS compatibility claims require a passed runtime suite")
        for suite in row.get("suites", []):
            if suite not in suites:
                errors.append(f"{rid}: unknown suite {suite}")
    for name, suite in suites.items():
        if name not in SUITE_RUNNERS:
            errors.append(f"suite {name}: no reference runner")
        if not (root / COMPAT / name).exists():
            errors.append(f"suite {name}: fixture file missing")
        execution = suite.get("runtime_execution")
        if execution not in EXECUTIONS:
            errors.append(f"suite {name}: runtime_execution {execution!r}")
        if execution == "passed":
            receipt = suite.get("runtime_evidence")
            if not receipt or not (root / receipt).exists():
                errors.append(f"suite {name}: passed runtime execution needs an existing runtime_evidence receipt")
        elif not suite.get("pending_on"):
            errors.append(f"suite {name}: pending suites must name pending_on tickets")
        for ticket in suite.get("pending_on", []):
            if ticket not in backlog_keys:
                errors.append(f"suite {name}: unknown ticket {ticket}")
    for platform in matrix.get("platforms", []):
        for ticket in platform.get("tickets", []):
            if ticket not in backlog_keys:
                errors.append(f"platform {platform.get('id')}: unknown ticket {ticket}")
        if platform.get("status") not in STATUSES:
            errors.append(f"platform {platform.get('id')}: status")
        if platform.get("status") in ("implemented", "partial"):
            for path in platform.get("current_evidence") or [None]:
                if path is None or not (root / path).exists():
                    errors.append(f"platform {platform.get('id')}: missing evidence")
    return errors


def run(root: Path) -> dict:
    backlog = json.loads((root / "docs/tracking/backlog.json").read_text())
    keys = {issue["key"] for issue in backlog["issues"]}
    matrix = _load(root, "greengrass-compat.json")
    errors = check_matrix(root, matrix, keys)
    errors += managed.check_catalog(root, _managed_catalog(root), matrix, keys)
    if errors:
        raise AssertionError("compatibility matrix invalid:\n  " + "\n  ".join(errors))
    counts = {name: SUITE_RUNNERS[name](_load(root, name), root) for name in matrix["suites"]}
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        counts = run(args.root)
    except AssertionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    total = sum(counts.values())
    detail = ", ".join(f"{name}={count}" for name, count in counts.items())
    print(f"PASS: compatibility matrix and {total} reference-model fixture checks ({detail}); runtime execution evidence is recorded separately per matrix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
