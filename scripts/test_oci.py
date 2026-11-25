#!/usr/bin/env python3
"""Run the real-engine OCI supervisor regression with scoped cleanup.

Requires Linux, a local Docker or Podman engine, Rust 1.94, and
SF_OCI_TEST_IMAGE set to an already-cached immutable Alpine image. The registry-negative fixture binds
loopback; when containerized, use the host network so the engine can reach it.
No chain state, wallet, production credentials or external registry is used.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    if not os.environ.get("SF_OCI_TEST_IMAGE"):
        raise SystemExit("Set SF_OCI_TEST_IMAGE to a cached immutable Alpine image")
    selected = os.environ.get("SUIFLEET_OCI_ENGINE", "docker")
    if selected == "docker":
        engine = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
        tests = "oci_real_engine"
    elif selected in {"podman-rootful", "podman-rootless"}:
        run = "/run/podman" if selected == "podman-rootful" else "/run/suifleet-podman"
        engine = ["/usr/bin/podman-remote", "--url", f"unix://{run}/podman.sock"]
        tests = "supervisor::tests::oci_real_engine"
    else:
        raise SystemExit("Unsupported SUIFLEET_OCI_ENGINE")
    with tempfile.TemporaryDirectory(prefix="suifleet-oci-test-") as directory:
        env = dict(os.environ, TMPDIR=directory)
        try:
            subprocess.run([
                "cargo", "test", "--locked", "--manifest-path", "suifleet-edge/Cargo.toml",
                tests, "--", "--ignored", "--test-threads=1", "--nocapture",
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


if __name__ == "__main__":
    main()
