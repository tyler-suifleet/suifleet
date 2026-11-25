#!/usr/bin/env python3
"""SF-305: real local-cluster acceptance of the optional Kubernetes publisher.

Starts a disposable, digest-pinned k3s server in Docker (published only on
127.0.0.1), applies scripts/kubernetes/cluster-setup.yaml with the cluster
administrator credential, mints a short-lived ServiceAccount token, writes a
narrow profile and then exercises scripts/suifleet_k8s_apply.py with ONLY that
ServiceAccount credential:

- RBAC: the publisher can manage Deployments/ConfigMaps in its own namespace
  and nothing else (Services/networking, Secrets, Pods, exec, RBAC, quotas,
  namespaces, nodes, other namespaces, cluster-scoped objects).
- Pod Security Admission rejects a privileged pod, and the administrator's
  ValidatingAdmissionPolicy rejects Secret references, non-default
  ServiceAccounts, token automount, pull Secrets and unpinned images, even
  when the helper is bypassed with the same credential.
- Profile/credential hygiene and offline bundle rejection.
- Apply, ownership-checked readiness (including ConfigMap-only bundles and
  foreign or stale objects), content served through an administrator-owned
  Service, update, prune and rollback. Readiness includes prune convergence:
  an empty retirement bundle, a same-owner replacement left by a pruning race
  and a finalizer-delayed prune are all reported not ready until gone.
  Pruning a Deployment is foreground: while a finalizer holds one of its Pods
  the Deployment stays terminating and a retirement bundle is not ready; it
  becomes ready only once the Pod, ReplicaSet and Deployment are gone.
- No adoption of pre-existing objects and no overriding of fields owned by
  another field manager (two controllers do not fight). A healthy rollout of
  another manager's pod-template edit that keeps SuiFleet's metadata is not
  ready, and becomes ready once the bundle's template is restored.
- Races injected between the helper's ownership check and its write (through
  a kubectl wrapper): a competing creation, a replacement and a relabel are
  not mutated, and pruning leaves relabelled or replaced objects alone, even a
  replacement that carries this component's labels.
- Foreground run mode re-creates a deleted object, exits on SIGTERM and leaves
  the workload running.
- `remove` exits 1 while a finalizer holds a Pod of the removed Deployment
  (and a retry does not re-delete), and exits 0 only once the component's
  objects and Pods are gone.
- Optionally (--supervisor) the real SuiFleet supervisor deploys, fails
  readiness on a broken bundle and rolls back, then retires the component
  with an empty bundle whose readiness waits for a finalizer-delayed prune
  and for a held Pod of the pruned Deployment (ignored Rust test).

Requires Linux, Docker able to run a privileged container, network access to
pull the pinned k3s and busybox images (or a pre-populated cache), Python 3.9+
and, for --supervisor, Rust 1.94. No chain, wallet, Walrus, production
credential, physical device or firmware updater is used. The container and
temporary directory are always removed.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/suifleet_k8s_apply.py"
SETUP = ROOT / "scripts/kubernetes/cluster-setup.yaml"
BUNDLE = ROOT / "scripts/kubernetes/example-bundle.json"
K3S_IMAGE = "docker.io/rancher/k3s:v1.31.4-k3s1@sha256:627fc392e5e7992fb8aa738fb04bc0c9ae9a068999cb4cfbea5858661cd79d04"
NAMESPACE = "suifleet-apps"
ACCOUNT = "suifleet-publisher"
COMPONENT = "web-app"
MISSING_IMAGE = "docker.io/library/busybox@sha256:" + "0" * 64
results = []


def check(name, condition, detail=""):
    results.append({"check": name, "passed": bool(condition), "detail": detail})
    print(f"{'PASS' if condition else 'FAIL'}: {name}" + (f" ({detail})" if detail and not condition else ""), flush=True)
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def run(*command, stdin=None, env=None, timeout=120, ok=True):
    result = subprocess.run(list(command), input=stdin, capture_output=True, text=True, env=env, timeout=timeout)
    if ok and result.returncode != 0:
        raise AssertionError(f"{command[:4]} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result


def wait(what, condition, timeout=180, interval=1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except (AssertionError, subprocess.TimeoutExpired, ValueError) as error:
            last = error
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}: {last}")


class Cluster:
    def __init__(self, work):
        self.work = work
        self.name = f"sf305-k3s-{secrets.token_hex(4)}"
        self.kubectl = work / "kubectl"
        self.admin_config = work / "admin.kubeconfig"

    def start(self):
        run("docker", "run", "-d", "--privileged", "--name", self.name, "--label", "io.suifleet.test=sf305",
            "-p", "127.0.0.1::6443", K3S_IMAGE, "server", "--disable=traefik", "--disable=metrics-server",
            "--disable=servicelb", "--disable=local-storage", "--tls-san=127.0.0.1",
            "--write-kubeconfig-mode=600",
            # A nearly full development disk must not taint the disposable node
            # with disk-pressure; this changes only the test node's eviction.
            "--kubelet-arg=eviction-hard=imagefs.available<512Mi,nodefs.available<512Mi",
            "--kubelet-arg=image-gc-high-threshold=99", "--kubelet-arg=image-gc-low-threshold=98",
            timeout=300)
        port = run("docker", "port", self.name, "6443/tcp").stdout.split(":")[-1].strip()
        wait("k3s kubeconfig", lambda: run("docker", "exec", self.name, "test", "-s", "/etc/rancher/k3s/k3s.yaml", ok=False).returncode == 0)
        binary = self.work / "k3s"
        run("docker", "cp", f"{self.name}:/bin/k3s", str(binary))
        self.kubectl.symlink_to(binary)  # k3s is multi-call; argv[0] selects kubectl.
        config = run("docker", "exec", self.name, "cat", "/etc/rancher/k3s/k3s.yaml").stdout
        self.admin_config.write_text(config.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{port}"))
        self.admin_config.chmod(0o600)
        wait("node Ready", lambda: "True" in self.admin("get", "nodes", "-o",
             "jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}").stdout)
        wait("default ServiceAccount", lambda: self.admin("get", "serviceaccount", "default", ok=False).returncode == 0)
        return port

    def admin(self, *args, stdin=None, ok=True):
        return run(str(self.kubectl), "--kubeconfig", str(self.admin_config), *args, stdin=stdin, ok=ok)

    def publisher(self, *args, stdin=None, ok=True):
        return run(str(self.kubectl), "--kubeconfig", str(self.work / "profiles/edge/kubeconfig"),
                   "--namespace", NAMESPACE, *args, stdin=stdin, ok=ok)

    def stop(self):
        try:
            subprocess.run(["docker", "rm", "--force", "--volumes", self.name], capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"WARNING: container {self.name} (label io.suifleet.test=sf305) was not removed; remove it manually", flush=True)


def write_profile(cluster, profile_root, name, token, insecure=False, kubectl=None):
    view = json.loads(cluster.admin("config", "view", "--raw", "--minify", "-o", "json").stdout)
    server = view["clusters"][0]["cluster"]["server"]
    cluster_entry = {"server": server}
    if insecure:
        cluster_entry["insecure-skip-tls-verify"] = True
    else:
        cluster_entry["certificate-authority-data"] = view["clusters"][0]["cluster"]["certificate-authority-data"]
    kubeconfig = {"apiVersion": "v1", "kind": "Config", "current-context": "edge",
                  "clusters": [{"name": "edge", "cluster": cluster_entry}],
                  "users": [{"name": ACCOUNT, "user": {"token": token}}],
                  "contexts": [{"name": "edge", "context": {"cluster": "edge", "user": ACCOUNT, "namespace": NAMESPACE}}]}
    directory = profile_root / name
    directory.mkdir(mode=0o700, parents=True)
    path = directory / "kubeconfig"
    path.write_text(json.dumps(kubeconfig))
    path.chmod(0o600)
    profile = directory / "profile.json"
    profile.write_text(json.dumps({"version": 1, "namespace": NAMESPACE, "kubeconfig": "kubeconfig",
                                   "kubectl": [str(kubectl or cluster.kubectl)]}))
    profile.chmod(0o644)
    return directory


RACE_WRAPPER = """#!{python}
# Test-only kubectl wrapper: runs the queued administrator actions immediately
# before the first matching helper kubectl call, then forwards to kubectl.
import json, os, subprocess, sys
hook, real = {hook!r}, {real!r}
args = sys.argv[1:]
try:
    with open(hook) as handle:
        queued = json.load(handle)
