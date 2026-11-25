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
- Deployments may consume only ConfigMaps from the same bundle. A ConfigMap
  that a bundle Deployment consumes is published under a content-addressed
  name, `NAME-<first 16 hex digits of the sha256 of its data>`, as an
  immutable ConfigMap, and the Pod template references that name. Each name
  therefore denotes exactly one content, enforced by the API server, so every
  Pod (and the kubelet, which caches ConfigMaps by namespace/name) reads the
  configuration its template names: a configuration-only change or rollback
  changes the template and rolls the Pods out, and a partially applied update
  never changes the data an existing template names. ConfigMaps are written
  before Deployments; ConfigMaps no Deployment consumes keep their bundle
  name and stay mutable.
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
# Annotation on a content-addressed ConfigMap: its name in the bundle.
CONFIG_NAME_ANNOTATION = "suifleet.io/config-name"
# Hex digits of the data sha256 appended to a consumed ConfigMap's name.
CONTENT_HASH_CHARS = 16
# Names of this shape are reserved for content-addressed ConfigMaps in every
# bundle, so a stable (unconsumed, mutable) ConfigMap of one bundle can never
# occupy the name another bundle derives for a consumed one.
CONTENT_NAME = re.compile(r"-[0-9a-f]{%d}$" % CONTENT_HASH_CHARS)
# ConfigMaps per bundle. Changing a consumed ConfigMap creates its new
# content-addressed object before the old one is pruned, so an update or
# rollback needs room for two bundles' ConfigMaps at once: 2 * 9 = 18, plus the
# namespace's kube-root-ca.crt, fits the shipped quota of 20
# (scripts/kubernetes/cluster-setup.yaml; docs/KUBERNETES_EDGE.md).
MAX_CONFIGMAPS = 9
DEFAULT_PROFILE_ROOT = "/etc/suifleet/k8s"
KUBECTL_TIMEOUT = 60  # process limit per kubectl call (seconds)
REQUEST_TIMEOUT = 30  # kubectl --request-timeout per API request (seconds)
WRITE_ATTEMPTS = 8
MIN_RUN_INTERVAL = 1.0  # run: shortest accepted pause between reconciliations (seconds)

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


def _off(value):
    """An optional boolean that is absent or exactly false.

    Tuple membership compares by equality, so ``0 in (None, False)`` and
    ``1.0 in (True, False)`` hold; check identity so JSON numbers never pass
    as Kubernetes booleans.
    """
    return value is None or value is False


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
    _require(_off(context.get("privileged")), f"{where}.privileged is not allowed")
    _require(_off(context.get("allowPrivilegeEscalation")),
             f"{where}.allowPrivilegeEscalation must be false")
    _require(context.get("procMount") in (None, "Default"), f"{where}.procMount is not allowed")
    _require(context.get("runAsUser") != 0, f"{where}.runAsUser 0 is not allowed")
    _require(context.get("windowsOptions") is None, f"{where}.windowsOptions is not supported")
    if container:
        capabilities = context.get("capabilities")
        capabilities = {} if capabilities is None else capabilities
        _require(isinstance(capabilities, dict), f"{where}.capabilities must be an object")
        _require(not capabilities.get("add"), f"{where}.capabilities.add is not allowed")


def _config_ref(where, reference):
    """Name of a ConfigMap reference (envFrom configMapRef, env
    configMapKeyRef or configMap volume), or None when absent."""
    if reference is None:
        return None
    _require(isinstance(reference, dict) and isinstance(reference.get("name"), str)
             and DNS_LABEL.match(reference["name"]), f"{where}.name must name a ConfigMap (DNS label)")
    return reference["name"]


def _config_ref_slots(pod):
    """(where, reference object) for every ConfigMap reference of a pod spec:
    envFrom configMapRef, env valueFrom.configMapKeyRef (all containers and
    init containers) and configMap volumes (including subPath mounts)."""
    for kind in ("containers", "initContainers"):
        for container in pod.get(kind) or []:
            for item in container.get("envFrom") or []:
                yield "envFrom.configMapRef", item.get("configMapRef")
            for item in container.get("env") or []:
                yield "env.valueFrom.configMapKeyRef", (item.get("valueFrom") or {}).get("configMapKeyRef")
    for volume in pod.get("volumes") or []:
        yield "volumes.configMap", volume.get("configMap")


