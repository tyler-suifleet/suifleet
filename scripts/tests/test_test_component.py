"""Offline tests for the SF-030 test component: the real shell scripts, run
under the system shell and, when installed, a busybox-only PATH like the
Yocto image's userland."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_component as component  # noqa: E402

BUSYBOX = shutil.which("busybox")
APPLETS = ["sh", "sed", "wc", "sha256sum", "cut", "cat", "mkdir", "rm", "cp", "chmod", "mv", "date", "uname", "printf"]


class Fixture:
    def __init__(self, test, busybox=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="suifleet-test-component-"))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "state" / "test-components"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
        self.shell = "/bin/sh"
        if busybox:
            bin_dir = self.tmp / "bin"
            bin_dir.mkdir()
            for applet in APPLETS:
                (bin_dir / applet).symlink_to(BUSYBOX)
            self.env["PATH"] = str(bin_dir)
            self.shell = str(bin_dir / "sh")

    def artifact(self, version, run_id="run-1", fail="none", name=None):
        path = self.tmp / (name or f"app-{version}-{fail}.sh")
        path.write_bytes(component.build(version, run_id, fail))
        return path

    def apply(self, artifact):
        return subprocess.run([self.shell, str(component.UPDATER), "--root", str(self.root), str(artifact)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def receipt(self, name=component.DEFAULT_COMPONENT):
        return json.loads((self.root / name / "receipt.json").read_text())

    def history(self, name=component.DEFAULT_COMPONENT):
        return [json.loads(line) for line in (self.root / name / "history.jsonl").read_text().splitlines()]

    def current(self, name=component.DEFAULT_COMPONENT):
        return (self.root / name / "current").read_text().strip()

    def files(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.is_file())


class ComponentBuildTests(unittest.TestCase):
    def test_builds_are_deterministic_and_variants_differ(self):
        v1 = component.build("1.0.0", "run-1")
        self.assertEqual(v1, component.build("1.0.0", "run-1"))
        variants = {component.build("2.0.0", "run-1"), component.build("1.0.0", "run-2"),
                    component.build("1.0.0", "run-1", "install"), component.build("1.0.0", "run-1", "health")}
        self.assertEqual(len(variants | {v1}), 5)

    def test_recipe_is_deterministic_and_bound_to_the_artifact(self):
        artifact = component.build("1.0.0", "run-1")
        recipe = component.recipe(artifact, "blob-1")
        self.assertEqual(recipe, component.recipe(artifact, "blob-1"))
        document = json.loads(recipe)
        self.assertEqual(document["componentName"], "suifleet-test-app")
        self.assertEqual(document["componentVersion"], "1.0.0")
        self.assertEqual(document["artifactBlobId"], "blob-1")
        self.assertEqual(document["artifactSha256"], hashlib.sha256(artifact).hexdigest())
        self.assertEqual((document["lifecycle"], document["configuration"]), ({}, {}))

    def test_unsafe_identifiers_are_refused(self):
        for version in ("..", ".hidden", "1/2", "1 0", "", "v" * 65, '1"'):
            with self.assertRaises(ValueError):
                component.build(version, "run-1")
        with self.assertRaises(ValueError):
            component.build("1.0.0", "run-1", "reboot")
        with self.assertRaises(ValueError):
            component.recipe(b"#!/bin/sh\necho not a component\n", "blob")

    def test_cli_writes_artifact_and_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            script = str(Path(component.__file__))
            build = subprocess.run([sys.executable, script, "build", "--version", "1.0.0", "--run-id", "cli",
                                    "--out", str(out / "app.sh")], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(build.stdout)["sha256"], hashlib.sha256((out / "app.sh").read_bytes()).hexdigest())
            subprocess.run([sys.executable, script, "recipe", "--artifact", str(out / "app.sh"), "--blob-id", "b1",
                            "--out", str(out / "recipe.json")], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads((out / "recipe.json").read_text())["artifactBlobId"], "b1")


class UpdaterTests:
    """Mixed into one TestCase per shell environment."""
    busybox = False

    def fixture(self):
        return Fixture(self, self.busybox)

    def test_v1_then_v2_report_exact_versions_and_run_ids(self):
        f = self.fixture()
        v1, v2 = f.artifact("1.0.0", "accept-1"), f.artifact("2.0.0", "accept-2")
        first = f.apply(v1)
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = f.receipt()
        self.assertEqual((receipt["version"], receipt["runId"], receipt["install"], receipt["health"]),
                         ("1.0.0", "accept-1", "succeeded", "passed"))
        self.assertEqual(receipt["artifactSha256"], hashlib.sha256(v1.read_bytes()).hexdigest())
        self.assertIsNone(receipt["previousVersion"])
        self.assertEqual((receipt["run"]["version"], receipt["run"]["runId"]), ("1.0.0", "accept-1"))
        self.assertEqual(receipt["run"]["arch"], os.uname().machine)
        self.assertTrue(receipt["run"]["bootId"])
        second = f.apply(v2)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt = f.receipt()
        self.assertEqual((receipt["version"], receipt["runId"], receipt["previousVersion"]), ("2.0.0", "accept-2", "1.0.0"))
        self.assertEqual(f.current(), "2.0.0")
        self.assertEqual([r["version"] for r in f.history()], ["1.0.0", "2.0.0"])

    def test_install_failure_keeps_the_previous_version(self):
        f = self.fixture()
        self.assertEqual(f.apply(f.artifact("1.0.0")).returncode, 0)
        result = f.apply(f.artifact("2.0.0", fail="install"))
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn("injected install failure", result.stderr)
        receipt = f.receipt()
        self.assertEqual((receipt["version"], receipt["install"], receipt["health"], receipt["run"]),
                         ("2.0.0", "failed", "not-run", None))
        self.assertEqual(f.current(), "1.0.0")
        self.assertFalse((f.root / "suifleet-test-app/versions/2.0.0").exists())
        self.assertEqual([p for p in (f.root / "suifleet-test-app").iterdir() if p.name.startswith(".staging")], [])

    def test_health_failure_is_distinct_from_install_failure(self):
        f = self.fixture()
        result = f.apply(f.artifact("1.0.0", fail="health"))
        self.assertEqual(result.returncode, 20, result.stderr)
        receipt = f.receipt()
        self.assertEqual((receipt["install"], receipt["health"]), ("succeeded", "failed"))
        self.assertEqual(receipt["run"]["version"], "1.0.0", "the app ran before failing its health check")
        self.assertEqual(f.current(), "1.0.0")

    def test_malformed_artifacts_are_rejected_without_writes(self):
        f = self.fixture()
        good = f.artifact("1.0.0").read_text()
        cases = {
            "plain-script": "#!/bin/sh\necho hello\n",
            "dot-version": good.replace("# version=1.0.0", "# version=.."),
            "slash-component": good.replace("# component=suifleet-test-app", "# component=a/b"),
            "quote-run-id": good.replace("# run_id=run-1", '# run_id=a"b'),
            "missing-run-id": good.replace("# run_id=run-1\n", ""),
            "oversized": good + "#" * 70000 + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                path = f.tmp / name
                path.write_text(text)
                result = f.apply(path)
                self.assertEqual(result.returncode, 2, (name, result.stderr))
        link = f.tmp / "link"
        link.symlink_to(f.artifact("1.0.0", name="target.sh"))
        self.assertEqual(f.apply(link).returncode, 2)
        self.assertFalse(f.root.exists(), "a rejected artifact must not write anything")

    def test_writes_stay_under_the_root(self):
        f = self.fixture()
        before = f.files()
        artifact = f.artifact("1.0.0")
        self.assertEqual(f.apply(artifact).returncode, 0)
        created = sorted(set(f.files()) - set(before) - {str(artifact.relative_to(f.tmp))})
        self.assertTrue(created)
        root = str(f.root.relative_to(f.tmp)) + "/"
        self.assertTrue(all(path.startswith(root) for path in created), created)


class SystemShellUpdaterTests(UpdaterTests, unittest.TestCase):
    pass


@unittest.skipUnless(BUSYBOX, "busybox not installed")
class BusyboxUpdaterTests(UpdaterTests, unittest.TestCase):
    busybox = True


if __name__ == "__main__":
    unittest.main()
