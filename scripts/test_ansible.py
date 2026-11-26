#!/usr/bin/env python3
"""SF-304: run the Ansible examples (examples/ansible) against disposable hosts.

Everything runs in Docker on one host, in a single network namespace that has
only loopback (the chain container is started with --network none):

  - a disposable Sui localnet: publishes the current Move package, creates an
    enrollment policy, writes a claim batch with scripts/claims.py and
    registers/funds it with the PTB printed by `claims.py ptb`;
  - real systemd hosts reached over SSH as an unprivileged sudo account:
    Debian 12 and Ubuntu 24.04 with the local-file .deb profile, Debian 12 with
    the systemd-binary profile, plus a host without systemd that must be
    refused;
  - a pinned ansible-core controller (examples/ansible/requirements-controller.txt).

Phases: release/profile preflight cases with injected facts (Ubuntu 24.10 and
Debian 13 refused for deb unless explicitly allowed), refusal, check mode on
fresh hosts (no change), a control showing sudo input logging (enabled on
every host) records a become command's stdin, the inventory enables
ansible_become as a connection variable throughout, a claim transfer with
-e ansible_become=true is refused on the controller, transfers through a
sudo-wrapped interpreter are refused by the host probe both with readable
and with hidepid=2-hidden process ancestry (no staging), transfers with swap
are refused (the machine's real swap
when the role classifies it unsafe, a header-only /proc/swaps bind-mounted over
the kernel's (refused by the committed role as unverifiable), then /proc/swaps
fixtures through a test-only role copy whose swap kernel-source check passes:
zram/dm-crypt names with safe-looking attributes on a tmpfs over /sys/block,
because the names do not identify the devices, active swap while sed and other
text tools fail (real swap through the committed classifier when present, and a
fixture), and disk-backed swap), transfers
to hosts that could store a crash dump are refused (the machine's real
core_pattern/session core limit when classified unsafe, the real systemd-coredump
pattern with a literal limit argument under a session limit of 0, a core file
pattern bind-mounted over the sysctl (refused by the committed role as masked),
then core_pattern fixtures run through a test-only role copy whose kernel_pattern
check passes, for Apport, @ and @@ socket collectors, systemd-coredump with a literal
limit argument, persistent storage and a limit of 0, a same-name systemd-coredump outside its install location, core files, systemd-coredump with a nonzero limit and
systemd-coredump with Storage=none but a default/nonzero ProcessSizeMax (or with the
disabling settings outside an effective [Coredump] section, or only in a main
coredump.conf outside /etc that systemd 252's handler does not read), and
every piped handler otherwise accepted (systemd-coredump with Storage=none and
ProcessSizeMax=0 or a limit of 0, /usr/bin/false) because the containers do not
share the host's initial namespaces in which the kernel runs it, while a
test-only copy that assumes the host view accepts those; nothing is
crashed), a claim transfer interrupted after staging with no swap
and core files under a pam_limits core limit of 0 (plain and Vault claims only in tmpfs, none on host persistent
storage, in sudo I/O logs or controller files), install + on-device identity + claim enrollment +
configuration + start, rerun without the claim batch (changed=0), check mode
(changed=0), claim staging left by an enrollment interrupted after its record
was written (check mode reports it; retry without the batch erases it without
re-enrolling or restarting; rerun changed=0), package/binary upgrade preview in check mode (bytes, package
versions and service PIDs untouched), package/binary upgrade, rerun
(changed=0), an interrupted binary/config rollout (files land, restarts never
run) followed by a retry that must restart the stale services onto the new
inputs, rerun (changed=0), a configuration-only change timestamped within a
second of the daemon's start (detected; retry restarts; rerun changed=0), an
interrupted signing-socket unit-only update on the binary host (retry
recreates the socket with its new mode and restarts signer and daemon; rerun
changed=0), a unit-only update on the binary host's stopped but enabled units
interrupted after the unit files land and before the daemon-reload (retry
reloads systemd before starting them; rerun changed=0), a provisioning-unit
update on the binary host's stopped units interrupted before the
daemon-reload (retry loads the installed provisioning unit before starting
it; rerun changed=0), a signing-socket drop-in added and later removed on a
deb host before a run without a daemon-reload (each run loads it, recreates
the socket and verifies against the loaded unit files; rerun changed=0), a
socket drop-in the socket never reports, loaded by the reload a
configuration-only run makes (the run recreates the socket from the loaded
definition and verifies it; removal likewise; rerun changed=0), check mode on
configured hosts with their SuiFleet units stopped
(one with only the signing socket active, so a socket-activated signer would
show) and one journal not yet initialized, as an interrupted upgrade leaves them
(succeeds; starts nothing; identity, journal and units untouched) followed by
a real run that starts and verifies them (rerun changed=0),
rolling-restart preview in check mode (succeeds; no process or
identity changes), rolling restart. It checks preserved identity and
enrollment, on-chain enrollment effects (device sender, claim-sponsored gas,
cap owners), claim erasure, and that no device or claim key appears in
Ansible output, controller files or host journals (keys are passed to the
scanners on stdin, never as process arguments). Containers are not physical
targets.

  python3 scripts/test_ansible.py --move-volume VOLUME [--evidence FILE]

Needs the debug daemon and chain_facts example (cargo build --locked
--manifest-path suifleet-edge/Cargo.toml --bin suifleet --example chain_facts),
dpkg-deb, Docker, a Move dependency cache volume owned by uid 1001 and network
access once to build the test images (pip/apt).
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_deb  # noqa: E402
from test_deb import BASES  # noqa: E402

EXAMPLES = ROOT / "examples/ansible"
UNITS = ROOT / "suifleet-edge/meta-suifleet/recipes-edge/suifleet/files"
CONTROLLER_BASE = "python:3.12-slim"
RPC = "http://127.0.0.1:9000"
FAUCET = "http://127.0.0.1:9123/v2/gas"

TARGET_DOCKERFILE = '''ARG BASE
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus openssh-server sudo python3 python3-apt ca-certificates adduser procps systemd-coredump \\
 && rm -rf /var/lib/apt/lists/* \\
 && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service systemd-binfmt.service \\
 && (systemctl disable ssh.socket 2>/dev/null || true) && systemctl enable ssh.service \\
 && rm -f /etc/ssh/ssh_host_* \\
 && printf '[Unit]\\nBefore=ssh.service\\n[Service]\\nType=oneshot\\nExecStart=/usr/bin/ssh-keygen -A\\n[Install]\\nWantedBy=multi-user.target\\n' > /etc/systemd/system/ssh-hostkeys.service \\
 && systemctl enable ssh-hostkeys.service \\
 && useradd --create-home --shell /bin/bash deploy \\
 && printf 'deploy ALL=(ALL) NOPASSWD: ALL\\n' > /etc/sudoers.d/deploy && chmod 0440 /etc/sudoers.d/deploy \\
 && printf 'Defaults log_input, !compress_io\\n' > /etc/sudoers.d/io-log && chmod 0440 /etc/sudoers.d/io-log && visudo -c
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
'''
NOSYSTEMD_DOCKERFILE = '''ARG BASE
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server sudo python3 && rm -rf /var/lib/apt/lists/* \\
 && rm -f /etc/ssh/ssh_host_* && useradd --create-home --shell /bin/bash deploy \\
 && printf 'deploy ALL=(ALL) NOPASSWD: ALL\\n' > /etc/sudoers.d/deploy && chmod 0440 /etc/sudoers.d/deploy
CMD ["sh", "-c", "ssh-keygen -A >/dev/null && mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -p \\"$SSH_PORT\\" -o PasswordAuthentication=no -o PermitRootLogin=no"]
'''
CONTROLLER_DOCKERFILE = '''ARG BASE
FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
COPY requirements-controller.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && ansible --version
'''
HOSTS = {
    # name: (image key, ssh port, profile, serial)
    "deb-debian12": ("debian12", 2201, "deb", "SN-0001"),
    "deb-ubuntu2404": ("ubuntu2404", 2202, "deb", "SN-0002"),
    "bin-debian12": ("debian12", 2203, "systemd-binary", "SN-0003"),
}
UNSUPPORTED = ("nosystemd", 2204)
# Facts for the release/profile preflight cases.
PREFLIGHT_CASES = [
    # (name, distribution, version, major, extra vars, expected profile or refusal text)
    ("Debian 12 point release selects deb", "Debian", "12.8", "12", {}, "deb"),
    ("Ubuntu 24.04 selects deb", "Ubuntu", "24.04", "24", {}, "deb"),
    ("Ubuntu 24.10 is not a tested deb host", "Ubuntu", "24.10", "24", {}, "No supported profile"),
    ("Ubuntu 24.10 deb refused without opt-in", "Ubuntu", "24.10", "24", {"suifleet_profile": "deb"},
     "tested on Debian 12 and Ubuntu 24.04 only"),
    ("Ubuntu 24.10 deb accepted with opt-in", "Ubuntu", "24.10", "24",
     {"suifleet_profile": "deb", "suifleet_allow_untested_distribution": True}, "deb"),
    ("Debian 13 deb refused without opt-in", "Debian", "13.1", "13", {"suifleet_profile": "deb"},
     "tested on Debian 12 and Ubuntu 24.04 only"),
]
PREFLIGHT_PLAY = """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.include_role: {name: suifleet, tasks_from: preflight.yml}
    - ansible.builtin.debug:
        msg: "SELECTED={{ suifleet_selected_profile }} TESTED={{ suifleet_tested_deb }}"
"""
# The role's install, identity, enrollment and configuration tasks without
# the service and health tasks: a rollout interrupted after its files landed.
INTERRUPTED_PLAY = """- hosts: suifleet_devices
  gather_facts: true
  tasks:
    - ansible.builtin.include_role: {name: suifleet, tasks_from: preflight.yml}
    - ansible.builtin.include_role: {name: suifleet, tasks_from: install_deb.yml}
      when: suifleet_selected_profile == 'deb'
    - ansible.builtin.include_role: {name: suifleet, tasks_from: install_binary.yml}
      when: suifleet_selected_profile == 'systemd-binary'
    - ansible.builtin.include_role: {name: suifleet, tasks_from: identity.yml}
    - ansible.builtin.include_role: {name: suifleet, tasks_from: enroll.yml}
    - ansible.builtin.include_role: {name: suifleet, tasks_from: configure.yml}
"""
# Stages each host's claim exactly as enrollment does, then stops before the
# enrollment and its cleanup run: an interrupted claim transfer.
CLAIM_INTERRUPT_PLAY = """- hosts: suifleet_devices
  gather_facts: false
  tasks:
    - ansible.builtin.include_role: {name: suifleet, tasks_from: stage_claim.yml}
      vars:
        suifleet_claim_file: "/chain/batch/enrollment/{{ suifleet_serial }}/claim.key"
        suifleet_claim_owner: nobody
        suifleet_claim_group: nogroup
    - ansible.builtin.fail: {msg: interrupted after the claim transfer}
"""
# Control for the sudo input-logging check: a no_log command with a marker
# on stdin, run through become exactly as a claim was sent before the
# transfer stopped using privilege escalation. sudo must record it.
SUDO_LOG_CONTROL_PLAY = """- hosts: suifleet_devices
  gather_facts: false
  tasks:
    - ansible.builtin.command:
        argv: [sh, -c, 'cat > /dev/null']
        stdin: "{{ lookup('ansible.builtin.file', '/work/sudo-log-marker') }}"
      no_log: true
      changed_when: false
"""
# Runs the role's own swap and crash dump classifiers on every host, unescalated
# like the claim transfer, so the harness knows how the role sees the test
# machine's real configuration.
CLASSIFY_PLAY = """- hosts: suifleet_devices
  gather_facts: false
  become: false
  vars:
    ansible_become: false
  tasks:
    - ansible.builtin.command:
        argv: [sh, -c, "{{ lookup('ansible.builtin.file', policy_dir | default('/ansible/roles/suifleet/files') ~ '/' ~ item ~ '_policy.sh') }}"]
      loop: [swap, crash]
      register: policy
      changed_when: false
    - ansible.builtin.debug:
        msg: "CLASSIFY {{ inventory_hostname }} {{ item.item }} {{ item.stdout_lines | last }}"
      loop: "{{ policy.results }}"
      loop_control:
        label: "{{ item.item }}"
"""
SUDO_IO_LOGS = "/var/log/sudo-io"
# Lists files under the roots that contain any secret read from stdin. With
# --one-filesystem it stays on each root's filesystem (here: skips the tmpfs
# /run, /proc and /sys). Prints a JSON list of paths, never the secrets.
DISK_SCAN = """import json, os, sys
one = sys.argv[1] == "--one-filesystem"
roots = sys.argv[2:]
secrets = [s.encode() for s in sys.stdin.read().split()]
hits = []
for root in roots:
    device = os.lstat(root).st_dev
    for top, dirs, files in os.walk(root):
        if one:
            dirs[:] = [d for d in dirs if os.lstat(os.path.join(top, d)).st_dev == device]
        for name in files:
            path = os.path.join(top, name)
            try:
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if any(s in data for s in secrets):
                hits.append(path)