def config_refs(pod):
    """Sorted names of the ConfigMaps a validated pod spec consumes."""
    names = {_config_ref(where, reference) for where, reference in _config_ref_slots(pod)}
    names.discard(None)
    return sorted(names)


def content_name(name, data):
    """Content-addressed name of a consumed bundle ConfigMap: its bundle name
    plus the first CONTENT_HASH_CHARS hex digits of the sha256 of its data
    (canonical JSON). Distinct data gives a distinct name, so the kubelet's
    by-name ConfigMap cache can never serve a Pod other data than its
    template names; equal data (for example after a rollback) gives the same
    name again."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return f"{name}-{hashlib.sha256(canonical.encode()).hexdigest()[:CONTENT_HASH_CHARS]}"


def config_names(bundle):
    """{bundle name: published name} for every bundle ConfigMap that a bundle
    Deployment consumes (content-addressed). Other ConfigMaps keep their name."""
    consumed = set()
    for resource in bundle["resources"]:
        if resource["kind"] == "Deployment":
            consumed.update(config_refs(resource["spec"]["template"]["spec"]))
    return {resource["metadata"]["name"]: content_name(resource["metadata"]["name"], resource.get("data") or {})
            for resource in bundle["resources"]
            if resource["kind"] == "ConfigMap" and resource["metadata"]["name"] in consumed}


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
        _config_ref(f"{where}.envFrom.configMapRef", item.get("configMapRef"))
    for item in _list(f"{where}.env", container.get("env")):
        _require(isinstance(item, dict), f"{where}.env entries must be objects")
        source = item.get("valueFrom")
        source = {} if source is None else source
        _require(isinstance(source, dict), f"{where}.env valueFrom must be an object")
        _require("secretKeyRef" not in source, f"{where}.env must not reference Secrets")
        _config_ref(f"{where}.env.valueFrom.configMapKeyRef", source.get("configMapKeyRef"))
    _security_context(f"{where}.securityContext", container.get("securityContext"), True)
    _require("volumeDevices" not in container, f"{where}.volumeDevices is not allowed")


def _pod_spec(where, spec):
    _require(isinstance(spec, dict), f"{where} must be an object")
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        _require(_off(spec.get(flag)), f"{where}.{flag} is not allowed")
    for field in ("nodeName", "serviceAccountName", "serviceAccount", "imagePullSecrets",
                  "ephemeralContainers", "resourceClaims", "os"):
        _require(field not in spec, f"{where}.{field} is not allowed")
    _require(_off(spec.get("automountServiceAccountToken")),
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
        _config_ref(f"{where}.volumes[{index}].configMap", volume.get("configMap"))
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
        _require(resource.get("immutable") is None or isinstance(resource.get("immutable"), bool),
                 f"{where}.immutable must be a boolean")
        # Kubernetes never lets an immutable ConfigMap's data change or the flag
        # return to false, so a supervisor rollback could not restore a
        # stable-named ConfigMap in place. SuiFleet itself makes consumed
        # ConfigMaps immutable under content-addressed names (render), where
        # no in-place change is ever needed; bundles never request it.
        _require(resource.get("immutable") is not True,
                 f"{where}.immutable must be false or absent: an immutable ConfigMap cannot be rolled back in place")
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
    # Every ConfigMap a Deployment consumes must come from this bundle: render
    # binds its content into the Pod template so that a configuration-only
    # change or rollback rolls the Pods out. A ConfigMap outside the bundle
    # could change without any rollout, so readiness could not vouch for the
    # configuration the Pods actually run.
    configs = {name for kind, name in seen if kind == "ConfigMap"}
    _require(len(configs) <= MAX_CONFIGMAPS,
             f"bundle has more than {MAX_CONFIGMAPS} ConfigMaps; an update needs quota for two bundles' ConfigMaps at once")
    for index, resource in enumerate(resources):
        if resource["kind"] != "Deployment":
            continue
        for name in config_refs(resource["spec"]["template"]["spec"]):
            _require(name in configs, f"resources[{index}] consumes ConfigMap {name!r}, which is not in this bundle; "
                     "Deployments may consume only ConfigMaps the same bundle defines")
    # A content-addressed name must not collide with another bundle ConfigMap.
    # Stable ConfigMap names must stay disjoint from content-addressed names
    # of any bundle: the reserved suffix shape is refused for them, so no
    # earlier or later bundle can leave a mutable object at a name a consumed
    # ConfigMap derives (where a node's kubelet could still cache old data).
    published = config_names(bundle)
    for name in sorted(configs - set(published)):
        _require(not CONTENT_NAME.search(name),
                 f"ConfigMap {name!r} ends in '-' and {CONTENT_HASH_CHARS} hex digits, a suffix reserved for "
                 "content-addressed ConfigMaps; rename it")


def validate_component(component):
    _require(isinstance(component, str) and component and len(component) <= 63 and LABEL_VALUE.match(component),
             "component name must be a valid Kubernetes label value (set SUIFLEET_COMPONENT or --component)")


def render(bundle, namespace, component, version, digest):
    """Objects to apply: namespace forced, ownership labels/annotations added,
    ServiceAccount token automount disabled, each container's unset
    imagePullPolicy set to the default its image implies and every ConfigMap
    a Deployment consumes published immutable under its content-addressed
    name (config_names), with the Pod template's references rewritten to it.

    Kubernetes never restarts Pods when a ConfigMap they read through
    env/envFrom or a subPath mount changes, and the kubelet resolves (and
    caches) ConfigMaps by namespace/name only. With a mutable shared name, a
    Pod could start with data other than the bundle that wrote its template
    (a partially applied update, or a kubelet cache that has not yet seen the
    latest write) while readiness saw a healthy, current Deployment. A
    content-addressed immutable name has exactly one content, so the template
    determines the data every Pod reads."""
    names = config_names(bundle)
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
        if item["kind"] == "ConfigMap" and metadata["name"] in names:
            annotations[CONFIG_NAME_ANNOTATION] = metadata["name"]
            metadata["name"] = names[metadata["name"]]
            item["immutable"] = True
        if item["kind"] == "Deployment":
            pod = item["spec"]["template"]["spec"]
            pod["automountServiceAccountToken"] = False
            for _, reference in _config_ref_slots(pod):
                if reference is not None:
                    reference["name"] = names[reference["name"]]
            for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                # The API server defaults an unset imagePullPolicy from the
                # image only when the field is empty, and keeps a previously
                # defaulted value across later image changes. Send the default
                # explicitly so SuiFleet owns the field and an image change
                # (for example to "repo:latest@sha256:...") updates it.
                if container.get("imagePullPolicy") in (None, ""):
                    default = _image_pull_policy_default(container.get("image"))
                    if default:
                        container["imagePullPolicy"] = default
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
# Characters Go's strings.TrimSpace removes (unicode.IsSpace): Quantity's
# UnmarshalJSON trims them before ParseQuantity, so " 64Mi " is stored as
# "64Mi". Python's str.strip() differs (it also strips U+001C..U+001F).
GO_SPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006" \
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def _quantity_match(value):
    """QUANTITY match of a quantity's text as the API server parses it:
    surrounding Go whitespace trimmed, the whole remainder matched."""
    return QUANTITY.fullmatch(value.strip(GO_SPACE))


def _quantity(value):
    """Exact value of a Kubernetes resource quantity, or None if not one. The
    API server accepts JSON numbers and canonicalises quantities (for example
    0.1 or "0.1" becomes "100m"); see _parsed_quantity for its rounding."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float):
        value = repr(value)  # shortest round-trip text of the JSON number
    match = _quantity_match(str(value))
    if not match:
        return None
    number, suffix = Fraction(match.group(1)), match.group(2)
    if suffix in QUANTITY_BINARY:
        return number * 2 ** QUANTITY_BINARY[suffix]
    if suffix in QUANTITY_SCALE:  # includes the decimal SI suffix "E" (exa)
        return number * Fraction(10) ** QUANTITY_SCALE[suffix]
    return number * Fraction(10) ** int(suffix[1:])  # decimal exponent, e.g. "1e3"


