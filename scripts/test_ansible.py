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

Phases: refusal, check mode on fresh hosts (no change), install + on-device
identity + claim enrollment + configuration + start, rerun without the claim
batch (changed=0), check mode (changed=0), package/binary upgrade, rerun
(changed=0), rolling restart. It checks preserved identity and enrollment,
on-chain enrollment effects (device sender, claim-sponsored gas, cap owners),
claim erasure, and that no device or claim key appears in Ansible output,
controller files or host journals. Containers are not physical targets.

  python3 scripts/test_ansible.py --move-volume VOLUME [--evidence FILE]

Needs the debug daemon and chain_facts example (cargo build --locked
--manifest-path suifleet-edge/Cargo.toml --bin suifleet --example chain_facts),
dpkg-deb, Docker, a Move dependency cache volume owned by uid 1001 and network
access once to build the test images (pip/apt).
"""
import argparse
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
        binaries = {"1": packages / "suifleet-1", "2": packages / "suifleet-2"}
        shutil.copyfile(binary, binaries["1"])
        shutil.copyfile(binary, binaries["2"])
        with binaries["2"].open("ab") as handle:
            handle.write(b"\0sf304-binary-upgrade\n")
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

    def ansible(self, phase, playbook, *extra, expect_rc=0, tool="ansible-playbook"):
        command = ["docker", "run", "--rm", "--network", f"container:{self.chain}",
                   "-v", f"{EXAMPLES}:/ansible:ro", "-v", f"{self.work / 'ctl'}:/work", "-v", f"{self.work / 'chain'}:/chain",
                   "-v", f"{self.work / 'artifacts'}:/artifacts:ro", "-v", f"{UNITS}:/units:ro",
                   "-w", "/ansible", "-e", "ANSIBLE_CONFIG=/ansible/ansible.cfg", "-e", "ANSIBLE_FORCE_COLOR=0",
                   "-e", "ANSIBLE_NOCOLOR=1", self.controller, tool]
        if tool == "ansible-playbook":
            command += ["-i", "/work/inventory.yml", playbook]
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

    def unchanged(self, recap, phase):
        assert set(recap) == set(self.hosts), (phase, recap)
        for name, counts in recap.items():
            assert counts["changed"] == 0 and counts["failed"] == 0 and counts["unreachable"] == 0, (phase, name, counts)

    # Host state
    def host_state(self, name):
        container = self.hosts[name]["container"]

        def sh(script):
            return self.exec(container, "sh", "-c", script).stdout.strip()
        key = sh("cat /etc/suifleet/device.key")
        stat = sh("stat -c '%U:%G %a' /etc/suifleet/device.key")
        state = dict(
            key_sha256=hashlib.sha256(key.encode()).hexdigest(),
            key_stat=stat,
            address=sh("cat /etc/suifleet/device.address"),
            enrollment=json.loads(sh("cat /etc/suifleet/enrollment.json")),
            enrollment_sha256=hashlib.sha256(sh("cat /etc/suifleet/enrollment.json").encode()).hexdigest(),
            active=self.exec(container, "systemctl", "is-active", "suifleet.service", check=False).stdout.strip(),
            main_pid=sh("systemctl show -p MainPID --value suifleet.service"),
            binary_sha256=sh("sha256sum /usr/bin/suifleet | cut -d' ' -f1"),
            package=sh("dpkg-query -W -f='${Version}' suifleet 2>/dev/null || true"),
            claims_left=sh("find / -xdev \\( -name claim.key -o -name '*.enrollment.json.partial' \\) 2>/dev/null; "
                           "ls -A /run/suifleet-enroll 2>/dev/null || true"),
            ansible_tmp=sh("find /home/deploy/.ansible /root/.ansible -type f 2>/dev/null | head -5"),
            journal_key_hits=sh("journalctl --no-pager -o cat 2>/dev/null | grep -c " + shlex.quote(key) + " || true"),
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
        assert state["journal_key_hits"] in ("", "0"), (name, "device key in the host journal")
        if host["profile"] == "deb":
            assert state["package"], (name, "package not installed")
        else:
            assert not state["package"], (name, "systemd-binary host has the package")

    def scan_secrets(self, keys):
        secrets = [*keys, *self.claim_secrets.values()]
        assert all(len(s) == 64 for s in secrets), "unexpected key format"
        for output in self.outputs:
            for secret in secrets:
                assert secret not in output, "a key appeared in Ansible output"
        for path in (self.work / "ctl").rglob("*"):
            if path.is_file():
                data = path.read_text(errors="replace")
                for secret in keys:
                    assert secret not in data, f"a device key appeared in controller file {path.name}"
        print(f"PASS no device or claim key in {len(self.outputs)} Ansible runs, controller files or host journals")

    def main(self):
        self.packages()
        self.start()
        self.write_inventory()

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

        upgrade = ["-e", f"suifleet_deb_src=/artifacts/2/{self.debs['2'].name}", "-e", f"suifleet_deb_sha256={sha256(self.debs['2'])}",
                   "-e", "suifleet_binary_src=/artifacts/suifleet-2", "-e", f"suifleet_binary_sha256={sha256(self.binaries['2'])}"]
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
