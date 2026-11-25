#!/usr/bin/env python3
"""SuiFleet optional Kubernetes workload publisher (SF-305 prototype).

A supervised SuiFleet component uses this helper to apply an authenticated
workload bundle (the component's Walrus artifact) into ONE customer-provisioned
namespace of a customer-owned Kubernetes/k3s cluster. See
docs/KUBERNETES_EDGE.md for the ownership/security model.

It is deliberately narrow:
- The cluster administrator provisions the namespace (with Pod Security
  Admission `restricted` enforced), a ServiceAccount, a namespaced Role and a
  RoleBinding, and places a kubeconfig for that ServiceAccount in a device-local
  profile (default /etc/suifleet/k8s/PROFILE). Recipes never carry credentials,
  never choose the namespace and never receive cluster-admin.
- Only Deployments and ConfigMaps are accepted. Services and other
  networking objects stay administrator-owned. Pod specifications are rejected
  when they request host namespaces, host paths, privilege, added
  capabilities, Secrets, ServiceAccounts/tokens or mutable image tags. The
  cluster's RBAC, Pod Security Admission and the administrator's
  ValidatingAdmissionPolicy remain the authoritative boundary; this
  validation is early, explainable failure.
- Objects carry `app.kubernetes.io/managed-by=suifleet` and the component's
  label. Every write is bound to the observed object: absent objects are
  created with create-only semantics, existing owned objects are updated by
  server-side apply with the observed UID and resourceVersion as
  preconditions (one field manager, never forced), and deletions carry UID and
  resourceVersion delete preconditions. A competing creation, replacement or
  relabel between the ownership check and the write fails that write; the
  helper re-reads and re-checks ownership instead of mutating another
  instance. Objects SuiFleet did not create are never adopted, and a field
  held by another manager makes the apply fail instead of being overwritten.
- Pruning deletes only objects labelled for this component that the current
  bundle no longer contains, with foreground cascading deletion: a pruned
  Deployment remains visible (terminating) until its ReplicaSets and Pods are
  gone, so readiness does not report a retirement or rollback converged while
  old Pods still run.
- A multi-object apply is not transactional: if a later write fails, earlier
  objects may already carry the new bundle. Readiness never reports success
  until every object carries this bundle's digest and pruning has converged
  (no other object labelled for the component remains), and the next apply
  converges.

It manages workloads inside a cluster. It does not install, configure or
update Kubernetes, nodes, the OS, firmware or the SuiFleet client, and it never
reboots anything. Requires Python >= 3.9 and a kubectl client.

Subcommands (component name/version default to the supervisor's
SUIFLEET_COMPONENT / SUIFLEET_COMPONENT_VERSION environment):
  validate --bundle FILE                      offline validation only
  apply    --bundle FILE --profile NAME        validate, apply once, prune
  run      --bundle FILE --profile NAME        apply, then re-apply every
                                              --interval seconds (foreground)
  ready    --bundle FILE --profile NAME        exit 0 when every bundle object
                                              is owned, applied from this
                                              bundle, Deployments rolled out and
                                              no stale component object remains
  remove   --profile NAME --component NAME     delete the component's objects,
                                              then wait (up to --timeout
                                              seconds) until none remains;
                                              exit 0 only once converged,
                                              1 when objects still remain
"""
import argparse
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

BUNDLE_VERSION = 1
PROFILE_VERSION = 1
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_RESOURCES = 64
FIELD_MANAGER = "suifleet"
MANAGED_BY = "app.kubernetes.io/managed-by"
COMPONENT_LABEL = "suifleet.io/component"
VERSION_ANNOTATION = "suifleet.io/version"
BUNDLE_ANNOTATION = "suifleet.io/bundle-sha256"
DEFAULT_PROFILE_ROOT = "/etc/suifleet/k8s"
KUBECTL_TIMEOUT = 60  # process limit per kubectl call (seconds)
REQUEST_TIMEOUT = 30  # kubectl --request-timeout per API request (seconds)
WRITE_ATTEMPTS = 8

# (apiVersion, kind) -> kubectl resource name used for get/list/prune.
ALLOWED_KINDS = {
    ("v1", "ConfigMap"): "configmaps",
    ("apps/v1", "Deployment"): "deployments.apps",
}
# kubectl resource name -> namespaced REST collection path (for preconditioned DELETE).
API_PATHS = {
    "configmaps": "/api/v1/namespaces/{namespace}/configmaps/{name}",
    "deployments.apps": "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
}
# Cascading deletion policy for pruning; see Writer.delete.
PROPAGATION = "Foreground"
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
IMAGE_DIGEST = re.compile(r"^[a-z0-9]+([._/:-][a-z0-9]+)*@sha256:[0-9a-f]{64}$")
RESERVED_PREFIXES = ("suifleet.io/",)
ALLOWED_VOLUME_SOURCES = {"configMap", "emptyDir"}


class Rejected(Exception):
    """The bundle, profile or cluster state is not acceptable; nothing applied."""


class Conflict(Exception):
    """Another controller owns an object or field; SuiFleet does not fight it."""


# ---------------------------------------------------------------- validation

def _require(condition, message):
    if not condition:
        raise Rejected(message)


def _list(where, value):
    """An optional list field: None or a list; returns a list."""
    if value is None:
        return []
    _require(isinstance(value, list), f"{where} must be a list")
    return value


