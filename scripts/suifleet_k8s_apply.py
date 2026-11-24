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
- Only Deployments, ConfigMaps and ClusterIP Services are accepted. Pod
  specifications are rejected when they request host namespaces, host paths,
  privilege, added capabilities, Secrets, ServiceAccount tokens or mutable
  image tags. The cluster's RBAC and Pod Security Admission remain the
  authoritative boundary; this validation is early, explainable failure.
- Objects carry `app.kubernetes.io/managed-by=suifleet` and the component's
  label. Server-side apply uses one field manager and never forces conflicts;
  existing objects that SuiFleet did not create are never adopted. A field
  changed by another manager makes the apply fail instead of being overwritten.
- Pruning deletes only objects labelled for this component that the current
  bundle no longer contains.

It manages workloads inside a cluster. It does not install, configure or
update Kubernetes, nodes, the OS, firmware or the SuiFleet client, and it never
reboots anything. Requires Python >= 3.9 and a kubectl client.

Subcommands (component name/version default to the supervisor's
SUIFLEET_COMPONENT / SUIFLEET_COMPONENT_VERSION environment):
  validate --bundle FILE                      offline validation only
  apply    --bundle FILE --profile NAME        validate, apply once, prune
  run      --bundle FILE --profile NAME        apply, then re-apply every
                                              --interval seconds (foreground)
  ready    --bundle FILE --profile NAME        exit 0 when the bundle's
                                              Deployments are rolled out
  remove   --profile NAME --component NAME     delete the component's objects
"""
import argparse
import hashlib
import json
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
KUBECTL_TIMEOUT = 60

# (apiVersion, kind) -> kubectl resource name used for get/delete/prune.
ALLOWED_KINDS = {
    ("v1", "ConfigMap"): "configmaps",
    ("v1", "Service"): "services",
    ("apps/v1", "Deployment"): "deployments.apps",
}
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
        capabilities = context.get("capabilities") or {}
        _require(isinstance(capabilities, dict), f"{where}.capabilities must be an object")
        _require(not capabilities.get("add"), f"{where}.capabilities.add is not allowed")


def _container(where, container):
    _require(isinstance(container, dict), f"{where} must be an object")
    _require(isinstance(container.get("name"), str) and DNS_LABEL.match(container["name"]),
             f"{where}.name must be a DNS label")
    image = container.get("image")
    _require(isinstance(image, str) and IMAGE_DIGEST.match(image),
             f"{where}.image must be an explicit repository pinned by @sha256 digest")
    for port in container.get("ports") or []:
        _require(isinstance(port, dict) and "hostPort" not in port and "hostIP" not in port,
                 f"{where}.ports must not use hostPort/hostIP")
    for item in container.get("envFrom") or []:
        _require(isinstance(item, dict) and "secretRef" not in item, f"{where}.envFrom must not reference Secrets")
    for item in container.get("env") or []:
        source = isinstance(item, dict) and item.get("valueFrom") or {}
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
        for index, container in enumerate(spec.get(kind) or []):
            _container(f"{where}.{kind}[{index}]", container)
    for index, volume in enumerate(spec.get("volumes") or []):
        _require(isinstance(volume, dict), f"{where}.volumes[{index}] must be an object")
        sources = set(volume) - {"name"}
        _require(len(sources) == 1 and sources <= ALLOWED_VOLUME_SOURCES,
                 f"{where}.volumes[{index}] may only use one of {sorted(ALLOWED_VOLUME_SOURCES)}")
    _security_context(f"{where}.securityContext", spec.get("securityContext"), False)


def _resource(index, resource):
    where = f"resources[{index}]"
    _require(isinstance(resource, dict), f"{where} must be an object")
    key = (resource.get("apiVersion"), resource.get("kind"))
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
        data = resource.get("data") or {}
        _require(isinstance(data, dict) and all(isinstance(v, str) for v in data.values()),
                 f"{where}.data must map keys to strings")
    elif kind == "Service":
        _keys(where, resource, ("apiVersion", "kind", "metadata", "spec"))
        spec = resource.get("spec")
        _keys(f"{where}.spec", spec, ("type", "selector", "ports", "clusterIP", "sessionAffinity"))
        _require(spec.get("type", "ClusterIP") == "ClusterIP", f"{where}.spec.type must be ClusterIP")
        for port in spec.get("ports") or []:
            _require(isinstance(port, dict) and "nodePort" not in port, f"{where}.spec.ports must not set nodePort")
    else:
        _keys(where, resource, ("apiVersion", "kind", "metadata", "spec"))
        spec = resource.get("spec")
        _keys(f"{where}.spec", spec, ("replicas", "selector", "template", "strategy", "minReadySeconds",
                                       "revisionHistoryLimit", "progressDeadlineSeconds"))
        template = spec.get("template")
        _keys(f"{where}.spec.template", template, ("metadata", "spec"))
        template_metadata = template.get("metadata") or {}
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
    except ValueError as error:
        raise Rejected(f"bundle is not JSON: {error}") from None
    validate_bundle(bundle)
    return bundle, hashlib.sha256(data).hexdigest()


def validate_bundle(bundle):
    _keys("bundle", bundle, ("version", "resources"))
    _require(bundle.get("version") == BUNDLE_VERSION, f"bundle version must be {BUNDLE_VERSION}")
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
    verified TLS: no exec/auth-provider plugins, impersonation or basic auth."""
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
    _require(user.get("token") or user.get("tokenFile") or user.get("client-certificate-data") or user.get("client-certificate"),
             "kubeconfig user must have a token or client certificate")