# Fields whose values are Kubernetes resource.Quantity, which the API server
# canonicalises ("1.5Gi" is returned as "1536Mi"): ResourceRequirements limits
# and requests (container and pod level), PodSpec overhead and
# EmptyDirVolumeSource sizeLimit. Map keys may contain dots ("example.com/gpu"),
# so drift paths quote them (see _child): a key is never read as a field name.
# Every other field, including resources.claims names, compares as plain text.
QUANTITY_PATH = re.compile(r'(?:\.resources\.(?:limits|requests)|\.overhead)\["[^"]*"\]$|\.emptyDir\.sizeLimit$')
# ResourceList entries (resource limits/requests and overhead, but not the
# *Quantity emptyDir sizeLimit) are rounded up to milli precision by the API
# server's SetDefaults_ResourceList ("10.1m" is stored as "11m").
MILLI_ROUNDED_PATH = re.compile(r'(?:\.resources\.(?:limits|requests)|\.overhead)\["[^"]*"\]$')


def _round_away(value, digits):
    """Value rounded away from zero to 10**-digits precision (inf.RoundUp)."""
    scaled = value * 10 ** digits
    return Fraction(math.ceil(scaled) if scaled >= 0 else math.floor(scaled), 10 ** digits)


# resource.Quantity's maxAllowed: ParseQuantity caps BinarySI magnitudes here.
QUANTITY_BINARY_MAX = 2 ** 63 - 1


