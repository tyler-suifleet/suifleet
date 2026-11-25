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
  an empty retirement bundle, a remaining same-owner stale object and a
  finalizer-delayed prune are all reported not ready until gone.
  Pruning a Deployment is foreground: while a finalizer holds one of its Pods
  the Deployment stays terminating and a retirement bundle is not ready; it
  becomes ready only once the Pod, ReplicaSet and Deployment are gone.
- No adoption of pre-existing objects and no overriding of fields owned by
  another field manager (two controllers do not fight). A healthy rollout of
  another manager's pod-template edit that keeps SuiFleet's metadata is not
  ready, and becomes ready once the bundle's template is restored.
- Races injected between the helper's ownership check and its write (through
  a kubectl wrapper): a competing creation, a replacement and a relabel are
  not mutated, and a racing prune delete leaves relabelled or replaced objects
  alone, even a replacement that carries this component's labels (which the
  same apply's final prune then lists afresh and deletes).
- A configuration-only update and rollback of a ConfigMap consumed through
  envFrom, configMapKeyRef and a subPath mount is observed inside the Pods
  before readiness succeeds, and a Deployment consuming a ConfigMap outside
  its bundle is rejected offline. Consumed ConfigMaps are published immutable
  under content-addressed names that the Pod templates reference, and the API
  server refuses another manager's data change to one. A no-op re-apply
  starts no rollout. After a partially applied update (new ConfigMap written,
  Deployment write denied by a test admission policy) a v1-template Pod
  restarted in between still reads v1, and the rollback converges with every
  Pod on v1.
- A stable ConfigMap name with the reserved content-addressed suffix and a
  bundle over the ConfigMap limit are rejected offline, and an owned mutable
  ConfigMap at a content-addressed name stops the apply before any write.
  Changing every ConfigMap of a bundle at the limit peaks exactly at the
  shipped quota and converges; after a partial apply fills the quota, the
  next bundle frees the leftovers first and converges.
- A bundle with an immutable ConfigMap is rejected offline, and a live owned
  stable-name ConfigMap made immutable out of band (or a content-addressed one
  recreated with other content) stops the apply before any write; the apply
  recreates the object after the administrator deletes it.
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

sys.path.insert(0, str(ROOT / "scripts"))
import suifleet_k8s_apply as publisher  # noqa: E402


def published(bundle, base="web-content"):
    """Content-addressed name the helper publishes bundle ConfigMap `base` as."""
    return publisher.config_names(json.loads(Path(bundle).read_text()))[base]


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


def config_bundle(work, name, mode):
    """A ConfigMap consumed only through env (envFrom and configMapKeyRef) and
    a subPath mount: Kubernetes never refreshes these in running Pods, so the
    Pods see a new value only if the change rolls them out."""
    example = json.loads(BUNDLE.read_text())["resources"][1]
    container = example["spec"]["template"]["spec"]["containers"][0]
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "config-app", "labels": {"app": "config-app"}},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "config-app"}}, "progressDeadlineSeconds": 120,
                 "template": {"metadata": {"labels": {"app": "config-app"}}, "spec": {
                     "terminationGracePeriodSeconds": 5,
                     "securityContext": example["spec"]["template"]["spec"]["securityContext"],
                     "containers": [{
                         "name": "app", "image": container["image"],
                         "command": ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"],
                         "envFrom": [{"configMapRef": {"name": "config-settings"}}],
                         "env": [{"name": "KEYED", "valueFrom": {"configMapKeyRef": {"name": "config-settings", "key": "MODE"}}}],
                         "resources": container["resources"], "securityContext": container["securityContext"],
                         "volumeMounts": [{"name": "settings", "mountPath": "/cfg/mode", "subPath": "MODE", "readOnly": True}]}],
                     "volumes": [{"name": "settings", "configMap": {"name": "config-settings"}}]}}}}
    bundle = {"version": 1, "resources": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "config-settings"}, "data": {"MODE": mode}},
        deployment]}
    path = work / f"{name}.json"
    path.write_text(json.dumps(bundle))
    return path


def quota_bundle(work, name, message, count=publisher.MAX_CONFIGMAPS):
    """A Deployment consuming `count` ConfigMaps (the per-bundle limit by
    default), each through envFrom."""
    path = config_bundle(work, name, message)
    bundle = json.loads(path.read_text())
    config, deployment = bundle["resources"]
    deployment["metadata"]["name"] = "quota-app"
    deployment["metadata"]["labels"] = deployment["spec"]["selector"]["matchLabels"] = \
        deployment["spec"]["template"]["metadata"]["labels"] = {"app": "quota-app"}
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for key in ("env", "volumeMounts"):
        container.pop(key)
    deployment["spec"]["template"]["spec"].pop("volumes")
    container["envFrom"] = [{"configMapRef": {"name": f"quota-{index}"}} for index in range(count)]
    bundle["resources"] = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"quota-{index}"},
                            "data": {f"Q{index}": message}} for index in range(count)] + [deployment]
    path.write_text(json.dumps(bundle))
    return path


def observed_config(cluster):
    """(env MODE, env KEYED, subPath file) as seen inside every running,
    non-terminating config-app Pod."""
    pods = json.loads(cluster.admin("get", "pods", "-n", NAMESPACE, "-l", "app=config-app", "-o", "json").stdout)["items"]
    seen = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp") or pod["status"].get("phase") != "Running":
            continue
        output = cluster.admin("exec", "-n", NAMESPACE, pod["metadata"]["name"], "--", "sh", "-c",
                               'printf "%s|%s|%s" "$MODE" "$KEYED" "$(cat /cfg/mode)"').stdout
        seen.append(tuple(output.split("|")))
    return seen