print(json.dumps(hits))
"""
# Counts how many of the secrets on stdin appear in the host journal.
JOURNAL_SCAN = ("import subprocess, sys; secrets = sys.stdin.read().split(); "
                "data = subprocess.run(['journalctl', '--no-pager', '-o', 'cat'], capture_output=True, text=True).stdout; "
                "print(sum(s in data for s in secrets))")
APPLIED_RECORD = "/var/lib/suifleet-ansible/applied.json"
RECAP = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)
# The role's self-check connects to the signing socket; a preview must never dispatch it.
SELF_CHECK_TASK = "TASK [suifleet : Check configuration, identity and journal binding as the daemon user]"
TASK_RESULT = re.compile(r"^(ok|changed|skipping|fatal|failed): \[([^\]]+)\]", re.M)


def run(*args, **kwargs):
    kwargs.setdefault("text", True)
    return subprocess.run(args, check=True, **kwargs)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── Inside the chain container ────────────────────────────────────────────
def chain_setup(out, serials):
    """Runs as the dev user in the network-none chain container."""
    import deployment_manifest as dm
    from types import SimpleNamespace

    out = Path(out)
    for _ in range(240):
        try:
            facts = dm.chain_facts(RPC)
            break
        except ValueError:
            time.sleep(0.5)
    else:
        raise SystemExit("localnet did not become ready")
    wallet = out / "wallet"
    wallet.mkdir(mode=0o700)

    def client(*args, json_output=True):
        command = ["sui", "client", "--client.config", str(wallet / "client.yaml"), *map(str, args)]
        if json_output:
            command.append("--json")
        result = subprocess.run(command, text=True, capture_output=True, timeout=300)
        if result.returncode:
            raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}")
        if not json_output:
            return result.stdout
        text = result.stdout
        return json.loads(text[min(i for i in (text.find("{"), text.find("[")) if i >= 0):])

    client("-y", "active-address", json_output=False)
    # The generated config's "local" environment is this node; selecting it
    # first keeps the CLI from contacting the default public environment.
    client("switch", "--env", "local", json_output=False)
    operator = client("active-address", json_output=False).strip().split()[-1]
    client("faucet", "--url", FAUCET, "--address", operator, json_output=False)
    for _ in range(120):
        gas = client("gas", operator)
        coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
        if coins:
            break
        time.sleep(0.5)
    contracts = Path(tempfile.mkdtemp(prefix="sf304-contracts-")) / "contracts"
    shutil.copytree(ROOT / "contracts", contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
    published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path", out / "Pub.local.toml",
                       "--gas-budget", "900000000", "--silence-warnings")
    assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]

    def created(result, suffix):
        return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]
    package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
    registry = created(published, "::device_registry::DeviceRegistry")[0]
    cap = created(published, "::package::UpgradeCap")[0]
    manifest = dm.resolve(SimpleNamespace(chain_identifier=facts["chain_identifier"], rpc=RPC, package=package, upgrade_cap=cap,
                                          registry=registry, network="localnet", publisher="http://127.0.0.1:9",
                                          aggregator="http://127.0.0.1:9"))
    (out / "deployment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    policy_tx = client("ptb", "--move-call", f"{package}::enrollment::create_policy", f"@{registry}", "10", "0",
                       "--gas-budget", "200000000")
    assert policy_tx["effects"]["status"]["status"] == "success"
    policy = created(policy_tx, "::enrollment::EnrollmentPolicy")[0]
    roster = out / "devices.csv"
    roster.write_text("serial,alias\n" + "".join(f"{s},line-{s.lower()}\n" for s in serials))
    batch = out / "batch"
    run(sys.executable, str(ROOT / "scripts/claims.py"), "generate", "--roster", str(roster), str(batch),
        "--policy", policy, "--manifest", str(out / "deployment.json"))
    printed = run(sys.executable, str(ROOT / "scripts/claims.py"), "ptb", str(batch), "--package", package,
                  "--policy", policy, capture_output=True).stdout.strip().splitlines()
    assert len(printed) == 1, printed
    words = shlex.split(printed[0])
    assert words[:3] == ["sui", "client", "ptb"], words[:3]
    claims_tx = client(*words[2:])
    assert claims_tx["effects"]["status"]["status"] == "success", claims_tx["effects"]["status"]
    claims = json.loads((batch / "claims.json").read_text())
    (out / "setup.json").write_text(json.dumps(dict(
        chain_identifier=facts["chain_identifier"], package=package, registry=registry, policy=policy, operator=operator,
        publish_digest=published["digest"], policy_digest=policy_tx["digest"], claims_digest=claims_tx["digest"],
        claims={Path(k).parent.name: v for k, v in claims.items()}), indent=2) + "\n")
    print("chain setup complete")


def chain_verify(out, records):
    """Checks each device's enrollment effects and capability owners."""
    import deployment_manifest as dm

    out = Path(out)
    setup = json.loads((out / "setup.json").read_text())
    records = json.loads(Path(records).read_text())
    evidence = {}
    for name, record in records.items():
        serial = record["serial"]
        enrolled = record["enrollment"]
        block = dm.rpc(RPC, "sui_getTransactionBlock", [enrolled["digest"], {"showInput": True, "showEffects": True}])
        assert block["effects"]["status"]["status"] == "success", block["effects"]["status"]
        sender = block["transaction"]["data"]["sender"]
        gas_owner = block["transaction"]["data"]["gasData"]["owner"]
        assert dm.object_id(sender) == dm.object_id(enrolled["device_address"]), (name, "sender")
        assert dm.object_id(gas_owner) == dm.object_id(setup["claims"][serial]), (name, "gas not paid by its claim")
        caps = dm.rpc(RPC, "sui_multiGetObjects", [[enrolled["device_cap_id"], enrolled["owner_cap_id"]], {"showOwner": True}])
        device_cap, owner_cap = (c["data"]["owner"]["AddressOwner"] for c in caps)
        assert dm.object_id(device_cap) == dm.object_id(enrolled["device_address"]), (name, "device cap owner")
        assert dm.object_id(owner_cap) == dm.object_id(setup["operator"]), (name, "owner cap owner")
        evidence[name] = dict(serial=serial, digest=enrolled["digest"], device_address=enrolled["device_address"],
                              device_object_id=enrolled["device_object_id"], gas_owner=gas_owner, status="success")
    print(json.dumps(evidence))


