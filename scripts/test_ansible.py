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
fresh hosts (no change), a claim transfer interrupted after staging (plain
and Vault claims only in tmpfs, none on host persistent storage or controller
files), install + on-device identity + claim enrollment +
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
changed=0), rolling restart. It checks preserved identity and
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
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus openssh-server sudo python3 python3-apt ca-certificates adduser procps \\
 && rm -rf /var/lib/apt/lists/* \\
 && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service systemd-binfmt.service \\
 && (systemctl disable ssh.socket 2>/dev/null || true) && systemctl enable ssh.service \\
 && rm -f /etc/ssh/ssh_host_* \\
 && printf '[Unit]\\nBefore=ssh.service\\n[Service]\\nType=oneshot\\nExecStart=/usr/bin/ssh-keygen -A\\n[Install]\\nWantedBy=multi-user.target\\n' > /etc/systemd/system/ssh-hostkeys.service \\
 && systemctl enable ssh-hostkeys.service \\
 && useradd --create-home --shell /bin/bash deploy \\
 && printf 'deploy ALL=(ALL) NOPASSWD: ALL\\n' > /etc/sudoers.d/deploy && chmod 0440 /etc/sudoers.d/deploy
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
                      suifleet_units_src="/units")
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
                   "-v", f"{EXAMPLES}:/ansible:ro", "-v", f"{self.work / 'ctl'}:/work", "-v", f"{self.work / 'chain'}:/chain",
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
                           "ls -A /run/suifleet-enroll 2>/dev/null || true"),
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
        _, recap = self.ansible("check mode with staging left after the record", "site.yml", "--check", "--diff", *vault)
        for name, host in self.hosts.items():
            assert recap[name]["failed"] == 0 and recap[name]["changed"] > 0, (name, "abandoned staging not reported", recap[name])
            assert self.exec(host["container"], "test", "-e", staged[0], check=False).returncode == 0, (name, "check mode erased")
        print("PASS check mode reported claim staging abandoned after the enrollment record")
        # The credential batch is still absent: the cleanup must not need it.
        _, recap = self.ansible("retry with staging left after the record", "site.yml", *vault)
        for name, host in self.hosts.items():
            state, _ = self.host_state(name)
            self.assert_converged(name, state)
            assert recap[name]["changed"] > 0, (name, recap[name])
            assert self.exec(host["container"], "test", "-e", "/run/suifleet-enroll", check=False).returncode != 0, name
            assert not self.disk_hits(host["container"], [marker], "/run", one_filesystem=False), (name, "marker remains")
            for field in ("key_sha256", "address", "enrollment_sha256", "main_pid", "invocations"):
                assert state[field] == previous[name][field], (name, field, "changed by the staging cleanup")
        print("PASS retry erased the abandoned claim staging without the credential batch, re-enrollment or restarts")
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

        _, recap = self.ansible("check mode on fresh hosts", "site.yml", "--check", "--diff", *vault)
        for name, host in self.hosts.items():
            assert recap[name]["failed"] == 0 and recap[name]["changed"] > 0, (name, recap[name])
            assert self.exec(host["container"], "test", "-e", "/usr/bin/suifleet", check=False).returncode != 0, name
            assert self.exec(host["container"], "test", "-e", "/etc/suifleet/device.key", check=False).returncode != 0, name
        self.interrupted_claim_transfer(vault)

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

        # The claim batch is no longer needed once hosts are enrolled.
        hidden = self.work / "chain/batch-erased"
        self.batch.rename(hidden)
        _, recap = self.ansible("rerun without the claim batch", "site.yml", *vault)
        self.unchanged(recap, "rerun")
        _, recap = self.ansible("check mode after convergence", "site.yml", "--check", "--diff", *vault)
        self.unchanged(recap, "check")
        self.staging_after_record(vault, before)

        upgrade = ["-e", f"suifleet_deb_src=/artifacts/2/{self.debs['2'].name}", "-e", f"suifleet_deb_sha256={sha256(self.debs['2'])}",
                   "-e", "suifleet_binary_src=/artifacts/suifleet-2", "-e", f"suifleet_binary_sha256={sha256(self.binaries['2'])}"]
        _, recap = self.ansible("upgrade preview in check mode", "site.yml", "--check", "--diff", *vault, *upgrade)
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