def _is_binary_si(value):
    """Whether ParseQuantity reads this quantity in BinarySI format (a Ki..Ei
    suffix); JSON numbers and decimal/exponent suffixes are never BinarySI."""
    match = _quantity_match(value) if isinstance(value, str) else None
    return bool(match) and match.group(2) in QUANTITY_BINARY


def _parsed_quantity(value, binary=False):
    """Value the API server stores for a quantity it parses: ParseQuantity
    rounds a nonzero value away from zero to nano precision ("0.1n" is stored
    as "1n"), for every quantity field, then caps a BinarySI magnitude at
    2**63-1 ("8Ei" is stored as "9223372036854775807"; decimal "10E" is not
    capped)."""
    value = _round_away(value, 9)
    if binary and abs(value) > QUANTITY_BINARY_MAX:
        value = QUANTITY_BINARY_MAX if value > 0 else -QUANTITY_BINARY_MAX
    return value


def _round_up_milli(value):
    """Quantity rounded away from zero to milli precision, as
    resource.Quantity.RoundUp(-3) does."""
    return _round_away(value, 3)

FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Containers of the pod template, whose imagePullPolicy default depends on the
# container's own image.
CONTAINER_PATH = re.compile(r"^spec\.template\.spec\.(?:containers|initContainers)\[[0-9]+\]$")


def _image_pull_policy_default(image):
    """imagePullPolicy the API server fills in for an omitted, null or empty
    policy (SetDefaults_Container): Always when the image reference's tag is
    "latest" or it has neither a tag nor a digest, otherwise IfNotPresent. A
    digest-pinned image without a tag therefore defaults to IfNotPresent and
    "repo:latest@sha256:..." to Always. None for a non-string image."""
    if not isinstance(image, str):
        return None
    name, _, digest = image.partition("@")
    tag = name.rsplit("/", 1)[-1].partition(":")[2]  # a registry port is not a tag
    return "Always" if tag == "latest" or not (tag or digest) else "IfNotPresent"


def _child(path, key, *, is_map=False):
    """Drift path of a struct field or map entry. Struct fields are dotted
    (spec.replicas). Map keys, and any key that is not a plain field name, are
    quoted (metadata.annotations["example.com/x.emptyDir.sizeLimit"]) with '%'
    and '"' percent-encoded, so a key can never end a path in a field name or
    open a quoted segment. Field-name rules (quantities, server defaults,
    pointer presence, map fields) therefore apply only to real fields."""
    if not is_map and FIELD_NAME.fullmatch(key):
        return f"{path}.{key}"
    return f'{path}["{key.replace("%", "%25").replace(chr(34), "%22")}"]'


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
            f"{container}.ports[].protocol": ("TCP",),
            f"{container}.env[].valueFrom.fieldRef.apiVersion": ("v1",),
            **_http_get_defaults(f"{container}.lifecycle.postStart"),
            **_http_get_defaults(f"{container}.lifecycle.preStop"),
        })
        for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
            table.update(_probe_defaults(f"{container}.{probe}"))
            # GRPCAction.Service is a *string with +default="".
            table[f"{container}.{probe}.grpc.service"] = ("",)
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
# Defaulted fields that are plain (non-pointer) int32s in the Go API types:
# SetDefaults_Probe replaces an explicit 0 timeoutSeconds, periodSeconds,
# successThreshold or failureThreshold with its default, exactly as for an
# omitted one. Pointer ints (revisionHistoryLimit, progressDeadlineSeconds,
# terminationGracePeriodSeconds, defaultMode) keep an explicit 0 and are excluded.
ZERO_INT_DEFAULTS = {path: allowed for path, allowed in SERVER_DEFAULTS.items()
                     if path.rsplit(".", 1)[-1] in ("timeoutSeconds", "periodSeconds", "successThreshold",
                                                     "failureThreshold") and "Probe." in path}