def _keys(where, value, allowed):
    _require(isinstance(value, dict), f"{where} must be an object")
    unknown = sorted(set(value) - set(allowed))
    _require(not unknown, f"{where} has unsupported field(s) {unknown}")


def _labels(where, labels):
    if labels is None:
        return
    _require(isinstance(labels, dict), f"{where} must be an object")
    for key, value in labels.items():
        _require(isinstance(key, str) and isinstance(value, str), f"{where} keys and values must be strings")
        _require(key != MANAGED_BY and not key.startswith(RESERVED_PREFIXES),
                 f"{where} must not set reserved key {key!r}; SuiFleet sets ownership itself")


def _security_context(where, context, container):
    if context is None:
        return
    _require(isinstance(context, dict), f"{where} must be an object")
    _require(context.get("privileged") in (None, False), f"{where}.privileged is not allowed")
    _require(context.get("allowPrivilegeEscalation") in (None, False),
             f"{where}.allowPrivilegeEscalation must be false")
    _require(context.get("procMount") in (None, "Default"), f"{where}.procMount is not allowed")
    _require(context.get("runAsUser") != 0, f"{where}.runAsUser 0 is not allowed")
    _require(context.get("windowsOptions") is None, f"{where}.windowsOptions is not supported")
    if container:
        capabilities = context.get("capabilities")
        capabilities = {} if capabilities is None else capabilities
        _require(isinstance(capabilities, dict), f"{where}.capabilities must be an object")
        _require(not capabilities.get("add"), f"{where}.capabilities.add is not allowed")


def _container(where, container):
    _require(isinstance(container, dict), f"{where} must be an object")
    _require(isinstance(container.get("name"), str) and DNS_LABEL.match(container["name"]),
             f"{where}.name must be a DNS label")
    image = container.get("image")
    _require(isinstance(image, str) and IMAGE_DIGEST.match(image),
             f"{where}.image must be an explicit repository pinned by @sha256 digest")
    for port in _list(f"{where}.ports", container.get("ports")):
        _require(isinstance(port, dict) and "hostPort" not in port and "hostIP" not in port,
                 f"{where}.ports must not use hostPort/hostIP")
    for item in _list(f"{where}.envFrom", container.get("envFrom")):
        _require(isinstance(item, dict) and "secretRef" not in item, f"{where}.envFrom must not reference Secrets")
    for item in _list(f"{where}.env", container.get("env")):
        _require(isinstance(item, dict), f"{where}.env entries must be objects")
        source = item.get("valueFrom")
        source = {} if source is None else source
        _require(isinstance(source, dict), f"{where}.env valueFrom must be an object")
        _require("secretKeyRef" not in source, f"{where}.env must not reference Secrets")
    _security_context(f"{where}.securityContext", container.get("securityContext"), True)
    _require("volumeDevices" not in container, f"{where}.volumeDevices is not allowed")


def _pod_spec(where, spec):
    _require(isinstance(spec, dict), f"{where} must be an object")
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        _require(spec.get(flag) in (None, False), f"{where}.{flag} is not allowed")
    for field in ("nodeName", "serviceAccountName", "serviceAccount", "imagePullSecrets",
                  "ephemeralContainers", "resourceClaims", "os"):
        _require(field not in spec, f"{where}.{field} is not allowed")
    _require(spec.get("automountServiceAccountToken") in (None, False),
             f"{where}.automountServiceAccountToken must be false")
    containers = spec.get("containers")
    _require(isinstance(containers, list) and containers, f"{where}.containers must be a non-empty list")
    for kind in ("containers", "initContainers"):
        for index, container in enumerate(_list(f"{where}.{kind}", spec.get(kind))):
            _container(f"{where}.{kind}[{index}]", container)
    for index, volume in enumerate(_list(f"{where}.volumes", spec.get("volumes"))):
        _require(isinstance(volume, dict), f"{where}.volumes[{index}] must be an object")
        sources = set(volume) - {"name"}
        _require(len(sources) == 1 and sources <= ALLOWED_VOLUME_SOURCES,
                 f"{where}.volumes[{index}] may only use one of {sorted(ALLOWED_VOLUME_SOURCES)}")
    _security_context(f"{where}.securityContext", spec.get("securityContext"), False)


