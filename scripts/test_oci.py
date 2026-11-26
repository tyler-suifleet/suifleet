#!/usr/bin/env python3
"""Run the real-engine OCI supervisor regression with scoped cleanup.

Requires Linux, a local Docker or Podman engine, Rust 1.94, and
SF_OCI_TEST_IMAGE set to an already-cached immutable Alpine image.
When containerized, use the host network for the loopback registry fixture
and set SF_OCI_TEST_TMPDIR to a host directory mounted at the same absolute
path for the Docker grant fixture.
No chain state, wallet, production credentials or external registry is used.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    image = os.environ.get("SF_OCI_TEST_IMAGE")
    if not image:
        raise SystemExit("Set SF_OCI_TEST_IMAGE to a cached immutable Alpine image")
    selected = os.environ.get("SUIFLEET_OCI_ENGINE", "docker")
    if selected == "docker":
        engine = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
        tests = ["oci_real_engine"]
    elif selected in {"podman-rootful", "podman-rootless"}:
        run = "/run/podman" if selected == "podman-rootful" else "/run/suifleet-podman"
        engine = ["/usr/bin/podman-remote", "--url", f"unix://{run}/podman.sock"]
        tests = ["supervisor::tests::oci_real_engine", "--skip", "oci_real_engine_grants"]
    else:
        raise SystemExit("Unsupported SUIFLEET_OCI_ENGINE")
    with tempfile.TemporaryDirectory(
        prefix="sf-oci-", dir=os.environ.get("SF_OCI_TEST_TMPDIR")
    ) as directory:
        env = dict(os.environ, TMPDIR=directory)
        try:
            subprocess.run([
                "cargo", "test", "--locked", "--manifest-path", "suifleet-edge/Cargo.toml",
                "--", *tests, "--ignored", "--test-threads=1", "--nocapture",
            ], env=env, check=True)
        finally:
            # Includes containers left by a failed assertion, scoped exclusively
            # to workspaces created in this invocation's private temporary root.
            for root in Path(directory).glob("suifleet-supervisor-*/supervised"):
                scope = hashlib.sha256(os.fsencode(root)).hexdigest()
                ids = subprocess.check_output([
                    *engine,
                    "container", "ls", "-aq", "--filter", f"label=io.suifleet.supervisor={scope}",
                ], text=True, timeout=25).split()
                if ids:
                    subprocess.run([
                        *engine,
                        "container", "rm", "--force", *ids,
                    ], check=True, timeout=25)
            # Delete only the marker created by the test so its parent owner can remove
            # the empty directory. The test UID owns it with mode 0700.
            for data in Path(directory).glob("suifleet-supervisor-oci-grants-*/oci-data/app"):
                subprocess.run([
                    *engine, "run", "--rm", "--pull", "never", "--network", "none",
                    "--read-only", "--user", "31001:31001", "--cap-drop", "ALL",
                    "--mount", f"type=bind,src={data},dst=/data,bind-recursive=disabled",
                    image, "rm", "-f", "/data/value",
                ], check=True, timeout=25)
                data.rmdir()


if __name__ == "__main__":
    main()