def served(cluster):
    pod = cluster.admin("get", "pods", "-n", NAMESPACE, "-l", "app=web", "--field-selector=status.phase=Running",
                        "-o", "jsonpath={.items[0].metadata.name}").stdout
    return cluster.admin("exec", "-n", NAMESPACE, pod, "--", "wget", "-qO-", "http://web").stdout


def wait_ready(profile_root, bundle, version, component=COMPONENT):
    wait(f"rollout of {bundle.name}",
         lambda: helper("ready", profile_root, bundle, version=version, component=component).returncode == 0,
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
    # Unusable pacing is rejected with a valid profile, before any cluster call.
    for value in ("0", "0.5", "1e-20", "-1", "nan", "inf"):
        paced = helper("run", profiles, BUNDLE, extra=(f"--interval={value}",))
        check(f"helper run rejects --interval={value} before applying",
              paced.returncode == 2 and "--interval" in paced.stderr, paced.stderr)
    for value in ("-1", "nan", "inf"):
        bounded = helper("remove", profiles, extra=(f"--timeout={value}",))
        check(f"helper remove rejects --timeout={value} before deleting",
              bounded.returncode == 2 and "--timeout" in bounded.stderr, bounded.stderr)
    check("insecure/loose profile and rejected pacing attempts created nothing",
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

    # The consumed ConfigMap is published immutable under its content-addressed
    # name, which the Pod template references: the API server itself refuses
    # another manager's data change to it.
    web_config = published(v1)
    live_config = json.loads(cluster.admin("get", "configmap", web_config, "-n", NAMESPACE, "-o", "json").stdout)
    live_web = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)
    check("the consumed ConfigMap is published immutable under its content-addressed name",
          live_config.get("immutable") is True and "SuiFleet v1" in live_config["data"]["index.html"]
          and live_config["metadata"]["annotations"].get("suifleet.io/config-name") == "web-content"
          and publisher.config_refs(live_web["spec"]["template"]["spec"]) == [web_config]
          and cluster.admin("get", "configmap", "web-content", "-n", NAMESPACE, ok=False).returncode != 0,
          json.dumps(live_config.get("metadata", {}))[:500])
    edit = cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                         ok=False, stdin=json.dumps({
                             "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": web_config, "namespace": NAMESPACE},
                             "data": {"index.html": "<p>customer edit</p>\n"}}))
    unchanged = json.loads(cluster.admin("get", "configmap", web_config, "-n", NAMESPACE, "-o", "json").stdout)["data"]
    check("the API server refuses another manager's data change to a content-addressed ConfigMap",
          edit.returncode != 0 and "immutable" in edit.stderr.lower() and unchanged == live_config["data"], edit.stderr)

    # No fighting over a field owned by another manager (a stable-name
    # ConfigMap that no bundle Deployment consumes stays mutable).
    shared = variant(work, "shared", "hello from SuiFleet v1", extra_configmap="shared-settings")
    check("apply a bundle with an unconsumed stable-name ConfigMap", helper("apply", profiles, shared).returncode == 0)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "shared-settings", "namespace": NAMESPACE},
        "data": {"k": "customer edit"}}))
    fight = helper("apply", profiles, shared)
    content = json.loads(cluster.admin("get", "configmap", "shared-settings", "-n", NAMESPACE, "-o", "json").stdout)["data"]["k"]
    check("helper reports a field-manager conflict instead of overriding it",
          fight.returncode == 3 and "conflict" in fight.stderr.lower() and "customer edit" in content, fight.stderr)
    # The customer's controller relinquishes the field; SuiFleet can apply again.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "shared-settings", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, shared)
    check("apply succeeds after the other manager relinquishes the field", result.returncode == 0, result.stderr)
    # binaryData is outside the bundle schema; another manager adding it to an
    # owned ConfigMap (projected as an extra file) keeps SuiFleet's markers but
    # is not this bundle's content.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "shared-settings", "namespace": NAMESPACE},
        "binaryData": {"extra.sh": "IyEvYmluL3NoCg=="}}))
    result = helper("ready", profiles, shared)
    check("readiness rejects foreign binaryData in an owned ConfigMap",
          result.returncode == 1 and "ConfigMap/shared-settings content differs" in result.stderr, result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "shared-settings", "namespace": NAMESPACE}}))
    wait_ready(profiles, shared, "1.0.0")
    check("readiness succeeds once the other manager relinquishes its binaryData", True)
    result = helper("apply", profiles, v1)
    check("apply returns to v1 and prunes the unconsumed ConfigMap",
          result.returncode == 0 and "configmaps/shared-settings" in result.stderr, result.stderr)

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
          and result.returncode == 1 and 'spec.template.metadata.annotations["example.com/content"] differs' in result.stderr,
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
          and result.returncode == 1 and 'spec.template.spec.nodeSelector["example.com/pool"] differs' in result.stderr,
          result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    cluster.admin("label", "node", "--all", "example.com/pool-")
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the other manager relinquishes its nodeSelector", True)

    # Optional pointer fields keep presence: a container runAsGroup 0 overrides
    # the pod's runAsGroup, an absent one inherits it. The server keeps an
    # explicit 0, and readiness must reject another manager adding one.
    def container_group():
        context = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]["containers"][0]["securityContext"]
        return "runAsGroup" in context, context.get("runAsGroup")

    bundle = json.loads(v1.read_text())
    pod = bundle["resources"][1]["spec"]["template"]["spec"]
    # containerd rejects a pod sandbox group without a pod user.
    pod["securityContext"].update(runAsUser=65534, runAsGroup=65534)
    pod["containers"][0]["securityContext"]["runAsGroup"] = 0
    explicit = work / "group-explicit-zero.json"
    explicit.write_text(json.dumps(bundle))
    result = helper("apply", profiles, explicit)
    check("helper applies a bundle with an explicit container runAsGroup 0", result.returncode == 0, result.stderr)
    wait_ready(profiles, explicit, "1.0.0")
    check("the server keeps an explicit container runAsGroup 0 and readiness accepts it",
          container_group() == (True, 0), repr(container_group()))
    del pod["containers"][0]["securityContext"]["runAsGroup"]
    inherited = work / "group-inherited.json"
    inherited.write_text(json.dumps(bundle))
    result = helper("apply", profiles, inherited)
    check("helper applies a bundle whose container inherits the pod runAsGroup", result.returncode == 0, result.stderr)
    wait_ready(profiles, inherited, "1.0.0")
    check("the inherited bundle leaves the container runAsGroup absent", container_group() == (False, None),
          repr(container_group()))
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
        "spec": {"template": {"spec": {"containers": [{"name": "web", "securityContext": {"runAsGroup": 0}}]}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    overridden = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)
    result = helper("ready", profiles, inherited)
    check("readiness rejects a healthy rollout that overrides the inherited group with runAsGroup 0",
          container_group() == (True, 0) and overridden["status"].get("availableReplicas") == 1
          and result.returncode == 1
          and "spec.template.spec.containers[0].securityContext.runAsGroup differs" in result.stderr,
          result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    wait_ready(profiles, inherited, "1.0.0")
    check("readiness succeeds once the other manager relinquishes its runAsGroup", container_group() == (False, None),
          repr(container_group()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the runAsGroup bundles", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # An explicit JSON null struct field is accepted by the server as unset and
    # defaulted (terminationGracePeriodSeconds null becomes 30); readiness must
    # treat it like the omitted field and still reject a genuine live change.
    def grace():
        pod = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]
        return pod.get("terminationGracePeriodSeconds")

    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = None
    nulled = work / "grace-null.json"
    nulled.write_text(json.dumps(bundle))
    result = helper("apply", profiles, nulled)
    check("helper applies a bundle with an explicit null terminationGracePeriodSeconds", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, nulled, "1.0.0")
    check("the server defaults a null terminationGracePeriodSeconds to 30 and readiness accepts it",
          grace() == 30, repr(grace()))
    # SuiFleet's apply of the null field claims it, so the override must force.
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                  stdin=json.dumps({
                      "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
                      "spec": {"template": {"spec": {"terminationGracePeriodSeconds": 5}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    result = helper("ready", profiles, nulled)
    check("readiness rejects another manager's terminationGracePeriodSeconds against a null bundle field",
          grace() == 5 and result.returncode == 1
          and "spec.template.spec.terminationGracePeriodSeconds differs" in result.stderr, result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, nulled)
    check("apply restores the null-field bundle after the other manager relinquishes it", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, nulled, "1.0.0")
    check("readiness succeeds once the other manager relinquishes terminationGracePeriodSeconds", grace() == 30,
          repr(grace()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the null-field bundle", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # emptyDir sizeLimit is a Kubernetes quantity the server canonicalises
    # ("1.5Gi" is returned as "1536Mi"); readiness must compare it by value and
    # still reject a genuine size change.
    def size_limit():
        pod = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]
        return {v["name"]: v.get("emptyDir", {}).get("sizeLimit") for v in pod.get("volumes", [])}.get("scratch")

    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["volumes"].append(
        {"name": "scratch", "emptyDir": {"sizeLimit": "1.5Gi"}})
    sized = work / "size-limit.json"
    sized.write_text(json.dumps(bundle))
    result = helper("apply", profiles, sized)
    check("helper applies a bundle with an emptyDir sizeLimit of 1.5Gi", result.returncode == 0, result.stderr)
    wait_ready(profiles, sized, "1.0.0")
    check("the server canonicalises sizeLimit 1.5Gi to 1536Mi and readiness accepts it",
          size_limit() == "1536Mi", repr(size_limit()))
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                  stdin=json.dumps({
                      "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
                      "spec": {"template": {"spec": {"volumes": [
                          {"name": "scratch", "emptyDir": {"sizeLimit": "2Gi"}}]}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    result = helper("ready", profiles, sized)
    check("readiness rejects another manager's emptyDir sizeLimit change",
          size_limit() == "2Gi" and result.returncode == 1 and "emptyDir.sizeLimit differs" in result.stderr,
          result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, sized)
    check("apply restores the sizeLimit bundle after the other manager relinquishes it", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, sized, "1.0.0")
    check("readiness succeeds once the sizeLimit is restored", size_limit() == "1536Mi", repr(size_limit()))

    # ParseQuantity rounds a nonzero quantity away from zero to nano precision,
    # so a sizeLimit with a sub-nano fraction is stored rounded up; readiness
    # must expect the rounded value instead of timing out.
    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["volumes"].append(
        {"name": "scratch", "emptyDir": {"sizeLimit": "1073741824.0000000001"}})
    nano = work / "size-limit-nano.json"
    nano.write_text(json.dumps(bundle))
    result = helper("apply", profiles, nano)
    check("helper applies a bundle with a sub-nano emptyDir sizeLimit fraction", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, nano, "1.0.0")
    check("the server rounds the sub-nano sizeLimit up to nano precision and readiness accepts it",
          size_limit() == "1073741824000000001n", repr(size_limit()))

    # ParseQuantity also caps a BinarySI magnitude at 2**63-1, so an accepted
    # sizeLimit of 8Ei is stored as 9223372036854775807; readiness must expect
    # the capped value instead of timing out.
    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["volumes"].append(
        {"name": "scratch", "emptyDir": {"sizeLimit": "8Ei"}})
    capped = work / "size-limit-capped.json"
    capped.write_text(json.dumps(bundle))
    result = helper("apply", profiles, capped)
    check("helper applies a bundle with an 8Ei emptyDir sizeLimit", result.returncode == 0, result.stderr)
    wait_ready(profiles, capped, "1.0.0")
    check("the server caps the BinarySI sizeLimit at 2**63-1 and readiness accepts it",
          size_limit() == "9223372036854775807", repr(size_limit()))

    # Quantity.UnmarshalJSON trims surrounding whitespace before parsing, so a
    # padded " 64Mi " is stored as "64Mi"; readiness must trim it too.
    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["volumes"].append(
        {"name": "scratch", "emptyDir": {"sizeLimit": " 64Mi "}})
    padded = work / "size-limit-padded.json"
    padded.write_text(json.dumps(bundle))
    result = helper("apply", profiles, padded)
    check("helper applies a bundle with a whitespace-padded emptyDir sizeLimit", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, padded, "1.0.0")
    check("the server trims the padded sizeLimit and readiness accepts it",
          size_limit() == "64Mi", repr(size_limit()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the sizeLimit bundle", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # Resource limits/requests are ResourceLists, which the API server rounds
    # up to milli precision ("10.1m" is stored as "11m"); readiness must
    # compare against the rounded value and still reject a genuine change.
    def cpu_request():
        pod = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]
        return pod["containers"][0]["resources"]["requests"]["cpu"]

    bundle = json.loads(v1.read_text())
    container = bundle["resources"][1]["spec"]["template"]["spec"]["containers"][0]
    container["resources"]["requests"]["cpu"] = "10.1m"
    rounded = work / "milli-round-up.json"
    rounded.write_text(json.dumps(bundle))
    result = helper("apply", profiles, rounded)
    check("helper applies a bundle with a cpu request of 10.1m", result.returncode == 0, result.stderr)
    wait_ready(profiles, rounded, "1.0.0")
    check("the server rounds the 10.1m cpu request up to 11m and readiness accepts it",
          cpu_request() == "11m", repr(cpu_request()))
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                  stdin=json.dumps({
                      "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
                      "spec": {"template": {"spec": {"containers": [
                          {"name": container["name"], "resources": {"requests": {"cpu": "12m"}}}]}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    result = helper("ready", profiles, rounded)
    check("readiness rejects another manager's cpu request change against a rounded bundle value",
          cpu_request() == "12m" and result.returncode == 1 and 'requests["cpu"] differs' in result.stderr,
          result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, rounded)
    check("apply restores the rounded cpu request after the other manager relinquishes it", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, rounded, "1.0.0")
    check("readiness succeeds once the cpu request is restored to 11m", cpu_request() == "11m", repr(cpu_request()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the rounded-request bundle", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    # The API server derives an omitted imagePullPolicy from the image: a
    # digest-pinned image without a tag gets IfNotPresent, "repo:latest@sha256"
    # gets Always. It keeps an existing value across an image change, so the
    # helper sends that default explicitly. Readiness must expect exactly that
    # default for each form, follow image changes in both directions and reject
    # another manager's policy change.
    def pull_policy():
        pod = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]["spec"]
        return pod["containers"][0].get("imagePullPolicy")

    pinned = json.loads(v1.read_text())["resources"][1]["spec"]["template"]["spec"]["containers"][0]["image"]
    repository, _, digest = pinned.partition("@")
    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["spec"]["containers"][0]["image"] = f"{repository}:latest@{digest}"
    latest = work / "latest-tag.json"
    latest.write_text(json.dumps(bundle))
    for form, path, expected, changed in (("digest-only", v1, "IfNotPresent", "Always"),
                                          (":latest@digest", latest, "Always", "IfNotPresent")):
        result = helper("apply", profiles, path)
        check(f"helper applies the {form} image bundle", result.returncode == 0, result.stderr)
        wait_ready(profiles, path, "1.0.0")
        check(f"the server defaults the {form} image's imagePullPolicy to {expected} and readiness accepts it",
              pull_policy() == expected, repr(pull_policy()))
        cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                      stdin=json.dumps({
                          "apiVersion": "apps/v1", "kind": "Deployment",
                          "metadata": {"name": "web", "namespace": NAMESPACE},
                          "spec": {"template": {"spec": {"containers": [
                              {"name": container["name"], "imagePullPolicy": changed}]}}}}))
        cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
        result = helper("ready", profiles, path)
        check(f"readiness rejects another manager's imagePullPolicy {changed} for the {form} image",
              pull_policy() == changed and result.returncode == 1
              and "containers[0].imagePullPolicy differs" in result.stderr, result.stderr)
        cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
        result = helper("apply", profiles, path)
        check(f"apply restores the {form} image bundle after the other manager relinquishes imagePullPolicy",
              result.returncode == 0, result.stderr)
        wait_ready(profiles, path, "1.0.0")
        check(f"readiness succeeds once the {form} imagePullPolicy default is restored",
              pull_policy() == expected, repr(pull_policy()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the imagePullPolicy bundles", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")
    check("returning to the digest-only image resets imagePullPolicy to IfNotPresent", pull_policy() == "IfNotPresent",
          repr(pull_policy()))

    # A map key that ends in a quantity field path is still a literal key: a
    # pod-template annotation "example.com/setting.emptyDir.sizeLimit" changed
    # from "1" to "1000m" (equal as quantities) is drift, not canonicalisation.
    dotted = "example.com/setting.emptyDir.sizeLimit"

    def dotted_annotation():
        template = json.loads(cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "json").stdout)[
            "spec"]["template"]
        return (template["metadata"].get("annotations") or {}).get(dotted)

    bundle = json.loads(v1.read_text())
    bundle["resources"][1]["spec"]["template"]["metadata"].setdefault("annotations", {})[dotted] = "1"
    annotated = work / "dotted-annotation.json"
    annotated.write_text(json.dumps(bundle))
    result = helper("apply", profiles, annotated)
    check("helper applies a bundle with a dotted quantity-like annotation key", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, annotated, "1.0.0")
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "--force-conflicts", "-f", "-",
                  stdin=json.dumps({
                      "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE},
                      "spec": {"template": {"metadata": {"annotations": {dotted: "1000m"}}}}}))
    cluster.admin("rollout", "status", "deployment/web", "-n", NAMESPACE, "--timeout=120s")
    result = helper("ready", profiles, annotated)
    check("readiness rejects a dotted annotation changed to an equal-quantity text",
          dotted_annotation() == "1000m" and result.returncode == 1
          and f'spec.template.metadata.annotations["{dotted}"] differs' in result.stderr, result.stderr)
    cluster.admin("apply", "--server-side", "--field-manager=customer-gitops", "-f", "-", stdin=json.dumps(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NAMESPACE}}))
    result = helper("apply", profiles, annotated)
    check("apply restores the dotted annotation after the other manager relinquishes it", result.returncode == 0,
          result.stderr)
    wait_ready(profiles, annotated, "1.0.0")
    check("readiness succeeds once the dotted annotation is restored", dotted_annotation() == "1",
          repr(dotted_annotation()))
    result = helper("apply", profiles, v1)
    check("apply returns to v1 after the dotted-annotation bundle", result.returncode == 0, result.stderr)
    wait_ready(profiles, v1, "1.0.0")

    def labels_of(name):
        item = json.loads(cluster.admin("get", "configmap", name, "-n", NAMESPACE, "-o", "json").stdout)
        return item["metadata"].get("labels") or {}, item.get("data") or {}

    def restore(name):
        cluster.admin("delete", "configmap", name, "-n", NAMESPACE, "--ignore-not-found")
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

    # Race: the owned object is replaced by a customer object after the check
    # (the helper has just created the race bundle's content-addressed
    # ConfigMap and is about to apply over it).
    racing = variant(work, "race-replace", "hello from SuiFleet race")
    race_config = published(racing)
    race.arm(["apply", "--server-side"], ["delete", "configmap", race_config, "--wait=true"],
             ["create", "configmap", race_config, "--from-literal=index.html=customer replacement"])
    result = helper("apply", profiles, racing, profile="race")
    found, data = labels_of(race_config)
    check("a replacement after the ownership check is not mutated",
          race.fired() and result.returncode == 3 and "does not adopt" in result.stderr
          and "app.kubernetes.io/managed-by" not in found and data == {"index.html": "customer replacement"}, result.stderr)
    restore(race_config)

    # Race: the customer relabels (takes back) the object after the check.
    racing = variant(work, "race-relabel", "hello from SuiFleet race")
    race.arm(["apply", "--server-side"], ["label", "configmap", race_config, "app.kubernetes.io/managed-by-", "suifleet.io/component-"])
    before = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    result = helper("apply", profiles, racing, profile="race")
    after = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    found, data = labels_of(race_config)
    check("a relabel after the ownership check is not mutated",
          race.fired() and result.returncode == 3 and "does not adopt" in result.stderr
          and "app.kubernetes.io/managed-by" not in found and before == after, result.stderr)
    restore(race_config)

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
        if owner == "suifleet":
            # The racing delete is the pre-write ConfigMap freeing pass; it
            # leaves the replacement (another UID) alone. The same apply's
            # final prune then lists the replacement afresh as a stale object
            # of this component and deletes that listed UID.
            check(f"pruning leaves a {name} object alone in the racing pass and prunes it once listed",
                  race.fired() and result.returncode == 0 and "left configmaps/v2-extra" in result.stderr
                  and "'configmaps/v2-extra'" in result.stderr
                  and cluster.admin("get", "configmap", "v2-extra", "-n", NAMESPACE, ok=False).returncode != 0,
                  result.stderr)
            cluster.admin("create", "configmap", "v2-extra", "-n", NAMESPACE, "--from-literal=k=customer")
            cluster.admin("label", "configmap", "v2-extra", "-n", NAMESPACE, *same_owner[3:])
        else:
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
            check("readiness reports an incomplete prune for a same-owner stale object",
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

    # Combined review 4602: a configuration-only update or rollback of a
    # ConfigMap consumed through envFrom, configMapKeyRef or a subPath mount
    # must reach the running Pods before readiness succeeds. Kubernetes never
    # refreshes those in place; the helper publishes the ConfigMap under a
    # content-addressed name that the Pod template references, so the change
    # rolls the Pods out.
    outside = json.loads(config_bundle(work, "config-outside", "x").read_text())
    outside["resources"] = outside["resources"][1:]
    (work / "config-outside.json").write_text(json.dumps(outside))
    result = run(sys.executable, str(HELPER), "validate", "--bundle", str(work / "config-outside.json"), ok=False)
    check("helper validate rejects a Deployment consuming a ConfigMap outside the bundle",
          result.returncode == 2 and "not in this bundle" in result.stderr, result.stderr)
    config_v1, config_v2 = config_bundle(work, "config-v1", "alpha"), config_bundle(work, "config-v2", "beta")
    check("config-only v1 applies", helper("apply", profiles, config_v1, component="config-app").returncode == 0)
    wait_ready(profiles, config_v1, "1.0.0", component="config-app")
    seen = observed_config(cluster)
    check("pods observe the v1 configuration once ready", seen == [("alpha",) * 3], str(seen))
    template_v1 = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    check("config-only v2 applies", helper("apply", profiles, config_v2, component="config-app", version="2.0.0").returncode == 0)
    wait_ready(profiles, config_v2, "2.0.0", component="config-app")
    seen = observed_config(cluster)
    check("a config-only update reaches env, keyed env and subPath before readiness succeeds",
          seen == [("beta",) * 3], str(seen))
    template_v2 = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    check("the config-only update started a new rollout",
          template_v2["metadata"]["generation"] > template_v1["metadata"]["generation"]
          and template_v2["spec"]["template"] != template_v1["spec"]["template"])
    check("config-only rollback applies", helper("apply", profiles, config_v1, component="config-app").returncode == 0)
    wait_ready(profiles, config_v1, "1.0.0", component="config-app")
    seen = observed_config(cluster)
    check("a config-only rollback restores the v1 configuration in the pods before readiness succeeds",
          seen == [("alpha",) * 3], str(seen))
    steady = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    check("config-only re-apply of the same bundle succeeds",
          helper("apply", profiles, config_v1, component="config-app").returncode == 0)
    again = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    check("a no-op re-apply keeps the content-addressed name and starts no rollout",
          again["metadata"]["generation"] == steady["metadata"]["generation"]
          and again["spec"]["template"] == steady["spec"]["template"])

    # Combined reviews 4605/4609: a partially applied update writes the v2
    # ConfigMap, then its Deployment write fails. The v1 template names the
    # v1 content-addressed ConfigMap, whose data cannot change, so a
    # v1-template Pod that starts in that window (whatever the kubelet's
    # by-name cache holds) still reads v1, and the rollback converges with
    # every Pod on v1.
    cluster.admin("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
         "metadata": {"name": "test-freeze-config-app"},
         "spec": {"failurePolicy": "Fail",
                  "matchConstraints": {"resourceRules": [{"apiGroups": ["apps"], "apiVersions": ["v1"],
                                                          "operations": ["UPDATE"], "resources": ["deployments"]}]},
                  "validations": [{"expression": "object.metadata.name != 'config-app'",
                                   "message": "test: config-app Deployment writes are frozen"}]}},
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
         "metadata": {"name": "test-freeze-config-app"},
         "spec": {"policyName": "test-freeze-config-app", "validationActions": ["Deny"],
                  "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}}}}}]}))

    def frozen():
        return cluster.admin("annotate", "deployment", "config-app", "-n", NAMESPACE, "test-probe=1", "--overwrite",
                             "--dry-run=server", ok=False).returncode != 0

    wait("the test Deployment write freeze to take effect", frozen, timeout=60)
    result = helper("apply", profiles, config_v2, component="config-app", version="2.0.0")
    check("a partially applied v2 fails at the Deployment write",
          result.returncode == 2 and "frozen" in result.stderr, result.stderr[-500:])
    config_alpha, config_beta = published(config_v1, "config-settings"), published(config_v2, "config-settings")
    live_beta = json.loads(cluster.admin("get", "configmap", config_beta, "-n", NAMESPACE, "-o", "json").stdout)
    live_alpha = json.loads(cluster.admin("get", "configmap", config_alpha, "-n", NAMESPACE, "-o", "json").stdout)
    frozen_deployment = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    check("the partial apply wrote the v2 ConfigMap but left the v1 template and the v1 ConfigMap",
          live_beta["data"] == {"MODE": "beta"} and live_alpha["data"] == {"MODE": "alpha"}
          and live_alpha.get("immutable") is True
          and frozen_deployment["spec"]["template"] == again["spec"]["template"]
          and publisher.config_refs(frozen_deployment["spec"]["template"]["spec"]) == [config_alpha],
          json.dumps([live_alpha.get("data"), live_beta.get("data")]))
    cluster.admin("delete", "pods", "-n", NAMESPACE, "-l", "app=config-app", "--wait=true")
    wait("a replacement v1-template Pod", lambda: len(observed_config(cluster)) == 1, timeout=120, interval=2)
    seen = observed_config(cluster)
    check("a v1-template Pod started during the partial apply still reads v1", seen == [("alpha",) * 3], str(seen))
    cluster.admin("delete", "validatingadmissionpolicybinding,validatingadmissionpolicy", "test-freeze-config-app")
    wait("the test Deployment write freeze to lift", lambda: not frozen(), timeout=60)
    result = helper("apply", profiles, config_v1, component="config-app")
    check("rollback after the partial apply applies and prunes the v2 ConfigMap",
          result.returncode == 0 and f"configmaps/{config_beta}" in result.stderr, result.stderr)
    wait_ready(profiles, config_v1, "1.0.0", component="config-app")
    settled = json.loads(cluster.admin("get", "deployment", "config-app", "-n", NAMESPACE, "-o", "json").stdout)
    seen = observed_config(cluster)
    check("once the rollback is ready, every live Pod runs the v1 configuration",
          seen == [("alpha",) * 3], str(seen))
    check("the rollback needed no new rollout (the v1 template never named other data)",
          settled["spec"]["template"] == again["spec"]["template"]
          and settled["metadata"]["generation"] == again["metadata"]["generation"])
    retire_config = work / "config-retire.json"
    retire_config.write_text(json.dumps({"version": 1, "resources": []}))
    check("config-only test component retires",
          helper("apply", profiles, retire_config, component="config-app").returncode == 0)
    wait_ready(profiles, retire_config, "1.0.0", component="config-app")

    # Review 4613: names are disjoint across bundles and updates fit the
    # shipped ConfigMap quota. A stable ConfigMap may not take the reserved
    # content-addressed shape, and an owned mutable object already at a
    # derived name is refused before any write.
    reserved = json.loads(v1.read_text())
    reserved["resources"].append({"apiVersion": "v1", "kind": "ConfigMap",
                                  "metadata": {"name": "stable-0123456789abcdef"}, "data": {}})
    (work / "reserved.json").write_text(json.dumps(reserved))
    result = run(sys.executable, str(HELPER), "validate", "--bundle", str(work / "reserved.json"), ok=False)
    check("helper validate rejects a stable ConfigMap name with the reserved content-addressed suffix",
          result.returncode == 2 and "reserved for content-addressed" in result.stderr, result.stderr)
    result = run(sys.executable, str(HELPER), "validate", "--bundle",
                 str(quota_bundle(work, "quota-over", "x", publisher.MAX_CONFIGMAPS + 1)), ok=False)
    check(f"helper validate rejects a bundle with more than {publisher.MAX_CONFIGMAPS} ConfigMaps",
          result.returncode == 2 and f"more than {publisher.MAX_CONFIGMAPS} ConfigMaps" in result.stderr, result.stderr)

    def configmaps():
        listed = json.loads(cluster.admin("get", "configmaps", "-n", NAMESPACE, "-o", "json").stdout)["items"]
        return sorted(item["metadata"]["name"] for item in listed if not item["metadata"].get("deletionTimestamp"))

    def quota_limit():
        return cluster.admin("get", "resourcequota", ACCOUNT, "-n", NAMESPACE,
                             "-o", "jsonpath={.spec.hard.configmaps}").stdout

    # With kube-root-ca.crt and the web component's one ConfigMap, a change of
    # all MAX_CONFIGMAPS consumed ConfigMaps peaks exactly at the quota.
    # The customer object from the no-adoption check is set aside for the
    # boundary and restored afterwards (the remove check needs it).
    cluster.admin("delete", "configmap", "legacy", "-n", NAMESPACE, "--wait=true")

    def only_baseline():
        present = configmaps()
        if present != sorted(["kube-root-ca.crt", web_config]):
            raise AssertionError(f"ConfigMaps present: {present}")
        return True

    wait("only kube-root-ca.crt and the web ConfigMap remain", only_baseline, timeout=60)
    check("the shipped quota leaves exactly two bundles' ConfigMaps of headroom",
          int(quota_limit()) == 2 + 2 * publisher.MAX_CONFIGMAPS, quota_limit())
    quota = [quota_bundle(work, f"quota-v{index}", f"q{index}") for index in range(1, 5)]
    check("a bundle at the ConfigMap limit applies",
          helper("apply", profiles, quota[0], component="quota-app").returncode == 0)
    wait_ready(profiles, quota[0], "1.0.0", component="quota-app")
    alias = published(quota[1], "quota-0")
    cluster.admin("create", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "data": {"Q0": "stale"},
        "metadata": {"name": alias, "namespace": NAMESPACE,
                     "labels": {publisher.MANAGED_BY: publisher.FIELD_MANAGER, publisher.COMPONENT_LABEL: "quota-app"}}}))
    before = configmaps()
    rv = cluster.admin("get", "deployment", "quota-app", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    result = helper("apply", profiles, quota[1], component="quota-app", version="2.0.0")
    check("apply refuses an owned mutable ConfigMap at a content-addressed name before writing anything",
          result.returncode == 2 and "mutable ConfigMap at a content-addressed name" in result.stderr
          and configmaps() == before and rv == cluster.admin(
              "get", "deployment", "quota-app", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout,
          result.stderr)
    cluster.admin("delete", "configmap", alias, "-n", NAMESPACE, "--wait=true")
    result = helper("apply", profiles, quota[1], component="quota-app", version="2.0.0")
    check("changing every ConfigMap of a bundle at the limit fits the shipped quota",
          result.returncode == 0, result.stderr[-500:])
    wait_ready(profiles, quota[1], "2.0.0", component="quota-app")
    check("the update converged and pruned the previous ConfigMaps",
          len(configmaps()) == 2 + publisher.MAX_CONFIGMAPS, str(configmaps()))
    # A partial apply leaves its ConfigMaps behind at the quota; the next
    # bundle frees those unreferenced leftovers before creating its own.
    cluster.admin("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
         "metadata": {"name": "test-freeze-quota-app"},
         "spec": {"failurePolicy": "Fail",
                  "matchConstraints": {"resourceRules": [{"apiGroups": ["apps"], "apiVersions": ["v1"],
                                                          "operations": ["UPDATE"], "resources": ["deployments"]}]},
                  "validations": [{"expression": "object.metadata.name != 'quota-app'",
                                   "message": "test: quota-app Deployment writes are frozen"}]}},
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
         "metadata": {"name": "test-freeze-quota-app"},
         "spec": {"policyName": "test-freeze-quota-app", "validationActions": ["Deny"],
                  "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}}}}}]}))

    def quota_frozen():
        return cluster.admin("annotate", "deployment", "quota-app", "-n", NAMESPACE, "test-probe=1", "--overwrite",
                             "--dry-run=server", ok=False).returncode != 0

    wait("the quota-app Deployment write freeze to take effect", quota_frozen, timeout=60)
    result = helper("apply", profiles, quota[2], component="quota-app", version="3.0.0")
    check("a partially applied update at the limit fails at the Deployment write",
          result.returncode == 2 and "frozen" in result.stderr, result.stderr[-500:])
    cluster.admin("delete", "validatingadmissionpolicybinding,validatingadmissionpolicy", "test-freeze-quota-app")
    wait("the quota-app Deployment write freeze to lift", lambda: not quota_frozen(), timeout=60)
    extra = cluster.publisher("create", "configmap", "quota-extra", "-n", NAMESPACE, ok=False)
    check("the partial apply's leftovers fill the namespace ConfigMap quota",
          len(configmaps()) == 2 + 2 * publisher.MAX_CONFIGMAPS and extra.returncode != 0
          and "exceeded quota" in extra.stderr, f"{configmaps()} {extra.stderr}")
    result = helper("apply", profiles, quota[3], component="quota-app", version="4.0.0")
    leftovers = [published(quota[2], f"quota-{index}") for index in range(publisher.MAX_CONFIGMAPS)]
    check("the next update frees the unreferenced leftovers first and fits the quota",
          result.returncode == 0 and all(f"configmaps/{name}" in result.stderr for name in leftovers), result.stderr[-800:])
    wait_ready(profiles, quota[3], "4.0.0", component="quota-app")
    check("quota test component retires",
          helper("apply", profiles, retire_config, component="quota-app").returncode == 0)
    wait_ready(profiles, retire_config, "1.0.0", component="quota-app")
    cluster.admin("create", "configmap", "legacy", "-n", NAMESPACE, "--from-literal=k=customer")

    # A stable-name (unconsumed) ConfigMap cannot be restored in place once
    # immutable (the API server refuses data changes and a return to
    # mutable), so a rollback could not recover. Bundles cannot request one,
    # and a live owned immutable stable-name ConfigMap (set out of band) stops
    # the apply before any write instead of half-applying.
    frozen = json.loads(v1.read_text())
    frozen["resources"][0]["immutable"] = True
    (work / "frozen.json").write_text(json.dumps(frozen))
    result = run(sys.executable, str(HELPER), "validate", "--bundle", str(work / "frozen.json"), ok=False)
    check("helper validate rejects an immutable ConfigMap offline",
          result.returncode == 2 and "cannot be rolled back in place" in result.stderr, result.stderr)
    kept_v1 = variant(work, "kept-v1", "hello from SuiFleet v1", extra_configmap="kept")
    kept_v2 = variant(work, "kept-v2", "hello from SuiFleet v2", extra_configmap="kept")
    check("apply a bundle with a stable-name ConfigMap", helper("apply", profiles, kept_v1).returncode == 0)
    cluster.admin("patch", "configmap", "kept", "-n", NAMESPACE, "--type=merge", "-p", '{"immutable":true}')
    before = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    result = helper("apply", profiles, kept_v2, version="2.0.0")
    after = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    check("apply refuses a live owned immutable stable-name ConfigMap before writing anything",
          result.returncode == 2 and "immutable ConfigMap" in result.stderr and before == after
          and cluster.admin("get", "configmap", published(kept_v2), "-n", NAMESPACE, ok=False).returncode != 0,
          f"{result.stderr} deployment rv {before} -> {after}")
    content = json.loads(cluster.admin("get", "configmap", "kept", "-n", NAMESPACE, "-o", "json").stdout)
    check("the refused apply left the immutable ConfigMap's v1 data untouched",
          content.get("immutable") is True and "SuiFleet v1" in content["data"]["k"], str(content.get("data")))
    cluster.admin("delete", "configmap", "kept", "-n", NAMESPACE)
    result = helper("apply", profiles, kept_v1)
    recreated = json.loads(cluster.admin("get", "configmap", "kept", "-n", NAMESPACE, "-o", "json").stdout)
    check("apply recreates a mutable ConfigMap after the administrator deletes the immutable one",
          result.returncode == 0 and not recreated.get("immutable"), result.stderr)
    # A content-addressed ConfigMap recreated (with SuiFleet's labels) holding
    # other content than its name denotes is refused before any write too.
    forged = json.loads(cluster.admin("get", "configmap", web_config, "-n", NAMESPACE, "-o", "json").stdout)
    cluster.admin("delete", "configmap", web_config, "-n", NAMESPACE, "--wait=true")
    cluster.admin("create", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True, "data": {"index.html": "<p>forged</p>\n"},
        "metadata": {"name": web_config, "namespace": NAMESPACE, "labels": forged["metadata"]["labels"],
                     "annotations": forged["metadata"]["annotations"]}}))
    before = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    result = helper("apply", profiles, v1)
    after = cluster.admin("get", "deployment", "web", "-n", NAMESPACE, "-o", "jsonpath={.metadata.resourceVersion}").stdout
    check("apply refuses a content-addressed ConfigMap whose content differs from its name before writing anything",
          result.returncode == 2 and "differs from the content its name denotes" in result.stderr and before == after
          and cluster.admin("get", "configmap", "kept", "-n", NAMESPACE, ok=False).returncode == 0, result.stderr)
    # The forged copy carries the markers of kept_v1, the last bundle applied.
    result = helper("ready", profiles, kept_v1)
    check("readiness rejects the forged content-addressed ConfigMap",
          result.returncode == 1 and f"ConfigMap/{web_config} content differs" in result.stderr, result.stderr)
    cluster.admin("delete", "configmap", web_config, "-n", NAMESPACE, "--wait=true")
    result = helper("apply", profiles, v1)
    check("apply recreates the content-addressed ConfigMap after the administrator deletes the forged one",
          result.returncode == 0 and "configmaps/kept" in result.stderr, result.stderr)
    wait_ready(profiles, v1, "1.0.0")
    check("readiness succeeds once the recreated ConfigMaps are applied", True)

    # Foreground run mode: reconciles deletion, stops cleanly, workload remains.
    env = {"PATH": "/usr/bin:/bin", "SUIFLEET_COMPONENT": COMPONENT, "SUIFLEET_COMPONENT_VERSION": "1.0.0"}
    process = subprocess.Popen([sys.executable, str(HELPER), "run", "--profile", "edge", "--profile-root", str(profiles),
                                "--bundle", str(v1), "--interval", "1"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(3)
        cluster.admin("delete", "configmap", web_config, "-n", NAMESPACE)
        wait("run mode re-creates the deleted ConfigMap",
             lambda: cluster.admin("get", "configmap", web_config, "-n", NAMESPACE, ok=False).returncode == 0, timeout=30)
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
k patch configmap {shlex.quote(web_config)} --type=merge -p "$patch" >&2 || exit 1
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
                   SF_K8S_HOLD=hold, SF_K8S_RELEASE=release, SF_K8S_CONFIG_GOOD=web_config,
                   SF_K8S_CONFIG_BAD=published(bad), RUSTUP_TOOLCHAIN="1.94.0")
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
