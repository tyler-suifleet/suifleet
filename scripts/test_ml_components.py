#!/usr/bin/env python3
"""Actual isolated subprocess inference and interrupted-install evidence, SF-073.

No Python ML dependencies needed on the host. Fetch hash-locked wheels first
with ml_component.py download; this test is offline and uses only those wheels.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import ml_component as builder
import ml_runner as runner

WHEELS = None


class Components(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sf073-test-")
        cls.root = Path(cls.temp.name)
        cls.platform = runner.target()
        cls.paths = {}
        for task in ("classifier", "detector"):
            path = cls.root / (task + ".pyz")
            path.write_bytes(builder.build(task, cls.platform, WHEELS))
            cls.paths[task] = path
        cls.state = cls.root / "runtime"
        cls.execute(cls.paths["classifier"], "install", state=cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def execute(cls, bundle, command, *args, state=None, success=True):
        result = subprocess.run([sys.executable, "-I", "-B", str(bundle), command, "--state", str(state or cls.state), *args],
                                capture_output=True, text=True, timeout=60,
                                # Verify isolated mode ignores injected Python paths.
                                env={**os.environ, "PYTHONPATH": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"})
        if success and result.returncode != 0:
            raise AssertionError(result.stderr)
        if not success:
            if result.returncode == 0 or result.stdout:
                raise AssertionError("failure returned a success result: " + result.stdout)
            return result.stderr
        return json.loads(result.stdout)

    def mutate(self, name, edit):
        with zipfile.ZipFile(self.paths["classifier"]) as z:
            files = {n: z.read(n) for n in z.namelist()}
        manifest = json.loads(files["manifest.json"])
        edit(manifest, files)
        files["manifest.json"] = json.dumps(manifest).encode()
        path = self.root / (name + ".pyz")
        with zipfile.ZipFile(path, "w") as z:
            for n, raw in files.items():
                z.writestr(n, raw)
        return path

    def test_actual_inference(self):
        models = builder.document("models.json")
        for task, bundle in self.paths.items():
            for case in models["cases"]:
                with self.subTest(task=task, image=case["image"]):
                    result = self.execute(bundle, "infer", "--fixture", case["image"])
                    expected = case[task + "_scores"]
                    self.assertEqual(len(result["scores"]), len(expected))
                    for actual, wanted in zip(result["scores"], expected):
                        self.assertLessEqual(abs(actual - wanted), models["tolerance_absolute"])
                    self.assertEqual(result["model_version"], "1.0.0")
                    self.assertEqual(result["runtime_version"], "2.14.0")
                    if task == "classifier":
                        self.assertEqual(result["label"], case["classifier_label"])
                    else:
                        self.assertEqual([d["box_xyxy"] for d in result["detections"]], case["boxes_xyxy"])

    def test_deterministic_artifacts_and_authenticated_recipe(self):
        for task, bundle in self.paths.items():
            raw = builder.build(task, self.platform, WHEELS)
            self.assertEqual(raw, bundle.read_bytes())
            recipe = builder.recipe(raw, "uploaded_blob")
            self.assertEqual(recipe["artifactSha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(recipe["componentName"], "suifleet-ml-" + task)
            # Run the exact generated lifecycle in a workspace with spaces.
            workspace = self.root / ("recipe workspace " + task)
            workspace.mkdir()
            import shlex
            command = recipe["lifecycle"]["install"].replace("{artifact}", shlex.quote(str(bundle))).replace("{artifacts}", shlex.quote(str(workspace)))
            result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(output[-1]["task"], task)

    def test_wrong_digest_and_no_state_mutation(self):
        def corrupt(manifest, files):
            files["models/classifier-1.0.0.tflite"] += b"corrupt"
        bundle = self.mutate("digest", corrupt)
        state = self.root / "bad-digest-state"
        error = self.execute(bundle, "install", state=state, success=False)
        self.assertIn("digest mismatch", error)
        self.assertFalse(state.exists())

    def test_versions_and_platform_fail_closed(self):
        for field, value, message in [("format_version", 99, "component version"),
                                      ("component_version", "9.0.0", "component version"),
                                      ("model_version", "9.0.0", "model/version"),
                                      ("runtime_version", "9.0.0", "runtime version"),
                                      ("platform", "linux-armv7l-cp310", "platform"),
                                      ("glibc_min", "999.0", "libc")]:
            with self.subTest(field=field):
                bundle = self.mutate(field, lambda m, f: m.update({field: value}))
                self.assertIn(message, self.execute(bundle, "infer", "--fixture", "marker", success=False))

    def test_wrong_model_even_with_matching_digest(self):
        def wrong_model(manifest, files):
            name = "models/classifier-1.0.0.tflite"
            raw = (builder.ASSETS / "models/detector-1.0.0.tflite").read_bytes()
            files[name] = raw
            manifest["files"][name] = builder.sha(raw)
        bundle = self.mutate("wrong-model", wrong_model)
        self.assertIn("tensor contract mismatch", self.execute(bundle, "infer", "--fixture", "marker", success=False))

    def test_bad_inputs_then_recovery(self):
        for index, raw in enumerate([b"", b"P5\n2 2\n255\n" + bytes(4), b"P5\n4 4\n255\n" + bytes(15),
                                     b"P5\n4 4\n255\n" + bytes(17), b"P5\n4 4\n65535\n" + bytes(32)]):
            path = self.root / f"bad-{index}.pgm"
            path.write_bytes(raw)
            self.assertIn("input must be", self.execute(self.paths["classifier"], "infer", "--image", str(path), success=False))
        path = builder.ASSETS / "images/bright.pgm"
        self.assertEqual(self.execute(self.paths["classifier"], "infer", "--image", str(path))["label"], "bright")

    def test_installed_runtime_tamper_and_recovery(self):
        state = self.root / "tampered"
        self.execute(self.paths["classifier"], "install", state=state)
        victim = next(state.glob("tflite_runtime/*.so"))
        original = victim.read_bytes()
        victim.write_bytes(b"not a runtime")
        self.assertIn("runtime digest mismatch", self.execute(self.paths["classifier"], "infer", "--fixture", "dark", state=state, success=False))
        self.assertIn("runtime digest mismatch", self.execute(self.paths["classifier"], "install", state=state, success=False))
        # Explicit operator repair, never automatic deletion/overwrite by installer.
        victim.write_bytes(original)
        self.assertEqual(self.execute(self.paths["classifier"], "infer", "--fixture", "dark", state=state)["label"], "dark")

    def test_actual_sigkill_during_install_and_restart(self):
        parent = self.root / "interrupted"
        parent.mkdir()
        state = parent / "runtime"
        process = subprocess.Popen([sys.executable, "-I", "-B", str(self.paths["classifier"]), "install", "--state", str(state)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not list(parent.glob(".ml-stage-*")):
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("installer did not reach staging before exit/deadline")
                time.sleep(0.001)
            process.kill()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, -9)
            self.assertFalse(state.exists(), "kill missed pre-commit staging window")
            self.assertTrue(list(parent.glob(".ml-stage-*")))
            self.assertIn("missing/unsafe", self.execute(self.paths["classifier"], "infer", "--fixture", "marker", state=state, success=False))
            self.execute(self.paths["classifier"], "install", state=state)
            self.execute(self.paths["classifier"], "install", state=state)  # idempotent
            self.assertEqual(self.execute(self.paths["classifier"], "infer", "--fixture", "marker", state=state)["scores"], [.75, .25])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_unsupported_selection_and_bad_wheel(self):
        with self.assertRaisesRegex(ValueError, "unsupported platform"):
            builder.build("classifier", "windows-amd64-cp310", WHEELS)
        _, selection = builder.selection(self.platform)
        with self.assertRaisesRegex(ValueError, "wheel digest mismatch"):
            builder.check_wheel(selection["wheels"][0], b"wrong-wheel")
        with self.assertRaisesRegex(ValueError, "unsupported model"):
            builder.build("unknown", self.platform, WHEELS)

    def test_license_and_architecture_pins(self):
        lock = builder.document("runtime-lock.json")
        self.assertEqual(lock["platforms"]["linux-aarch64-cp310"]["glibc_min"], "2.34")
        for selection in lock["platforms"].values():
            for wheel in selection["wheels"]:
                self.assertTrue(wheel["license"])
                if wheel["name"] == "numpy":
                    self.assertTrue(wheel["notices"])
        _, selection = builder.selection(self.platform)
        numpy = selection["wheels"][1]
        changed = {**numpy, "notices": {name: "0"*64 for name in numpy["notices"]}}
        with self.assertRaisesRegex(ValueError, "license mismatch"):
            builder.check_wheel(changed, (WHEELS / numpy["filename"]).read_bytes())
        for name in ("licenses/tensorflow-LICENSE.txt", "licenses/tensorflow-THIRD_PARTY_NOTICES.txt", "LICENSE"):
            with zipfile.ZipFile(self.paths["classifier"]) as archive:
                self.assertEqual(archive.read(name), (builder.ASSETS / name).read_bytes())

    def test_archive_and_state_symlinks_denied(self):
        state = self.root / "linked-state"
        state.symlink_to(self.state, target_is_directory=True)
        self.assertIn("unsafe", self.execute(self.paths["classifier"], "install", state=state, success=False))
        def traversal(manifest, files):
            files["../escape"] = b"bad"
            manifest["files"]["../escape"] = builder.sha(b"bad")
        bundle = self.mutate("traversal", traversal)
        self.assertIn("unsafe archive member", self.execute(bundle, "install", success=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    WHEELS = args.wheels
    unittest.main(argv=[sys.argv[0], *remaining])