def _resource(index, resource):
    where = f"resources[{index}]"
    _require(isinstance(resource, dict), f"{where} must be an object")
    key = (resource.get("apiVersion"), resource.get("kind"))
    _require(all(isinstance(part, str) for part in key), f"{where}.apiVersion and kind must be strings")
    _require(key in ALLOWED_KINDS, f"{where}: {key[0]}/{key[1]} is not an allowed kind "
             f"(allowed: {sorted(f'{a}/{k}' for a, k in ALLOWED_KINDS)})")
    metadata = resource.get("metadata")
    _keys(f"{where}.metadata", metadata, ("name", "labels", "annotations"))
    name = metadata.get("name")
    _require(isinstance(name, str) and DNS_LABEL.match(name), f"{where}.metadata.name must be a DNS label")
    _labels(f"{where}.metadata.labels", metadata.get("labels"))
    _labels(f"{where}.metadata.annotations", metadata.get("annotations"))
    kind = key[1]
    if kind == "ConfigMap":
        _keys(where, resource, ("apiVersion", "kind", "metadata", "data", "immutable"))
        data = resource.get("data")
        data = {} if data is None else data
        _require(isinstance(data, dict) and all(isinstance(v, str) for v in data.values()),
                 f"{where}.data must map keys to strings")
        _require(resource.get("immutable") in (None, True, False), f"{where}.immutable must be a boolean")
    else:
        _keys(where, resource, ("apiVersion", "kind", "metadata", "spec"))
        spec = resource.get("spec")
        _keys(f"{where}.spec", spec, ("replicas", "selector", "template", "strategy", "minReadySeconds",
                                       "revisionHistoryLimit", "progressDeadlineSeconds"))
        template = spec.get("template")
        _keys(f"{where}.spec.template", template, ("metadata", "spec"))
        template_metadata = template.get("metadata")
        template_metadata = {} if template_metadata is None else template_metadata
        _keys(f"{where}.spec.template.metadata", template_metadata, ("labels", "annotations"))
        _labels(f"{where}.spec.template.metadata.labels", template_metadata.get("labels"))
        _labels(f"{where}.spec.template.metadata.annotations", template_metadata.get("annotations"))
        _pod_spec(f"{where}.spec.template.spec", template.get("spec"))
    return (kind, name)


def load_bundle(path):
    """Read and validate a bundle file; returns (parsed bundle, sha256 hex)."""
    data = Path(path).read_bytes()
    _require(len(data) <= MAX_BUNDLE_BYTES, f"bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    try:
        bundle = json.loads(data)
    except (ValueError, RecursionError) as error:
        raise Rejected(f"bundle is not JSON: {error}") from None
    validate_bundle(bundle)
    return bundle, hashlib.sha256(data).hexdigest()


def validate_bundle(bundle):
    """Raise Rejected (never another exception) unless the bundle is acceptable."""
    try:
        _validate_bundle(bundle)
    except Rejected:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, RecursionError) as error:
        # Explicit type checks above should make this unreachable; stay fail-closed.
        raise Rejected(f"malformed bundle: {type(error).__name__}") from None


def _validate_bundle(bundle):
    _keys("bundle", bundle, ("version", "resources"))
    version = bundle.get("version")
    _require(type(version) is int and version == BUNDLE_VERSION, f"bundle version must be {BUNDLE_VERSION}")
    resources = bundle.get("resources")
    _require(isinstance(resources, list), "bundle.resources must be a list")
    _require(len(resources) <= MAX_RESOURCES, f"bundle has more than {MAX_RESOURCES} resources")
    seen = set()
    for index, resource in enumerate(resources):
        identity = _resource(index, resource)
        _require(identity not in seen, f"resources[{index}] duplicates {identity[0]}/{identity[1]}")
        seen.add(identity)


def validate_component(component):
    _require(isinstance(component, str) and component and len(component) <= 63 and LABEL_VALUE.match(component),
             "component name must be a valid Kubernetes label value (set SUIFLEET_COMPONENT or --component)")


def render(bundle, namespace, component, version, digest):
    """Objects to apply: namespace forced, ownership labels/annotations added,
    ServiceAccount token automount disabled."""
    rendered = []
    for resource in bundle["resources"]:
        item = json.loads(json.dumps(resource))
        metadata = item["metadata"]
        metadata["namespace"] = namespace
        metadata["labels"] = dict(metadata.get("labels") or {}, **{MANAGED_BY: FIELD_MANAGER, COMPONENT_LABEL: component})
        annotations = dict(metadata.get("annotations") or {}, **{BUNDLE_ANNOTATION: digest})
        if version:
            annotations[VERSION_ANNOTATION] = version
        metadata["annotations"] = annotations
        if item["kind"] == "Deployment":
            item["spec"]["template"]["spec"]["automountServiceAccountToken"] = False
        rendered.append(item)
    return rendered


def ownership(existing, component):
    """None when SuiFleet may apply over `existing` (absent or ours)."""
    if existing is None:
        return None
    labels = (existing.get("metadata") or {}).get("labels") or {}
    if labels.get(MANAGED_BY) != FIELD_MANAGER:
        return "exists without app.kubernetes.io/managed-by=suifleet; SuiFleet does not adopt objects it did not create"
    if labels.get(COMPONENT_LABEL) != component:
        return f"is owned by SuiFleet component {labels.get(COMPONENT_LABEL)!r}"
    return None


def prune_candidates(listed, desired_names):
    """Names of listed component objects that the bundle no longer contains."""
    return sorted(item["metadata"]["name"] for item in listed if item["metadata"]["name"] not in desired_names)