except FileNotFoundError:
    queued = None
if queued and all(token in args for token in queued["match"]):
    os.unlink(hook)
    for command in queued["commands"]:
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    with open(hook + ".fired", "w") as handle:
        handle.write(" ".join(args))
os.execv(real, [real] + args)
"""


class Race:
    """Queue administrator actions to run inside the helper's read-check-write window."""

    def __init__(self, cluster, work):
        self.cluster, self.hook = cluster, work / "race-hook.json"
        self.wrapper = work / "race-kubectl"
        self.wrapper.write_text(RACE_WRAPPER.format(python=sys.executable, hook=str(self.hook), real=str(cluster.kubectl)))
        self.wrapper.chmod(0o700)

    def arm(self, match, *commands):
        admin = [str(self.cluster.kubectl), "--kubeconfig", str(self.cluster.admin_config), "-n", NAMESPACE]
        Path(str(self.hook) + ".fired").unlink(missing_ok=True)
        self.hook.write_text(json.dumps({"match": list(match), "commands": [admin + list(c) for c in commands]}))

    def fired(self):
        return Path(str(self.hook) + ".fired").exists() and not self.hook.exists()


def helper(command, profile_root, bundle=None, profile="edge", extra=(), component=COMPONENT, version="1.0.0"):
    env = {"PATH": "/usr/bin:/bin", "SUIFLEET_COMPONENT": component, "SUIFLEET_COMPONENT_VERSION": version,
           # Must be ignored: the helper never inherits ambient kube credentials.
           "KUBECONFIG": "/nonexistent/should-not-be-used"}
    args = [sys.executable, str(HELPER), command, "--profile", profile, "--profile-root", str(profile_root)]
    if bundle:
        args += ["--bundle", str(bundle)]
    return run(*args, *extra, env=env, timeout=240, ok=False)


def variant(work, name, message, extra_configmap=None, image=None, foreign=None):
    bundle = json.loads(BUNDLE.read_text())
    for resource in bundle["resources"]:
        if resource["kind"] == "ConfigMap":
            resource["data"]["index.html"] = f"<p>{message}</p>\n"
        if resource["kind"] == "Deployment":
            template = resource["spec"]["template"]
            template["metadata"]["annotations"]["example.com/content"] = message
            if image:
                template["spec"]["containers"][0]["image"] = image
                resource["spec"]["progressDeadlineSeconds"] = 15
    for extra in filter(None, (extra_configmap, foreign)):
        bundle["resources"].append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": extra}, "data": {"k": message}})
    path = work / f"{name}.json"
    path.write_text(json.dumps(bundle))
    return path


def served(cluster):
    pod = cluster.admin("get", "pods", "-n", NAMESPACE, "-l", "app=web", "--field-selector=status.phase=Running",
                        "-o", "jsonpath={.items[0].metadata.name}").stdout
    return cluster.admin("exec", "-n", NAMESPACE, pod, "--", "wget", "-qO-", "http://web").stdout


def wait_ready(profile_root, bundle, version):
    wait(f"rollout of {bundle.name}", lambda: helper("ready", profile_root, bundle, version=version).returncode == 0,
         timeout=240, interval=2)


