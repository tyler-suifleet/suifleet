"""Offline tests for scripts/suifleet_k8s_apply.py (SF-305).

Bundle validation, rendering, ownership/prune/readiness decisions and profile/
credential hygiene. No cluster, kubectl or network is used; the real-cluster
behaviour is covered by scripts/test_kubernetes_edge.py."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

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
        self.assertEqual(len(bundle["resources"]), 3)
        self.assertEqual(len(digest), 64)

    def test_envelope(self):
        self.rejects(lambda b: b.update(version=2), "bundle version")
        self.rejects(lambda b: b.update(extra=1), "unsupported field")
        self.rejects(lambda b: b["resources"].append(copy.deepcopy(b["resources"][0])), "duplicates ConfigMap/web-content")
        self.rejects(lambda b: b.update(resources=[b["resources"][0]] * (k8s.MAX_RESOURCES + 1)), "more than")

    def test_kinds_and_metadata(self):
        for api, kind in [("v1", "Secret"), ("v1", "Pod"), ("apps/v1", "DaemonSet"), ("v1", "Namespace"),
                          ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"), ("v1", "ServiceAccount"),
                          ("apiextensions.k8s.io/v1", "CustomResourceDefinition"), ("batch/v1", "Job")]:
            self.rejects(lambda b: b["resources"].append({"apiVersion": api, "kind": kind, "metadata": {"name": "x"}}),
                         "not an allowed kind")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(namespace="kube-system"), "unsupported field")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(name="Bad_Name"), "DNS label")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(labels={"app.kubernetes.io/managed-by": "argo"}), "reserved")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(annotations={"suifleet.io/version": "9"}), "reserved")
        self.rejects(lambda b: b["resources"][0]["metadata"].update(ownerReferences=[]), "unsupported field")

    def test_service_is_cluster_internal(self):
        service = lambda b: b["resources"][2]["spec"]
        self.rejects(lambda b: service(b).update(type="NodePort"), "ClusterIP")
        self.rejects(lambda b: service(b).update(type="LoadBalancer"), "ClusterIP")
        self.rejects(lambda b: service(b).update(externalIPs=["1.2.3.4"]), "unsupported field")
        self.rejects(lambda b: service(b)["ports"][0].update(nodePort=30000), "nodePort")

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
        os.chmod(self.directory / "kubeconfig", 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, profile):
        path = self.directory / "profile.json"
        path.write_text(json.dumps(profile))
        os.chmod(path, 0o644)

    def test_valid_profile(self):
        profile = k8s.load_profile(self.root, "edge")
        self.assertEqual(profile["namespace"], "suifleet-apps")
        self.assertEqual(profile["kubeconfig"], str(self.directory / "kubeconfig"))

    def test_rejections(self):
        with self.assertRaisesRegex(k8s.Rejected, "DNS label"):
            k8s.load_profile(self.root, "../etc")
        os.chmod(self.directory / "kubeconfig", 0o640)
        with self.assertRaisesRegex(k8s.Rejected, "0600"):
            k8s.load_profile(self.root, "edge")
        os.chmod(self.directory / "kubeconfig", 0o600)
        os.chmod(self.directory, 0o777)
        with self.assertRaisesRegex(k8s.Rejected, "writable"):
            k8s.load_profile(self.root, "edge")
        os.chmod(self.directory, 0o700)
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


class CliTest(unittest.TestCase):
    def test_validate_needs_no_profile_or_cluster(self):
        self.assertEqual(k8s.main(["validate", "--bundle", str(EXAMPLE)]), 0)
        with tempfile.TemporaryDirectory() as directory:
            bad = example()
            deployment(bad)["spec"]["template"]["spec"]["hostPID"] = True
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(bad))
            self.assertEqual(k8s.main(["validate", "--bundle", str(path)]), 2)

    def test_apply_requires_component_and_profile(self):
        self.assertEqual(k8s.main(["apply", "--bundle", str(EXAMPLE), "--component", "", "--profile", "edge"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(k8s.main(["apply", "--bundle", str(EXAMPLE), "--component", "web", "--profile", "edge",
                                       "--profile-root", directory]), 2)


if __name__ == "__main__":
    unittest.main()