def object_ready(current, desired, component):
    """(ready, reason) for one live object against its rendered desired state:
    present, not terminating, owned by this component, carrying exactly the
    desired labels/annotations (so this bundle's digest and version) and, for
    ConfigMaps, the desired data. Deployments additionally need a rollout."""
    kind, name = desired["kind"], desired["metadata"]["name"]
    if current is None:
        return False, f"{kind}/{name} is missing"
    metadata = current.get("metadata") or {}
    if metadata.get("deletionTimestamp"):
        return False, f"{kind}/{name} is being deleted"
    problem = ownership(current, component)
    if problem:
        return False, f"{kind}/{name} {problem}"
    for field in ("labels", "annotations"):
        live = metadata.get(field) or {}
        for key, value in desired["metadata"].get(field, {}).items():
            if live.get(key) != value:
                return False, f"{kind}/{name} not yet applied from this bundle ({field} {key})"
    if kind == "ConfigMap":
        # The bundle schema has no binaryData, so any live binaryData entry
        # (projected as an extra file by a ConfigMap volume) is foreign content.
        if (current.get("data") or {}) != (desired.get("data") or {}) or current.get("binaryData") or \
                bool(current.get("immutable")) != bool(desired.get("immutable")):
            return False, f"{kind}/{name} content differs from this bundle"
    if kind == "Deployment":
        drift = spec_drift(desired.get("spec") or {}, current.get("spec") or {}, "spec")
        if drift:
            return False, f"{kind}/{name} {drift} differs from this bundle"
        ok, reason = deployment_ready(current, desired["metadata"]["annotations"][BUNDLE_ANNOTATION])
        if not ok:
            return False, f"{kind}/{name}: {reason}"
    return True, "ready"