# ── On the Docker host ────────────────────────────────────────────────────
class Harness:
    def __init__(self, args):
        self.args = args
        self.tag = uuid.uuid4().hex[:8]
        self.work = Path(tempfile.mkdtemp(prefix="sf304-ansible-"))
        self.containers = []
        self.outputs = []
        self.evidence = dict(ticket="SF-304", phases=[], hosts={})
        # The role as committed; phases with a bind-mounted kernel.core_pattern,
        # /proc/swaps or /sys/block fixture switch to the synthetic copy (see
        # synthetic_examples).
        self.examples = EXAMPLES
        self.synthetic = self.synthetic_examples()
        # The committed crash classifier with the swap classifier's kernel-source checks
        # passing, so crash cases run with a /proc/swaps fixture (see crash_fixture).
        self.crash_committed = self.variant_examples("examples-crash-committed", {"swap_policy.sh": self.SWAP_SOURCE_STUBS})

    def variant_examples(self, name, edits, folder="files"):
        """A test-only copy of the examples with each named text in edits replaced (each must occur once)."""
        copy = self.work / name
        shutil.copytree(EXAMPLES, copy)
        for script, changes in edits.items():
            path = copy / "roles/suifleet" / folder / script
            text = path.read_text()
            for old, new in changes:
                assert text.count(old) == 1, f"{script} no longer defines {old.strip()!r} as expected"
                text = text.replace(old, new)
            path.write_text(text)
        return copy

    def synthetic_examples(self):
        """A test-only copy of the examples whose classifiers skip their kernel-source checks.

        The hosts share this machine's kernel, whose core_pattern and swap are
        global and are not changed by the test, so crash dump and swap fixtures
        are bind-mounted over /proc/sys/kernel/core_pattern, /proc/swaps and
        /sys/block inside each host. The committed role (rightly) refuses such
        masked views; this copy lets the rest of the classifiers, and the
        enrollment phases, run against the fixtures. Its only differences are the
        crash classifier's kernel_pattern and the swap classifier's kernel_view
        checks, which always pass. Results from it are synthetic
        classifier evidence, never evidence about a real host's crash or swap policy.
        """
        return self.variant_examples("examples-synthetic", {
            "crash_policy.sh": [("\nkernel_pattern() {\n", "\nkernel_pattern() { return 0; }\nreal_kernel_pattern() {\n")],
            "swap_policy.sh": self.SWAP_SOURCE_STUBS})

    SWAP_SOURCE_STUBS = [("\nkernel_view() {\n", "\nkernel_view() { return 0; }\nreal_kernel_view() {\n")]

    # Images and containers
    def image(self, name, dockerfile, base, context=None):
        tag = f"suifleet-ansible-test:{name}"
        command = ["docker", "build", "-q", "--build-arg", "BASE=" + base, "-t", tag, "-f", "-", str(context or self.work / "empty")]
        (self.work / "empty").mkdir(exist_ok=True)
        run(*command, input=dockerfile, capture_output=True)
        self.evidence.setdefault("images", {})[name] = dict(base=base, id=run(
            "docker", "image", "inspect", "--format", "{{.Id}}", tag, capture_output=True).stdout.strip())
        return tag

    def container(self, *args):
        name = run("docker", "run", "-d", *args, capture_output=True).stdout.strip()
        self.containers.append(name)
        return name

    def exec(self, container, *command, user="root", check=True, input=None):
        result = subprocess.run(["docker", "exec", "-i", "-u", user, container, *command], text=True,
                                capture_output=True, input=input)
        if check and result.returncode:
            raise AssertionError(f"{command[:3]} failed ({result.returncode}):\n{result.stdout[-3000:]}\n{result.stderr[-6000:]}")
        return result

    def cleanup(self):
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        if self.args.keep:
            print(f"kept {self.work}")
        else:
            subprocess.run(["docker", "run", "--rm", "--user", "0", "-v", f"{self.work}:/w", CONTROLLER_BASE,
                            "sh", "-c", "rm -rf /w/* /w/.[!.]*"], capture_output=True)
            shutil.rmtree(self.work, ignore_errors=True)

    # Artifacts
    def packages(self):
        version = re.search(r'^version = "([^"]+)"', (ROOT / "suifleet-edge/Cargo.toml").read_text(), re.M).group(1)
        packages = self.work / "artifacts"
        packages.mkdir()
        # Debug information only slows packaging; the code is unchanged.
        binary = packages / "suifleet-built"
        run("strip", "--strip-debug", "-o", str(binary), str(self.args.binary))
        out = {}
        for revision in ("1", "2"):
            deb = build_deb.build(binary, packages / revision, f"{version}-{revision}", "amd64", 1793606400)
            out[revision] = deb
        # A content-different binary for the systemd-binary upgrade: bytes after
        # the ELF image are ignored by the loader.
        binaries = {r: packages / f"suifleet-{r}" for r in ("1", "2", "3")}
        for revision, path in binaries.items():
            shutil.copyfile(binary, path)
            if revision != "1":
                with path.open("ab") as handle:
                    handle.write(f"\0sf304-binary-upgrade-{revision}\n".encode())
        for path in binaries.values():
            path.chmod(0o755)
        self.debs, self.binaries, self.version = out, binaries, version
        self.evidence["artifacts"] = {p.name: sha256(p) for p in [*out.values(), *binaries.values()]}

    # Hosts
    def wait_systemd(self, name):
        for _ in range(60):
            state = self.exec(name, "systemctl", "is-system-running", check=False).stdout.strip()
            if state in ("running", "degraded"):
                return
            time.sleep(1)
        raise AssertionError(f"{name}: systemd did not boot")

    def start(self):
        dev = self.args.dev_image
        chain_dir = self.work / "chain"
        chain_dir.mkdir()
        (chain_dir / "sui").mkdir()
        self.chain = self.container(
            "--name", f"sf304-chain-{self.tag}", "--network", "none", "--init", "--user", "1001:1001",
            "-e", "HOME=/home/dev", "-e", "SUI_CONFIG_DIR=/chain/sui", "-v", f"{ROOT}:/workspace:ro",
            "-v", f"{chain_dir}:/chain", "-v", f"{self.args.move_volume}:/home/dev/.move", "-w", "/workspace",
            dev, "sui", "start", "--with-faucet", "--force-regenesis")
        netns = f"container:{self.chain}"
        images = {key: self.image(f"target-{key}", TARGET_DOCKERFILE, BASES[key]) for key in ("debian12", "ubuntu2404")}
        nosystemd = self.image("nosystemd", NOSYSTEMD_DOCKERFILE, BASES["debian12"])
        controller_context = self.work / "controller"
        controller_context.mkdir()
        shutil.copyfile(EXAMPLES / "requirements-controller.txt", controller_context / "requirements-controller.txt")
        base = run("docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", CONTROLLER_BASE,
                   capture_output=True).stdout.strip()
        self.controller = self.image("controller", CONTROLLER_DOCKERFILE, base, controller_context)
        self.evidence["ansible"] = run("docker", "run", "--rm", "--network", "none", self.controller, "ansible", "--version",
                                       capture_output=True).stdout.splitlines()[0]
        ssh = self.work / "ctl"
        ssh.mkdir()
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "sf304-test", "-f", str(ssh / "id_ed25519"))
        public = (ssh / "id_ed25519.pub").read_text()
        self.hosts = {}
        known = []
        for name, (key, port, profile, serial) in HOSTS.items():
            container = self.container("--name", f"sf304-{name}-{self.tag}", "--network", netns, "--privileged",
                                       "--cgroupns=private", "--tmpfs", "/run", "--tmpfs", "/run/lock", images[key])
            self.wait_systemd(container)
            self.exec(container, "sh", "-c", f"printf 'Port {port}\\nPasswordAuthentication no\\nPermitRootLogin no\\n'"
                      " > /etc/ssh/sshd_config.d/sf304.conf && systemctl restart ssh.service")
            self.hosts[name] = dict(container=container, port=port, profile=profile, serial=serial)
        container = self.container("--name", f"sf304-nosystemd-{self.tag}", "--network", netns,
                                   "-e", f"SSH_PORT={UNSUPPORTED[1]}", nosystemd)
        self.unsupported = container
        for container, port in [*((h["container"], h["port"]) for h in self.hosts.values()), (self.unsupported, UNSUPPORTED[1])]:
            self.exec(container, "sh", "-c", "install -d -o deploy -g deploy -m 0700 /home/deploy/.ssh && "
                      "cat > /home/deploy/.ssh/authorized_keys && chown deploy:deploy /home/deploy/.ssh/authorized_keys && "
                      "chmod 0600 /home/deploy/.ssh/authorized_keys", input=public)
            for _ in range(30):
                key = self.exec(container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub", check=False)
                if key.returncode == 0:
                    break
                time.sleep(1)
            known.append(f"[127.0.0.1]:{port} " + " ".join(key.stdout.split()[:2]) + "\n")
        (ssh / "known_hosts").write_text("".join(known))
        # Chain, policy and claim batch.
        self.exec(self.chain, "python3", "/workspace/scripts/test_ansible.py", "--chain-setup", "/chain",
                  "--serials", ",".join(h["serial"] for h in self.hosts.values()), user="1001:1001")
        self.setup = json.loads((chain_dir / "setup.json").read_text())
        self.evidence["chain"] = {k: v for k, v in self.setup.items() if k != "claims"}
        self.batch = chain_dir / "batch"
        self.claim_secrets = {s: (self.batch / "enrollment" / s / "claim.key").read_text().strip()
                              for s in (h["serial"] for h in self.hosts.values())}

    def write_inventory(self):
        common = dict(ansible_host="127.0.0.1", ansible_user="deploy", ansible_ssh_private_key_file="/work/id_ed25519",
                      ansible_ssh_common_args="-o UserKnownHostsFile=/work/known_hosts -o StrictHostKeyChecking=yes",
                      suifleet_manifest_src="/chain/deployment.json", suifleet_enrollment_root="/chain/batch",
                      suifleet_deb_src="/artifacts/1/" + self.debs["1"].name, suifleet_deb_sha256=sha256(self.debs["1"]),
                      suifleet_units_src="/units",
                      # Customers often enable escalation as a connection variable,
                      # which overrides the become task keyword; the claim transfer
                      # must still run unescalated.
                      ansible_become=True)
        hosts = {}
        for name, host in self.hosts.items():
            vars_ = dict(ansible_port=host["port"], suifleet_serial=host["serial"])
            if host["profile"] == "systemd-binary":
                vars_.update(suifleet_profile="systemd-binary", suifleet_binary_src="/artifacts/suifleet-1",
                             suifleet_binary_sha256=sha256(self.binaries["1"]))
            hosts[name] = vars_
        inventory = dict(
            suifleet_devices=dict(vars=common, hosts=hosts),
            unsupported=dict(vars=common, hosts={"nosystemd": dict(ansible_port=UNSUPPORTED[1], suifleet_serial="SN-0404")}))
        (self.work / "ctl/inventory.yml").write_text(json.dumps(inventory, indent=2))

    def ansible(self, phase, playbook, *extra, expect_rc=0, tool="ansible-playbook", inventory=True):
        command = ["docker", "run", "--rm", "--network", f"container:{self.chain}",
                   "-v", f"{self.examples}:/ansible:ro", "-v", f"{self.work / 'ctl'}:/work", "-v", f"{self.work / 'chain'}:/chain",
                   "-v", f"{self.work / 'artifacts'}:/artifacts:ro", "-v", f"{UNITS}:/units:ro",
                   "-w", "/ansible", "-e", "ANSIBLE_CONFIG=/ansible/ansible.cfg", "-e", "ANSIBLE_FORCE_COLOR=0",
                   # Ansible's controller temporary files land in the scanned work directory.
                   "-e", "ANSIBLE_HOME=/work/ansible-home",
                   "-e", "ANSIBLE_NOCOLOR=1", self.controller, tool]
        if tool == "ansible-playbook":
            command += [*(["-i", "/work/inventory.yml"] if inventory else []), playbook]
        else:
            command += [playbook]
        result = subprocess.run([*command, *extra], text=True, capture_output=True)
        output = result.stdout + result.stderr
        self.outputs.append(output)
        (self.work / f"phase-{len(self.outputs):02d}.log").write_text(output)
        if result.returncode != expect_rc and not (expect_rc == "nonzero" and result.returncode):
            print(output[-12000:])
            raise AssertionError(f"{phase}: ansible exited {result.returncode}")
        if tool != "ansible-playbook":
            print(f"PASS {phase}")
            return output, {}
        recap = {m[0]: dict(ok=int(m[1]), changed=int(m[2]), unreachable=int(m[3]), failed=int(m[4]))
                 for m in RECAP.findall(output)}
        self.evidence["phases"].append(dict(phase=phase, rc=result.returncode, recap=recap))
        print(f"PASS {phase}: rc={result.returncode} recap={json.dumps(recap, sort_keys=True)}")
        return output, recap

    def preview_skipped_self_check(self, output, phase, hosts=None):
        """In check mode, the socket-connecting self-check must be skipped on every host.

        It is skipped by check mode itself, not by a service state read earlier in
        the preview, so even a signing service that was active when the preview
        read it (and could stop before the check) is never socket-activated."""
        results = {}
        for block in output.split(SELF_CHECK_TASK)[1:]:
            block = block.split("\nTASK [", 1)[0].split("\nPLAY ", 1)[0]
            for status, host in TASK_RESULT.findall(block):
                results.setdefault(host, set()).add(status)
        expected = set(hosts or self.hosts)
        assert set(results) == expected, (phase, "self-check task not reported for every host", results)
        for host, statuses in results.items():
            assert statuses == {"skipping"}, (phase, host, "the preview dispatched the self-check", statuses)
        print(f"PASS {phase}: the preview dispatched no self-check (no signing-socket connection)")

    def unchanged(self, recap, phase, hosts=None):
        assert set(recap) == set(hosts or self.hosts), (phase, recap)
        if any(c["changed"] for c in recap.values()):
            print("\n".join(line for line in self.outputs[-1].splitlines() if "will restart" in line or "changed:" in line))
        for name, counts in recap.items():
            assert counts["changed"] == 0 and counts["failed"] == 0 and counts["unreachable"] == 0, (phase, name, counts)

    # Host state
    def host_state(self, name):
        container = self.hosts[name]["container"]

        def sh(script):
            return self.exec(container, "sh", "-c", script).stdout.strip()
        key = sh("cat /etc/suifleet/device.key")
        signer_pid = sh("systemctl show -p MainPID --value suifleet-signer.service")
        stat = sh("stat -c '%U:%G %a' /etc/suifleet/device.key")
        state = dict(
            key_sha256=hashlib.sha256(key.encode()).hexdigest(),
            key_stat=stat,
            address=sh("cat /etc/suifleet/device.address"),
            enrollment=json.loads(sh("cat /etc/suifleet/enrollment.json")),
            enrollment_sha256=hashlib.sha256(sh("cat /etc/suifleet/enrollment.json").encode()).hexdigest(),
            active=self.exec(container, "systemctl", "is-active", "suifleet.service", check=False).stdout.strip(),
            main_pid=sh("systemctl show -p MainPID --value suifleet.service"),
            signer_pid=signer_pid,
            running_inputs=self.running_inputs(name),
            daemon_exe_sha256=sh(f"sha256sum /proc/{sh('systemctl show -p MainPID --value suifleet.service')}/exe 2>/dev/null"
                                 " | cut -d' ' -f1"),
            signer_exe_sha256=sh(f"sha256sum /proc/{signer_pid}/exe 2>/dev/null | cut -d' ' -f1") if signer_pid != "0" else "",
            poll_seconds=sh("sed -n 's/^poll_seconds = //p' /etc/suifleet/config.toml"),
            socket_mode=sh("stat -c '%a' /run/suifleet-signer/sign.sock 2>/dev/null || true"),
            invocations={unit: sh(f"systemctl show -p InvocationID --value {unit}") for unit in
                         ("suifleet.service", "suifleet-signer.service", "suifleet-signer.socket", "suifleet-enrollment.path")},
            applied=sh(f"cat {APPLIED_RECORD} 2>/dev/null || true"),
            binary_sha256=sh("sha256sum /usr/bin/suifleet | cut -d' ' -f1"),
            package=sh("dpkg-query -W -f='${Version}' suifleet 2>/dev/null || true"),
            claims_left=sh("find / -xdev \\( -name claim.key -o -name '*.enrollment.json.partial' \\) 2>/dev/null; "
                           "ls -A /run/suifleet-enroll 2>/dev/null || true; ls -d /dev/shm/suifleet-claim.* 2>/dev/null || true"),
            ansible_tmp=sh("find /home/deploy/.ansible /root/.ansible -type f 2>/dev/null | head -5"),
            journal_key_hits=self.journal_hits(container, [key]),
            initialized=self.exec(container, "python3", "-c", "import json; print(json.load(open('/var/lib/suifleet/state.json'))"
                                  ".get('initialized'))", check=False).stdout.strip(),
            config=sh("stat -c '%U:%G %a' /etc/suifleet/config.toml /etc/suifleet/signer.toml /etc/suifleet/deployment.json"),
        )
        return state, key

    def assert_converged(self, name, state):
        host = self.hosts[name]
        assert state["active"] == "active", (name, state["active"])
        assert state["initialized"] == "True", (name, "journal not initialized")
        assert re.fullmatch(r"0x[0-9a-f]{64}", state["address"]), (name, state["address"])
        assert state["enrollment"]["device_address"] == state["address"], (name, "enrollment for another key")
        assert state["key_stat"] == "suifleet-signer:suifleet-signer 600", (name, state["key_stat"])
        assert state["config"].split("\n") == ["root:suifleet 640", "root:suifleet-signer 640", "root:suifleet 640"], (name, state["config"])
        assert not state["claims_left"], (name, "claim material left on host", state["claims_left"])
        assert not state["ansible_tmp"], (name, "Ansible temporary files left", state["ansible_tmp"])
        assert state["journal_key_hits"] == 0, (name, "device key in the host journal")
        assert state["applied"], (name, "no applied-inputs record")
        for unit, report in state["running_inputs"].items():
            assert not report["stale"], (name, unit, "running replaced inputs", report["reason"])
            assert report["applied"] == report["digest"], (name, unit, "applied record differs from the installed inputs")
        for unit in ("suifleet.service", "suifleet-signer.socket", "suifleet-enrollment.path"):
            assert state["running_inputs"][unit]["active"], (name, unit, "not active")
        if host["profile"] == "deb":
            assert state["package"], (name, "package not installed")
        else:
            assert not state["package"], (name, "systemd-binary host has the package")

    def journal_hits(self, container, secrets):
        # The secrets travel on stdin: never in a process argument list.
        return int(self.exec(container, "python3", "-c", JOURNAL_SCAN, input="\n".join(secrets) + "\n").stdout.strip())

    def running_inputs(self, name):
        container = self.hosts[name]["container"]
        script = (EXAMPLES / "roles/suifleet/files/running_inputs.py").read_text()
        units = ["suifleet-signer.socket=", "suifleet-enrollment.path=",
                 "suifleet-signer.service=/usr/bin/suifleet,/etc/suifleet/deployment.json,/etc/suifleet/signer.toml",
                 "suifleet.service=/usr/bin/suifleet,/usr/libexec/suifleet-provision,/etc/suifleet/deployment.json,"
                 "/etc/suifleet/signer.toml,/etc/suifleet/config.toml"]
        return json.loads(self.exec(container, "python3", "-", APPLIED_RECORD, *units, input=script).stdout)

    def disk_hits(self, container, secrets, *roots, one_filesystem=True):
        # The secrets travel on stdin: never in a process argument list.
        flag = "--one-filesystem" if one_filesystem else "--all"
        return json.loads(self.exec(container, "python3", "-c", DISK_SCAN, flag, *roots,
                                    input="\n".join(secrets) + "\n").stdout)

    def controller_hits(self, secrets):
        """Files in the controller work directory (inventory, SSH, Ansible home and temp) holding a secret."""
        result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{self.work / 'ctl'}:/work:ro",
                                 self.controller, "python3", "-c", DISK_SCAN, "--all", "/work"],
                                text=True, capture_output=True, input="\n".join(secrets) + "\n", check=True)
        return json.loads(result.stdout)

    def scan_secrets(self, keys):
        secrets = [*keys, *self.claim_secrets.values()]
        assert all(len(s) == 64 for s in secrets), "unexpected key format"
        for output in self.outputs:
            for secret in secrets:
                assert secret not in output, "a key appeared in Ansible output"
        hits = self.controller_hits(secrets)
        assert not hits, ("a device or claim key in controller files", hits)
        claims = list(self.claim_secrets.values())
        for name, host in self.hosts.items():
            assert self.journal_hits(host["container"], secrets) == 0, (name, "a device or claim key in the host journal")
            hits = self.disk_hits(host["container"], claims, "/")
            assert not hits, (name, "a claim key on persistent storage", hits)
        print(f"PASS no device or claim key in {len(self.outputs)} Ansible runs, controller files or host journals;"
              " no claim on any host's persistent storage")

    def preflight_cases(self):
        (self.work / "ctl/preflight.yml").write_text(PREFLIGHT_PLAY)
        common = dict(service_mgr="systemd", kernel="6.8.0-generic", architecture="x86_64", pkg_mgr="apt")
        base = dict(suifleet_manifest_src="/chain/deployment.json", suifleet_deb_src="/artifacts/1/" + self.debs["1"].name,
                    suifleet_deb_sha256=sha256(self.debs["1"]), ansible_become=False)
        for index, (case, distribution, version, major, extra, expected) in enumerate(PREFLIGHT_CASES):
            facts = dict(common, distribution=distribution, distribution_version=version, distribution_major_version=major)
            vars_file = self.work / f"ctl/preflight-{index}.json"
            vars_file.write_text(json.dumps(dict(base, ansible_facts=facts, **extra)))
            ok = expected in ("deb", "systemd-binary")
            output, _ = self.ansible(f"preflight: {case}", "/work/preflight.yml", "-i", "localhost,",
                                     "-e", f"@/work/preflight-{index}.json", expect_rc=0 if ok else "nonzero", inventory=False)
            if ok:
                assert f"SELECTED={expected} " in output, (case, output[-2000:])
            else:
                assert expected in output and "SELECTED=" not in output, (case, output[-2000:])

    def interrupted_rollout(self, vault, upgrade, previous):
        """A binary/config rollout stops after its files land, before restarts."""
        (self.work / "ctl/interrupted.yml").write_text(INTERRUPTED_PLAY)
        rollout = [*upgrade, "-e", "suifleet_binary_src=/artifacts/suifleet-3",
                   "-e", f"suifleet_binary_sha256={sha256(self.binaries['3'])}", "-e", "suifleet_poll_seconds=11"]
        self.ansible("interrupted rollout: files only", "/work/interrupted.yml", *vault, *rollout)
        interrupted = {}
        for name, host in self.hosts.items():
            state, _ = self.host_state(name)
            assert state["poll_seconds"] == "11", (name, "configuration not installed")
            assert state["main_pid"] == previous[name]["main_pid"] != "0", (name, "the interruption restarted the daemon")
            assert state["running_inputs"]["suifleet.service"]["stale"], (name, "stale daemon not detected")
            if host["profile"] == "systemd-binary":
                assert state["binary_sha256"] == sha256(self.binaries["3"]), name
                assert state["signer_pid"] == previous[name]["signer_pid"] != "0", (name, "signer not running or restarted")
                assert state["running_inputs"]["suifleet-signer.service"]["stale"], (name, "stale signer not detected")
            interrupted[name] = state
        print("PASS interrupted rollout left services running replaced inputs and they were detected")
        _, recap = self.ansible("retry after the interrupted rollout", "site.yml", *vault, *rollout)
        after = {}
        for name, host in self.hosts.items():
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert recap[name]["changed"] > 0, (name, "retry restarted nothing")
            assert state["main_pid"] != interrupted[name]["main_pid"], (name, "daemon not restarted by the retry")
            assert state["daemon_exe_sha256"] == state["binary_sha256"], (name, "daemon runs other bytes")
            if host["profile"] == "systemd-binary":
                assert state["signer_pid"] != interrupted[name]["signer_pid"], (name, "signer not restarted by the retry")
                assert state["signer_exe_sha256"] == sha256(self.binaries["3"]), (name, "signer runs other bytes")
                assert state["daemon_exe_sha256"] == sha256(self.binaries["3"]), name
            for field in ("key_sha256", "address", "enrollment_sha256"):
                assert state[field] == previous[name][field], (name, field)
            after[name] = state
        print("PASS retry restarted the stale services onto the installed binary and configuration")
        _, recap = self.ansible("rerun after the retry", "site.yml", *vault, *rollout)
        self.unchanged(recap, "rerun after the retry")
        return after, rollout

    def sudo_log_control(self):
        """sudo records the standard input of become commands on every host, so the claim checks can see it."""
        marker = uuid.uuid4().hex + uuid.uuid4().hex  # a disposable stand-in for a claim, never a real key
        (self.work / "ctl/sudo-log-marker").write_text(marker + "\n")
        (self.work / "ctl/sudo-log-control.yml").write_text(SUDO_LOG_CONTROL_PLAY)
        self.ansible("control: sudo records a become command's stdin", "/work/sudo-log-control.yml")
        (self.work / "ctl/sudo-log-marker").unlink()
        for name, host in self.hosts.items():
            hits = self.disk_hits(host["container"], [marker], SUDO_IO_LOGS)
            assert hits and all(h.endswith("/stdin") for h in hits), (name, "sudo did not record the control stdin", hits)
        print("PASS control: sudo input logging recorded a no_log become command's stdin in persistent I/O logs on every host")

    def assert_no_claim_in_sudo_logs(self, phase):
        claims = list(self.claim_secrets.values())
        for name, host in self.hosts.items():
            hits = self.disk_hits(host["container"], claims, SUDO_IO_LOGS)
            assert not hits, (name, phase, "a claim in the sudo I/O logs", hits)
            sessions = self.exec(host["container"], "sh", "-c", f"find {SUDO_IO_LOGS} -name stdin | wc -l").stdout.strip()
            assert int(sessions) > 0, (name, "no sudo I/O sessions were logged")
        print(f"PASS {phase}: no claim in the hosts' sudo I/O logs (input logging on)")

    def refused_claim_transfer(self, phase, vault, *extra, expected):
        """The claim transfer must stop before staging or sending anything."""
        (self.work / "ctl/claim-interrupt.yml").write_text(CLAIM_INTERRUPT_PLAY)
        output, _ = self.ansible(phase, "/work/claim-interrupt.yml", *vault, *extra, expect_rc="nonzero")
        assert all(text in output for text in expected), (phase, "refused for another reason", output[-3000:])
        assert "interrupted after the claim transfer" not in output, (phase, "the claim transfer ran")
        for name, host in self.hosts.items():
            container = host["container"]
            assert self.exec(container, "test", "-e", "/run/suifleet-enroll", check=False).returncode != 0, (name, phase, "staged")
            left = self.exec(container, "sh", "-c", "ls -d /dev/shm/suifleet-claim.* 2>/dev/null | wc -l").stdout.strip()
            assert left == "0", (name, phase, "a transfer directory was created")

    SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"

    def swap_fixture(self, entries, sysfs=None):
        """Replace /proc/swaps (and optionally /sys/block) inside each host's mount namespace.

        The hosts share the test machine's kernel, so its real swap shows in
        /proc/swaps; these fixtures stand in for other swap configurations.
        Nothing is ever swapped on or off.
        """
        lines = self.SWAPS_HEADER + "".join(f"{dev}\t{kind}\t1048576\t0\t-2\n" for dev, kind in entries)
        for host in self.hosts.values():
            container = host["container"]
            self.clear_swap_fixture(container)
            self.exec(container, "sh", "-c", "umask 022 && cat > /root/sf304-swaps", input=lines)
            self.exec(container, "mount", "--bind", "/root/sf304-swaps", "/proc/swaps")
            if sysfs:
                self.exec(container, "mount", "-t", "tmpfs", "-o", "mode=0755", "sf304-sysblock", "/sys/block")
                for path, value in sysfs.items():
                    self.exec(container, "sh", "-c", f"umask 022 && mkdir -p \"$(dirname /sys/block/{path})\""
                              f" && printf '%s\\n' '{value}' > /sys/block/{path}")

    def clear_swap_fixture(self, container):
        for target in ("/sys/block", "/proc/swaps"):
            mounted = self.exec(container, "sh", "-c", f"grep -c ' {target} ' /proc/self/mounts || true").stdout.strip()
            if mounted != "0":
                self.exec(container, "umount", target)

    def classify(self, assume_host_view=False):
        """The role's swap and crash dump verdicts for every host: {host: {"swap": ..., "crash": ...}}.

        With assume_host_view, the crash classifier runs from a test-only copy whose
        host_view check passes unconditionally. The test hosts are containers, which
        the real check (rightly) refuses for every piped handler; the copy shows
        what the rest of the classifier decides when the session does share the
        host's view. The role itself never uses the copy.
        """
        (self.work / "ctl/classify.yml").write_text(CLASSIFY_PLAY)
        extra = []
        if assume_host_view:
            role_files = self.examples / "roles/suifleet/files"
            stub = self.work / "ctl/assume-host-view"
            stub.mkdir(exist_ok=True)
            crash = (role_files / "crash_policy.sh").read_text()
            assert crash.count("\nhost_view() {\n") == 1, "crash_policy.sh no longer defines host_view as expected"
            (stub / "crash_policy.sh").write_text(crash.replace("\nhost_view() {\n", "\nhost_view() { return 0; }\nreal_host_view() {\n"))
            (stub / "swap_policy.sh").write_text((role_files / "swap_policy.sh").read_text())
            extra = ["-e", "policy_dir=/work/assume-host-view"]
        output, _ = self.ansible("classify the hosts' swap and crash dump policy", "/work/classify.yml", *extra)
        verdicts = {name: {} for name in self.hosts}
        for host, kind, verdict in re.findall(r'"msg": "CLASSIFY (\S+) (swap|crash) VERDICT ([^"]*)"', output):
            verdicts[host][kind] = verdict
        assert all(set(v) == {"swap", "crash"} for v in verdicts.values()), ("missing verdicts", verdicts)
        return verdicts

    def swap_refused(self, vault):
        """Any active swap, or an unverifiable swap list, refuses the claim before it is read."""
        # The test machine's own swap (for example WSL's disk swap) with no fixture; a machine
        # without swap is classified ok and skips that case.
        previous = self.examples
        self.examples = EXAMPLES
        try:
            self.swap_refused_cases(vault)
        finally:
            self.examples = previous
            self.no_swap()

    def swap_refused_cases(self, vault):
        for host in self.hosts.values():
            self.clear_swap_fixture(host["container"])
        real = subprocess.run(["sh", "-c", "sed 1d /proc/swaps"], text=True, capture_output=True, check=True).stdout.split()
        # The committed role reads the machine's real swap through the containers' procfs.
        verdicts = {name: v["swap"] for name, v in self.classify().items()}
        assert len(set(verdicts.values())) == 1, ("hosts sharing one kernel disagree about its swap", verdicts)
        verdict = next(iter(verdicts.values()))
        if verdict == "unsafe":
            self.refused_claim_transfer("claim transfer with the machine's real swap is refused", vault,
                                        expected=["active swap that can write the claim", real[0]])
            print(f"PASS claim transfer refused with this machine's real disk swap ({real[0]} {real[1]})")
        else:
            assert verdict == "ok", ("unexpected swap verdict", verdict)
            print(f"NOTE the role accepts this machine's real swap ({' '.join(real) or 'none'}); the real-swap"
                  " refusal case did not run (fixtures below did)")
        # A header-only file bind-mounted over /proc/swaps hides the machine's real swap:
        # the committed role refuses it as unverifiable before reading it.
        self.swap_fixture([])
        assert {v["swap"] for v in self.classify().values()} == {"unverifiable"}, "masked /proc/swaps not refused"
        self.refused_claim_transfer("claim transfer with a masked /proc/swaps is refused", vault,
                                    expected=["active swap cannot be verified", "/proc/swaps is not the kernel's swap list"])
        print("PASS claim transfer refused with a header-only file bind-mounted over /proc/swaps (committed role)")
        # The remaining fixtures are synthetic classifier evidence (see synthetic_examples).
        self.examples = self.synthetic
        # /proc/swaps names an entry by the path it was opened through, which does not
        # identify the device (a disk node can be called /dev/zram0). Swap named like zram
        # and dm-crypt, with safe-looking attributes found under those names, is refused.
        self.swap_fixture([("/dev/zram0", "partition"), ("/dev/dm-0", "partition")],
                          sysfs={"zram0/backing_dev": "none", "dm-0/dm/uuid": "CRYPT-LUKS2-sf304fixture-swap"})
        assert {v["swap"] for v in self.classify().values()} == {"unsafe"}, "zram/dm-crypt names accepted"
        self.refused_claim_transfer("claim transfer with zram/dm-crypt-named swap is refused", vault,
                                    expected=["active swap that can write the claim", "/dev/zram0, /dev/dm-0"])
        print("PASS claim transfer refused with /dev/zram0 and /dev/dm-0 swap despite zram backing_dev none and a"
              " dm-crypt UUID under those names (names do not identify the devices; synthetic copy)")
        self.swap_parser_failure(vault, real_unsafe=verdict == "unsafe", real=real)
        self.examples = self.synthetic
        unsafe = [("/swapfile", "file"), ("/dev/sdb2", "partition"), ("/dev/zram1", "partition"), ("/dev/dm-1", "partition"),
                  ("/dev/zram2", "partition")]
        self.swap_fixture(unsafe, sysfs={"zram1/backing_dev": "/dev/sdb3", "dm-1/dm/uuid": "LVM-sf304fixture"})
        self.refused_claim_transfer("claim transfer with disk-backed swap is refused", vault,
                                    expected=["active swap that can write the claim",
                                              "/swapfile, /dev/sdb2, /dev/zram1, /dev/dm-1, /dev/zram2"])
        print("PASS claim transfer refused with a swap file, a disk partition, zram writeback, non-crypt dm and"
              " an unknown zram device (each listed; synthetic copy)")

    FAULT_PATH = "/usr/local/lib/sf304-faultpath"
    # Runs the swap classifier with only awk and cat working; every other text tool
    # (sed among them, as a classifier once piped the list through it) exits 1.
    SWAP_FAULT_PATH = [("\nv() { printf", f"\nPATH={FAULT_PATH}; export PATH\nv() {{ printf")]

    def swap_parser_failure(self, vault, real_unsafe, real):
        """A failed or missing text tool must never turn active swap into "VERDICT ok".

        Secret-free: the claim transfer is refused before any claim is read.
        """
        for host in self.hosts.values():
            self.exec(host["container"], "sh", "-c",
                      f"d={self.FAULT_PATH}; rm -rf \"$d\" && mkdir -m 755 \"$d\""
                      " && for t in awk cat; do ln -s \"$(command -v $t)\" \"$d/$t\"; done"
                      " && for t in sed tr grep head tail cut wc; do ln -s /bin/false \"$d/$t\"; done")
        try:
            if real_unsafe:
                # The committed classifier against the machine's real swap.
                for host in self.hosts.values():
                    self.clear_swap_fixture(host["container"])
                self.examples = self.variant_examples("examples-swap-faultpath", {"swap_policy.sh": self.SWAP_FAULT_PATH})
                assert {v["swap"] for v in self.classify().values()} == {"unsafe"}, "real swap accepted with failing tools"
                self.refused_claim_transfer("claim transfer with real swap and failing text tools is refused", vault,
                                            expected=["active swap that can write the claim", real[0]])
                print(f"PASS claim transfer refused with this machine's real swap ({real[0]}) while sed, tr, grep,"
                      " head, tail, cut and wc fail (committed classifier)")
            # A swap fixture, so the case runs on machines without swap too.
            self.examples = self.variant_examples("examples-swap-faultpath-synthetic", {
                "crash_policy.sh": [("\nkernel_pattern() {\n", "\nkernel_pattern() { return 0; }\nreal_kernel_pattern() {\n")],
                "swap_policy.sh": self.SWAP_SOURCE_STUBS + self.SWAP_FAULT_PATH})
            self.swap_fixture([("/dev/sdb2", "partition")])
            assert {v["swap"] for v in self.classify().values()} == {"unsafe"}, "fixture swap accepted with failing tools"
            self.refused_claim_transfer("claim transfer with fixture swap and failing text tools is refused", vault,
                                        expected=["active swap that can write the claim", "/dev/sdb2"])
            print("PASS claim transfer refused with active swap while sed, tr, grep, head, tail, cut and wc fail"
                  " (synthetic copy)")
        finally:
            for host in self.hosts.values():
                self.exec(host["container"], "rm", "-rf", self.FAULT_PATH)

    SYSTEMD_COREDUMP = "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h"
    # A literal limit where systemd-coredump reads the crashing process's (%c),
    # as some hosts configure it (this WSL machine's own pattern does).
    SYSTEMD_COREDUMP_LITERAL = "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t 9223372036854775808 %h %d"
    DECOY_COREDUMP = "/usr/local/bin/systemd-coredump"

    def crash_fixture(self, pattern, limit_zero=False, coredump_conf=None, vendor_main_conf=None, decoy=None, synthetic=True):
        """Replace kernel.core_pattern inside each host's mount namespace and set the SSH session's core limit.

        The hosts share the test machine's kernel, whose core_pattern is
        global, so no test process is ever crashed: a real crash would reach
        the machine's own handler. Only the role's policy decision is tested.
        With pattern None the machine's real pattern stays visible. With
        synthetic (the default) later runs use the synthetic copy of the role,
        which does not refuse the bind-mounted pattern; otherwise the crash
        classifier as committed (from a copy whose swap classifier trusts the
        hosts' /proc/swaps fixture, because the machine's real swap or a masked
        /proc/swaps would refuse every transfer before the crash check).
        """
        self.examples = self.synthetic if synthetic else self.crash_committed
        for host in self.hosts.values():
            container = host["container"]
            self.clear_crash_fixture(container)
            if pattern is not None:
                self.exec(container, "sh", "-c", "umask 022 && cat > /root/sf304-core-pattern", input=pattern + "\n")
                self.exec(container, "mount", "--bind", "/root/sf304-core-pattern", "/proc/sys/kernel/core_pattern")
            if coredump_conf is not None:
                self.exec(container, "sh", "-c", "mkdir -p /etc/systemd/coredump.conf.d"
                          " && umask 022 && cat > /etc/systemd/coredump.conf.d/sf304.conf",
                          input="".join(f"{line}\n" for line in coredump_conf))
            if vendor_main_conf is not None:
                # systemd-coredump's package installs /etc/systemd/coredump.conf; set it
                # aside so cat-config falls back to the /usr/local/lib main file.
                self.exec(container, "sh", "-c", "if [ -e /etc/systemd/coredump.conf ]; then"
                          " mv /etc/systemd/coredump.conf /root/sf304-coredump.conf; fi")
                self.exec(container, "sh", "-c", "mkdir -p /usr/local/lib/systemd"
                          " && umask 022 && cat > /usr/local/lib/systemd/coredump.conf",
                          input="".join(f"{line}\n" for line in vendor_main_conf))
            if decoy is not None:
                # An inert executable sharing a supported handler's name outside its
                # install location; no crash is induced, so it never runs.
                self.exec(container, "sh", "-c", f"mkdir -p {shlex.quote(str(Path(decoy).parent))}"
                          f" && umask 022 && printf '#!/bin/sh\\nexit 0\\n' > {shlex.quote(decoy)}"
                          f" && chmod 0755 {shlex.quote(decoy)}")
            if limit_zero:
                # pam_limits sets each new SSH session's limits (each controller run
                # connects afresh), starting from PID 1's, so sshd's own limit is not enough.
                self.exec(container, "sh", "-c", "printf 'deploy soft core 0\\ndeploy hard core 0\\n'"
                          " > /etc/security/limits.d/sf304-core.conf")

    def clear_crash_fixture(self, container):
        mounted = self.exec(container, "sh", "-c", "grep -c ' /proc/sys/kernel/core_pattern ' /proc/self/mounts || true").stdout.strip()
        if mounted != "0":
            self.exec(container, "umount", "/proc/sys/kernel/core_pattern")
        self.exec(container, "rm", "-f", "/etc/systemd/coredump.conf.d/sf304.conf", "/etc/security/limits.d/sf304-core.conf",
                  "/usr/local/lib/systemd/coredump.conf", self.DECOY_COREDUMP)
        self.exec(container, "sh", "-c", "if [ -e /root/sf304-coredump.conf ]; then"
                  " mv /root/sf304-coredump.conf /etc/systemd/coredump.conf; fi")

    def crash_refused(self, vault):
        """A host that could store a crash dump of a claim-handling process refuses the claim before it is read."""
        for host in self.hosts.values():
            self.clear_crash_fixture(host["container"])
        self.examples = self.crash_committed
        real = Path("/proc/sys/kernel/core_pattern").read_text().strip()
        limits = {name: self.exec(h["container"], "sh", "-c", "grep '^Max core' /proc/$(systemctl show -p MainPID --value"
                                  " ssh.service)/limits").stdout.split()[4] for name, h in self.hosts.items()}
        verdicts = {name: v["crash"] for name, v in self.classify().items()}
        if all(v.startswith("unsafe") for v in verdicts.values()):
            self.refused_claim_transfer("claim transfer with the machine's real crash dump policy is refused", vault,
                                        expected=["could store a crash dump containing the claim"])
            print(f"PASS claim transfer refused with this machine's real crash dump policy ({real!r}, sshd core limit"
                  f" {sorted(set(limits.values()))})")
        else:
            print(f"NOTE the role accepts this machine's real crash dump policy on some host ({verdicts});"
                  " the real-policy refusal case did not run (fixtures below did)")
        # The machine's real systemd-coredump pattern with a literal limit argument: a
        # session limit of 0 must not count, because the handler never receives it.
        words = real.lstrip("|").split()
        if (real.startswith("|") and words[0] in ("/usr/lib/systemd/systemd-coredump", "/lib/systemd/systemd-coredump")
                and words[1:7] != ["%P", "%u", "%g", "%s", "%t", "%c"]):
            self.crash_fixture(None, limit_zero=True, synthetic=False)
            self.refused_claim_transfer("claim transfer with the real systemd-coredump pattern and a session limit of 0", vault,
                                        expected=["could store a crash dump containing the claim", "which the handler does not receive"])
            print(f"PASS claim transfer refused with this machine's real systemd-coredump pattern ({real!r}) under a session"
                  " limit of 0: its limit argument is not %c")
        else:
            print(f"NOTE this machine's real core_pattern ({real!r}) is not systemd-coredump with a literal limit argument;"
                  " that real-pattern case did not run (the fixture below did)")
        # A bind mount over the sysctl masks the machine-wide pattern: the committed role
        # refuses it before classifying, even though it shows an otherwise safe file pattern.
        self.crash_fixture("core", limit_zero=True, synthetic=False)
        self.refused_claim_transfer("claim transfer with a masked kernel.core_pattern is refused", vault,
                                    expected=["could store a crash dump containing the claim",
                                              "/proc/sys/kernel/core_pattern is not the kernel's sysctl"])
        print("PASS claim transfer refused with a file bind-mounted over /proc/sys/kernel/core_pattern (committed role)")
        # The test containers' init and sshd inherit Docker's unlimited core limit, so every
        # case below without limit_zero has a nonzero session limit.
        assert all(lim == "unlimited" for lim in limits.values()), ("fixtures assume an unlimited sshd core limit", limits)
        cases = [
            ("a piped handler that ignores the limit (apport)", "|/usr/share/apport/apport -p%p -s%s -c%c -d%d -P%P -u%u -g%g -- %E",
             {}, "the crash handler /usr/share/apport/apport is not a supported handler at a verified location"),
            # A handler is identified by its full path: a collector that merely shares
            # systemd-coredump's name may keep dumps whatever coredump.conf says.
            ("a same-name systemd-coredump outside its install location", f"|{self.DECOY_COREDUMP} %P %u %g %s %t %c %h",
             {"decoy": self.DECOY_COREDUMP, "coredump_conf": ["[Coredump]", "Storage=none", "ProcessSizeMax=0"]},
             f"the crash handler {self.DECOY_COREDUMP} is not a supported handler at a verified location"),
            ("core files with a nonzero limit", "core", {}, "core dumps are written to files (core)"),
            ("systemd-coredump storing dumps with a nonzero limit", self.SYSTEMD_COREDUMP, {},
             "systemd-coredump may write core dumps to disk (Storage=external, ProcessSizeMax=default;"),
            # Storage=none alone still lets systemd-coredump write the dump to disk while processing it.
            ("systemd-coredump with Storage=none and the default ProcessSizeMax", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "Storage=none"]},
             "systemd-coredump may write core dumps to disk (Storage=none, ProcessSizeMax=default;"),
            ("systemd-coredump with Storage=none and ProcessSizeMax=1G", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "Storage=none", "ProcessSizeMax=1G"]},
             "systemd-coredump may write core dumps to disk (Storage=none, ProcessSizeMax=1G;"),
            # systemd reads these keys only in [Coredump]; a disabling pair in another section changes nothing.
            ("systemd-coredump with the disabling settings outside [Coredump]", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "Storage=external", "ProcessSizeMax=1G",
                                "[Other]", "Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump may write core dumps to disk (Storage=external, ProcessSizeMax=1G;"),
            # Each file starts outside any section, so a drop-in without a header is ignored.
            ("systemd-coredump with the disabling settings in a drop-in without a section header", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump may write core dumps to disk (Storage=external, ProcessSizeMax=default;"),
            # A comment that looks like cat-config's "# /path" file header must not end the
            # [Coredump] section: the assignments after it still count and re-enable dumps.
            ("systemd-coredump with a path-like comment before settings that re-enable dumps", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "Storage=none", "ProcessSizeMax=0",
                                "# /etc/systemd/coredump.conf.d/zz-sf304.conf", "Storage=external", "ProcessSizeMax=1G"]},
             "systemd-coredump may write core dumps to disk (Storage=external, ProcessSizeMax=1G;"),
            # systemd strips a UTF-8 byte order mark before a section header, so a BOM-bearing
            # drop-in can carry effective [Coredump] settings; syntax the check does not reproduce
            # exactly is refused rather than guessed.
            ("systemd-coredump configuration with a UTF-8 byte order mark", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["\ufeff[Coredump]", "Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump's configuration uses syntax this check does not support"),
            # systemd stops reading the file at an invalid section header, so the disabling
            # settings after it never apply.
            ("systemd-coredump configuration with a malformed section header", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "[Bad", "Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump's configuration uses syntax this check does not support"),
            # With /etc/systemd/coredump.conf absent, cat-config lists a main file from
            # /usr/local/lib (or /run, /usr/lib), but systemd 252's handler (Debian 12) reads
            # only /etc/systemd/coredump.conf besides the drop-ins, so these settings
            # may not apply; such a file is refused rather than trusted.
            ("systemd-coredump with the disabling settings only in a /usr/local/lib main file", self.SYSTEMD_COREDUMP,
             {"vendor_main_conf": ["[Coredump]", "Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump's main configuration file is not /etc/systemd/coredump.conf"),
            ("systemd-coredump configuration with a line continuation", self.SYSTEMD_COREDUMP,
             {"coredump_conf": ["[Coredump]", "Storage=external \\", "Storage=none", "ProcessSizeMax=0"]},
             "systemd-coredump's configuration uses line continuations"),
            # systemd-coredump reads the crashing process's limit from its sixth argument;
            # with a literal there, a session limit of 0 proves nothing and persistent
            # storage stays enabled.
            ("systemd-coredump with a literal limit argument, persistent storage and a session limit of 0",
             self.SYSTEMD_COREDUMP_LITERAL, {"limit_zero": True, "coredump_conf": ["[Coredump]", "Storage=external"]},
             "which the handler does not receive (its arguments start '%P %u %g %s %t 9223372036854775808'"),
            # Linux 6.17+ socket collectors receive the dump with an unlimited size.
            ("a socket core collector (@)", "@/run/sf304-core.sock", {"limit_zero": True},
             "core dumps are sent to a socket collector (@/run/sf304-core.sock)"),
            ("a socket core collector (@@)", "@@/run/sf304-core.sock", {"limit_zero": True},
             "core dumps are sent to a socket collector (@@/run/sf304-core.sock)"),
        ] + [
            # The kernel runs a piped handler, which reads its own configuration, in the
            # host's initial namespaces. The test hosts are containers: what they show at
            # the handler's path and in coredump.conf need not be what the kernel runs, so
            # every piped configuration that would otherwise be accepted is refused.
            (f"{label} seen only from a container's filesystem view", pattern, options,
             "is not in the host's initial PID and user namespaces")
            for label, pattern, options in self.PIPED_ACCEPTED_IN_HOST_VIEW
        ]
        try:
            for label, pattern, options, reason in cases:
                self.crash_fixture(pattern, **options)
                self.refused_claim_transfer(f"claim transfer with {label} is refused", vault,
                                            expected=["could store a crash dump containing the claim", reason])
                print(f"PASS claim transfer refused with {label}")
            # The view check is the only reason those piped configurations were refused.
            for label, pattern, options in self.PIPED_ACCEPTED_IN_HOST_VIEW:
                self.crash_fixture(pattern, **options)
                verdicts = {name: v["crash"] for name, v in self.classify(assume_host_view=True).items()}
                assert all(v == "ok" for v in verdicts.values()), (label, "not accepted when the host view is assumed", verdicts)
                print(f"PASS classifier accepts {label} once the session's host view is assumed (test-only copy)")
        finally:
            self.safe_crash_policy()

    # Piped handlers the classifier accepts when the session shares the host's view.
    PIPED_ACCEPTED_IN_HOST_VIEW = [
        ("systemd-coredump with Storage=none and ProcessSizeMax=0", SYSTEMD_COREDUMP,
         {"coredump_conf": ["[Coredump]", "Storage=none", "ProcessSizeMax=0"]}),
        ("systemd-coredump with a session core limit of 0", SYSTEMD_COREDUMP, {"limit_zero": True}),
        ("a handler that discards dumps (/usr/bin/false)", "|/usr/bin/false", {}),
    ]

    def safe_crash_policy(self):
        """Core files under a session core limit of 0: the kernel writes nothing.

        A file pattern is written in the crashing process's own namespace, so a
        container's view is the right one. Piped handlers are refused in the test
        containers (see crash_refused). The pattern is a bind-mounted fixture, so
        the synthetic copy of the role runs from here on (see synthetic_examples).
        """
        self.crash_fixture("core", limit_zero=True)

    def accepted_claim_policy(self):
        """No swap, and core files with a session core limit of 0 (the kernel writes nothing), are accepted.

        Both are fixtures, so the synthetic copy of the role runs (see synthetic_examples).
        """
        self.no_swap()
        self.crash_fixture("core", limit_zero=True)
        verdicts = self.classify()
        assert all(v == {"swap": "ok", "crash": "ok"} for v in verdicts.values()), ("fixtures not accepted", verdicts)

    def no_swap(self):
        """The rest of the run behaves as a host with swap off."""
        self.swap_fixture([])

    def escalated_claim_transfer_refused(self, vault):
        """Escalation the role's task variables cannot remove, and unverifiable transport, refuse the claim."""
        # Extra vars outrank task variables: the controller sees escalation still on.
        self.refused_claim_transfer("claim transfer with -e ansible_become=true is refused", vault,
                                    "-e", "ansible_become=true", expected=["Privilege escalation is enabled"])
        print("PASS claim transfer refused on the controller when escalation is forced through extra vars")
        # Escalation without Ansible's become: the interpreter itself runs through sudo
        # (ansible-core takes the interpreter as one path, so a wrapper script).
        wrapper = "/usr/local/bin/sf304-sudo-python"
        sudo_python = ["-e", json.dumps({"ansible_python_interpreter": wrapper})]
        try:
            for host in self.hosts.values():
                self.exec(host["container"], "sh", "-c", f"umask 022 && printf '#!/bin/sh\\nexec sudo -n -u nobody"
                          f" /usr/bin/python3 \"$@\"\\n' > {wrapper} && chmod 0755 {wrapper}")
            self.refused_claim_transfer("claim transfer through a sudo interpreter is refused", vault, *sudo_python,
                                        expected=["privilege escalation or another user's process"])
            print("PASS claim transfer refused when the module's process chain changes user (sudo interpreter)")
            # The same escalation with /proc hidepid=2: the chain is unreadable, which is not evidence of safety.
            for host in self.hosts.values():
                self.exec(host["container"], "mount", "-o", "remount,hidepid=2", "/proc")
            self.refused_claim_transfer("claim transfer with an unreadable process chain is refused", vault, *sudo_python,
                                        expected=["Could not verify", "was not fully readable"])
            print("PASS claim transfer refused when the module's process chain is not fully readable (hidepid=2)")
        finally:
            for host in self.hosts.values():
                self.exec(host["container"], "mount", "-o", "remount,hidepid=0", "/proc")
                self.exec(host["container"], "rm", "-f", wrapper)

    def interrupted_claim_transfer(self, vault):
        """Enrollment stops right after the claim transfer, before its cleanup."""
        (self.work / "ctl/claim-interrupt.yml").write_text(CLAIM_INTERRUPT_PLAY)
        output, _ = self.ansible("interrupted claim transfer (plain and Vault claims)", "/work/claim-interrupt.yml", *vault,
                                 expect_rc="nonzero")
        assert "interrupted after the claim transfer" in output, "the play stopped elsewhere"
        for name, host in self.hosts.items():
            container, secret = host["container"], self.claim_secrets[host["serial"]]
            # The scanner finds the claim where it was staged, so its other results are meaningful.
            staged = self.disk_hits(container, [secret], "/run/suifleet-enroll", one_filesystem=False)
            assert staged == ["/run/suifleet-enroll/claim.key"], (name, "claim not staged", staged)
            assert self.exec(container, "stat", "-f", "-c", "%T", "/run/suifleet-enroll").stdout.strip() == "tmpfs", name
            meta = self.exec(container, "stat", "-c", "%U:%G %a", "/run/suifleet-enroll/claim.key").stdout.strip()
            assert meta == "nobody:nogroup 600", (name, meta)
            hits = self.disk_hits(container, [secret], "/")
            assert not hits, (name, "the claim reached persistent storage", hits)
            assert not self.disk_hits(container, [secret], "/dev/shm", one_filesystem=False), (name, "transfer copy left")
        hits = self.controller_hits(list(self.claim_secrets.values()))
        assert not hits, ("a claim in controller files", hits)
        print("PASS interrupted claim transfer left the claim only in tmpfs staging: none on host persistent storage"
              " (including Ansible remote temp) or in controller files (including Vault decryption)")

    def staging_after_record(self, vault, previous):
        """Enrollment stopped after the record's rename, before its cleanup: the staged claim is left behind."""
        marker = uuid.uuid4().hex + uuid.uuid4().hex  # a disposable stand-in for the consumed claim
        staged = ["/run/suifleet-enroll/claim.key", "/run/suifleet-enroll/enroll.toml", "/run/suifleet-enroll/deployment.json"]
        for name, host in self.hosts.items():
            container = host["container"]
            # The marker travels on stdin, like the real claim.
            self.exec(container, "sh", "-c", "umask 077 && mkdir -p /run/suifleet-enroll && cat > /run/suifleet-enroll/claim.key"
                      " && cp /etc/suifleet/deployment.json /run/suifleet-enroll/deployment.json"
                      " && : > /run/suifleet-enroll/enroll.toml", input=marker + "\n")
            assert sorted(self.disk_hits(container, [marker], "/run/suifleet-enroll", one_filesystem=False)) == staged[:1], name
            # A run killed during the claim transfer leaves the SSH account's copy;
            # another account's directory with the same name pattern is not the role's.
            self.exec(container, "sh", "-c", "umask 077 && mkdir /dev/shm/suifleet-claim.AbandonedX"
                      " && cat > /dev/shm/suifleet-claim.AbandonedX/claim.key", user="deploy", input=marker + "\n")
            self.exec(container, "mkdir", "/dev/shm/suifleet-claim.OtherOwner", user="nobody")
        _, recap = self.ansible("check mode with staging left after the record", "site.yml", "--check", "--diff", *vault)
        for name, host in self.hosts.items():
            assert recap[name]["failed"] == 0 and recap[name]["changed"] > 0, (name, "abandoned staging not reported", recap[name])
            assert self.exec(host["container"], "test", "-e", staged[0], check=False).returncode == 0, (name, "check mode erased")
            assert self.exec(host["container"], "test", "-e", "/dev/shm/suifleet-claim.AbandonedX", check=False).returncode == 0, \
                (name, "check mode erased the transfer copy")
        print("PASS check mode reported claim staging abandoned after the enrollment record")
        # The credential batch is still absent: the cleanup must not need it.
        _, recap = self.ansible("retry with staging left after the record", "site.yml", *vault)
        for name, host in self.hosts.items():
            container = host["container"]
            assert not self.disk_hits(container, [marker], "/dev/shm", one_filesystem=False), (name, "transfer copy remains")
            assert self.exec(container, "test", "-d", "/dev/shm/suifleet-claim.OtherOwner", check=False).returncode == 0, \
                (name, "erased another account's directory")
            self.exec(container, "rmdir", "/dev/shm/suifleet-claim.OtherOwner")
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert recap[name]["changed"] > 0, (name, recap[name])
            assert self.exec(host["container"], "test", "-e", "/run/suifleet-enroll", check=False).returncode != 0, name
            assert not self.disk_hits(host["container"], [marker], "/run", one_filesystem=False), (name, "marker remains")
            for field in ("key_sha256", "address", "enrollment_sha256", "main_pid", "invocations"):
                assert state[field] == previous[name][field], (name, field, "changed by the staging cleanup")
        print("PASS retry erased the abandoned claim staging and transfer copy without the credential batch,"
              " re-enrollment or restarts; another account's directory was kept")
        _, recap = self.ansible("rerun after the staging cleanup", "site.yml", *vault)
        self.unchanged(recap, "rerun after the staging cleanup")

    def rapid_config_rollout(self, vault, rollout, previous):
        """A configuration-only change lands within a second of the daemon's start and is never applied."""
        rollout = [*rollout, "-e", "suifleet_poll_seconds=12"]
        self.ansible("config-only rollout: files only", "/work/interrupted.yml", *vault, *rollout)
        for name, host in self.hosts.items():
            container = host["container"]
            stamp = self.exec(container, "systemctl", "show", "--timestamp=us+utc", "-p", "ExecMainStartTimestamp",
                              "--value", "suifleet.service").stdout.strip()
            start = datetime.datetime.strptime(stamp.split(" ", 1)[1], "%Y-%m-%d %H:%M:%S.%f UTC")
            start = start.replace(tzinfo=datetime.timezone.utc).timestamp()
            # Indistinguishable by timestamp from a file the daemon read at startup.
            self.exec(container, "touch", "-d", f"@{start + 0.5:.6f}", "/etc/suifleet/config.toml")
            state, _ = self.host_state(name)
            assert state["poll_seconds"] == "12", (name, "configuration not installed")
            assert state["main_pid"] == previous[name]["main_pid"], (name, "the interruption restarted the daemon")
            report = state["running_inputs"]["suifleet.service"]
            assert report["stale"] and report["reason"] == "its inputs changed since they were applied", (name, report)
        print("PASS a configuration change made within a second of the daemon's start was detected")
        _, recap = self.ansible("retry after the config-only rollout", "site.yml", *vault, *rollout)
        after = {}
        for name in self.hosts:
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert recap[name]["changed"] > 0, (name, "retry restarted nothing")
            assert state["invocations"]["suifleet.service"] != previous[name]["invocations"]["suifleet.service"], name
            assert state["main_pid"] != previous[name]["main_pid"], (name, "daemon not restarted onto the configuration")
            after[name] = state
        print("PASS retry restarted the daemon onto the configuration")
        _, recap = self.ansible("rerun after the config-only retry", "site.yml", *vault, *rollout)
        self.unchanged(recap, "rerun after the config-only retry")
        return after, rollout

    def socket_unit_update(self, vault, rollout, previous):
        """Only the signing socket unit changes; the interrupted rollout reloads systemd but restarts nothing."""
        name = next(n for n, h in self.hosts.items() if h["profile"] == "systemd-binary")
        units = self.work / "artifacts/units-2"
        shutil.copytree(UNITS, units)
        socket = units / "suifleet-signer.socket"
        text = socket.read_text()
        assert "SocketMode=0660\n" in text
        # Still lets the daemon's group connect (connecting needs write permission only).
        socket.write_text(text.replace("SocketMode=0660\n", "SocketMode=0620\n"))
        rollout = [*rollout, "-e", "suifleet_units_src=/artifacts/units-2"]
        assert previous[name]["socket_mode"] == "660", previous[name]["socket_mode"]
        self.ansible("socket unit update: files only", "/work/interrupted.yml", "--limit", name, *vault, *rollout)
        state, _ = self.host_state(name)
        assert state["socket_mode"] == "660", (name, "the interruption recreated the socket")
        assert state["invocations"] == previous[name]["invocations"], (name, "the interruption restarted a unit")
        report = state["running_inputs"]["suifleet-signer.socket"]
        assert report["stale"] and not report["reload_needed"], (name, report)
        print("PASS interrupted socket unit update left the old socket active and it was detected")
        _, recap = self.ansible("retry after the socket unit update", "site.yml", "--limit", name, *vault, *rollout)
        state, _ = self.host_state(name)
        self.assert_converged(name, state)
        assert state["socket_mode"] == "620", (name, "socket not recreated with the installed unit", state["socket_mode"])
        for unit in ("suifleet-signer.socket", "suifleet-signer.service", "suifleet.service"):
            assert state["invocations"][unit] != previous[name]["invocations"][unit], (name, unit, "not restarted")
        assert state["invocations"]["suifleet-enrollment.path"] == previous[name]["invocations"]["suifleet-enrollment.path"]
        for field in ("key_sha256", "address", "enrollment_sha256"):
            assert state[field] == previous[name][field], (name, field)
        print("PASS retry recreated the signing socket, then restarted the signer and the daemon")
        _, recap = self.ansible("rerun after the socket retry", "site.yml", "--limit", name, *vault, *rollout)
        self.unchanged(recap, "rerun after the socket retry", hosts=[name])
        previous = dict(previous, **{name: state})
        return previous, rollout

    RELOAD_TASK = ("- name: Reload systemd after unit changes\n  ansible.builtin.systemd_service:\n"
                   "    daemon_reload: true\n  when: suifleet_units_copy is changed\n")
    RELOAD_INTERRUPT = ("- name: Interrupted before the daemon-reload (test only)\n"
                        "  ansible.builtin.fail: {msg: interrupted before the daemon-reload}\n"
                        "  when: suifleet_units_copy is changed\n")

    def pending_reload_on_stopped_units(self, vault, rollout, previous):
        """Stopped, enabled units whose new unit files were installed but never loaded.

        A run stops after installing the unit files and before its daemon-reload
        while the units are stopped. On the retry the copies are unchanged and
        no unit is active (so none is stale), but systemd still holds the old
        definitions: the role must reload before it starts them."""
        name = next(n for n, h in self.hosts.items() if h["profile"] == "systemd-binary")
        container = self.hosts[name]["container"]
        all_units = ["suifleet-enrollment.path", "suifleet.service", "suifleet-signer.service", "suifleet-signer.socket"]
        assert previous[name]["socket_mode"] == "620", previous[name]["socket_mode"]
        self.exec(container, "systemctl", "stop", *all_units)
        for unit in ("suifleet-signer.socket", "suifleet-enrollment.path"):
            enabled = self.exec(container, "systemctl", "is-enabled", unit, check=False).stdout.strip()
            assert enabled == "enabled", (name, unit, "not already enabled", enabled)
        # Back to the committed units (SocketMode=0660), with the reload replaced by a failure.
        rollout = [*rollout, "-e", "suifleet_units_src=/units"]
        committed = self.examples
        self.examples = self.variant_examples("examples-reload-interrupt", {
            "install_binary.yml": [(self.RELOAD_TASK, self.RELOAD_INTERRUPT)]}, folder="tasks")
        try:
            output, _ = self.ansible("unit update on stopped units: interrupted before daemon-reload", "/work/interrupted.yml",
                                     "--limit", name, *vault, *rollout, expect_rc="nonzero")
        finally:
            self.examples = committed
        assert "interrupted before the daemon-reload" in output, (name, "not interrupted at the daemon-reload")
        state, _ = self.host_state(name)
        report = state["running_inputs"]
        for unit in all_units:
            assert not report[unit]["active"] and not report[unit]["stale"], (name, unit, report[unit])
        assert report["suifleet-signer.socket"]["reload_needed"], (name, "socket unit change not pending", report)
        print("PASS stopped units with installed but unloaded unit files report a pending reload without being stale")
        _, recap = self.ansible("retry with a pending reload on stopped units", "site.yml", "--limit", name, *vault, *rollout)
        state, _ = self.host_state(name)
        self.assert_converged(name, state)
        assert recap[name]["changed"] > 0, (name, recap[name])
        assert not any(r["reload_needed"] for r in state["running_inputs"].values()), (name, state["running_inputs"])
        assert state["socket_mode"] == "660", (name, "socket started from the cached unit", state["socket_mode"])
        for field in ("key_sha256", "address", "enrollment_sha256"):
            assert state[field] == previous[name][field], (name, field)
        print("PASS retry reloaded systemd before starting the stopped units; the socket uses the installed unit")
        _, recap = self.ansible("rerun after the pending-reload retry", "site.yml", "--limit", name, *vault, *rollout)
        self.unchanged(recap, "rerun after the pending-reload retry", hosts=[name])
        return dict(previous, **{name: state}), rollout

    PROVISION_MARKER = "/run/sf304-provision-unit"

    def provision_unit_pending_reload(self, vault, rollout, previous):
        """A changed provisioning unit installed but never loaded while the units are stopped.

        The retry's copy is unchanged, so the identity task must load the
        pending definition before it starts the oneshot. The test-only unit
        writes a marker from a privileged ExecStartPost, which exists only if
        the oneshot started from the installed definition."""
        name = next(n for n, h in self.hosts.items() if h["profile"] == "systemd-binary")
        container = self.hosts[name]["container"]
        units = self.work / "artifacts/units-3"
        shutil.copytree(UNITS, units)
        unit = units / "suifleet-provision.service"
        text = unit.read_text()
        assert "ExecStart=/usr/libexec/suifleet-provision\n" in text
        unit.write_text(text.replace("ExecStart=/usr/libexec/suifleet-provision\n",
                                     "ExecStart=/usr/libexec/suifleet-provision\n"
                                     f"ExecStartPost=+/bin/sh -c 'echo installed > {self.PROVISION_MARKER}'\n"))
        # Stopping the oneshot also stops the services that require it.
        self.exec(container, "systemctl", "stop", "suifleet-provision.service", "suifleet-enrollment.path",
                  "suifleet.service", "suifleet-signer.service", "suifleet-signer.socket")
        self.exec(container, "rm", "-f", self.PROVISION_MARKER)
        rollout = [*rollout, "-e", "suifleet_units_src=/artifacts/units-3"]
        committed = self.examples
        self.examples = self.variant_examples("examples-provision-reload-interrupt", {
            "install_binary.yml": [(self.RELOAD_TASK, self.RELOAD_INTERRUPT)]}, folder="tasks")
        try:
            output, _ = self.ansible("provisioning unit update on stopped units: interrupted before daemon-reload",
                                     "/work/interrupted.yml", "--limit", name, *vault, *rollout, expect_rc="nonzero")
        finally:
            self.examples = committed
        assert "interrupted before the daemon-reload" in output, (name, "not interrupted at the daemon-reload")

        def show(prop):
            return self.exec(container, "systemctl", "show", "-p", prop, "--value", "suifleet-provision.service").stdout.strip()
        assert show("NeedDaemonReload") == "yes", (name, "provisioning unit change not pending")
        assert show("ActiveState") != "active", (name, "provisioning unit not stopped")
        print("PASS a changed provisioning unit was installed but not loaded while the oneshot was stopped")
        _, recap = self.ansible("retry with a pending provisioning-unit reload", "site.yml", "--limit", name, *vault, *rollout)
        state, _ = self.host_state(name)
        self.assert_converged(name, state)
        assert recap[name]["changed"] > 0, (name, recap[name])
        marker = self.exec(container, "cat", self.PROVISION_MARKER, check=False).stdout.strip()
        assert marker == "installed", (name, "the oneshot started from the cached provisioning unit")
        assert show("NeedDaemonReload") == "no" and show("ActiveState") == "active", (name, "provisioning unit")
        for field in ("key_sha256", "address", "enrollment_sha256"):
            assert state[field] == previous[name][field], (name, field)
        print("PASS retry loaded the installed provisioning unit before starting the oneshot; identity preserved")
        _, recap = self.ansible("rerun after the provisioning-unit retry", "site.yml", "--limit", name, *vault, *rollout)
        self.unchanged(recap, "rerun after the provisioning-unit retry", hosts=[name])
        return dict(previous, **{name: state}), rollout

    def socket_drop_in_before_run(self, vault, rollout, previous):
        """A drop-in added, then removed, before runs without a daemon-reload.

        The socket's unit file paths change on the reload alone; verification
        must compare against the loaded paths, record them, and recreate the
        socket with the drop-in's settings. systemd (252 and 255) does not report
        a newly added drop-in as needing a reload, so the addition comes with a
        reinstalled (touched, unchanged) fragment, as a package or unit update
        leaves it; a removed drop-in is reported on its own."""
        name = next(n for n, h in self.hosts.items() if h["profile"] == "deb")
        container = self.hosts[name]["container"]
        drop_in = "/etc/systemd/system/suifleet-signer.socket.d"
        assert previous[name]["socket_mode"] == "660", (name, previous[name]["socket_mode"])
        for phase, script, mode in (
                ("socket drop-in added", f"umask 022 && mkdir -p {drop_in} && printf '[Socket]\\nSocketMode=0620\\n' > {drop_in}/sf304.conf"
                 " && touch \"$(systemctl show -p FragmentPath --value suifleet-signer.socket)\"", "620"),
                ("socket drop-in removed", f"rm -r {drop_in}", "660")):
            self.exec(container, "sh", "-c", script)
            report = self.running_inputs(name)["suifleet-signer.socket"]
            assert report["active"] and report["reload_needed"] and report["stale"], (name, phase, report)
            _, recap = self.ansible(f"run after the {phase} without a reload", "site.yml", "--limit", name, *vault, *rollout)
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert state["socket_mode"] == mode, (name, phase, "socket not recreated with the loaded unit", state["socket_mode"])
            for unit in ("suifleet-signer.socket", "suifleet-signer.service", "suifleet.service"):
                assert state["invocations"][unit] != previous[name]["invocations"][unit], (name, phase, unit, "not restarted")
            for field in ("key_sha256", "address", "enrollment_sha256"):
                assert state[field] == previous[name][field], (name, field)
            print(f"PASS run after the {phase} loaded it, recreated the socket (mode {mode}) and verified the loaded unit files")
            _, recap = self.ansible(f"rerun after the {phase}", "site.yml", "--limit", name, *vault, *rollout)
            self.unchanged(recap, f"rerun after the {phase}", hosts=[name])
            previous = dict(previous, **{name: state})
        return previous

    def drop_in_loaded_by_config_reload(self, vault, rollout, previous):
        """A socket drop-in systemd never reports, loaded by a reload the role makes for a config change.

        The drop-in is added without touching the fragment, so the socket reports
        no pending reload and matches its applied record before the run. The run
        changes only the daemon configuration, which makes the role reload
        systemd; that reload loads the drop-in, so the role must recreate the
        socket from the sample taken after the reload and verify against it.
        The drop-in is then removed in a second run that restores the earlier
        configuration."""
        name = next(n for n, h in self.hosts.items() if h["profile"] == "deb")
        container = self.hosts[name]["container"]
        drop_in = "/etc/systemd/system/suifleet-signer.socket.d"
        assert previous[name]["socket_mode"] == "660", (name, previous[name]["socket_mode"])
        self.exec(container, "sh", "-c", f"umask 022 && mkdir -p {drop_in}"
                  f" && printf '[Socket]\\nSocketMode=0620\\n' > {drop_in}/sf304.conf")
        report = self.running_inputs(name)["suifleet-signer.socket"]
        assert report["active"] and not report["reload_needed"] and not report["stale"], (name, report)
        for phase, extra, mode in (("config change with an unreported socket drop-in",
                                    ["-e", "suifleet_poll_seconds=13"], "620"),
                                   ("config restored with the socket drop-in removed", [], "660")):
            if not extra:
                self.exec(container, "rm", "-r", drop_in)
            _, recap = self.ansible(f"run after the {phase}", "site.yml", "--limit", name, *vault, *rollout, *extra)
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert state["poll_seconds"] == ("13" if extra else "12"), (name, phase, state["poll_seconds"])
            assert state["socket_mode"] == mode, (name, phase, "socket not recreated with the loaded unit", state["socket_mode"])
            for unit in ("suifleet-signer.socket", "suifleet-signer.service", "suifleet.service"):
                assert state["invocations"][unit] != previous[name]["invocations"][unit], (name, phase, unit, "not restarted")
            for field in ("key_sha256", "address", "enrollment_sha256"):
                assert state[field] == previous[name][field], (name, field)
            print(f"PASS run after the {phase} recreated the socket (mode {mode}) from the definition its reload loaded")
            _, recap = self.ansible(f"rerun after the {phase}", "site.yml", "--limit", name, *vault, *rollout, *extra)
            self.unchanged(recap, f"rerun after the {phase}", hosts=[name])
            previous = dict(previous, **{name: state})
        return previous

    def stopped_host_preview(self, vault, rollout, previous):
        """Check mode on configured hosts whose units are stopped, one with its journal not yet initialized.

        This is the state an interrupted upgrade leaves (the package stopped the
        units, then the run ended). On a second host only the signing socket stays
        active, so a preview that connected to it would start the signing service.
        The preview must succeed, start nothing and leave identity and journal
        untouched; the real run then starts and verifies."""
        all_units = ["suifleet-enrollment.path", "suifleet.service", "suifleet-signer.service", "suifleet-signer.socket"]
        pending, socket_only = list(self.hosts)[:2]
        units = {name: all_units[:3] if name == socket_only else all_units for name in self.hosts}
        stopped = {}
        for name, host in self.hosts.items():
            container = host["container"]
            self.exec(container, "systemctl", "stop", *units[name])
            if name == pending:
                self.exec(container, "mv", "/var/lib/suifleet/state.json", "/var/lib/suifleet/state.json.sf304-pending")
            state, _ = self.host_state(name)
            assert state["active"] != "active" and state["main_pid"] == "0" and state["signer_pid"] == "0", (name, "units not stopped")
            stopped[name] = state
        output, recap = self.ansible("check mode on configured hosts with stopped services", "site.yml", "--check", "--diff",
                                     *vault, *rollout)
        self.preview_skipped_self_check(output, "check mode on configured hosts with stopped services")
        for name, host in self.hosts.items():
            assert recap[name]["failed"] == 0 and recap[name]["unreachable"] == 0 and recap[name]["changed"] > 0, (name, recap[name])
            state, _ = self.host_state(name)
            for field in ("active", "main_pid", "signer_pid", "invocations", "key_sha256", "address", "enrollment_sha256",
                          "binary_sha256", "package", "applied", "initialized", "socket_mode"):
                assert state[field] == stopped[name][field], (name, field, "changed by the stopped-host preview")
            for unit in units[name]:
                active = self.exec(host["container"], "systemctl", "is-active", unit, check=False).stdout.strip()
                assert active != "active", (name, unit, "started by the preview")
            if name == socket_only:
                active = self.exec(host["container"], "systemctl", "is-active", "suifleet-signer.socket", check=False).stdout.strip()
                assert active == "active", (name, "the signing socket was stopped by the preview")
            journal = self.exec(host["container"], "test", "-e", "/var/lib/suifleet/state.json", check=False).returncode == 0
            assert journal == (name != pending), (name, "the preview created or removed the journal")
        print("PASS check mode on stopped configured hosts (one journal pending, one with only the signing socket"
              " active) succeeded and started nothing")
        self.exec(self.hosts[pending]["container"], "mv", "/var/lib/suifleet/state.json.sf304-pending",
                  "/var/lib/suifleet/state.json")
        _, recap = self.ansible("start the stopped hosts", "site.yml", *vault, *rollout)
        after = {}
        for name in self.hosts:
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert recap[name]["changed"] > 0, (name, "nothing started")
            assert state["main_pid"] != "0" and state["main_pid"] != previous[name]["main_pid"], (name, "daemon not started")
            for field in ("key_sha256", "address", "enrollment_sha256"):
                assert state[field] == previous[name][field], (name, field)
            after[name] = state
        print("PASS the real run started and verified the stopped hosts with identity preserved")
        _, recap = self.ansible("rerun after starting the stopped hosts", "site.yml", *vault, *rollout)
        self.unchanged(recap, "rerun after starting the stopped hosts")
        return after

    def main(self):
        self.packages()
        self.start()
        self.write_inventory()
        self.preflight_cases()

        output, _ = self.ansible("refuse a host without systemd", "site.yml", "-e", "suifleet_hosts=unsupported",
                                 expect_rc="nonzero")
        assert "not systemd" in output, "missing the systemd refusal"
        assert self.exec(self.unsupported, "test", "-e", "/usr/bin/suifleet", check=False).returncode != 0

        # Ubuntu's claim is Ansible Vault-encrypted on the controller.
        (self.work / "ctl/vault-pass").write_text(uuid.uuid4().hex + "\n")
        self.ansible("vault-encrypt one claim", "encrypt", "/chain/batch/enrollment/SN-0002/claim.key",
                     "--vault-password-file", "/work/vault-pass", tool="ansible-vault")
        vault = ["--vault-password-file", "/work/vault-pass"]
        # The test machine's own swap and crash handler would refuse every enrollment;
        # see swap_refused and crash_refused.
        self.no_swap()
        self.safe_crash_policy()

        _, recap = self.ansible("check mode on fresh hosts", "site.yml", "--check", "--diff", *vault)
        for name, host in self.hosts.items():
            assert recap[name]["failed"] == 0 and recap[name]["changed"] > 0, (name, recap[name])
            assert self.exec(host["container"], "test", "-e", "/usr/bin/suifleet", check=False).returncode != 0, name
            assert self.exec(host["container"], "test", "-e", "/etc/suifleet/device.key", check=False).returncode != 0, name
        self.sudo_log_control()
        self.escalated_claim_transfer_refused(vault)
        self.swap_refused(vault)
        self.crash_refused(vault)
        self.accepted_claim_policy()
        self.interrupted_claim_transfer(vault)
        self.no_swap()
        self.safe_crash_policy()
        verdicts = self.classify()
        assert all(v == {"swap": "ok", "crash": "ok"} for v in verdicts.values()), ("enrollment fixtures not accepted", verdicts)
        self.assert_no_claim_in_sudo_logs("interrupted claim transfer")

        output, recap = self.ansible("install, provision, enroll, configure and start", "site.yml", *vault)
        batches = output.count("PLAY [SuiFleet device client]")
        assert batches == 3, f"expected three rollout batches, saw {batches}"
        before, keys = {}, []
        for name in self.hosts:
            state, key = self.host_state(name)
            self.assert_converged(name, state)
            before[name] = state
            keys.append(key)
        records = {n: dict(serial=self.hosts[n]["serial"], enrollment=before[n]["enrollment"]) for n in self.hosts}
        (self.work / "chain/records.json").write_text(json.dumps(records))
        verified = self.exec(self.chain, "python3", "/workspace/scripts/test_ansible.py", "--chain-verify", "/chain",
                             "--records", "/chain/records.json", user="1001:1001").stdout.strip().splitlines()[-1]
        self.evidence["enrollments"] = json.loads(verified)
        print("PASS on-chain enrollments: device-signed, claim-sponsored, caps owned by device and operator")
        self.assert_no_claim_in_sudo_logs("enrollment")

        # The claim batch is no longer needed once hosts are enrolled.
        hidden = self.work / "chain/batch-erased"
        self.batch.rename(hidden)
        _, recap = self.ansible("rerun without the claim batch", "site.yml", *vault)
        self.unchanged(recap, "rerun")
        signers = {name: self.host_state(name)[0]["invocations"]["suifleet-signer.service"] for name in self.hosts}
        assert all(before[name]["signer_pid"] != "0" for name in self.hosts), ("signers not active before the preview", before)
        output, recap = self.ansible("check mode after convergence", "site.yml", "--check", "--diff", *vault)
        self.unchanged(recap, "check")
        # Every signing service was active when the preview read it.
        self.preview_skipped_self_check(output, "check mode after convergence")
        for name in self.hosts:
            assert self.host_state(name)[0]["invocations"]["suifleet-signer.service"] == signers[name], (name, "signer restarted")
        self.staging_after_record(vault, before)

        upgrade = ["-e", f"suifleet_deb_src=/artifacts/2/{self.debs['2'].name}", "-e", f"suifleet_deb_sha256={sha256(self.debs['2'])}",
                   "-e", "suifleet_binary_src=/artifacts/suifleet-2", "-e", f"suifleet_binary_sha256={sha256(self.binaries['2'])}"]
        output, recap = self.ansible("upgrade preview in check mode", "site.yml", "--check", "--diff", *vault, *upgrade)
        self.preview_skipped_self_check(output, "upgrade preview in check mode")
        for name in self.hosts:
            assert recap[name]["failed"] == 0 and recap[name]["changed"] > 0, (name, recap[name])
            state, _ = self.host_state(name)
            for field in ("binary_sha256", "package", "main_pid", "signer_pid", "key_sha256", "enrollment_sha256"):
                assert state[field] == before[name][field], (name, field, "changed by the check-mode preview")
        print("PASS upgrade preview changed no bytes, package version or service process")
        _, recap = self.ansible("upgrade package and binary", "site.yml", *vault, *upgrade)
        after = {}
        for name, host in self.hosts.items():
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            for field in ("key_sha256", "address", "enrollment_sha256"):
                assert state[field] == before[name][field], (name, field, "changed by the upgrade")
            assert state["main_pid"] != before[name]["main_pid"], (name, "daemon not restarted after upgrade")
            if host["profile"] == "deb":
                assert state["package"] == f"{self.version}-2", (name, state["package"])
            else:
                assert state["binary_sha256"] == sha256(self.binaries["2"]), name
            after[name] = state
        print("PASS upgrade preserved key, address and enrollment; daemons restarted on the new code")
        _, recap = self.ansible("rerun after upgrade", "site.yml", *vault, *upgrade)
        self.unchanged(recap, "rerun after upgrade")
        after, rollout = self.interrupted_rollout(vault, upgrade, after)
        after, rollout = self.rapid_config_rollout(vault, rollout, after)
        after, rollout = self.socket_unit_update(vault, rollout, after)
        after, rollout = self.pending_reload_on_stopped_units(vault, rollout, after)
        after, rollout = self.provision_unit_pending_reload(vault, rollout, after)
        after = self.socket_drop_in_before_run(vault, rollout, after)
        after = self.drop_in_loaded_by_config_reload(vault, rollout, after)
        after = self.stopped_host_preview(vault, rollout, after)

        _, recap = self.ansible("rolling restart preview in check mode", "restart.yml", "--check")
        for name in self.hosts:
            # The restart task reports the pending restart; the health checks are skipped.
            assert recap[name]["failed"] == 0 and recap[name]["unreachable"] == 0 and recap[name]["changed"] > 0, (name, recap[name])
            state, _ = self.host_state(name)
            for field in ("main_pid", "signer_pid", "key_sha256", "address", "enrollment_sha256"):
                assert state[field] == after[name][field], (name, field, "changed by the restart preview")
        print("PASS rolling restart preview restarted nothing and preserved identity")
        self.ansible("rolling restart", "restart.yml")
        for name in self.hosts:
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert state["main_pid"] != after[name]["main_pid"], (name, "not restarted")
            assert state["key_sha256"] == before[name]["key_sha256"], name
            self.evidence["hosts"][name] = dict(profile=self.hosts[name]["profile"], serial=self.hosts[name]["serial"],
                                                address=state["address"], package=state["package"],
                                                device_object_id=state["enrollment"]["device_object_id"])
        print("PASS rolling restart preserved identity")
        self.scan_secrets(keys)
        self.evidence["result"] = "pass"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=ROOT / "suifleet-edge/target/debug/suifleet")
    parser.add_argument("--dev-image", default="suifleet-dev:latest")
    parser.add_argument("--move-volume", help="Docker volume with a warm Move dependency cache (uid 1001)")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the private work directory")
    parser.add_argument("--chain-setup", help=argparse.SUPPRESS)
    parser.add_argument("--chain-verify", help=argparse.SUPPRESS)
    parser.add_argument("--serials", help=argparse.SUPPRESS)
    parser.add_argument("--records", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.chain_setup:
        chain_setup(args.chain_setup, args.serials.split(","))
        return
    if args.chain_verify:
        chain_verify(args.chain_verify, args.records)
        return
    if not args.move_volume:
        parser.error("--move-volume is required")
    harness = Harness(args)
    try:
        harness.main()
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(harness.evidence, indent=2) + "\n")
        harness.cleanup()


if __name__ == "__main__":
    main()