def scenario(cluster, work, supervisor):
    profiles = work / "profiles"
    profiles.mkdir(mode=0o700)
    cluster.admin("apply", "-f", str(SETUP))
    # Networking is administrator-owned: the admin, not SuiFleet, exposes the workload.
    cluster.admin("apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": NAMESPACE},
        "spec": {"selector": {"app": "web"}, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}}))
    token = cluster.admin("create", "token", ACCOUNT, "-n", NAMESPACE, "--duration=1h").stdout.strip()
    edge = write_profile(cluster, profiles, "edge", token)
    write_profile(cluster, profiles, "insecure", token, insecure=True)
    race = Race(cluster, work)
    write_profile(cluster, profiles, "race", token, kubectl=race.wrapper)

    # RBAC: narrow namespaced Role, no cluster-admin shortcut.
    allowed = [("patch", "deployments.apps"), ("create", "configmaps"), ("delete", "configmaps"), ("list", "deployments.apps")]
    denied = [("create", "services"), ("patch", "services"), ("delete", "services"),
              ("create", "ingresses.networking.k8s.io"), ("create", "networkpolicies.networking.k8s.io"),
              ("get", "secrets"), ("create", "pods"), ("create", "pods/exec"), ("get", "pods/log"),
              ("create", "roles.rbac.authorization.k8s.io"), ("create", "rolebindings.rbac.authorization.k8s.io"),
              ("patch", "resourcequotas"), ("patch", "limitranges"), ("create", "serviceaccounts/token"),
              ("patch", "serviceaccounts"), ("create", "daemonsets.apps"), ("create", "persistentvolumeclaims")]
    for verb, resource in allowed:
        check(f"rbac allows {verb} {resource} in {NAMESPACE}", cluster.publisher("auth", "can-i", verb, resource, ok=False).stdout.strip() == "yes")
    for verb, resource in denied:
        check(f"rbac denies {verb} {resource} in {NAMESPACE}", cluster.publisher("auth", "can-i", verb, resource, ok=False).stdout.strip() == "no")
    for verb, resource in [("create", "deployments.apps"), ("create", "configmaps")]:
        answer = cluster.publisher("auth", "can-i", verb, resource, "--namespace", "default", ok=False).stdout.strip()
        check(f"rbac denies {verb} {resource} in default", answer == "no")
    for verb, resource in [("get", "namespaces"), ("list", "nodes"), ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("create", "clusterrolebindings.rbac.authorization.k8s.io"), ("create", "customresourcedefinitions.apiextensions.k8s.io"),
                           ("*", "*")]:
        answer = run(str(cluster.kubectl), "--kubeconfig", str(edge / "kubeconfig"), "auth", "can-i", verb, resource,
                     "--all-namespaces", ok=False).stdout.strip()
        check(f"rbac denies cluster-wide {verb} {resource}", answer == "no")

    # Pod Security Admission is authoritative even when the helper is bypassed.
    privileged = json.loads(BUNDLE.read_text())["resources"][1]
    privileged["metadata"] = {"name": "bypass", "namespace": NAMESPACE}
    privileged["spec"]["selector"] = {"matchLabels": {"app": "bypass"}}
    pod = privileged["spec"]["template"]
    pod["metadata"] = {"labels": {"app": "bypass"}}
    pod["spec"].pop("volumes")
    container = pod["spec"]["containers"][0]
    container.pop("volumeMounts")
    container["securityContext"] = {"privileged": True}
    pod["spec"]["automountServiceAccountToken"] = False  # passes the admission policy; PSA must still reject
    bypass = cluster.publisher("apply", "--server-side", "--field-manager=bypass-test", "-f", "-", stdin=json.dumps(privileged), ok=False)
    check("PSA warns on a privileged pod template applied with the publisher credential",
          bypass.returncode == 0 and "would violate PodSecurity" in bypass.stderr, bypass.stderr[-500:])

    def rejected():
        events = cluster.admin("get", "events", "-n", NAMESPACE, "--field-selector", "reason=FailedCreate", "-o", "json").stdout
        return any("violates PodSecurity" in e.get("message", "") for e in json.loads(events)["items"])
    wait("PSA rejection of bypass pods", rejected, timeout=60)
    pods = cluster.admin("get", "pods", "-n", NAMESPACE, "-l", "app=bypass", "-o", "json").stdout
    check("PSA admitted no privileged pod", json.loads(pods)["items"] == [])
    cluster.publisher("delete", "deployment", "bypass")

    # The administrator's admission policy enforces the credential-level
    # restrictions that PSA does not, also when the helper is bypassed.
    def bypass_variant(mutate):
        item = json.loads(BUNDLE.read_text())["resources"][1]
        item["metadata"] = {"name": "policy-bypass", "namespace": NAMESPACE}
        item["spec"]["template"]["spec"]["automountServiceAccountToken"] = False
        mutate(item["spec"]["template"]["spec"])
        return cluster.publisher("apply", "--server-side", "--field-manager=bypass-test", "-f", "-",
                                 stdin=json.dumps(item), ok=False)
    first = lambda spec: spec["containers"][0]
    cases = {
        "a Secret env reference": lambda spec: first(spec).update(env=[{"name": "T", "valueFrom": {"secretKeyRef": {"name": "any", "key": "k"}}}]),
        "a Secret envFrom reference": lambda spec: first(spec).update(envFrom=[{"secretRef": {"name": "any"}}]),
        "a Secret volume": lambda spec: spec["volumes"].append({"name": "s", "secret": {"secretName": "any"}}),
        "a non-default ServiceAccount": lambda spec: spec.update(serviceAccountName=ACCOUNT),
        "ServiceAccount token automount": lambda spec: spec.update(automountServiceAccountToken=True),
        "imagePullSecrets": lambda spec: spec.update(imagePullSecrets=[{"name": "any"}]),
        "an unpinned image": lambda spec: first(spec).update(image="docker.io/library/busybox:latest"),
    }
    for name, mutate in cases.items():
        result = bypass_variant(mutate)
        check(f"admission policy denies {name} applied directly with the publisher credential",
              result.returncode != 0 and "ValidatingAdmissionPolicy" in result.stderr, result.stderr[-500:])
    check("admission policy admitted no bypass Deployment",
          cluster.admin("get", "deployment", "policy-bypass", "-n", NAMESPACE, ok=False).returncode != 0)

    # Profile and credential hygiene.
    insecure = helper("apply", profiles, BUNDLE, profile="insecure")
    check("helper refuses a kubeconfig without TLS verification", insecure.returncode == 2 and "verify TLS" in insecure.stderr, insecure.stderr)
    (edge / "kubeconfig").chmod(0o644)
    loose = helper("apply", profiles, BUNDLE)
    check("helper refuses a group/other-readable kubeconfig", loose.returncode == 2 and "0600" in loose.stderr, loose.stderr)
    (edge / "kubeconfig").chmod(0o600)
    check("insecure/loose profile attempts created nothing",
          cluster.admin("get", "deployments,configmaps", "-n", NAMESPACE, "-l", f"suifleet.io/component={COMPONENT}", "-o", "name").stdout.strip() == "")

    # Offline validation before any cluster contact.
    bad = json.loads(BUNDLE.read_text())
    bad["resources"][1]["spec"]["template"]["spec"]["hostNetwork"] = True
    (work / "bad.json").write_text(json.dumps(bad))
    result = run(sys.executable, str(HELPER), "validate", "--bundle", str(work / "bad.json"), ok=False)
    check("helper validate rejects hostNetwork offline", result.returncode == 2 and "hostNetwork" in result.stderr, result.stderr)

    # Apply, readiness and served content.
    v1 = variant(work, "v1", "hello from SuiFleet v1")
    result = helper("apply", profiles, v1)
    check("helper applies v1 with the narrow credential", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")
    check("readiness reports v1 rolled out", True)
    labels = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "--show-managed-fields", "-o", "json").stdout)
    check("objects carry SuiFleet ownership labels and annotations",
          labels["metadata"]["labels"].get("app.kubernetes.io/managed-by") == "suifleet"
          and labels["metadata"]["labels"].get("suifleet.io/component") == COMPONENT
          and labels["metadata"]["annotations"].get("suifleet.io/version") == "1.0.0")
    entries = labels["metadata"]["managedFields"]
    managers = {entry["manager"] for entry in entries if entry.get("operation") == "Apply"}
    check("server-side apply uses the single suifleet field manager", managers == {"suifleet"}, str(managers))
    check("create-only ownership was transferred to server-side apply",
          not any(e["manager"] == "suifleet" and e.get("operation") == "Update" and not e.get("subresource") for e in entries),
          str([(e["manager"], e.get("operation")) for e in entries]))
    check("pods do not mount a ServiceAccount token",
          labels["spec"]["template"]["spec"].get("automountServiceAccountToken") is False)
    wait("v1 content", lambda: "SuiFleet v1" in served(cluster), timeout=60)
    check("administrator-owned service serves v1 content", True)

    # Readiness checks ownership, digest and content for every kind, including
    # ConfigMap-only bundles (no Deployment rollout to wait for).
    config_only = work / "config-only.json"
    config_only.write_text(json.dumps({"version": 1, "resources": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "settings"}, "data": {"mode": "v1"}}]}))
    result = helper("ready", profiles, config_only, component="settings-app")
    check("ConfigMap-only readiness fails while the object is missing", result.returncode == 1 and "missing" in result.stderr, result.stderr)
    cluster.admin("create", "configmap", "settings", "-n", NAMESPACE, "--from-literal=mode=v1")
    result = helper("ready", profiles, config_only, component="settings-app")
    check("readiness rejects a pre-existing foreign ConfigMap even with matching data",
          result.returncode == 1 and "does not adopt" in result.stderr, result.stderr)
    cluster.admin("delete", "configmap", "settings", "-n", NAMESPACE)
    check("ConfigMap-only bundle applies", helper("apply", profiles, config_only, component="settings-app").returncode == 0)
    result = helper("ready", profiles, config_only, component="settings-app")
    check("ConfigMap-only readiness succeeds once applied from this bundle", result.returncode == 0, result.stderr)
    result = helper("ready", profiles, config_only, component="web-app")
    check("readiness rejects an object owned by another component", result.returncode == 1 and "component 'settings-app'" in result.stderr, result.stderr)
    newer = work / "config-only-v2.json"
    newer.write_text(json.dumps({"version": 1, "resources": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "settings"}, "data": {"mode": "v2"}}]}))
    result = helper("ready", profiles, newer, component="settings-app", version="2.0.0")
    check("readiness rejects an object from a stale bundle digest", result.returncode == 1 and "not yet applied" in result.stderr, result.stderr)
    # Empty retirement bundle: readiness includes prune convergence.
    retire = work / "retire.json"
    retire.write_text(json.dumps({"version": 1, "resources": []}))
    result = helper("ready", profiles, retire, component="settings-app")
    check("empty retirement bundle is not ready while the old object remains",
          result.returncode == 1 and "configmaps/settings not yet pruned" in result.stderr, result.stderr)
    result = helper("apply", profiles, retire, component="settings-app")
    check("empty retirement bundle prunes the component",
          result.returncode == 0 and cluster.admin("get", "configmap", "settings", "-n", NAMESPACE, ok=False).returncode != 0,
          result.stderr)
    result = helper("ready", profiles, retire, component="settings-app")
    check("empty retirement bundle is ready once pruning converged", result.returncode == 0, result.stderr)

    # Objects in another namespace are never touched (namespaced credential).
    cluster.admin("create", "configmap", "decoy", "-n", "default", "--from-literal=k=v")
    cluster.admin("label", "configmap", "decoy", "-n", "default", "app.kubernetes.io/managed-by=suifleet", f"suifleet.io/component={COMPONENT}")

    # No adoption of pre-existing objects.
    cluster.admin("create", "configmap", "legacy", "-n", NAMESPACE, "--from-literal=k=customer")
    adopt = helper("apply", profiles, variant(work, "adopt", "hello from SuiFleet v1", foreign="legacy"))
    legacy = json.loads(cluster.admin("get", "configmap", "legacy", "-n", NAMESPACE, "-o", "json").stdout)
    check("helper refuses to adopt an object it did not create",
          adopt.returncode == 3 and "does not adopt" in adopt.stderr and legacy["data"] == {"k": "customer"}
          and "labels" not in legacy["metadata"], adopt.stderr)

    # No fighting over a field owned by another manager.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-content", "namespace": NAMESPACE},
        "data": {"index.html": "<p>customer edit</p>\n"}}))
    fight = helper("apply", profiles, v1)
    content = json.loads(cluster.admin("get", "configmap", "web-content", "-n", NAMESPACE, "-o", "json").stdout)["data"]["index.html"]
    check("helper reports a field-manager conflict instead of overriding it",
          fight.returncode == 3 and "conflict" in fight.stderr.lower() and "customer edit" in content, fight.stderr)
    # The customer's controller relinquishes the field; SuiFleet can apply again.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-content", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, v1)
    check("apply succeeds after the other manager relinquishes the field", result.returncode == 0, result.stderr)

    # Readiness compares the bundle's Deployment spec: another manager's pod
    # template edit keeps SuiFleet's labels/annotations and rolls out
    # healthily, but it is not this bundle's rollout.
    wait_ready(profiles, v1, "1.0.0")
    template_edit = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
                     "spec": {"template": {"metadata": {"annotations": {"example.com/content": "customer rollout"}}}}}
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                  stdin=json.dumps(template_edit))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    drifted = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)
    result = helper("ready", profiles, v1)
    check("readiness rejects a healthy rollout of another manager's pod template edit",
          drifted["metadata"]["annotations"].get("suifleet.io/bundle-sha256")
          and drifted["status"].get("availableReplicas") == 1
          and drifted["status"].get("observedGeneration") == drifted["metadata"]["generation"]
          and result.returncode == 1 and "spec.template.metadata.annotations.example.com/content differs" in result.stderr,
          result.stderr)
    fight = helper("apply", profiles, v1)
    check("apply reports the template conflict instead of overriding it", fight.returncode == 3, fight.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, v1)
    check("apply restores the bundle template after the other manager relinquishes it", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the bundle's template has rolled out again", True)

    # The API server defaults an omitted or empty probe httpGet path to "/";
    # readiness must accept that default rather than time out and roll back.
    for label, path in (("omitted", None), ("empty", "")):
        bundle = json.loads(v1.read_text())
        http_get = bundle["resources"][1]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]
        if path is None:
            del http_get["path"]
        else:
            http_get["path"] = path
        defaulted = work / f"path-{label}.json"
        defaulted.write_text(json.dumps(bundle))
        result = helper("apply", profiles, defaulted)
        check(f"helper applies a bundle with an {label} readiness probe path", result.returncode == 0, result.stderr)
        live_path = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"].get("path")
        result = helper("ready", profiles, defaulted)
        check(f"readiness accepts the server-defaulted path for an {label} bundle path",
              live_path == "/" and result.returncode == 0, f"live path {live_path!r}: {result.stderr}")
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the defaulted-path bundles", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # SetDefaults_Probe replaces an explicit 0 in the non-pointer probe int32s
    # with the default; readiness must accept a healthy rollout of those defaults.
    probe_defaults = {"timeoutSeconds": 1, "periodSeconds": 10, "successThreshold": 1, "failureThreshold": 3}
    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"].update(
        dict.fromkeys(probe_defaults, 0))
    zeroed = work / "probe-zero.json"
    zeroed.write_text(json.dumps(bundle))
    result = helper("apply", profiles, zeroed)
    check("helper applies a bundle with explicit zero readiness probe timings", result.returncode == 0, result.stderr)
    wait_ready(profiles, zeroed, "1.0.0")
    live_probe = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
        "spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    check("readiness accepts the server-defaulted probe timings for explicit zero bundle values",
          all(live_probe.get(field) == default for field, default in probe_defaults.items()), json.dumps(live_probe))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the zero probe timing bundle", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # binaryData is outside the bundle schema; another manager adding it to an
    # owned ConfigMap (projected as an extra file) keeps SuiFleet's markers but
    # is not this bundle's content.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-content", "namespace": NAMESPACE},
        "binaryData": {"extra.sh": "IyEvYmluL3NoCg=="}}))
    result = helper("ready", profiles, v1)
    check("readiness rejects foreign binaryData in an owned ConfigMap",
          result.returncode == 1 and "ConfigMap/web-content content differs" in result.stderr, result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-content", "namespace": NAMESPACE}}))
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the other manager relinquishes its binaryData", True)

    # A map key with an empty value is still meaningful: nodeSelector
    # {"example.com/pool": ""} requires that node label. Label the node so the
    # other manager's edit rolls out healthily, then require readiness to
    # reject it as drift.
    cluster.admin("label", "node", "--all", "--overwrite", "example.com/pool=")
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
        "spec": {"template": {"spec": {"nodeSelector": {"example.com/pool": ""}}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    selected = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)
    result = helper("ready", profiles, v1)
    check("readiness rejects a healthy rollout that adds an empty-valued nodeSelector key",
          selected["spec"]["template"]["spec"].get("nodeSelector") == {"example.com/pool": ""}
          and selected["status"].get("availableReplicas") == 1
          and result.returncode == 1 and "spec.template.spec.nodeSelector.example.com/pool differs" in result.stderr,
          result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    cluster.admin("label", "node", "--all", "example.com/pool-")
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the other manager relinquishes its nodeSelector", True)

    def labels_of(name):
        item = json.loads(cluster.admin("get", "configmap", name, "-n", NAMESPACE, "-o", "json").stdout)
        return item["metadata"].get("labels") or {}, item.get("data") or {}

    def restore():
        cluster.admin("delete", "configmap", "web-content", "-n", NAMESPACE, "--ignore-not-found")
        result = helper("apply", profiles, v1)
        check("apply restores the owned object after the race", result.returncode == 0, result.stderr)

    # Race: a competing creation between the read (absent) and the create.
    racing = variant(work, "race-create", "hello from SuiFleet v1", extra_configmap="race-new")
    race.arm(["create", "--field-manager=suifleet"], ["create", "configmap", "race-new", "--from-literal=k=customer"])
    result = helper("apply", profiles, racing, profile="race")
    found, data = labels_of("race-new")
    check("a competing creation after the ownership check is not adopted",
          race.fired() and result.returncode == 3 and "does not adopt" in result.stderr
          and "app.kubernetes.io/managed-by" not in found and data == {"k": "customer"}, result.stderr)
    cluster.admin("delete", "configmap", "race-new", "-n", NAMESPACE)

    # Race: the owned object is replaced by a customer object after the check.
    race.arm(["apply", "--server-side"], ["delete", "configmap", "web-content", "--wait=true"],
             ["create", "configmap", "web-content", "--from-literal=index.html=customer replacement"])
    result = helper("apply", profiles, variant(work, "race-replace", "hello from SuiFleet race"), profile="race")
    found, data = labels_of("web-content")
    check("a replacement after the ownership check is not mutated",
          race.fired() and result.returncode == 3 and "does not adopt" in result.stderr
          and "app.kubernetes.io/managed-by" not in found and data == {"index.html": "customer replacement"}, result.stderr)
    restore()

    # Race: the customer relabels (takes back) the object after the check.
    race.arm(["apply", "--server-side"], ["label", "configmap", "web-content", "app.kubernetes.io/managed-by-", "suifleet.io/component-"])
    result = helper("apply", profiles, variant(work, "race-relabel", "hello from SuiFleet race"), profile="race")
    found, data = labels_of("web-content")
    check("a relabel after the ownership check is not mutated",
          race.fired() and result.returncode == 3 and "does not adopt" in result.stderr
          and "app.kubernetes.io/managed-by" not in found and "race" not in data["index.html"], result.stderr)
    restore()

    # Race: a prune candidate is relabelled, then replaced, between list and delete.
    # A replacement that still carries this component's labels must not be
    # deleted either: pruning only ever deletes the listed UID.
    v2 = variant(work, "v2", "hello from SuiFleet v2", extra_configmap="v2-extra")
    replace = [["delete", "configmap", "v2-extra", "--wait=true"], ["create", "configmap", "v2-extra", "--from-literal=k=customer"]]
    same_owner = ["label", "configmap", "v2-extra", "app.kubernetes.io/managed-by=suifleet", f"suifleet.io/component={COMPONENT}"]
    for name, commands, expected, owner in (
            ("relabelled", [["label", "configmap", "v2-extra", "app.kubernetes.io/managed-by=customer", "--overwrite"]], {"k": "hello from SuiFleet v2"}, "customer"),
            ("replaced", replace, {"k": "customer"}, None),
            ("same-owner replaced", replace + [same_owner], {"k": "customer"}, "suifleet")):
        check(f"apply v2 before the {name}-prune race", helper("apply", profiles, v2, version="2.0.0").returncode == 0)
        before = json.loads(cluster.admin("get", "configmap", "v2-extra", "-n", NAMESPACE, "-o", "json").stdout)["metadata"]["uid"]
        race.arm(["delete", "--raw"], *commands)
        result = helper("apply", profiles, v1, profile="race")
        after = json.loads(cluster.admin("get", "configmap", "v2-extra", "-n", NAMESPACE, "-o", "json").stdout)["metadata"]
        found, data = labels_of("v2-extra")
        check(f"pruning leaves a {name} object alone",
              race.fired() and result.returncode == 0 and "left configmaps/v2-extra" in result.stderr
              and found.get("app.kubernetes.io/managed-by") == owner and data == expected
              and (after["uid"] != before) == (name != "relabelled"), result.stderr)
        if owner == "suifleet":
            # The surviving same-owner object is stale for v1: readiness reports
            # the incomplete prune instead of success. Readiness reports
            # desired objects first, so let the v1 rollout settle (old
            # replicas may still be terminating) before asserting on it.
            outcome = []
            wait("v1 rollout settled while the same-owner object survives", lambda: outcome.append(
                helper("ready", profiles, v1)) or "not ready: Deployment/" not in outcome[-1].stderr, timeout=180, interval=2)
            result = outcome[-1]
            check("readiness reports an incomplete prune after a same-owner replacement",
                  result.returncode == 1 and "configmaps/v2-extra not yet pruned" in result.stderr, result.stderr)
        cluster.admin("delete", "configmap", "v2-extra", "-n", NAMESPACE)
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the stale object is gone", True)

    # Delayed pruning: a finalizer holds the deleted stale object; readiness
    # waits for it to disappear.
    check("apply v2 before the delayed-prune case", helper("apply", profiles, v2, version="2.0.0").returncode == 0)
    cluster.admin("patch", "configmap", "v2-extra", "-n", NAMESPACE, "--type=merge",
                  "-p", '{"metadata":{"finalizers":["example.com/hold"]}}')
    result = helper("apply", profiles, v1)
    check("rollback with a held stale object issues the prune", result.returncode == 0, result.stderr)
    held = json.loads(cluster.admin("get", "configmap", "v2-extra", "-n", NAMESPACE, "-o", "json").stdout)["metadata"]
    # Readiness reports desired-object problems first: once the v1 rollout has
    # settled, the held stale object is the only remaining reason.
    pending = []

    def only_prune_pending():
        result = helper("ready", profiles, v1)
        pending[:] = [result]
        return result.returncode == 1 and "configmaps/v2-extra not yet pruned" in result.stderr

    wait("readiness to report only the held prune", only_prune_pending, timeout=240, interval=2)
    check("readiness waits for a terminating stale object after the rollout settles",
          held.get("deletionTimestamp") and pending[0].returncode == 1, pending[0].stderr)
    result = helper("apply", profiles, v1)
    check("re-applying while the prune is pending stays successful without re-deleting",
          result.returncode == 0 and "v2-extra" not in result.stderr, result.stderr)
    cluster.admin("patch", "configmap", "v2-extra", "-n", NAMESPACE, "--type=json",
                  "-p", '[{"op":"remove","path":"/metadata/finalizers"}]')
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the delayed prune completes", True)

    # Update with an extra object, then roll back and prune it.
    check("helper applies v2", helper("apply", profiles, v2, version="2.0.0").returncode == 0)
    wait_ready(profiles, v2, "2.0.0")
    wait("v2 content", lambda: "SuiFleet v2" in served(cluster), timeout=90)
    check("administrator-owned service serves v2 content", True)
    result = helper("apply", profiles, v1)
    check("rollback to v1 prunes the v2-only object",
          result.returncode == 0 and "v2-extra" in result.stderr
          and cluster.admin("get", "configmap", "v2-extra", "-n", NAMESPACE, ok=False).returncode != 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")
    wait("v1 content after rollback", lambda: "SuiFleet v1" in served(cluster), timeout=90)
    check("service serves v1 content after rollback", True)

    # Foreground run mode: reconciles deletion, stops cleanly, workload remains.
    env = {"PATH": "/usr/bin:/bin", "SUIFLEET_COMPONENT": COMPONENT, "SUIFLEET_COMPONENT_VERSION": "1.0.0"}
    process = subprocess.Popen([sys.executable, str(HELPER), "run", "--profile", "edge", "--profile-root", str(profiles),
                                "--bundle", str(v1), "--interval", "1"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(3)
        cluster.admin("delete", "configmap", "web-content", "-n", NAMESPACE)
        wait("run mode re-creates the deleted ConfigMap",
             lambda: cluster.admin("get", "configmap", "web-content", "-n", NAMESPACE, ok=False).returncode == 0, timeout=30)
        check("run mode re-creates a deleted owned object", True)
    finally:
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=30)
    check("run mode exits 0 on SIGTERM", code == 0, process.stderr.read()[-500:])
    check("workload keeps running after the client stops",
          cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.status.availableReplicas}").stdout == "1")

    # Retirement waits for the pruned Deployment's dependents: a finalizer
    # holds its Pod, so the ReplicaSet and the (terminating) Deployment remain
    # and an empty bundle must not report ready while the old workload exists.
    def web_pods():
        listed = json.loads(cluster.admin("get", "pods", "-n", NAMESPACE, "-l", "app=web", "-o", "json").stdout)
        return [item["metadata"]["name"] for item in listed["items"]]

    def replicasets():
        listed = json.loads(cluster.admin("get", "replicasets", "-n", NAMESPACE, "-l", "app=web", "-o", "json").stdout)
        return listed["items"]

    held_pods = [pod for pod in web_pods() if cluster.admin(
        "patch", "pod", pod, "-n", NAMESPACE, "--type=merge",
        "-p", '{"metadata":{"finalizers":["example.com/hold"]}}', ok=False).returncode == 0]
    check("a live web Pod is held before the dependent-held retirement", bool(held_pods), str(held_pods))
    result = helper("apply", profiles, retire)
    check("empty retirement bundle issues the Deployment prune while a Pod is held",
          result.returncode == 0 and "deployments.apps/web" in result.stderr, result.stderr)
    deployment = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json", ok=False).stdout or "null")
    check("foreground pruning keeps the Deployment (terminating) while its Pod is held",
          deployment is not None and deployment["metadata"].get("deletionTimestamp")
          and "foregroundDeletion" in deployment["metadata"].get("finalizers", []),
          json.dumps((deployment or {}).get("metadata", {}))[:500])
    time.sleep(5)  # Give the garbage collector time to act, if it would.
    result = helper("ready", profiles, retire)
    check("retirement is not ready while the pruned Deployment's Pod and ReplicaSet remain",
          result.returncode == 1 and "deployments.apps/web not yet pruned" in result.stderr
          and set(held_pods) <= set(web_pods()) and replicasets(), result.stderr)
    result = helper("apply", profiles, retire)
    check("re-applying the retirement while dependents are held does not re-delete",
          result.returncode == 0 and "pruned" not in result.stderr, result.stderr)
    for pod in held_pods:
        cluster.admin("patch", "pod", pod, "-n", NAMESPACE, "--type=merge", "-p", '{"metadata":{"finalizers":null}}')
    wait("retirement ready after the held Pod is released",
         lambda: helper("ready", profiles, retire).returncode == 0, timeout=180, interval=2)
    check("retirement is ready once the Pod, ReplicaSet and Deployment are gone",
          web_pods() == [] and replicasets() == []
          and cluster.admin("get", "deployment", "web", "-n", NAMESPACE, ok=False).returncode != 0)
    check("re-apply v1 after the dependent-held retirement", helper("apply", profiles, v1).returncode == 0)
    wait_ready(profiles, v1, "1.0.0")

    # Removal deletes only this component's objects in its namespace, and its
    # exit status reports convergence: while a finalizer holds a web Pod the
    # foreground-deleted Deployment remains, so remove must not exit 0.
    def remaining_objects():
        return cluster.admin("get", "deployments,configmaps", "-n", NAMESPACE, "-l",
                             f"suifleet.io/component={COMPONENT}", "-o", "name").stdout.strip()

    held_pods = [pod for pod in web_pods() if cluster.admin(
        "patch", "pod", pod, "-n", NAMESPACE, "--type=merge",
        "-p", '{"metadata":{"finalizers":["example.com/hold"]}}', ok=False).returncode == 0]
    check("a live web Pod is held before the dependent-held removal", bool(held_pods), str(held_pods))
    result = helper("remove", profiles, extra=("--component", COMPONENT, "--timeout", "10"))
    check("remove exits 1 (not removed) while a finalizer holds the Deployment's Pod",
          result.returncode == 1 and "not removed" in result.stderr and "deployments.apps/web" in result.stderr
          and set(held_pods) <= set(web_pods()) and "deployment.apps/web" in remaining_objects(), result.stderr)
    started = time.monotonic()
    result = helper("remove", profiles, extra=("--component", COMPONENT, "--timeout", "5"))
    elapsed = time.monotonic() - started
    check("retrying remove while held does not re-delete and still exits 1",
          result.returncode == 1 and "no object to delete" in result.stderr, result.stderr)
    check("remove --timeout 5 returns near its deadline while held", elapsed < 15, f"{elapsed:.1f}s")
    for pod in held_pods:
        cluster.admin("patch", "pod", pod, "-n", NAMESPACE, "--type=merge", "-p", '{"metadata":{"finalizers":null}}')
    result = helper("remove", profiles, extra=("--component", COMPONENT))
    remaining, pods = remaining_objects(), web_pods()
    check("remove exits 0 only once the component's objects and their Pods are gone",
          result.returncode == 0 and remaining == "" and pods == [], result.stderr + remaining + str(pods))
    check("remove leaves the customer's own object", cluster.admin("get", "configmap", "legacy", "-n", NAMESPACE, ok=False).returncode == 0)
    check("objects in other namespaces were never touched",
          cluster.admin("get", "configmap", "decoy", "-n", "default", ok=False).returncode == 0)

    if supervisor:
        # The supervisor fixture's component is named "app": it must not (and
        # cannot) adopt the removed "web-app" objects, so it starts clean.
        bad = variant(work, "broken", "broken", extra_configmap="bad-extra", image=MISSING_IMAGE)
        admin = " ".join(shlex.quote(str(part)) for part in
                         (cluster.kubectl, "--kubeconfig", cluster.admin_config, "-n", NAMESPACE))
        # Hold both a deleted bundle object (the ConfigMap) and a dependent of
        # the pruned Deployment (its Pods) during retirement. A Pod already
        # terminating cannot gain a finalizer; at least one live Pod must be
        # held. Traces go to hold.log, reported if the supervisor test fails.
        trace = work / "hold.log"
        script = work / "hold.sh"
        script.write_text(f"""#!/bin/sh
exec 2>>{shlex.quote(str(trace))}
set -x
k() {{ {admin} "$@"; }}
case "$1" in
  hold) value='["example.com/hold"]' ;;
  release) value=null ;;
  *) exit 2 ;;
esac
patch='{{"metadata":{{"finalizers":'"$value"'}}}}'
k patch configmap web-content --type=merge -p "$patch" >&2 || exit 1
for pod in $(k get pods -l app=web -o name); do
  k patch "$pod" --type=merge -p "$patch" >&2 || true
done
held=$(k get pods -l app=web -o jsonpath='{{.items[*].metadata.finalizers}}')
echo "held finalizers: $held" >&2
case "$1:$held" in
  hold:*example.com/hold*|release:) exit 0 ;;
  release:*example.com/hold*) exit 1 ;;
  release:*) exit 0 ;;
  *) exit 1 ;;
esac
""")
        script.chmod(0o700)
        hold, release = f"{shlex.quote(str(script))} hold", f"{shlex.quote(str(script))} release"
        env = dict(os.environ, SF_K8S_HELPER=str(HELPER), SF_K8S_PROFILE_ROOT=str(profiles),
                   SF_K8S_BUNDLE_GOOD=str(v1), SF_K8S_BUNDLE_BAD=str(bad), SF_K8S_BUNDLE_EMPTY=str(retire),
                   SF_K8S_HOLD=hold, SF_K8S_RELEASE=release, RUSTUP_TOOLCHAIN="1.94.0")
        test = subprocess.run(["cargo", "test", "--locked", "--manifest-path", str(ROOT / "suifleet-edge/Cargo.toml"),
                               "kubernetes_publisher_real_cluster", "--", "--ignored", "--nocapture"],
                              env=env, capture_output=True, text=True, timeout=1800)
        tail = (test.stdout + test.stderr)[-3000:]
        check("real supervisor deploys, rolls back a broken bundle (readiness includes its prune) and waits "
              "for a delayed prune and a held Deployment Pod before an empty retirement bundle is ready",
              test.returncode == 0 and "1 passed" in test.stdout,
              tail + (("\nhold.log:\n" + trace.read_text()[-2000:]) if trace.exists() else ""))
        check("supervisor rollback pruned the broken bundle's extra object",
              cluster.admin("get", "configmap", "bad-extra", "-n", NAMESPACE, ok=False).returncode != 0)
        remaining = cluster.admin("get", "deployments,configmaps", "-n", NAMESPACE, "-l",
                                  "app.kubernetes.io/managed-by=suifleet", "-o", "name").stdout.strip()
        check("supervised empty retirement bundle removed every SuiFleet object", remaining == "", remaining)
        check("supervised retirement left no Pod or ReplicaSet of the pruned Deployment",
              web_pods() == [] and replicasets() == [])
        result = helper("remove", profiles, extra=("--component", "app"))
        check("remove after retirement is a successful no-op", result.returncode == 0, result.stderr)


def main():
    parser = argparse.ArgumentParser(description="SF-305 local-cluster Kubernetes publisher acceptance")
    parser.add_argument("--supervisor", action="store_true", help="also run the real supervisor rollback test (Rust 1.94)")
    parser.add_argument("--report", help="write the JSON check list here")
    args = parser.parse_args()
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="suifleet-k8s-test-") as directory:
        work = Path(directory)
        work.chmod(0o700)
        cluster = Cluster(work)
        error = None
        try:
            cluster.start()
            version = cluster.admin("version", "-o", "json").stdout
            print(f"cluster: {json.loads(version)['serverVersion']['gitVersion']} ({K3S_IMAGE})", flush=True)
            scenario(cluster, work, args.supervisor)
        except Exception as caught:  # Report, then always clean up.
            error = caught
            print(f"ERROR: {caught!r}", flush=True)
            try:  # Cluster-side context for the failure (no credentials).
                for query in (("describe", "nodes"), ("get", "pods", "-A", "-o", "wide"),
                              ("get", "events", "-n", NAMESPACE, "--sort-by=.lastTimestamp")):
                    print(cluster.admin(*query, ok=False).stdout[-3000:], flush=True)
            except Exception as diagnostic:  # noqa: BLE001 - best effort only
                print(f"(diagnostics unavailable: {diagnostic!r})", flush=True)
        finally:
            cluster.stop()
    report = {"ticket": "SF-305", "k3s_image": K3S_IMAGE, "seconds": round(time.time() - started),
              "passed": error is None, "checks": results, "error": str(error) if error else None}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(f"{sum(r['passed'] for r in results)} checks passed" + (f"; FAILED: {error}" if error else ""))
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