QUANTITY = re.compile(r"^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(Ki|Mi|Gi|Ti|Pi|Ei|[eE][+-]?[0-9]+|n|u|m|k|M|G|T|P|E)?$")
QUANTITY_SCALE = {None: 0, "n": -9, "u": -6, "m": -3, "k": 3, "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}
QUANTITY_BINARY = {"Ki": 10, "Mi": 20, "Gi": 30, "Ti": 40, "Pi": 50, "Ei": 60}


def _quantity(value):
    """Exact value of a Kubernetes resource quantity, or None if not one. The
    API server accepts JSON numbers and canonicalises quantities (for example
    0.1 or "0.1" becomes "100m")."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float):
        value = repr(value)  # shortest round-trip text of the JSON number
    match = QUANTITY.match(str(value))
    if not match:
        return None
    number, suffix = Fraction(match.group(1)), match.group(2)
    if suffix in QUANTITY_BINARY:
        return number * 2 ** QUANTITY_BINARY[suffix]
    if suffix in QUANTITY_SCALE:  # includes the decimal SI suffix "E" (exa)
        return number * Fraction(10) ** QUANTITY_SCALE[suffix]
    return number * Fraction(10) ** int(suffix[1:])  # decimal exponent, e.g. "1e3"


def _http_get_defaults(prefix):
    # SetDefaults_HTTPGetAction: an omitted or empty path becomes "/" and an
    # omitted or empty scheme becomes HTTP.
    return {f"{prefix}.httpGet.path": ("/",), f"{prefix}.httpGet.scheme": ("HTTP",)}


def _probe_defaults(prefix):
    return {f"{prefix}.timeoutSeconds": (1,), f"{prefix}.periodSeconds": (10,),
            f"{prefix}.successThreshold": (1,), f"{prefix}.failureThreshold": (3,),
            **_http_get_defaults(prefix)}


def _server_defaults():
    pod = "spec.template.spec"
    table = {
        "spec.strategy.type": ("RollingUpdate",),
        "spec.strategy.rollingUpdate.maxUnavailable": ("25%",),
        "spec.strategy.rollingUpdate.maxSurge": ("25%",),
        "spec.revisionHistoryLimit": (10,),
        "spec.progressDeadlineSeconds": (600,),
        f"{pod}.restartPolicy": ("Always",),
        f"{pod}.terminationGracePeriodSeconds": (30,),
        f"{pod}.dnsPolicy": ("ClusterFirst",),
        f"{pod}.schedulerName": ("default-scheduler",),
        f"{pod}.enableServiceLinks": (True,),
        f"{pod}.volumes[].configMap.defaultMode": (420,),
    }
    for kind in ("containers", "initContainers"):
        container = f"{pod}.{kind}[]"
        table.update({
            f"{container}.terminationMessagePath": ("/dev/termination-log",),
            f"{container}.terminationMessagePolicy": ("File",),
            # Digest-pinned images default to IfNotPresent; a ":latest" tag
            # alongside the digest defaults to Always. Both pull the pinned digest.
            f"{container}.imagePullPolicy": ("IfNotPresent", "Always"),
            f"{container}.ports[].protocol": ("TCP",),
            f"{container}.env[].valueFrom.fieldRef.apiVersion": ("v1",),
            **_http_get_defaults(f"{container}.lifecycle.postStart"),
            **_http_get_defaults(f"{container}.lifecycle.preStop"),
        })
        for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
            table.update(_probe_defaults(f"{container}.{probe}"))
    return table


# Fields the bundle may omit that the API server fills in, with the values it
# fills in. A live field the bundle omits must be one of these defaults or a
# zero value; anything else (an injected init container, an extra template
# annotation, a changed default) is drift.
SERVER_DEFAULTS = _server_defaults()
# Defaulted fields that are plain (non-pointer) strings in the Go API types:
# the server cannot tell an explicit "" from an omitted value, so it fills in
# the default for both. Pointer fields (for example terminationGracePeriodSeconds
# or the rollingUpdate IntOrStrings) keep an explicit value and are excluded.
EMPTY_STRING_DEFAULTS = {path: allowed for path, allowed in SERVER_DEFAULTS.items()
                         if ".rollingUpdate." not in path and all(isinstance(v, str) for v in allowed)}
# Omitted fields intentionally left to another controller (an HPA owns replicas).
DELEGATED_FIELDS = {"spec.replicas"}


def _is_zero(value):
    return value is None or value is False or value == 0 and not isinstance(value, bool) or value in ("", [], {})


def _unexpected(live, path):
    """Path of a non-default value in a live field the bundle omits, or None."""
    pattern = re.sub(r"\[[0-9]+\]", "[]", path)
    if pattern in DELEGATED_FIELDS:
        return None
    if pattern in SERVER_DEFAULTS:
        allowed = SERVER_DEFAULTS[pattern]
        return None if any(type(live) is type(value) and live == value for value in allowed) else path
    if isinstance(live, dict):
        return spec_drift({}, live, path)
    return None if _is_zero(live) else path


def spec_drift(desired, live, path):
    """Path of the first field where the live object differs from what the
    bundle specifies, or None. Every field the bundle specifies must match. A
    live field the bundle omits must hold the API server's default for it (or a
    zero value) unless it is delegated (replicas left to a
    HorizontalPodAutoscaler). A field the server omits matches a zero/empty
    desired value (Go omitempty), and resource quantities compare by value."""
    if live is None:
        if not desired:
            return None
        if isinstance(desired, dict):
            live = {}
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return path
        for key, value in desired.items():
            drift = spec_drift(value, live.get(key), f"{path}.{key}")
            if drift:
                return drift
        for key in sorted(set(live) - set(desired)):
            drift = _unexpected(live[key], f"{path}.{key}")
            if drift:
                return drift
        return None
    if isinstance(desired, list):
        if not isinstance(live, list) or len(live) != len(desired):
            return path
        for index, (want, have) in enumerate(zip(desired, live)):
            drift = spec_drift(want, have, f"{path}[{index}]")
            if drift:
                return drift
        return None
    if type(desired) is type(live) and desired == live:
        return None
    if isinstance(desired, (int, float)) and isinstance(live, (int, float)) and \
            not isinstance(desired, bool) and not isinstance(live, bool) and desired == live:
        return None
    if ".resources." in path:
        want, have = _quantity(desired), _quantity(live)
        if want is not None and want == have:
            return None
    if desired == "" and isinstance(live, str):
        allowed = EMPTY_STRING_DEFAULTS.get(re.sub(r"\[[0-9]+\]", "[]", path), ())
        if live in allowed:
            return None
    return path


def transfer_patch(current):
    """Merge patch that converts the field ownership recorded by SuiFleet's own
    create-only write (operation Update) into its server-side apply ownership,
    conditional on the observed UID and resourceVersion. None when nothing to
    transfer. Without this, later applies would conflict with SuiFleet's own
    creation entry."""
    metadata = current.get("metadata") or {}
    entries = metadata.get("managedFields") or []
    created = [e for e in entries if e.get("manager") == FIELD_MANAGER and e.get("operation") == "Update"
               and not e.get("subresource")]
    if not created:
        return None
    applied = [e for e in entries if e.get("manager") == FIELD_MANAGER and e.get("operation") == "Apply"
               and not e.get("subresource")]
    fields = {}
    for entry in applied + created:
        _merge_fields(fields, entry.get("fieldsV1") or {})
    merged = dict((applied or created)[0], operation="Apply", fieldsType="FieldsV1", fieldsV1=fields)
    merged.pop("time", None)
    others = [e for e in entries if e not in created and e not in applied]
    return {"metadata": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
                         "managedFields": others + [merged]}}


def _merge_fields(target, source):
    for key, value in source.items():
        _merge_fields(target.setdefault(key, {}), value if isinstance(value, dict) else {})


def deployment_ready(deployment, digest):
    """(ready, reason) for a Deployment applied from bundle `digest`."""
    metadata, spec, status = (deployment.get(k) or {} for k in ("metadata", "spec", "status"))
    if (metadata.get("annotations") or {}).get(BUNDLE_ANNOTATION) != digest:
        return False, "not yet applied from this bundle"
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Progressing" and condition.get("reason") == "ProgressDeadlineExceeded":
            return False, "progress deadline exceeded"
    replicas = spec.get("replicas", 1)
    if status.get("observedGeneration", 0) < metadata.get("generation", 0):
        return False, "rollout not yet observed"
    for field in ("updatedReplicas", "readyReplicas", "availableReplicas"):
        if status.get(field, 0) < replicas:
            return False, f"{field} {status.get(field, 0)}/{replicas}"
    if status.get("replicas", 0) > replicas:
        return False, "old replicas still terminating"
    return True, "available"


# ------------------------------------------------------------------- profile

def _private(path, *, directory, secret):
    info = os.lstat(path)
    kind = "directory" if directory else "file"
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
             f"{path} must be a {kind}, not a symlink or other type")
    _require(info.st_uid in (0, os.getuid()), f"{path} must be owned by root or the daemon user")
    _require(not info.st_mode & 0o022, f"{path} must not be group/other writable")
    if secret:
        _require(not info.st_mode & 0o077, f"{path} must not be group/other accessible (use mode 0600)")


def load_profile(root, name):
    _require(isinstance(name, str) and DNS_LABEL.match(name), "profile name must be a DNS label")
    directory = Path(root) / name
    _private(directory, directory=True, secret=False)
    profile_path = directory / "profile.json"
    _private(profile_path, directory=False, secret=False)
    try:
        profile = json.loads(profile_path.read_text())
    except ValueError as error:
        raise Rejected(f"{profile_path} is not JSON: {error}") from None
    _keys("profile", profile, ("version", "namespace", "kubeconfig", "kubectl"))
    _require(profile.get("version") == PROFILE_VERSION, f"profile version must be {PROFILE_VERSION}")
    _require(isinstance(profile.get("namespace"), str) and DNS_LABEL.match(profile["namespace"]),
             "profile.namespace must be a DNS label")
    _require(profile["namespace"] not in ("default", "kube-system", "kube-public", "kube-node-lease"),
             "profile.namespace must be a dedicated namespace")
    kubectl = profile.get("kubectl", ["kubectl"])
    _require(isinstance(kubectl, list) and kubectl and all(isinstance(a, str) and a for a in kubectl),
             "profile.kubectl must be a non-empty argv list")
    kubeconfig = profile.get("kubeconfig", "kubeconfig")
    _require(isinstance(kubeconfig, str) and "/" not in kubeconfig and kubeconfig not in (".", ".."),
             "profile.kubeconfig must be a file name inside the profile directory")
    _private(directory / kubeconfig, directory=False, secret=True)
    return {"namespace": profile["namespace"], "kubectl": kubectl, "kubeconfig": str(directory / kubeconfig)}


def check_credentials(view):
    """Reject kubeconfigs that are not one ServiceAccount-style credential over
    verified TLS: exactly one method (a token, or a complete client
    certificate/key pair), no exec/auth-provider plugins, impersonation or
    basic auth."""
    _require(isinstance(view, dict), "kubeconfig view must be an object")
    contexts, clusters, users = (view.get(k) or [] for k in ("contexts", "clusters", "users"))
    _require(len(contexts) == 1 and len(clusters) == 1 and len(users) == 1,
             "kubeconfig must select exactly one context, cluster and user")
    cluster = clusters[0].get("cluster") or {}
    _require(str(cluster.get("server", "")).startswith("https://"), "kubeconfig server must use https")
    _require(not cluster.get("insecure-skip-tls-verify"), "kubeconfig must verify TLS")
    _require(cluster.get("certificate-authority-data") or cluster.get("certificate-authority"),
             "kubeconfig must pin the cluster certificate authority")
    user = users[0].get("user") or {}
    for field in ("exec", "auth-provider", "username", "password", "as", "as-groups", "as-uid", "as-user-extra"):
        _require(field not in user, f"kubeconfig user must not use {field!r}")

    def present(*fields):
        return [field for field in fields if user.get(field) not in (None, "")]

    tokens = present("token", "tokenFile")
    certificates = present("client-certificate-data", "client-certificate")
    keys = present("client-key-data", "client-key")
    _require(len(tokens) <= 1, "kubeconfig user must not set both token and tokenFile")
    _require(len(certificates) <= 1 and len(keys) <= 1,
             "kubeconfig user must set the client certificate and key once each (inline data or file, not both)")
    _require(not (tokens and (certificates or keys)),
             "kubeconfig user must use exactly one credential method, not a token and a client certificate")
    _require(bool(certificates) == bool(keys), "kubeconfig client certificate and client key must be supplied together")
    _require(tokens or certificates, "kubeconfig user must have a token or client certificate")


# ------------------------------------------------------------------- kubectl

class DeadlineExceeded(Rejected):
    """A deadline-bounded kubectl call (Kubectl.deadline) ran out of time."""


class Kubectl:
    def __init__(self, profile, cache_dir):
        self.profile = profile
        self.base = list(profile["kubectl"]) + ["--kubeconfig", profile["kubeconfig"], "--cache-dir", cache_dir]
        # Never inherit KUBECONFIG, contexts or credentials from the environment.
        self.env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": cache_dir, "LANG": "C"}
        # Optional time.monotonic() deadline. When set, every call's API
        # request timeout and process limit shrink to the remaining time, and
        # a call past the deadline raises DeadlineExceeded instead of running.
        self.deadline = None

    def __call__(self, *args, stdin=None, namespaced=True):
        request, limit = REQUEST_TIMEOUT, KUBECTL_TIMEOUT
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("deadline reached before kubectl " + " ".join(args[:2]))
            request, limit = min(request, max(1, math.ceil(remaining))), min(limit, remaining)
        command = (self.base + ["--request-timeout", f"{request}s"]
                   + (["--namespace", self.profile["namespace"]] if namespaced else []) + list(args))
        try:
            return subprocess.run(command, input=stdin, capture_output=True, text=True, env=self.env,
                                  timeout=limit, check=False)
        except subprocess.TimeoutExpired:
            if self.deadline is None:
                raise
            raise DeadlineExceeded("deadline reached during kubectl " + " ".join(args[:2])) from None

    def json(self, *args, namespaced=True):
        result = self(*args, "-o", "json", namespaced=namespaced)
        if result.returncode != 0:
            raise Rejected(f"kubectl {' '.join(args)} failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def connect(profile, cache_dir):
    kubectl = Kubectl(profile, cache_dir)
    check_credentials(kubectl.json("config", "view", "--minify", "--raw", namespaced=False))
    return kubectl


def _stale(error, *extra):
    """The write lost an optimistic-concurrency race: a resourceVersion or UID
    precondition failed (or an `extra` race marker matched). The caller
    re-reads and re-checks ownership; it never retries blindly."""
    lowered = error.lower()
    return any(marker in lowered for marker in (
        "the object has been modified", "uid mismatch", "metadata.uid", "precondition") + extra)


class Writer:
    """Ownership-checked, precondition-bound writes for one component."""

    def __init__(self, kubectl, component, pause=0.2):
        self.kubectl, self.component, self.pause = kubectl, component, pause
        self.namespace = kubectl.profile["namespace"]

    def get(self, resource, name, managed_fields=False):
        extra = ("--show-managed-fields",) if managed_fields else ()
        return self.kubectl.json("get", resource, name, "--ignore-not-found", *extra)

    def _retry(self, attempt):
        if self.pause:
            time.sleep(self.pause * (attempt + 1))

    def apply(self, item):
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        name = item["metadata"]["name"]
        label = f"{item['kind']}/{name} in {self.namespace}"
        for attempt in range(WRITE_ATTEMPTS):
            current = self.get(resource, name, managed_fields=True)
            if current is None:
                # Create-only: fails instead of adopting an object that appeared
                # after the read. The next iteration verifies and applies.
                result = self.kubectl("create", f"--field-manager={FIELD_MANAGER}", "-f", "-", stdin=json.dumps(item))
                if result.returncode != 0 and not _stale(result.stderr, "already exists", "alreadyexists"):
                    raise Rejected(f"kubectl create {label} failed: {result.stderr.strip()[:500]}")
                continue
            problem = ownership(current, self.component)
            if problem:
                raise Conflict(f"{label} {problem}")
            if (current.get("metadata") or {}).get("deletionTimestamp"):
                raise Rejected(f"{label} is being deleted; retry after deletion completes")
            patch = transfer_patch(current)
            if patch is not None:
                result = self.kubectl("patch", resource, name, "--type=merge", f"--field-manager={FIELD_MANAGER}",
                                      "--patch-file=/dev/stdin", stdin=json.dumps(patch))
                if result.returncode != 0 and not _stale(result.stderr, "not found", "notfound"):
                    raise Rejected(f"kubectl patch {label} failed: {result.stderr.strip()[:500]}")
                continue
            bound = json.loads(json.dumps(item))
            bound["metadata"]["uid"] = current["metadata"]["uid"]
            bound["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            # No --force-conflicts: a field owned by another manager fails the apply.
            result = self.kubectl("apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-",
                                  stdin=json.dumps(bound))
            if result.returncode == 0:
                return
            error = result.stderr.strip()[:1000]
            if "apply failed with" in error.lower():
                # The API server may report a field conflict before a failed
                # UID/resourceVersion precondition. If the object changed since
                # the check, re-read and re-check ownership for an accurate result.
                again = self.get(resource, name)
                same = again is not None and all(
                    (again.get("metadata") or {}).get(key) == current["metadata"][key] for key in ("uid", "resourceVersion"))
                if not same:
                    self._retry(attempt)
                    continue
                raise Conflict(f"another field manager owns fields of {label}; not overriding: {error}")
            if not _stale(error):
                raise Rejected(f"kubectl apply {label} failed: {error}")
            self._retry(attempt)
        raise Conflict(f"{label} kept changing during {WRITE_ATTEMPTS} attempts; not overriding")

    def delete(self, resource, current):
        """Delete one listed object only while it is the same owned instance.
        Returns True when deleted or already gone, False when left alone
        because it was relabelled or replaced (even by an object that carries
        this component's labels: only the listed UID is ever deleted)."""
        name = current["metadata"]["name"]
        uid = current["metadata"]["uid"]
        path = API_PATHS[resource].format(namespace=self.namespace, name=name)
        for attempt in range(WRITE_ATTEMPTS):
            if current is None:
                return True
            metadata = current.get("metadata") or {}
            if metadata.get("uid") != uid or ownership(current, self.component):
                return False
            # Foreground: the owner stays (terminating, still labelled) until
            # the garbage collector has removed its blocking dependents
            # (a Deployment's ReplicaSets and their Pods), so readiness's
            # prune-convergence check also covers workloads that still run.
            # Background deletion would remove the Deployment first and let
            # an empty retirement bundle report ready while old Pods serve.
            options = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": PROPAGATION,
                       "preconditions": {"uid": uid, "resourceVersion": metadata["resourceVersion"]}}
            result = self.kubectl("delete", "--raw", path, "-f", "-", stdin=json.dumps(options), namespaced=False)
            if result.returncode == 0:
                return True
            error = result.stderr.strip()[:500]
            if "notfound" in error.lower().replace(" ", ""):
                return True
            if not _stale(error, "(conflict)"):
                raise Rejected(f"delete {resource}/{name} failed: {error}")
            self._retry(attempt)
            current = self.get(resource, name)
        raise Conflict(f"{resource}/{name} kept changing during {WRITE_ATTEMPTS} delete attempts")


