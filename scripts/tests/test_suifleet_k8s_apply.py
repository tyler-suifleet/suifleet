"""Offline tests for scripts/suifleet_k8s_apply.py (SF-305).

Bundle validation (including malformed nested types), rendering,
ownership/prune/readiness decisions, the precondition-bound write protocol
against a scripted fake kubectl, and profile/credential hygiene. No cluster,
kubectl or network is used; the real-cluster behaviour (including injected
races) is covered by scripts/test_kubernetes_edge.py."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suifleet_k8s_apply as k8s  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "kubernetes/example-bundle.json"


def example():
    return json.loads(EXAMPLE.read_text())


def deployment(bundle):
    return next(r for r in bundle["resources"] if r["kind"] == "Deployment")


class ValidationTest(unittest.TestCase):
    def rejects(self, mutate, message):
        bundle = example()
        mutate(bundle)
        with self.assertRaises(k8s.Rejected) as caught:
            k8s.validate_bundle(bundle)
        self.assertIn(message, str(caught.exception))

    def pod(self, bundle):
        return deployment(bundle)["spec"]["template"]["spec"]

    def container(self, bundle):
        return self.pod(bundle)["containers"][0]

    def test_example_bundle_is_valid(self):
        k8s.validate_bundle(example())
        bundle, digest = k8s.load_bundle(EXAMPLE)
        self.assertEqual(len(bundle["resources"]), 2)
        self.assertEqual(len(digest), 64)

    def test_envelope(self):
        self.rejects(lambda b: b.update(version=2), "bundle version")
        self.rejects(lambda b: b.update(version=True), "bundle version")
        self.rejects(lambda b: b.update(extra=1), "unsupported field")
        self.rejects(lambda b: b["resources"].append(copy.deepcopy(b["resources"][0])), "duplicates ConfigMap/web-content")
        self.rejects(lambda b: b.update(resources=[b["resources"][0]] * (k8s.MAX_RESOURCES + 1)), "more than")

    def test_kinds_and_metadata(self):
        for api, kind in [("v1", "Secret"), ("v1", "Pod"), ("v1", "Service"), ("networking.k8s.io/v1", "Ingress"), ("apps/v1", "DaemonSet"), ("v1", "Namespace"),
                          ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"), ("v1", "ServiceAccount"),
                          ("apiextensions.k8s.io/v1", "CustomResourceDefinition"), ("batch/v1", "Job")]:
            self.rejects(lambda b: b["resources"].append({"apiVersion": api, "kind": kind, "metadata": {"name": "x"}}),
                         "not an allowed kind")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(namespace="kube-system"), "unsupported field")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(name="Bad_Name"), "DNS label")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(labels={"app.kubernetes.io/managed-by": "argo"}), "reserved")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(annotations={"suifleet.io/version": "9"}), "reserved")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(ownerReferences=[]), "unsupported field")

    def test_malformed_types_are_rejected_not_crashes(self):
        # Every nested field of the example replaced by every JSON type must be
        # accepted or raise Rejected -- never TypeError/AttributeError.
        replacements = [[], [1], {}, {"a": 1}, 0, 7, None, True, "x", 1.5]

        def paths(value, prefix=()):
            yield prefix
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from paths(child, prefix + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    yield from paths(child, prefix + (index,))

        base = example()
        checked = 0
        for path in paths(base):
            if not path:
                continue
            for replacement in replacements:
                bundle = copy.deepcopy(base)
                target = bundle
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = copy.deepcopy(replacement)
                try:
                    k8s.validate_bundle(bundle)
                except k8s.Rejected:
                    pass
                checked += 1
        self.assertGreater(checked, 300)
        self.rejects(lambda b: b["resources"][0].update(apiVersion=["v1"]), "must be strings")
        self.rejects(lambda b: b["resources"][0].update(kind={"k": 1}), "must be strings")
        self.rejects(lambda b: self.container(b).update(ports=5), "ports must be a list")
        self.rejects(lambda b: self.container(b).update(env=[{"name": "A", "valueFrom": 3}]), "valueFrom must be an object")
        self.rejects(lambda b: self.pod(b).update(volumes={"name": "x"}), "volumes must be a list")
        self.rejects(lambda b: b["resources"][0].update(immutable="yes"), "immutable must be a boolean")

    def test_pod_host_access_and_privilege(self):
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            self.rejects(lambda b: self.pod(b).update({flag: True}), flag)
        self.rejects(lambda b: self.pod(b).update(nodeName="n1"), "nodeName")
        self.rejects(lambda b: self.pod(b).update(serviceAccountName="admin"), "serviceAccountName")
        self.rejects(lambda b: self.pod(b).update(automountServiceAccountToken=True), "automountServiceAccountToken")
        self.rejects(lambda b: self.pod(b).update(imagePullSecrets=[{"name": "x"}]), "imagePullSecrets")
        self.rejects(lambda b: self.pod(b)["volumes"].append({"name": "h", "hostPath": {"path": "/"}}), "may only use")
        self.rejects(lambda b: self.pod(b)["volumes"].append({"name": "s", "secret": {"secretName": "x"}}), "may only use")
        self.rejects(lambda b: self.pod(b)["volumes"].append({"name": "p", "projected": {"sources": []}}), "may only use")
        self.rejects(lambda b: self.container(b)["securityContext"].update(privileged=True), "privileged")
        self.rejects(lambda b: self.container(b)["securityContext"].update(allowPrivilegeEscalation=True), "allowPrivilegeEscalation")
        self.rejects(lambda b: self.container(b)["securityContext"].update(runAsUser=0), "runAsUser 0")
        self.rejects(lambda b: self.container(b)["securityContext"]["capabilities"].update(add=["NET_ADMIN"]), "capabilities.add")
        self.rejects(lambda b: self.pod(b)["securityContext"].update(runAsUser=0), "runAsUser 0")
        self.rejects(lambda b: self.container(b)["ports"][0].update(hostPort=80), "hostPort")
        self.rejects(lambda b: self.container(b).update(volumeDevices=[]), "volumeDevices")

    def test_secrets_and_images(self):
        self.rejects(lambda b: self.container(b).update(envFrom=[{"secretRef": {"name": "x"}}]), "Secrets")
        self.rejects(lambda b: self.container(b).update(env=[{"name": "A", "valueFrom": {"secretKeyRef": {"name": "x", "key": "k"}}}]), "Secrets")
        for image in ("busybox", "busybox:1.37", "docker.io/library/busybox:latest", "busybox@sha256:abc"):
            self.rejects(lambda b: self.container(b).update(image=image), "pinned by @sha256")
        self.rejects(lambda b: self.pod(b).update(initContainers=[{"name": "init", "image": "busybox:latest"}]), "initContainers[0].image")
        ok = example()
        self.container(ok)["image"] = "registry.example.com:5000/team/app@sha256:" + "a" * 64
        self.container(ok)["env"] = [{"name": "A", "valueFrom": {"configMapKeyRef": {"name": "web-content", "key": "index.html"}}}]
        k8s.validate_bundle(ok)

    def test_component_name(self):
        k8s.validate_component("web-app")
        for bad in (None, "", "a" * 64, "bad name", "-x"):
            with self.assertRaises(k8s.Rejected):
                k8s.validate_component(bad)

    def test_bundle_file_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.json"
            path.write_bytes(b"{" + b" " * k8s.MAX_BUNDLE_BYTES + b"}")
            with self.assertRaisesRegex(k8s.Rejected, "exceeds"):
                k8s.load_bundle(path)
            path.write_text("not json")
            with self.assertRaisesRegex(k8s.Rejected, "not JSON"):
                k8s.load_bundle(path)


class DecisionTest(unittest.TestCase):
    def test_render_forces_namespace_ownership_and_no_token(self):
        bundle = example()
        bundle_copy = copy.deepcopy(bundle)
        objects = k8s.render(bundle, "suifleet-apps", "web-app", "1.2.3", "d" * 64)
        self.assertEqual(bundle, bundle_copy, "render must not mutate the bundle")
        for item in objects:
            self.assertEqual(item["metadata"]["namespace"], "suifleet-apps")
            self.assertEqual(item["metadata"]["labels"][k8s.MANAGED_BY], "suifleet")
            self.assertEqual(item["metadata"]["labels"][k8s.COMPONENT_LABEL], "web-app")
            self.assertEqual(item["metadata"]["annotations"][k8s.BUNDLE_ANNOTATION], "d" * 64)
            self.assertEqual(item["metadata"]["annotations"][k8s.VERSION_ANNOTATION], "1.2.3")
        self.assertIs(deployment({"resources": objects})["spec"]["template"]["spec"]["automountServiceAccountToken"], False)
        self.assertEqual(objects[1]["metadata"]["labels"]["app"], "web", "customer labels are kept")

    def live(self, desired, component="web-app", **changes):
        current = copy.deepcopy(desired)
        current["metadata"].update(uid="u1", resourceVersion="5")
        current.update(changes)
        return current

    def test_object_ready_checks_ownership_digest_and_content(self):
        objects = k8s.render(example(), "suifleet-apps", "web-app", "1.0.0", "d" * 64)
        config = objects[0]
        self.assertEqual(k8s.object_ready(self.live(config), config, "web-app"), (True, "ready"))
        self.assertIn("missing", k8s.object_ready(None, config, "web-app")[1])
        foreign = self.live(config)
        foreign["metadata"]["labels"] = {}
        self.assertIn("does not adopt", k8s.object_ready(foreign, config, "web-app")[1],
                      "a pre-existing foreign ConfigMap with the right name is not ready")
        other = self.live(config)
        other["metadata"]["labels"][k8s.COMPONENT_LABEL] = "other"
        self.assertIn("component 'other'", k8s.object_ready(other, config, "web-app")[1])
        stale = self.live(config)
        stale["metadata"]["annotations"][k8s.BUNDLE_ANNOTATION] = "0" * 64
        self.assertIn("not yet applied", k8s.object_ready(stale, config, "web-app")[1],
                      "a ConfigMap from another bundle digest is not ready")
        old_version = self.live(config)
        old_version["metadata"]["annotations"][k8s.VERSION_ANNOTATION] = "0.9.0"
        self.assertFalse(k8s.object_ready(old_version, config, "web-app")[0])
        edited = self.live(config, data={"index.html": "customer"})
        self.assertIn("content differs", k8s.object_ready(edited, config, "web-app")[1])
        # binaryData is outside the bundle schema; a ConfigMap volume would
        # project it as an extra file while the bundle markers stay intact.
        binary = self.live(config, binaryData={"extra.sh": "IyEvYmluL3NoCg=="})
        self.assertEqual(binary["metadata"]["annotations"], config["metadata"]["annotations"])
        self.assertIn("content differs", k8s.object_ready(binary, config, "web-app")[1])
        self.assertEqual(k8s.object_ready(self.live(config, binaryData={}), config, "web-app"), (True, "ready"))
        deleting = self.live(config)
        deleting["metadata"]["deletionTimestamp"] = "2026-11-24T00:00:00Z"
        self.assertIn("being deleted", k8s.object_ready(deleting, config, "web-app")[1])
        # Deployments additionally need the rollout.
        deploy = objects[1]
        live = self.live(deploy, status={"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
                                         "readyReplicas": 1, "availableReplicas": 1})
        live["metadata"]["generation"] = 1
        self.assertEqual(k8s.object_ready(live, deploy, "web-app"), (True, "ready"))
        live["status"]["availableReplicas"] = 0
        self.assertIn("availableReplicas", k8s.object_ready(live, deploy, "web-app")[1])

    def healthy(self, desired, replicas=1, generation=2):
        """`desired` as the API server returns it after a completed rollout:
        server defaults filled in, quantities canonicalised, status healthy."""
        live = self.live(desired, status={"observedGeneration": generation, "replicas": replicas,
                                          "updatedReplicas": replicas, "readyReplicas": replicas,
                                          "availableReplicas": replicas})
        live["metadata"]["generation"] = generation
        spec = live["spec"]
        spec.setdefault("replicas", replicas)
        spec.update(revisionHistoryLimit=10, strategy={"type": "RollingUpdate", "rollingUpdate": {
            "maxSurge": "25%", "maxUnavailable": "25%"}})
        pod = spec["template"]["spec"]
        pod.update(dnsPolicy="ClusterFirst", restartPolicy="Always", schedulerName="default-scheduler",
                   terminationGracePeriodSeconds=30)
        for container in pod["containers"]:
            container.update(imagePullPolicy="IfNotPresent", terminationMessagePath="/dev/termination-log",
                             terminationMessagePolicy="File")
            for port in container.get("ports", []):
                port["protocol"] = "TCP"
            probe = container.get("readinessProbe")
            if probe:
                probe.update(timeoutSeconds=1, successThreshold=1, failureThreshold=3)
                probe["httpGet"]["scheme"] = "HTTP"
        for volume in pod.get("volumes", []):
            if "configMap" in volume:
                volume["configMap"]["defaultMode"] = 420
        return live

    def test_deployment_readiness_requires_the_bundle_spec(self):
        objects = k8s.render(example(), "suifleet-apps", "web-app", "1.0.0", "d" * 64)
        deploy = objects[1]
        self.assertEqual(k8s.object_ready(self.healthy(deploy), deploy, "web-app"), (True, "ready"),
                         "server defaults are tolerated")
        # An external update changes the pod template but keeps SuiFleet's
        # metadata; the drifted generation then rolls out healthily.
        for mutate, where in [
                (lambda c: c.update(image="docker.io/library/busybox@sha256:" + "1" * 64), "containers[0].image"),
                (lambda c: c["command"].append("-x"), "containers[0].command"),
                (lambda c: c["resources"]["limits"].update(memory="64Mi"), "limits.memory"),
                (lambda c: c["securityContext"].update(readOnlyRootFilesystem=False), "readOnlyRootFilesystem")]:
            drifted = self.healthy(deploy)
            mutate(drifted["spec"]["template"]["spec"]["containers"][0])
            ok, why = k8s.object_ready(drifted, deploy, "web-app")
            self.assertFalse(ok, f"healthy rollout of a drifted {where} is not this bundle's readiness")
            self.assertIn(where, why)
            self.assertIn("differs from this bundle", why)
        extra = self.healthy(deploy)
        extra["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "image": "x@sha256:" + "2" * 64})
        self.assertIn("spec.template.spec.containers differs", k8s.object_ready(extra, deploy, "web-app")[1])
        annotated = self.healthy(deploy)
        annotated["spec"]["template"]["metadata"]["annotations"]["example.com/content"] = "customer"
        self.assertFalse(k8s.object_ready(annotated, deploy, "web-app")[0])
        token = self.healthy(deploy)
        token["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        self.assertFalse(k8s.object_ready(token, deploy, "web-app")[0])
        # Workload-bearing fields the bundle omits must not appear either.
        init = self.healthy(deploy)
        self.assertNotIn("initContainers", deploy["spec"]["template"]["spec"])
        init["spec"]["template"]["spec"]["initContainers"] = [{"name": "inject", "image": "x@sha256:" + "3" * 64}]
        self.assertIn("spec.template.spec.initContainers differs", k8s.object_ready(init, deploy, "web-app")[1])
        for mutate, where in [
                (lambda p: p.update(hostAliases=[{"ip": "10.0.0.1", "hostnames": ["x"]}]), "spec.hostAliases"),
                (lambda p: p.update(nodeSelector={"pool": "other"}), "spec.nodeSelector"),
                (lambda p: p["containers"][0].update(args=["-x"]), "containers[0].args"),
                (lambda p: p["containers"][0].update(env=[{"name": "A", "value": "1"}]), "containers[0].env"),
                (lambda p: p["containers"][0].update(imagePullPolicy="Never"), "imagePullPolicy"),
                (lambda p: p.update(terminationGracePeriodSeconds=0), "terminationGracePeriodSeconds"),
                (lambda p: p["containers"][0]["readinessProbe"].update(failureThreshold=99), "failureThreshold")]:
            drifted = self.healthy(deploy)
            mutate(drifted["spec"]["template"]["spec"])
            ok, why = k8s.object_ready(drifted, deploy, "web-app")
            self.assertFalse(ok, f"an unexpected live {where} is drift")
            self.assertIn(where, why)
        restarted = self.healthy(deploy)
        restarted["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "now"
        self.assertIn("restartedAt", k8s.object_ready(restarted, deploy, "web-app")[1],
                      "an extra template annotation is drift")
        zeroes = self.healthy(deploy)
        zeroes["spec"]["template"]["metadata"]["creationTimestamp"] = None
        zeroes["spec"]["template"]["spec"].update(hostNetwork=False, enableServiceLinks=True)
        self.assertEqual(k8s.object_ready(zeroes, deploy, "web-app"), (True, "ready"),
                         "zero values and server defaults for omitted fields are tolerated")
        scaled = self.healthy(deploy, replicas=3)
        scaled["spec"]["replicas"] = 3
        self.assertIn("spec.replicas", k8s.object_ready(scaled, deploy, "web-app")[1],
                      "replicas the bundle specifies are compared")

    def test_deployment_readiness_preserves_map_key_presence(self):
        objects = k8s.render(example(), "suifleet-apps", "web-app", "1.0.0", "d" * 64)
        deploy = objects[1]
        template = deploy["spec"]["template"]
        self.assertNotIn("nodeSelector", template["spec"])
        self.assertTrue(template["metadata"]["labels"] and template["metadata"]["annotations"])
        # An empty-valued key is still a constraint or a selectable label, both
        # in a map the bundle omits and in one it already populates.
        for mutate, where in [
                (lambda t: t["spec"].update(nodeSelector={"example.com/pool": ""}),
                 "spec.nodeSelector.example.com/pool"),
                (lambda t: t["metadata"]["labels"].update({"example.com/extra": ""}),
                 "metadata.labels.example.com/extra"),
                (lambda t: t["metadata"]["annotations"].update({"example.com/extra": ""}),
                 "metadata.annotations.example.com/extra"),
                (lambda t: t["spec"]["containers"][0]["resources"]["limits"].update({"example.com/gpu": "0"}),
                 "limits.example.com/gpu"),
                (lambda t: t["spec"].update(overhead={"cpu": ""}), "spec.overhead.cpu")]:
            drifted = self.healthy(deploy)
            mutate(drifted["spec"]["template"])
            ok, why = k8s.object_ready(drifted, deploy, "web-app")
            self.assertFalse(ok, f"an extra empty-valued {where} key is drift")
            self.assertIn(where, why)
        selector = self.healthy(deploy)
        selector["spec"]["selector"]["matchLabels"]["example.com/extra"] = ""
        self.assertIn("matchLabels.example.com/extra", k8s.object_ready(selector, deploy, "web-app")[1])
        # A bundle map key with an empty value must be present live, not omitted.
        bundle = example()
        deployment(bundle)["spec"]["template"]["spec"]["nodeSelector"] = {"example.com/pool": ""}
        k8s.validate_bundle(bundle)
        pinned = k8s.render(bundle, "suifleet-apps", "web-app", "1.0.0", "d" * 64)[1]
        live = self.healthy(pinned)
        self.assertEqual(k8s.object_ready(live, pinned, "web-app"), (True, "ready"))
        del live["spec"]["template"]["spec"]["nodeSelector"]
        self.assertIn("spec.nodeSelector.example.com/pool", k8s.object_ready(live, pinned, "web-app")[1])
        # Struct defaults and zero values for omitted struct fields still pass.
        zero = self.healthy(deploy)
        zero["spec"]["template"]["spec"].update(nodeSelector={}, hostNetwork=False)
        self.assertEqual(k8s.object_ready(zero, deploy, "web-app"), (True, "ready"))

    def test_deployment_readiness_tolerates_defaulted_http_get_path(self):
        for path in (None, ""):  # omitted or explicitly empty
            bundle = example()
            http_get = deployment(bundle)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]
            if path is None:
                del http_get["path"]
            else:
                http_get["path"] = path
            k8s.validate_bundle(bundle)
            deploy = k8s.render(bundle, "suifleet-apps", "web-app", "1.0.0", "d" * 64)[1]
            live = self.healthy(deploy)
            live["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = "/"
            self.assertEqual(k8s.object_ready(live, deploy, "web-app"), (True, "ready"),
                             f"server-defaulted path for a {path!r} bundle path")
            live["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = "/admin"
            self.assertIn("readinessProbe.httpGet.path", k8s.object_ready(live, deploy, "web-app")[1],
                          "a genuinely different path is still drift")
        # Explicit empty strings match only plain-string defaults.
        pod = "spec.template.spec"
        self.assertIsNone(k8s.spec_drift({"imagePullPolicy": ""}, {"imagePullPolicy": "IfNotPresent"},
                                         f"{pod}.containers[0]"))
        self.assertEqual(k8s.spec_drift({"imagePullPolicy": ""}, {"imagePullPolicy": "Never"},
                                        f"{pod}.containers[0]"), f"{pod}.containers[0].imagePullPolicy")
        self.assertEqual(k8s.spec_drift({"maxSurge": ""}, {"maxSurge": "25%"}, "spec.strategy.rollingUpdate"),
                         "spec.strategy.rollingUpdate.maxSurge", "pointer IntOrString keeps an explicit value")
        self.assertEqual(k8s.spec_drift({"image": ""}, {"image": "x"}, f"{pod}.containers[0]"),
                         f"{pod}.containers[0].image", "fields without a default are compared")
        self.assertIsNone(k8s.spec_drift({"httpGet": {"port": 8080}},
                                         {"httpGet": {"port": 8080, "path": "/", "scheme": "HTTP"}},
                                         f"{pod}.containers[0].lifecycle.preStop"))

    def test_deployment_readiness_tolerates_defaulted_zero_probe_fields(self):
        # SetDefaults_Probe replaces an explicit 0 in these non-pointer int32
        # fields with the default, exactly as for an omitted field.
        defaults = {"timeoutSeconds": 1, "periodSeconds": 10, "successThreshold": 1, "failureThreshold": 3}
        for field, default in defaults.items():
            for desired in ("omitted", 0):
                bundle = example()
                probe = deployment(bundle)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
                probe.pop(field, None)
                if desired == 0:
                    probe[field] = 0
                k8s.validate_bundle(bundle)
                deploy = k8s.render(bundle, "suifleet-apps", "web-app", "1.0.0", "d" * 64)[1]
                live = self.healthy(deploy)
                live_probe = live["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
                live_probe[field] = default
                self.assertEqual(k8s.object_ready(live, deploy, "web-app"), (True, "ready"),
                                 f"server-defaulted {field} for a {desired!r} bundle value")
                live_probe[field] = default + 4
                self.assertIn(f"readinessProbe.{field}", k8s.object_ready(live, deploy, "web-app")[1],
                              f"a non-default live {field} is still drift")
            # A specified nonzero value must match exactly.
            bundle = example()
            probe = deployment(bundle)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
            probe[field] = default + 1
            deploy = k8s.render(bundle, "suifleet-apps", "web-app", "1.0.0", "d" * 64)[1]
            live = self.healthy(deploy)
            live["spec"]["template"]["spec"]["containers"][0]["readinessProbe"][field] = default
            self.assertIn(f"readinessProbe.{field}", k8s.object_ready(live, deploy, "web-app")[1])
        pod = "spec.template.spec"
        for probe in ("livenessProbe", "startupProbe"):
            self.assertIsNone(k8s.spec_drift({"periodSeconds": 0}, {"periodSeconds": 10},
                                             f"{pod}.initContainers[0].{probe}"))
        # Pointer ints keep an explicit zero; it is not replaced by the default.
        self.assertEqual(k8s.spec_drift({"terminationGracePeriodSeconds": 0}, {"terminationGracePeriodSeconds": 30},
                                        pod), f"{pod}.terminationGracePeriodSeconds")
        self.assertEqual(k8s.spec_drift({"progressDeadlineSeconds": 0}, {"progressDeadlineSeconds": 600}, "spec"),
                         "spec.progressDeadlineSeconds")
        self.assertEqual(k8s.spec_drift({"timeoutSeconds": False}, {"timeoutSeconds": 1},
                                        f"{pod}.containers[0].readinessProbe"),
                         f"{pod}.containers[0].readinessProbe.timeoutSeconds", "bool is not int")

    def test_deployment_readiness_tolerates_canonical_quantities_and_delegated_replicas(self):
        bundle = example()
        resources = deployment(bundle)["spec"]["template"]["spec"]["containers"][0]["resources"]
        resources["requests"] = {"cpu": "0.01", "memory": "8388608"}
        resources["limits"] = {"cpu": "0.1", "memory": "32Mi"}
        del deployment(bundle)["spec"]["replicas"]  # left to a HorizontalPodAutoscaler
        deploy = k8s.render(bundle, "suifleet-apps", "web-app", "1.0.0", "d" * 64)[1]
        live = self.healthy(deploy, replicas=3)
        live["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"cpu": "10m", "memory": "8Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}}
        self.assertEqual(live["spec"]["replicas"], 3)
        self.assertEqual(k8s.object_ready(live, deploy, "web-app"), (True, "ready"))
        live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "200m"
        self.assertIn("limits.cpu", k8s.object_ready(live, deploy, "web-app")[1])

    def test_spec_drift(self):
        self.assertEqual(k8s.spec_drift({"a": 1}, {"a": 1, "b": 2}, "spec"), "spec.b",
                         "a non-default live field the bundle omits is drift")
        for zero in (False, 0, "", {}, [], None, {"x": {}}):
            self.assertIsNone(k8s.spec_drift({}, {"b": zero}, "spec"), f"omitted field holding {zero!r}")
        self.assertEqual(k8s.spec_drift({"a": 1}, {"a": 2}, "spec"), "spec.a")
        self.assertEqual(k8s.spec_drift({"a": 1}, {}, "spec"), "spec.a")
        for empty in (False, 0, "", {}, [], None, {"x": {}}):
            self.assertIsNone(k8s.spec_drift({"a": empty}, {}, "spec"), f"omitted live field matches {empty!r}")
        self.assertEqual(k8s.spec_drift({"a": True}, {}, "spec"), "spec.a")
        self.assertEqual(k8s.spec_drift({"a": True}, {"a": 1}, "spec"), "spec.a", "bool is not int")
        self.assertEqual(k8s.spec_drift({"a": "1"}, {"a": 1}, "spec"), "spec.a", "IntOrString kinds differ")
        self.assertEqual(k8s.spec_drift({"a": [1, 2]}, {"a": [2, 1]}, "spec"), "spec.a[0]")
        self.assertEqual(k8s.spec_drift({"a": [1]}, {"a": [1, 2]}, "spec"), "spec.a")
        self.assertEqual(k8s.spec_drift({"a": {"b": 1}}, {"a": "x"}, "spec"), "spec.a")
        cases = [("1", "1000m", True), ("0.5", "500m", True), ("1Gi", "1073741824", True), ("1e3", "1k", True),
                 (1, "1", True), ("1G", "1Gi", False), ("100m", "0.2", False),
                 (0.1, "100m", True), (0.5, "500m", True), (1.5, "1500m", True), (0.1, "200m", False),
                 (1e-05, "10u", True), ("1E", "1000P", True), ("2E", "1000P", False), ("1Ei", "1024Pi", True)]
        for want, have, same in cases:
            drift = k8s.spec_drift({"resources": {"limits": {"cpu": want}}},
                                   {"resources": {"limits": {"cpu": have}}}, "spec.c")
            self.assertEqual(drift is None, same, f"{want!r} vs {have!r}")
        self.assertEqual(k8s.spec_drift({}, {"nodeSelector": {"k": ""}}, "spec"), "spec.nodeSelector.k",
                         "map keys are meaningful even with empty values")
        self.assertEqual(k8s.spec_drift({"labels": {"a": "1"}}, {"labels": {"a": "1", "b": ""}}, "spec"),
                         "spec.labels.b")
        self.assertEqual(k8s.spec_drift({"labels": {"a": ""}}, {"labels": {}}, "spec"), "spec.labels.a")
        self.assertIsNone(k8s.spec_drift({"labels": {"a": ""}}, {"labels": {"a": ""}}, "spec"))
        self.assertIsNone(k8s.spec_drift({}, {"nodeSelector": {}}, "spec"))
        self.assertEqual(k8s.spec_drift({"image": "1"}, {"image": "1000m"}, "spec"), "spec.image",
                         "quantity equivalence applies only to resources")

    def test_transfer_patch(self):
        current = {"metadata": {"uid": "u1", "resourceVersion": "7", "managedFields": [
            {"manager": "suifleet", "operation": "Update", "apiVersion": "v1", "fieldsType": "FieldsV1",
             "time": "t", "fieldsV1": {"f:data": {"f:a": {}}}},
            {"manager": "kube-controller-manager", "operation": "Update", "apiVersion": "v1",
             "fieldsV1": {"f:metadata": {"f:annotations": {"f:x": {}}}}},
            {"manager": "suifleet", "operation": "Update", "subresource": "status", "fieldsV1": {"f:status": {}}},
        ]}}
        patch = k8s.transfer_patch(current)
        self.assertEqual((patch["metadata"]["uid"], patch["metadata"]["resourceVersion"]), ("u1", "7"))
        entries = patch["metadata"]["managedFields"]
        self.assertEqual(len(entries), 3, "other managers and subresource entries are preserved")
        applied = [e for e in entries if e.get("operation") == "Apply"]
        self.assertEqual(applied, [{"manager": "suifleet", "operation": "Apply", "apiVersion": "v1",
                                    "fieldsType": "FieldsV1", "fieldsV1": {"f:data": {"f:a": {}}}}])
        current["metadata"]["managedFields"][0]["operation"] = "Apply"
        self.assertIsNone(k8s.transfer_patch(current), "nothing to transfer once applied")
        current["metadata"]["managedFields"].append({"manager": "suifleet", "operation": "Update", "apiVersion": "v1",
                                                     "fieldsV1": {"f:data": {"f:b": {}}}})
        merged = [e for e in k8s.transfer_patch(current)["metadata"]["managedFields"] if e.get("operation") == "Apply"]
        self.assertEqual(merged[0]["fieldsV1"], {"f:data": {"f:a": {}, "f:b": {}}})

    def test_ownership(self):
        ours = {"metadata": {"labels": {k8s.MANAGED_BY: "suifleet", k8s.COMPONENT_LABEL: "web-app"}}}
        self.assertIsNone(k8s.ownership(None, "web-app"))
        self.assertIsNone(k8s.ownership(ours, "web-app"))
        self.assertIn("does not adopt", k8s.ownership({"metadata": {"name": "x"}}, "web-app"))
        self.assertIn("does not adopt", k8s.ownership({"metadata": {"labels": {k8s.MANAGED_BY: "Helm"}}}, "web-app"))
        self.assertIn("component 'web-app'", k8s.ownership(ours, "other"))

    def test_prune_candidates(self):
        listed = [{"metadata": {"name": n}} for n in ("a", "b", "c")]
        self.assertEqual(k8s.prune_candidates(listed, {"b"}), ["a", "c"])
        self.assertEqual(k8s.prune_candidates(listed, set()), ["a", "b", "c"])
        self.assertEqual(k8s.prune_candidates([], {"a"}), [])

    def test_deployment_ready(self):
        digest = "e" * 64
        base = {"metadata": {"generation": 2, "annotations": {k8s.BUNDLE_ANNOTATION: digest}}, "spec": {"replicas": 2},
                "status": {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}}
        self.assertEqual(k8s.deployment_ready(base, digest), (True, "available"))
        self.assertFalse(k8s.deployment_ready(base, "f" * 64)[0], "another bundle's rollout is not this one's readiness")
        for change, reason in [({"observedGeneration": 1}, "not yet observed"), ({"availableReplicas": 1}, "availableReplicas"),
                               ({"updatedReplicas": 1}, "updatedReplicas"), ({"replicas": 3}, "terminating"),
                               ({"conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}]}, "deadline")]:
            item = copy.deepcopy(base)
            item["status"].update(change)
            ok, why = k8s.deployment_ready(item, digest)
            self.assertFalse(ok)
            self.assertIn(reason, why)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "edge"
        self.directory.mkdir(mode=0o700)
        self.write({"version": 1, "namespace": "suifleet-apps", "kubeconfig": "kubeconfig", "kubectl": ["/usr/bin/kubectl"]})
        (self.directory / "kubeconfig").write_text("{}")
        (self.directory / "kubeconfig").chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, profile):
        path = self.directory / "profile.json"
        path.write_text(json.dumps(profile))
        path.chmod(0o644)

    def test_valid_profile(self):
        profile = k8s.load_profile(self.root, "edge")
        self.assertEqual(profile["namespace"], "suifleet-apps")
        self.assertEqual(profile["kubeconfig"], str(self.directory / "kubeconfig"))

    def test_rejections(self):
        with self.assertRaisesRegex(k8s.Rejected, "DNS label"):
            k8s.load_profile(self.root, "../etc")
        (self.directory / "kubeconfig").chmod(0o640)
        with self.assertRaisesRegex(k8s.Rejected, "0600"):
            k8s.load_profile(self.root, "edge")
        (self.directory / "kubeconfig").chmod(0o600)
        self.directory.chmod(0o777)
        with self.assertRaisesRegex(k8s.Rejected, "writable"):
            k8s.load_profile(self.root, "edge")
        self.directory.chmod(0o700)
        for namespace in ("default", "kube-system", "Bad"):
            self.write({"version": 1, "namespace": namespace})
            with self.assertRaises(k8s.Rejected):
                k8s.load_profile(self.root, "edge")
        self.write({"version": 1, "namespace": "suifleet-apps", "kubeconfig": "../kubeconfig"})
        with self.assertRaisesRegex(k8s.Rejected, "inside the profile"):
            k8s.load_profile(self.root, "edge")
        self.write({"version": 1, "namespace": "suifleet-apps", "token": "x"})
        with self.assertRaisesRegex(k8s.Rejected, "unsupported"):
            k8s.load_profile(self.root, "edge")
        self.write({"version": 1, "namespace": "suifleet-apps"})
        os.unlink(self.directory / "kubeconfig")
        os.symlink("/etc/passwd", self.directory / "kubeconfig")
        with self.assertRaisesRegex(k8s.Rejected, "symlink"):
            k8s.load_profile(self.root, "edge")


class CredentialTest(unittest.TestCase):
    def view(self, cluster=None, user=None):
        return {"contexts": [{"name": "c"}],
                "clusters": [{"cluster": cluster if cluster is not None else {"server": "https://127.0.0.1:6443", "certificate-authority-data": "Q0E="}}],
                "users": [{"user": user if user is not None else {"token": "t"}}]}

    def test_accepts_token_or_client_certificate(self):
        k8s.check_credentials(self.view())
        k8s.check_credentials(self.view(user={"client-certificate-data": "x", "client-key-data": "y"}))
        k8s.check_credentials(self.view(user={"client-certificate": "/p/c.crt", "client-key": "/p/c.key"}))
        k8s.check_credentials(self.view(user={"tokenFile": "/p/token"}))

    def test_exactly_one_complete_credential_method(self):
        cert = {"client-certificate-data": "x", "client-key-data": "y"}
        cases = [
            ({"token": "t", **cert}, "exactly one credential method"),
            ({"tokenFile": "/p/token", **cert}, "exactly one credential method"),
            ({"token": "t", "client-key-data": "y"}, "exactly one credential method"),
            ({"token": "t", "client-certificate": "/p/c.crt"}, "exactly one credential method"),
            ({"token": "t", "tokenFile": "/p/token"}, "both token and tokenFile"),
            ({"client-certificate-data": "x"}, "supplied together"),
            ({"client-certificate": "/p/c.crt"}, "supplied together"),
            ({"client-key-data": "y"}, "supplied together"),
            ({"client-certificate-data": "x", "client-certificate": "/p/c.crt", "client-key-data": "y"}, "once each"),
            ({"client-certificate-data": "x", "client-key-data": "y", "client-key": "/p/c.key"}, "once each"),
            ({"token": ""}, "token or client certificate"),
        ]
        for user, message in cases:
            with self.subTest(user=sorted(user)), self.assertRaises(k8s.Rejected) as caught:
                k8s.check_credentials(self.view(user=user))
            self.assertIn(message, str(caught.exception))

    def test_rejections(self):
        cases = [
            (self.view(cluster={"server": "http://127.0.0.1:6443", "certificate-authority-data": "Q0E="}), "https"),
            (self.view(cluster={"server": "https://h", "insecure-skip-tls-verify": True, "certificate-authority-data": "Q0E="}), "verify TLS"),
            (self.view(cluster={"server": "https://h"}), "certificate authority"),
            (self.view(user={"exec": {"command": "aws"}}), "exec"),
            (self.view(user={"auth-provider": {"name": "oidc"}}), "auth-provider"),
            (self.view(user={"token": "t", "as": "system:admin"}), "'as'"),
            (self.view(user={"username": "u", "password": "p"}), "username"),
            (self.view(user={}), "token or client certificate"),
            ({"contexts": [], "clusters": [], "users": []}, "exactly one"),
        ]
        for view, message in cases:
            with self.assertRaises(k8s.Rejected) as caught:
                k8s.check_credentials(view)
            self.assertIn(message, str(caught.exception))


OURS = {k8s.MANAGED_BY: "suifleet", k8s.COMPONENT_LABEL: "web-app"}


def obj(name="web-content", uid="u1", rv="1", labels=OURS, managed="Apply"):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "uid": uid, "resourceVersion": rv, "labels": dict(labels or {}),
                         "managedFields": [{"manager": "suifleet", "operation": managed, "fieldsV1": {}}]}}


class FakeKubectl:
    """Scripted kubectl: `gets` is a list of successive GET results; `writes`
    a list of (returncode, stderr) for successive mutating calls."""

    def __init__(self, gets, writes):
        self.profile = {"namespace": "suifleet-apps"}
        self.gets, self.writes, self.calls = list(gets), list(writes), []

    def json(self, *args, namespaced=True):
        self.calls.append(("get",) + args)
        return self.gets.pop(0)

    def __call__(self, *args, stdin=None, namespaced=True):
        self.calls.append(args + ((json.loads(stdin),) if stdin else ()))
        code, error = self.writes.pop(0)
        return subprocess.CompletedProcess(args, code, "", error)


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.item = k8s.render({"resources": [example()["resources"][0]]}, "suifleet-apps", "web-app", "1", "d" * 64)[0]

    def writer(self, gets, writes):
        fake = FakeKubectl(gets, writes)
        return k8s.Writer(fake, "web-app", pause=0), fake

    def verbs(self, fake):
        return [call[0] for call in fake.calls]

    def test_create_transfer_then_bound_apply(self):
        writer, fake = self.writer([None, obj(managed="Update"), obj(rv="2")], [(0, ""), (0, ""), (0, "")])
        writer.apply(self.item)
        self.assertEqual(self.verbs(fake), ["get", "create", "get", "patch", "get", "apply"])
        create = fake.calls[1]
        self.assertNotIn("uid", create[-1]["metadata"], "creation is create-only, not an upsert")
        patch = fake.calls[3][-1]
        self.assertEqual(patch["metadata"]["resourceVersion"], "1")
        bound = fake.calls[5][-1]["metadata"]
        self.assertEqual((bound["uid"], bound["resourceVersion"]), ("u1", "2"), "apply is bound to the observed object")
        self.assertNotIn("--force-conflicts", fake.calls[5])

    def test_competing_creation_is_not_adopted(self):
        writer, fake = self.writer([None, obj(labels={})], [(1, 'Error from server (AlreadyExists): configmaps "web-content" already exists')])
        with self.assertRaisesRegex(k8s.Conflict, "does not adopt"):
            writer.apply(self.item)
        self.assertEqual(self.verbs(fake), ["get", "create", "get"])

    def test_replacement_after_check_is_not_mutated(self):
        stale = 'The ConfigMap "web-content" is invalid: metadata.uid: Invalid value: "u1": field is immutable'
        writer, fake = self.writer([obj(), obj(uid="u2", labels={"team": "customer"})], [(1, stale)])
        with self.assertRaisesRegex(k8s.Conflict, "does not adopt"):
            writer.apply(self.item)
        self.assertEqual(self.verbs(fake), ["get", "apply", "get"])

    def test_field_conflict_on_a_replacement_rechecks_ownership(self):
        # Observed on k3s: the field conflict is reported before the UID check.
        conflict = 'error: Apply failed with 1 conflict: conflict with "kubectl-create" using v1: .data.index.html'
        replacement = obj(uid="u2", labels={})
        writer, fake = self.writer([obj(), replacement, replacement], [(1, conflict)])
        with self.assertRaisesRegex(k8s.Conflict, "does not adopt"):
            writer.apply(self.item)
        self.assertEqual(self.verbs(fake), ["get", "apply", "get", "get"])

    def test_relabel_after_check_is_not_mutated(self):
        modified = 'Operation cannot be fulfilled on configmaps "web-content": the object has been modified'
        writer, fake = self.writer([obj(), obj(rv="2", labels={k8s.MANAGED_BY: "suifleet", k8s.COMPONENT_LABEL: "other"})],
                                   [(1, modified)])
        with self.assertRaisesRegex(k8s.Conflict, "component 'other'"):
            writer.apply(self.item)

    def test_deleted_after_check_is_recreated_not_resurrected_by_apply(self):
        gone = 'Operation cannot be fulfilled on configmaps "web-content": uid mismatch: the provided object specified uid u1, and no existing object was found'
        writer, fake = self.writer([obj(), None, obj(uid="u3", rv="9")], [(1, gone), (0, ""), (0, "")])
        writer.apply(self.item)
        self.assertEqual(self.verbs(fake), ["get", "apply", "get", "create", "get", "apply"])

    def test_field_manager_conflict_and_persistent_churn(self):
        writer, _ = self.writer([obj(), obj()], [(1, 'error: Apply failed with 1 conflict: conflict with "gitops" using v1: .data.x')])
        with self.assertRaisesRegex(k8s.Conflict, "not overriding"):
            writer.apply(self.item)
        modified = "the object has been modified"
        writer, _ = self.writer([obj()] * k8s.WRITE_ATTEMPTS, [(1, modified)] * k8s.WRITE_ATTEMPTS)
        with self.assertRaisesRegex(k8s.Conflict, "kept changing"):
            writer.apply(self.item)
        writer, _ = self.writer([None], [(1, 'namespaces "suifleet-apps" not found')])
        with self.assertRaisesRegex(k8s.Rejected, "create"):
            writer.apply(self.item)

    def test_delete_uses_preconditions_and_skips_replaced_or_relabelled(self):
        writer, fake = self.writer([], [(0, "")])
        self.assertTrue(writer.delete("configmaps", obj(name="old", rv="4")))
        call = fake.calls[0]
        self.assertEqual(call[:3], ("delete", "--raw", "/api/v1/namespaces/suifleet-apps/configmaps/old"))
        self.assertEqual(call[-1]["preconditions"], {"uid": "u1", "resourceVersion": "4"})
        # Foreground: a pruned Deployment stays listed until its ReplicaSets
        # and Pods are gone, so readiness covers still-running dependents.
        self.assertEqual(call[-1]["propagationPolicy"], "Foreground")
        conflict = "Error from server (Conflict): the UID in the precondition (u1) does not match"
        writer, fake = self.writer([obj(name="old", uid="u2", labels={})], [(1, conflict)])
        self.assertFalse(writer.delete("configmaps", obj(name="old")), "a foreign replacement is left alone")
        writer, fake = self.writer([obj(name="old", rv="5", labels={"team": "customer"})], [(1, conflict)])
        self.assertFalse(writer.delete("configmaps", obj(name="old")), "a relabelled object is left alone")
        writer, fake = self.writer([None], [(1, conflict)])
        self.assertTrue(writer.delete("configmaps", obj(name="old")))
        writer, fake = self.writer([obj(name="old", rv="5")], [(1, conflict), (0, "")])
        self.assertTrue(writer.delete("configmaps", obj(name="old")))
        self.assertEqual(fake.calls[-1][-1]["preconditions"]["resourceVersion"], "5")
        writer, _ = self.writer([], [(1, "Error from server (Forbidden): nope")])
        with self.assertRaises(k8s.Rejected):
            writer.delete("configmaps", obj(name="old"))

    def test_delete_never_follows_a_same_owner_replacement(self):
        # The listed object (u1@4) is replaced by a NEW object that still
        # carries this component's labels. Only the listed UID may be deleted.
        conflict = "Error from server (Conflict): the UID in the precondition (u1) does not match"
        writer, fake = self.writer([obj(name="old", uid="u2", rv="5")], [(1, conflict)])
        self.assertFalse(writer.delete("configmaps", obj(name="old", rv="4")))
        deletes = [call for call in fake.calls if call[0] == "delete"]
        self.assertEqual([call[-1]["preconditions"] for call in deletes], [{"uid": "u1", "resourceVersion": "4"}])
        # A refreshed resourceVersion is retried only for the original UID.
        writer, fake = self.writer([obj(name="old", rv="5"), obj(name="old", uid="u2", rv="6")],
                                   [(1, "the object has been modified"), (1, conflict)])
        self.assertFalse(writer.delete("configmaps", obj(name="old", rv="4")))
        deletes = [call[-1]["preconditions"] for call in fake.calls if call[0] == "delete"]
        self.assertEqual(deletes, [{"uid": "u1", "resourceVersion": "4"}, {"uid": "u1", "resourceVersion": "5"}])

    def test_prune_reports_only_deleted(self):
        listed = {"items": [obj(name="keep"), obj(name="old")]}
        fake = FakeKubectl([listed, {"items": []}], [(0, "")])
        removed = k8s.prune(fake, "web-app", {"configmaps": {"keep"}}, k8s.Writer(fake, "web-app", pause=0))
        self.assertEqual(removed, ["configmaps/old"])

    def test_prune_does_not_redelete_a_terminating_object(self):
        held = obj(name="old")
        held["metadata"]["deletionTimestamp"] = "2026-11-24T00:00:00Z"
        fake = FakeKubectl([{"items": [held]}, {"items": []}], [])
        self.assertEqual(k8s.prune(fake, "web-app", {}, k8s.Writer(fake, "web-app", pause=0)), [])
        self.assertNotIn("delete", [call[0] for call in fake.calls])


    def test_remove_waits_until_nothing_remains(self):
        # prune lists configmaps+deployments and deletes "old"; the first
        # convergence poll still sees it terminating (foreground deletion in
        # progress) and stops listing there, the second sees nothing.
        terminating = obj(name="old")
        terminating["metadata"]["deletionTimestamp"] = "2026-11-24T00:00:00Z"
        fake = FakeKubectl([{"items": [obj(name="old")]}, {"items": []},
                            {"items": [terminating]},
                            {"items": []}, {"items": []}], [(0, "")])
        ok, reason = k8s.remove(fake, "web-app", 60, k8s.Writer(fake, "web-app", pause=0), pause=0)
        self.assertEqual((ok, reason), (True, "removed"))
        self.assertEqual([call[0] for call in fake.calls].count("delete"), 1)
        self.assertEqual(fake.gets, [])

    def test_remove_reports_unconverged_when_a_finalizer_holds(self):
        held = obj(name="old")
        held["metadata"]["deletionTimestamp"] = "2026-11-24T00:00:00Z"
        fake = FakeKubectl([{"items": [held]}, {"items": []}, {"items": [held]}], [])
        ok, reason = k8s.remove(fake, "web-app", 0, k8s.Writer(fake, "web-app", pause=0), pause=0)
        self.assertFalse(ok)
        self.assertIn("configmaps/old not yet pruned", reason)
        self.assertNotIn("delete", [call[0] for call in fake.calls])

    def test_remove_stops_at_the_deadline_when_a_poll_is_slow(self):
        # Reviewer scenario: --timeout 5 and an API server that takes 30s to
        # answer the first convergence poll. The deadline-bounded call is cut
        # off (DeadlineExceeded) and remove returns at the deadline instead
        # of after the slow response.
        clock = [1000.0]
        fake = FakeKubectl([], [])
        fake.deadline = None
        seen = []

        def json_call(*args, namespaced=True):
            seen.append(fake.deadline)
            if fake.deadline is None:
                return {"items": []}  # the delete phase's lists: unbounded
            clock[0] += 5  # the bounded subprocess is killed at the deadline
            raise k8s.DeadlineExceeded("deadline reached during kubectl get configmaps")

        fake.json = json_call
        with mock.patch.object(k8s.time, "monotonic", lambda: clock[0]):
            ok, reason = k8s.remove(fake, "web-app", 5, k8s.Writer(fake, "web-app", pause=0), pause=0)
        self.assertFalse(ok)
        self.assertIn("timed out after 5s", reason)
        self.assertEqual(seen, [None, None, 1005.0])
        self.assertEqual(clock[0], 1005.0)
        self.assertIsNone(fake.deadline)


class KubectlDeadlineTest(unittest.TestCase):
    def kubectl(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        return k8s.Kubectl({"kubectl": ["kubectl"], "kubeconfig": "/k", "namespace": "ns"}, tmp)

    def run_call(self, kubectl, now, side_effect=None):
        result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(k8s.time, "monotonic", lambda: now), \
                mock.patch.object(k8s.subprocess, "run", side_effect=side_effect, return_value=result) as run:
            kubectl("get", "configmaps")
        return run

    def test_unbounded_calls_use_fixed_limits(self):
        run = self.run_call(self.kubectl(), 0.0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--request-timeout") + 1], "30s")
        self.assertEqual(run.call_args.kwargs["timeout"], k8s.KUBECTL_TIMEOUT)

    def test_deadline_shrinks_request_and_process_limits(self):
        kubectl = self.kubectl()
        kubectl.deadline = 104.5
        run = self.run_call(kubectl, 100.0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--request-timeout") + 1], "5s")
        self.assertEqual(run.call_args.kwargs["timeout"], 4.5)

    def test_a_hanging_kubectl_process_is_killed_at_the_deadline(self):
        # Real process: a "kubectl" that hangs for 30s is cut off at the
        # remaining deadline instead of the fixed 60s process limit.
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        kubectl = k8s.Kubectl({"kubectl": ["/bin/sh", "-c", "sleep 30", "kubectl"], "kubeconfig": "/k",
                               "namespace": "ns"}, tmp)
        started = time.monotonic()
        kubectl.deadline = started + 0.5
        with self.assertRaises(k8s.DeadlineExceeded):
            kubectl.json("get", "configmaps")
        self.assertLess(time.monotonic() - started, 5)

    def test_past_deadline_or_killed_call_raises_deadline_exceeded(self):
        kubectl = self.kubectl()
        kubectl.deadline = 100.0
        with self.assertRaises(k8s.DeadlineExceeded):
            self.run_call(kubectl, 100.0)
        kubectl.deadline = 130.0
        with self.assertRaises(k8s.DeadlineExceeded):
            self.run_call(kubectl, 100.0, side_effect=subprocess.TimeoutExpired("kubectl", 30))
        kubectl.deadline = None
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_call(kubectl, 100.0, side_effect=subprocess.TimeoutExpired("kubectl", 60))


class ReadyTest(unittest.TestCase):
    """ready() requires every desired object ready AND prune convergence."""

    def setUp(self):
        self.objects = k8s.render({"resources": [example()["resources"][0]]}, "suifleet-apps", "web-app", "1", "d" * 64)

    def live(self, item, **metadata):
        current = copy.deepcopy(item)
        current["metadata"].update(uid="u1", resourceVersion="1", **metadata)
        return current

    def test_empty_retirement_bundle_waits_for_stale_objects(self):
        stale = {"items": [obj(name="old-web")]}
        fake = FakeKubectl([stale, {"items": []}], [])
        ok, reason = k8s.ready(fake, [], "web-app")
        self.assertFalse(ok)
        self.assertIn("configmaps/old-web not yet pruned", reason)
        self.assertTrue(fake.calls, "an empty bundle still queries the cluster")
        fake = FakeKubectl([{"items": []}, {"items": [obj(name="old-deploy")]}], [])
        ok, reason = k8s.ready(fake, [], "web-app")
        self.assertFalse(ok)
        self.assertIn("deployments.apps/old-deploy", reason)
        fake = FakeKubectl([{"items": []}, {"items": []}], [])
        self.assertEqual(k8s.ready(fake, [], "web-app"), (True, "ready"))
        selectors = [call[call.index("-l") + 1] for call in fake.calls]
        self.assertEqual(selectors, ["app.kubernetes.io/managed-by=suifleet,suifleet.io/component=web-app"] * 2)

    def test_replacement_bundle_waits_for_incomplete_prune(self):
        item = self.objects[0]
        current = self.live(item)
        terminating = obj(name="old-web")
        terminating["metadata"]["deletionTimestamp"] = "2026-11-24T00:00:00Z"
        fake = FakeKubectl([current, {"items": [current, terminating]}, {"items": []}], [])
        ok, reason = k8s.ready(fake, self.objects, "web-app")
        self.assertFalse(ok)
        self.assertIn("old-web not yet pruned", reason)
        fake = FakeKubectl([current, {"items": [current]}, {"items": []}], [])
        self.assertEqual(k8s.ready(fake, self.objects, "web-app"), (True, "ready"))

    def test_desired_object_problems_are_reported_first(self):
        fake = FakeKubectl([None], [])
        ok, reason = k8s.ready(fake, self.objects, "web-app")
        self.assertFalse(ok)
        self.assertIn("missing", reason)


class CliTest(unittest.TestCase):
    def test_validate_needs_no_profile_or_cluster(self):
        self.assertEqual(k8s.main(["validate", "--bundle", str(EXAMPLE)]), 0)
        with tempfile.TemporaryDirectory() as directory:
            bad = example()
            deployment(bad)["spec"]["template"]["spec"]["hostPID"] = True
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(bad))
            self.assertEqual(k8s.main(["validate", "--bundle", str(path)]), 2)
            # Malformed nested types keep the documented rejection exit code.
            for mutate in (lambda b: b["resources"][0].update(apiVersion=["v1"]),
                           lambda b: deployment(b)["spec"]["template"]["spec"].update(containers={"a": 1}),
                           lambda b: deployment(b)["spec"]["template"]["spec"]["containers"][0].update(env=7),
                           lambda b: b.update(resources={"x": 1})):
                malformed = example()
                mutate(malformed)
                path.write_text(json.dumps(malformed))
                self.assertEqual(k8s.main(["validate", "--bundle", str(path)]), 2)
            path.write_text("[" * 100000 + "]" * 100000)
            self.assertEqual(k8s.main(["validate", "--bundle", str(path)]), 2)

    def test_apply_requires_component_and_profile(self):
        self.assertEqual(k8s.main(["apply", "--bundle", str(EXAMPLE), "--component", "", "--profile", "edge"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(k8s.main(["apply", "--bundle", str(EXAMPLE), "--component", "web", "--profile", "edge",
                                       "--profile-root", directory]), 2)


if __name__ == "__main__":
    unittest.main()