# Omitted fields intentionally left to another controller (an HPA owns replicas).
DELEGATED_FIELDS = {"spec.replicas"}


# Go map fields (map[string]string, ResourceList) reachable from a Deployment
# spec, by field name. Unlike struct fields, a map key is meaningful even when
# its value is empty or zero (nodeSelector {"pool": ""} requires that node
# label; a label selector term matches on it), so map key sets must match the
# bundle exactly rather than tolerating extra zero-valued keys.
MAP_FIELDS = frozenset({"labels", "annotations", "matchLabels", "nodeSelector", "limits", "requests",
                        "overhead", "volumeAttributes", "options"})


# Optional fields that are Go pointers (*int64, *bool, *string, *IntOrString,
# *Quantity, *LabelSelector) in the apps/v1 and core/v1 types reachable from a
# Deployment spec, by field name. For these, presence is meaningful in both
# directions: the server keeps an explicit zero, false or empty value, and an
# absent field means "inherit" or "unset". For example, a container's
# runAsGroup 0 overrides the pod's runAsGroup while an absent one inherits it,
# and an empty namespaceSelector selects all namespaces while an absent one
# selects only the pod's own. So a bundle value (even a zero) must be present
# live, and a live value the bundle omits must be a server default. Plain
# (non-pointer) omitempty fields keep the zero-value tolerance: the server
# omits their zero value, so an absent live field equals an explicit zero.
PRESENCE_FIELDS = frozenset({
    # DeploymentSpec, RollingUpdateDeployment
    "replicas", "revisionHistoryLimit", "progressDeadlineSeconds", "maxUnavailable", "maxSurge",
    # PodSpec
    "terminationGracePeriodSeconds", "activeDeadlineSeconds", "automountServiceAccountToken",
    "shareProcessNamespace", "priority", "enableServiceLinks", "preemptionPolicy", "setHostnameAsFQDN",
    "hostUsers", "runtimeClassName",
    # PodSecurityContext, SecurityContext, SeccompProfile, AppArmorProfile, WindowsSecurityContextOptions
    "runAsUser", "runAsGroup", "runAsNonRoot", "fsGroup", "fsGroupChangePolicy", "supplementalGroupsPolicy",
    "seLinuxChangePolicy", "privileged", "readOnlyRootFilesystem", "allowPrivilegeEscalation", "procMount",
    "localhostProfile", "gmsaCredentialSpecName", "gmsaCredentialSpec", "runAsUserName", "hostProcess",
    # VolumeMount, GRPCAction, ConfigMap{KeySelector,EnvSource,VolumeSource}, KeyToPath, EmptyDirVolumeSource
    "mountPropagation", "recursiveReadOnly", "service", "optional", "defaultMode", "mode", "sizeLimit",
    # Toleration, TopologySpreadConstraint, PodAffinityTerm, PodDNSConfigOption
    "tolerationSeconds", "minDomains", "nodeAffinityPolicy", "nodeTaintsPolicy", "labelSelector",
    "namespaceSelector", "value",
})
# Container.RestartPolicy is a *string (sidecar init containers); the PodSpec
# restartPolicy of the same name is a plain string with a server default.
CONTAINER_RESTART_POLICY = re.compile(r"\.(?:containers|initContainers)\[[0-9]*\]\.restartPolicy$")
# EnvVar.Value and HTTPHeader.Value are plain strings; only PodDNSConfigOption's is a pointer.
DNS_OPTION_VALUE = re.compile(r"\.dnsConfig\.options\[[0-9]*\]\.value$")


def _presence_matters(path):
    name = path.rsplit(".", 1)[-1]
    if name == "restartPolicy":
        return bool(CONTAINER_RESTART_POLICY.search(path))
    if name == "value":
        return bool(DNS_OPTION_VALUE.search(path))
    return name in PRESENCE_FIELDS


def _is_map(path, value):
    return isinstance(value, dict) and path.rsplit(".", 1)[-1] in MAP_FIELDS