def apply(kubectl, objects, component, writer=None):
    writer = writer or Writer(kubectl, component)
    desired = {}
    for item in objects:
        writer.apply(item)
        desired.setdefault(ALLOWED_KINDS[(item["apiVersion"], item["kind"])], set()).add(item["metadata"]["name"])
    return prune(kubectl, component, desired, writer)


def prune(kubectl, component, desired, writer=None):
    writer = writer or Writer(kubectl, component)
    selector = f"{MANAGED_BY}={FIELD_MANAGER},{COMPONENT_LABEL}={component}"
    removed = []
    for resource in ALLOWED_KINDS.values():
        listed = kubectl.json("get", resource, "-l", selector) or {}
        items = {item["metadata"]["name"]: item for item in listed.get("items") or []}
        for name in prune_candidates(list(items.values()), desired.get(resource, set())):
            if items[name]["metadata"].get("deletionTimestamp"):
                continue  # Already deleted, held by a finalizer; readiness waits for it.
            if writer.delete(resource, items[name]):
                removed.append(f"{resource}/{name}")
            else:
                _log(f"left {resource}/{name}: no longer this component's object")
    return removed


def ready(kubectl, objects, component):
    """(ready, reason): every desired object is ready (object_ready) AND prune
    has converged -- no other object labelled for this component remains, so
    an update, rollback or empty retirement bundle is not ready while stale
    workloads still exist or are still terminating. Because pruning uses
    foreground deletion, a pruned Deployment stays listed until its
    ReplicaSets and Pods are gone; no Pod/ReplicaSet permission is needed."""
    desired = {}
    for item in objects:
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        desired.setdefault(resource, set()).add(item["metadata"]["name"])
        current = kubectl.json("get", resource, item["metadata"]["name"], "--ignore-not-found")
        ok, reason = object_ready(current, item, component)
        if not ok:
            return False, reason
    selector = f"{MANAGED_BY}={FIELD_MANAGER},{COMPONENT_LABEL}={component}"
    for resource in ALLOWED_KINDS.values():
        listed = kubectl.json("get", resource, "-l", selector) or {}
        stale = prune_candidates(listed.get("items") or [], desired.get(resource, set()))
        if stale:
            return False, f"stale {resource}/{stale[0]} not yet pruned ({len(stale)} remaining)"
    return True, "ready"