# ------------------------------------------------------------------- kubectl

class Kubectl:
    def __init__(self, profile, cache_dir):
        self.profile = profile
        self.base = list(profile["kubectl"]) + ["--kubeconfig", profile["kubeconfig"], "--cache-dir", cache_dir,
                                                 "--request-timeout", "30s"]
        # Never inherit KUBECONFIG, contexts or credentials from the environment.
        self.env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": cache_dir, "LANG": "C"}

    def __call__(self, *args, stdin=None, namespaced=True):
        command = self.base + (["--namespace", self.profile["namespace"]] if namespaced else []) + list(args)
        return subprocess.run(command, input=stdin, capture_output=True, text=True, env=self.env,
                              timeout=KUBECTL_TIMEOUT, check=False)

    def json(self, *args, namespaced=True):
        result = self(*args, "-o", "json", namespaced=namespaced)
        if result.returncode != 0:
            raise Rejected(f"kubectl {' '.join(args)} failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def connect(profile, cache_dir):
    kubectl = Kubectl(profile, cache_dir)
    check_credentials(kubectl.json("config", "view", "--minify", "--raw", namespaced=False))
    return kubectl


def apply(kubectl, objects, component):
    namespace = kubectl.profile["namespace"]
    desired = {}
    for item in objects:
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        name = item["metadata"]["name"]
        existing = kubectl.json("get", resource, name, "--ignore-not-found")
        problem = ownership(existing, component)
        if problem:
            raise Conflict(f"{item['kind']}/{name} in {namespace} {problem}")
        desired.setdefault(resource, set()).add(name)
    if objects:
        payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects})
        # No --force-conflicts: a field owned by another manager fails the apply.
        result = kubectl("apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-", stdin=payload)
        if result.returncode != 0:
            error = result.stderr.strip()[:1000]
            if "conflict" in error.lower():
                raise Conflict(f"another field manager owns fields in the bundle; not overriding: {error}")
            raise Rejected(f"kubectl apply failed: {error}")
    return prune(kubectl, component, desired)


def prune(kubectl, component, desired):
    selector = f"{MANAGED_BY}={FIELD_MANAGER},{COMPONENT_LABEL}={component}"
    removed = []
    for resource in ALLOWED_KINDS.values():
        listed = kubectl.json("get", resource, "-l", selector) or {}
        for name in prune_candidates(listed.get("items") or [], desired.get(resource, set())):
            result = kubectl("delete", resource, name, "--ignore-not-found", "--wait=false")
            if result.returncode != 0:
                raise Rejected(f"kubectl delete {resource}/{name} failed: {result.stderr.strip()[:500]}")
            removed.append(f"{resource}/{name}")
    return removed


def ready(kubectl, objects, digest):
    for item in objects:
        resource = ALLOWED_KINDS[(item["apiVersion"], item["kind"])]
        name = item["metadata"]["name"]
        current = kubectl.json("get", resource, name, "--ignore-not-found")
        if current is None:
            return False, f"{item['kind']}/{name} is missing"
        if item["kind"] == "Deployment":
            ok, reason = deployment_ready(current, digest)
            if not ok:
                return False, f"Deployment/{name}: {reason}"
    return True, "ready"


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
                removed = prune(kubectl, args.component, {})
                _log(f"removed {removed}")
                return 0
            objects = render(bundle, profile["namespace"], args.component, args.version, digest)
            if args.command == "ready":
                ok, reason = ready(kubectl, objects, digest)
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