def _map_drift(desired, live, path):
    """Path of the first key present in only one of two map fields, or None."""
    extra = sorted(set(live) ^ set(desired))
    return _child(path, extra[0], is_map=True) if extra else None


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
    if live is None:
        return None
    if _presence_matters(path):
        return path
    if _is_map(path, live):
        return _map_drift({}, live, path)
    if isinstance(live, dict):
        return spec_drift({}, live, path)
    return None if _is_zero(live) else path


def spec_drift(desired, live, path):
    """Path of the first field where the live object differs from what the
    bundle specifies, or None. Every field the bundle specifies must match. A
    live field the bundle omits must hold the API server's default for it (or a
    zero value) unless it is delegated (replicas left to a
    HorizontalPodAutoscaler). Optional pointer fields (runAsGroup,
    allowPrivilegeEscalation, namespaceSelector, ...) must be present live
    exactly when the bundle specifies them, even with a zero value. Map fields (labels, annotations, nodeSelector,
    resource lists, ...) must have exactly the bundle's keys, even when an
    extra key's value is empty. A field the server omits matches a zero/empty
    desired value (Go omitempty), and resource quantities (resource limits and
    requests, overhead, emptyDir sizeLimit) compare by value after the
    server's parse-time round-up to nano precision and BinarySI cap, resource lists after its
    further round-up to milli precision."""
    if live is None:
        if desired is None:
            return None
        if _presence_matters(path):
            return path
        if not desired:
            return None
        if isinstance(desired, dict):
            live = {}
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return path
        if CONTAINER_PATH.match(path) and desired.get("imagePullPolicy") in (None, "") and \
                live.get("imagePullPolicy") is not None:
            # The server derives an unset policy from this container's image,
            # so compare the live policy with exactly that default; an explicit
            # policy is compared as given.
            default = _image_pull_policy_default(desired.get("image"))
            if default:
                desired = {**desired, "imagePullPolicy": default}
        is_map = _is_map(path, desired)
        if is_map:
            drift = _map_drift(desired, live, path)
            if drift:
                return drift
        for key, value in desired.items():
            if value is None and not is_map:
                # An explicit JSON null struct field is accepted by the API
                # server as unset, exactly like an omitted one: it is defaulted
                # (terminationGracePeriodSeconds null becomes 30) or left to
                # its delegated owner. Compare it by the omitted-field rules,
                # including pointer presence. Map entries keep key semantics.
                have = live.get(key)
                drift = None if have is None else _unexpected(have, _child(path, key))
            elif is_map and (isinstance(value, (dict, list)) or isinstance(live.get(key), (dict, list))):
                # Map values (map[string]string, ResourceList) are scalars:
                # never descend into one where field-name rules could apply.
                child = _child(path, key, is_map=True)
                drift = None if value == live.get(key) else child
            else:
                drift = spec_drift(value, live.get(key), _child(path, key, is_map=is_map))
            if drift:
                return drift
        for key in sorted(set(live) - set(desired)):
            drift = _unexpected(live[key], _child(path, key))
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
    if QUANTITY_PATH.search(path):
        # Typed comparison first: the server parses every quantity rounded up
        # to nano precision (BinarySI capped at 2**63-1) and stores a ResourceList entry further rounded up
        # to milli precision; the live value is canonical, so any other live
        # value, including the identical unrounded text, is drift.
        want, have = _quantity(desired), _quantity(live)
        if want is not None and have is not None:
            want = _parsed_quantity(want, _is_binary_si(desired))
            if MILLI_ROUNDED_PATH.search(path):
                want = _round_up_milli(want)
            return None if want == have else path
    if type(desired) is type(live) and desired == live:
        return None
    if isinstance(desired, (int, float)) and isinstance(live, (int, float)) and \
            not isinstance(desired, bool) and not isinstance(live, bool) and desired == live:
        return None
    if desired == "" and isinstance(live, str):
        allowed = EMPTY_STRING_DEFAULTS.get(re.sub(r"\[[0-9]+\]", "[]", path), ())
        if live in allowed:
            return None
    if isinstance(desired, (int, float)) and not isinstance(desired, bool) and desired == 0 and \
            type(live) is int:
        allowed = ZERO_INT_DEFAULTS.get(re.sub(r"\[[0-9]+\]", "[]", path), ())
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
                result = self.kubectl("create", f"--field-manager={FIELD_MANAGER}", "-f", "-",
                                      stdin=json.dumps(item))
                if result.returncode != 0 and not _stale(result.stderr, "already exists", "alreadyexists"):
                    raise Rejected(f"kubectl create {label} failed: {result.stderr.strip()[:500]}")
                continue
            problem = ownership(current, self.component)
            if problem:
                raise Conflict(f"{label} {problem}")
            if (current.get("metadata") or {}).get("deletionTimestamp"):
                raise Rejected(f"{label} is being deleted; retry after deletion completes")
            irreversible = immutable_problem(current, item)
            if irreversible:
                raise Rejected(f"{label} {irreversible}")
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