def remove(kubectl, component, timeout, writer=None, pause=2.0):
    """(converged, reason): delete every object labelled for this component,
    then poll until none remains (foreground deletion keeps a Deployment
    listed until its ReplicaSets and Pods are gone). Objects already
    terminating are not re-deleted; a finalizer or a raced replacement left
    alone keeps it unconverged.

    `timeout` bounds the convergence wait after the deletes are issued:
    polling kubectl calls run under Kubectl.deadline, so a slow API server
    cannot stretch the wait past it. The delete phase itself is not counted;
    each of its calls has the fixed REQUEST_TIMEOUT/KUBECTL_TIMEOUT limits.
    Returns (False, reason) at the deadline. Success is a point-in-time
    observation: anything that recreates the objects later (such as a still
    running `run` reconciler) is outside this check."""
    removed = prune(kubectl, component, {}, writer)
    _log(f"deleting {removed}" if removed else "no object to delete")
    deadline = time.monotonic() + max(0.0, timeout)
    previous, kubectl.deadline = getattr(kubectl, "deadline", None), deadline
    reason = "no convergence check completed"
    try:
        while True:
            try:
                ok, reason = ready(kubectl, [], component)
            except DeadlineExceeded:
                return False, f"timed out after {timeout:g}s; last state: {reason}"
            if ok:
                return True, "removed"
            if time.monotonic() >= deadline:
                return False, reason
            time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
    finally:
        kubectl.deadline = previous


