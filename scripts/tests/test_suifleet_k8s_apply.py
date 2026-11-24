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

    def test_prune_reports_only_deleted(self):
        listed = {"items": [obj(name="keep"), obj(name="old")]}
        fake = FakeKubectl([listed, {"items": []}], [(0, "")])
        removed = k8s.prune(fake, "web-app", {"configmaps": {"keep"}}, k8s.Writer(fake, "web-app", pause=0))
        self.assertEqual(removed, ["configmaps/old"])


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