def immutable_problem(current, desired):
    """Reason an owned live object cannot be brought to `desired` in place, or None.
    The API server refuses any data change to an immutable ConfigMap and any
    return to mutable; the helper never deletes and recreates it automatically.
    A content-addressed (immutable) ConfigMap may only be relabelled: its live
    content must already be exactly the content its name denotes."""
    if current.get("kind") != "ConfigMap":
        return None
    if current.get("immutable") is not True:
        if desired.get("immutable") is True:
            # A mutable object at a content-addressed name (left by an older
            # helper, or recreated out of band) may already be cached by a
            # node under that name with other data; never convert it in place.
            return ("is a mutable ConfigMap at a content-addressed name; an administrator must delete it "
                    "before SuiFleet publishes this content under that name")
        return None
    if desired.get("immutable") is not True:
        return ("is an immutable ConfigMap and cannot be updated or rolled back in place; "
                "an administrator must delete it before SuiFleet manages it again")
    if (current.get("data") or {}) != (desired.get("data") or {}) or current.get("binaryData"):
        return ("is an immutable ConfigMap whose content differs from the content its name denotes; "
                "an administrator must delete it before SuiFleet manages it again")
    return None


def live_config_refs(deployment):
    """ConfigMap names a live Deployment's Pod template references (tolerant
    of unexpected shapes: live objects are not validated bundles)."""
    pod = (((deployment.get("spec") or {}).get("template") or {}).get("spec")) or {}
    names = set()
    try:
        for _, reference in _config_ref_slots(pod):
            if isinstance(reference, dict) and isinstance(reference.get("name"), str):
                names.add(reference["name"])
    except (AttributeError, TypeError):
        pass
    return names


def content_addressed(configmap):
    """True for a ConfigMap this helper published under a content-addressed
    name for a bundle Deployment: immutable, annotated with its bundle name
    and named NAME-HASH. Only bundle Deployments consume these; stable
    (mutable, bundle-named) ConfigMaps may have consumers outside the bundle."""
    metadata = configmap.get("metadata") or {}
    name = metadata.get("name")
    return (configmap.get("immutable") is True and isinstance(name, str) and bool(CONTENT_NAME.search(name))
            and isinstance((metadata.get("annotations") or {}).get(CONFIG_NAME_ANNOTATION), str))


def free_configmaps(kubectl, component, objects, writer):
    """Before creating ConfigMaps, delete this component's content-addressed
    ConfigMaps that the bundle does not contain and no live (non-terminating)
    component Deployment references -- leftovers of an earlier apply that
    failed before pruning. Nothing can still read such an object: only bundle
    Deployments consume content-addressed names, and none references it.
    The end-of-apply prune would delete them anyway; doing it first keeps
    them from consuming the namespace's ConfigMap quota, so a retry or new
    bundle after a partial apply needs room for only the live and the
    desired ConfigMaps. Stable ConfigMaps are never freed here: one of the
    last converged bundle may be in use by a consumer outside the bundle,
    and a leftover cannot be told apart from it, so only the prune after a
    fully successful apply removes them."""
    selector = f"{MANAGED_BY}={FIELD_MANAGER},{COMPONENT_LABEL}={component}"
    listed = kubectl.json("get", "deployments.apps", "-l", selector) or {}
    keep = {item["metadata"]["name"] for item in objects if item["kind"] == "ConfigMap"}
    for deployment in listed.get("items") or []:
        if not (deployment.get("metadata") or {}).get("deletionTimestamp"):
            keep |= live_config_refs(deployment)
    return prune(kubectl, component, {"configmaps": keep}, writer, resources=("configmaps",),
                 only=content_addressed)