# ----------------------------------------------------------------------- CLI

def _log(message):
    print(f"suifleet-k8s-apply: {message}", file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("validate", "apply", "run", "ready", "remove"))
    parser.add_argument("--bundle")
    parser.add_argument("--profile")
    parser.add_argument("--profile-root", default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--component", default=os.environ.get("SUIFLEET_COMPONENT"))
    parser.add_argument("--version", default=os.environ.get("SUIFLEET_COMPONENT_VERSION", ""))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="remove: seconds to wait, after the deletes are issued, for the objects (and their Pods) to disappear; polling API calls are cut off at this deadline")
    args = parser.parse_args(argv)
    try:
        if args.command != "remove":
            _require(args.bundle, "--bundle is required")
            bundle, digest = load_bundle(args.bundle)
            if args.command == "validate":
                _log(f"bundle valid ({len(bundle['resources'])} resources)")
                return 0
        validate_component(args.component)
        _require(args.profile, "--profile is required")
        profile = load_profile(args.profile_root, args.profile)
        with tempfile.TemporaryDirectory(prefix="suifleet-k8s-") as cache:
            kubectl = connect(profile, cache)
            if args.command == "remove":
                ok, reason = remove(kubectl, args.component, args.timeout)
                _log(reason if ok else f"not removed: {reason}")
                return 0 if ok else 1
            objects = render(bundle, profile["namespace"], args.component, args.version, digest)
            if args.command == "ready":
                ok, reason = ready(kubectl, objects, args.component)
                if not ok:
                    _log(f"not ready: {reason}")
                return 0 if ok else 1
            stopping = []
            signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
            while True:
                removed = apply(kubectl, objects, args.component)
                _log(f"applied {len(objects)} objects to {profile['namespace']}" + (f"; pruned {removed}" if removed else ""))
                if args.command == "apply":
                    return 0
                deadline = time.monotonic() + args.interval
                while not stopping and time.monotonic() < deadline:
                    time.sleep(min(0.2, args.interval))
                if stopping:
                    return 0
    except Conflict as error:
        _log(f"conflict: {error}")
        return 3
    except (Rejected, OSError, subprocess.TimeoutExpired) as error:
        _log(f"rejected: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