def preflight(writer, objects):
    """Refuse before any write when an owned live object could not be updated,
    so a rejected bundle never leaves earlier objects half-applied. Writer.apply
    re-checks each object immediately before writing it."""
    for item in objects:
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        if resource != "configmaps":
            continue
        current = writer.get(resource, item["metadata"]["name"])
        if current is None or ownership(current, writer.component):
            continue  # Absent objects are created; foreign ones fail as a Conflict in apply.
        irreversible = immutable_problem(current, item)
        if irreversible:
            raise Rejected(f"{item['kind']}/{item['metadata']['name']} in {writer.namespace} {irreversible}")


def apply(kubectl, objects, component, writer=None):
    """Apply every object (ConfigMaps first), then prune. ConfigMaps go first
    so that a Deployment never references a content-addressed ConfigMap that
    does not exist yet; after each ConfigMap write the helper re-reads it and
    requires exactly this bundle's content before writing any Deployment.
    Pruning then deletes the content-addressed ConfigMaps of earlier bundles:
    Pods of the previous template keep the environment and files they
    already read; one that restarts before the rollout replaces it cannot
    start (rather than starting with different data). Leftover ConfigMaps of
    an earlier failed apply are freed first (free_configmaps) so they do not
    consume the namespace quota the new ones need."""
    writer = writer or Writer(kubectl, component)
    preflight(writer, objects)
    freed = free_configmaps(kubectl, component, objects, writer)
    desired = {}
    for item in sorted(objects, key=lambda item: item["kind"] != "ConfigMap"):
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        name = item["metadata"]["name"]
        writer.apply(item)
        if resource == "configmaps":
            ok, reason = object_ready(writer.get(resource, name), item, component)
            if not ok:
                raise Conflict(f"{reason} right after it was written; not writing its consumers")
        desired.setdefault(resource, set()).add(name)
    return freed + prune(kubectl, component, desired, writer)


def prune(kubectl, component, desired, writer=None, resources=None, only=None):
    """Delete this component's listed objects the bundle does not contain;
    with `only`, just those the predicate accepts. Each delete is bound to
    the listed object's UID and resourceVersion (Writer.delete)."""
    writer = writer or Writer(kubectl, component)
    selector = f"{MANAGED_BY}={FIELD_MANAGER},{COMPONENT_LABEL}={component}"
    removed = []
    for resource in resources or ALLOWED_KINDS.values():
        listed = kubectl.json("get", resource, "-l", selector) or {}
        items = {item["metadata"]["name"]: item for item in listed.get("items") or []
                 if only is None or only(item)}
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
    ReplicaSets and Pods are gone; no Pod/ReplicaSet permission is needed.
    A Deployment's exact template names the content-addressed ConfigMaps its
    Pods read, so a completed rollout of it is a rollout of this bundle's
    configuration."""
    desired = {}
    for item in sorted(objects, key=lambda item: item["kind"] != "ConfigMap"):
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        name = item["metadata"]["name"]
        desired.setdefault(resource, set()).add(name)
        current = kubectl.json("get", resource, name, "--ignore-not-found")
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
    parser.add_argument("--interval", type=float, default=30.0,
                        help=f"run: seconds between reconciliations; must be finite and at least {MIN_RUN_INTERVAL:g}")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="remove: seconds to wait, after the deletes are issued, for the objects (and their Pods) to disappear; polling API calls are cut off at this deadline; must be finite and 0 or greater")
    args = parser.parse_args(argv)
    try:
        # Reject unusable pacing before any profile, connection or write: a
        # zero, negative, NaN or tiny interval (one below the monotonic
        # clock's resolution adds nothing to a large reading) would re-apply
        # with no real pause, and an infinite or NaN timeout cannot bound the
        # removal wait.
        if args.command == "run":
            _require(math.isfinite(args.interval) and args.interval >= MIN_RUN_INTERVAL,
                     f"--interval must be a finite number of seconds, at least {MIN_RUN_INTERVAL:g}")
        if args.command == "remove":
            _require(math.isfinite(args.timeout) and args.timeout >= 0,
                     "--timeout must be a finite number of seconds, 0 or greater")
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
                # Always pause at least once, so a reconciliation is never
                # issued back to back even if the clock does not advance.
                deadline = time.monotonic() + args.interval
                while not stopping:
                    time.sleep(0.2)
                    if time.monotonic() >= deadline:
                        break
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
