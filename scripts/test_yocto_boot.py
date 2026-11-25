#!/usr/bin/env python3
"""Boot the SF-025 QEMU test image and verify first-boot/reboot identity behavior.

Requires an aarch64 QEMU system emulator, kernel and ext4 image built using
kas/packaging-qemu.yml. Copies the disk into a temporary directory; never
writes the input image or a physical device. Guest networking is disabled.

--extended (SF-309) additionally records the guest kernel/systemd versions,
rejects any systemd parse diagnostic for the SuiFleet units (an unknown
directive is silently ignored, weakening the sandbox), requires and runs the
packaged Node runtime, switches the device to the isolated signing service,
checks key ownership, activates the service by a local socket connection and
verifies harmless requests (address answer, refused signature, unknown op), and
applies the harmless SF-030 test component with the packaged test updater. It
needs suifleet-walrus-codec and suifleet-test-component in the image (both are
in the QEMU profiles). It is emulation evidence only and sends no transaction.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid


class Guest:
    def __init__(self, qemu, kernel, disk, console_timeout=300):
        self.console_timeout = console_timeout
        self.process = subprocess.Popen([
            qemu, "-machine", "virt", "-cpu", "cortex-a57", "-m", "512", "-smp", "2",
            "-kernel", str(kernel), "-append", "root=/dev/vda rw console=ttyAMA0",
            "-drive", f"file={disk},if=virtio,format=raw", "-nographic", "-nic", "none",
            "-device", "virtio-rng-pci",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.pending = b""

    def expect(self, pattern, timeout=None):
        timeout = self.console_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, self.pending)
            if match:
                self.before_match = self.pending[:match.start()]
                self.pending = self.pending[match.end():]
                return match
            if self.process.poll() is not None:
                raise RuntimeError(f"QEMU exited with {self.process.returncode}: "
                                   f"{self.pending[-4096:].decode(errors='replace')}")
            if self.selector.select(1):
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("QEMU console closed")
                self.pending += chunk
        raise TimeoutError("QEMU console deadline expired: " +
                           self.pending[-4096:].decode(errors="replace"))

    def send(self, command):
        self.process.stdin.write(command.encode() + b"\n")
        self.process.stdin.flush()

    def login(self):
        self.expect(rb"login: ")
        self.send("root")
        self.expect(rb"root@[^\r\n]+[#] ")

    def command(self, shell, timeout=None):
        token = "SF025_" + uuid.uuid4().hex
        # Encode the command so the echoed input cannot match its output marker.
        payload = f"( set -eu; {shell} ); result=$?; printf '\\n{token}:%s\\n' \"$result\""
        encoded = base64.b64encode(payload.encode()).decode()
        if len(encoded) > 768:
            # BusyBox's interactive shell may truncate lines below the tty limit.
            temporary = "/tmp/command-" + uuid.uuid4().hex
            self.command(f": > {temporary}")
            for offset in range(0, len(encoded), 256):
                self.command(f"printf '%s' {encoded[offset:offset + 256]} >> {temporary}")
            self.send(f"base64 -d < {temporary} | /bin/sh; rm -f {temporary}")
        else:
            self.send(f"echo {encoded} | base64 -d | /bin/sh")
        match = self.expect(token.encode() + rb":([0-9]+)", timeout=timeout)
        if match[1] != b"0":
            raise AssertionError(f"guest acceptance command failed ({match[1].decode()}): "
                                 f"{self.before_match[-4096:].decode(errors='replace')}")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()

    def put(self, destination, contents):
        temporary = "/tmp/transfer-" + uuid.uuid4().hex
        encoded = base64.b64encode(contents).decode()
        self.command(f": > {temporary}")
        # Stay below the serial terminal's canonical input limit.
        for offset in range(0, len(encoded), 256):
            self.command(f"echo {encoded[offset:offset + 256]} >> {temporary}")
        self.command(f"base64 -d < {temporary} > {destination}; rm {temporary}")


def offline_enrollment(guest):
    manifest = json.loads((Path(__file__).resolve().parents[1] /
                           "manifests/fixture.localnet.json").read_text())
    manifest["rpc_url"] = "http://127.0.0.1:9000"
    guest.put("/etc/suifleet/deployment.json", json.dumps(manifest).encode())
    guest.put("/etc/suifleet/config.toml.new", b'''[sui]
manifest_path = "deployment.json"
[device]
keypair_path = "/etc/suifleet/device.key"
device_object_id = "0x1"
device_cap_id = "0x2"
[apply]
command = "/usr/bin/false"
[runtime]
state_dir = "/var/lib/suifleet"
poll_seconds = 1
''')
    guest.command("chown root:suifleet /etc/suifleet/deployment.json /etc/suifleet/config.toml.new; "
                  "chmod 640 /etc/suifleet/deployment.json /etc/suifleet/config.toml.new; "
                  "mv /etc/suifleet/config.toml.new /etc/suifleet/config.toml; "
                  "attempt=0; until test $(systemctl show -p NRestarts --value suifleet.service) -gt 0; do "
                  "attempt=$((attempt + 1)); test $attempt -lt 180 || { "
                  "systemctl status suifleet.service suifleet-enrollment.path network-online.target --no-pager || :; "
                  "journalctl -u suifleet.service --no-pager; exit 1; }; sleep 1; done; "
                  "journalctl -u suifleet.service --no-pager; "
                  "test $(systemctl show -p NRestarts --value suifleet.service) -gt 0; "
                  "journalctl -u suifleet.service --no-pager | grep -qi 'currently unavailable: tcp connect error'", timeout=240)
    # Return to the unenrolled state before the next boot. No node was running,
    # so this proves offline startup/retry, not successful chain enrollment.
    guest.command("systemctl stop suifleet-enrollment.path; "
                  "rm /etc/suifleet/config.toml /etc/suifleet/deployment.json; "
                  "systemctl stop suifleet.service; systemctl reset-failed; sync")


TEST_APP = Path(__file__).resolve().parents[1] / "tests/components/suifleet-test-app/app.sh.in"


def test_component(version, run_id="sf309-qemu"):
    text = TEST_APP.read_text()
    for name, value in (("COMPONENT", "sf309-probe"), ("VERSION", version),
                        ("RUN_ID", run_id), ("FAIL", "none")):
        text = text.replace(f"@{name}@", value)
    return text.encode()


def guest_value(guest, label, shell):
    # The echoed input never contains "LABEL:", only the printed output does.
    guest.send(f"printf '%s:%s:END\\n' {label} \"$({shell})\"")
    return guest.expect(label.encode() + rb":([^\r\n]*):END")[1].decode()


# One request per run over the activation socket; prints the response line.
SIGNER_CLIENT = b'''const net = require("net");
const requests = { address: { op: "address" }, sign: { op: "sign", tx: "AA==" }, unknown: { op: "sf309-unknown" } };
const [path, name] = process.argv.slice(2);
if (!requests[name]) { console.error("unknown probe " + name); process.exit(5); }
const socket = net.createConnection(path);
let data = "";
socket.setTimeout(120000, () => { console.error("signer response timeout"); process.exit(2); });
socket.on("connect", () => socket.write(JSON.stringify(requests[name]) + "\\n"));
socket.on("data", (chunk) => {
  data += chunk;
  const end = data.indexOf("\\n");
  if (end >= 0) { process.stdout.write("RESPONSE " + data.slice(0, end) + "\\n"); socket.destroy(); process.exit(0); }
});
socket.on("error", (error) => { console.error(error.message); process.exit(3); });
socket.on("end", () => { console.error("signer closed without a response"); process.exit(4); });
'''

# Activation by connection, then verified responses. The probe runs as the
# daemon user but is not the recorded daemon main process, so signatures are
# refused (on old kernels without SO_PEERPIDFD the caller check uses the /proc
# fallback; see SF-292). No transaction bytes are signed.
SIGNER_REQUESTS = (
    "probe() { su -s /bin/sh suifleet -c \"/usr/lib/suifleet/node/bin/node /tmp/sf309-signer.js "
    "/run/suifleet-signer/sign.sock $1\"; }; "
    "out=$(probe address); echo \"$out\"; "
    "test \"$out\" = \"RESPONSE {\\\"address\\\":\\\"$(cat /etc/suifleet/device.address)\\\"}\"; "
    "systemctl is-active --quiet suifleet-signer.service; "
    "test $(systemctl show -p User --value suifleet-signer.service) = suifleet-signer; "
    "journalctl -u suifleet-signer.service --no-pager | grep -q 'Device signing service ready'; "
    "out=$(probe sign); echo \"$out\"; "
    "case \"$out\" in 'RESPONSE {\"error\":\"caller pid '*' may not request signatures '*) ;; *) exit 1;; esac; "
    "out=$(probe unknown); echo \"$out\"; "
    "test \"$out\" = 'RESPONSE {\"error\":\"unknown request\"}'; "
    "journalctl -u suifleet-signer.service --no-pager | grep -q 'Signing request refused'; "
    "rm -f /tmp/sf309-signer.js"
)


def extended_checks(guest):
    """Signer, unit-parse and harmless-update checks for an advertised runtime profile."""
    kernel = guest_value(guest, "KERNEL", "uname -r")
    systemd = guest_value(guest, "SYSTEMD", "systemctl --version | sed -n 1p")
    print(f"INFO: guest kernel {kernel}; {systemd}", flush=True)
    # systemd logs "unit:LINE: ..." for unknown keys/values it then ignores.
    # Capture the journal with its exit status checked, then require grep's
    # clean no-match status (1): a match (0) or a grep error (2) both fail.
    # The daemon's offline-retry line (checked above) proves the capture
    # includes SuiFleet unit output.
    guest.command("journal=/tmp/sf309-journal; journalctl -b --no-pager > $journal || { "
                  "echo 'journalctl failed'; exit 1; }; "
                  "grep -q 'currently unavailable: tcp connect error' $journal || { "
                  "echo \"journal capture lacks SuiFleet output ($(wc -l < $journal) lines)\"; exit 1; }; "
                  "rc=0; grep -E 'suifleet[a-z-]*\\.(service|socket|path):[0-9]+: ' $journal || rc=$?; "
                  "rm -f $journal; test $rc -eq 1 || { echo \"parse diagnostic scan status $rc\"; exit 1; }")
    print("PASS: no systemd parse diagnostics for SuiFleet units", flush=True)
    # The prebuilt archive runtime must load against this image's glibc/libstdc++.
    # Node is required: the extended profile installs the codec, which needs it.
    guest.command("node=/usr/lib/suifleet/node/bin/node; test -x $node; "
                  "test \"$($node --version)\" = v$(sed -n 's/.*\"node\": *\"\\([0-9.]*\\)\".*/\\1/p' "
                  "/usr/lib/suifleet/walrus-codec/manifest.json); "
                  "$node -e 'process.exit(require(\"fs\").existsSync(process.argv[1]) ? 0 : 1)' "
                  "/usr/lib/suifleet/walrus-codec/walrus_wasm_bg.wasm", timeout=180)
    print("PASS: packaged Node runtime matches the codec manifest release and executes", flush=True)

    # Harmless update: install v1, reject a malformed artifact, upgrade to v2.
    root = "/var/lib/suifleet/test-components"
    guest.put("/tmp/sf309-v1", test_component("1.0.0"))
    guest.put("/tmp/sf309-v2", test_component("2.0.0"))
    guest.put("/tmp/sf309-bad", b"#!/bin/sh\n# not a component\n")
    guest.command("chown suifleet:suifleet /tmp/sf309-v1 /tmp/sf309-v2 /tmp/sf309-bad; "
                  f"install -d -o suifleet -g suifleet {root}; "
                  f"su -s /bin/sh suifleet -c 'suifleet-test-updater --root {root} /tmp/sf309-v1'; "
                  f"test $(cat {root}/sf309-probe/current) = 1.0.0; "
                  f"grep -q '\"install\":\"succeeded\",\"health\":\"passed\"' {root}/sf309-probe/receipt.json; "
                  f"if su -s /bin/sh suifleet -c 'suifleet-test-updater --root {root} /tmp/sf309-bad'; then exit 1; fi; "
                  f"test $(cat {root}/sf309-probe/current) = 1.0.0; "
                  f"su -s /bin/sh suifleet -c 'suifleet-test-updater --root {root} /tmp/sf309-v2'; "
                  f"test $(cat {root}/sf309-probe/current) = 2.0.0; "
                  f"grep -q '\"previousVersion\":\"1.0.0\"' {root}/sf309-probe/receipt.json; "
                  f"test $(wc -l < {root}/sf309-probe/history.jsonl) = 2; "
                  f"test $(stat -c %U {root}/sf309-probe/current) = suifleet; "
                  f"rm -rf {root} /tmp/sf309-v1 /tmp/sf309-v2 /tmp/sf309-bad")
    print("PASS: test updater installs, refuses a malformed artifact and upgrades as the daemon user", flush=True)

    # Isolated signer: the key moves to suifleet-signer and the daemon user
    # loses read access; a local connection activates the service, which
    # answers harmless requests.
    manifest = json.loads((Path(__file__).resolve().parents[1] /
                           "manifests/fixture.localnet.json").read_text())
    manifest["rpc_url"] = "http://127.0.0.1:9000"
    guest.put("/etc/suifleet/deployment.json", json.dumps(manifest).encode())
    guest.put("/etc/suifleet/signer.toml", b'''manifest_path = "deployment.json"
device_cap_id = "0x2"
keypair_path = "/etc/suifleet/device.key"
caller_unit = "suifleet.service"
''')
    guest.put("/etc/suifleet/config.toml.new", b'''[sui]
manifest_path = "deployment.json"
[device]
device_object_id = "0x1"
device_cap_id = "0x2"
[device.signer]
kind = "service"
socket = "/run/suifleet-signer/sign.sock"
[apply]
command = "/usr/bin/false"
[runtime]
state_dir = "/var/lib/suifleet"
poll_seconds = 1
''')
    guest.put("/tmp/sf309-signer.js", SIGNER_CLIENT)
    guest.command("key_hash=$(sha256sum /etc/suifleet/device.key | cut -d' ' -f1); "
                  "chown root:suifleet-signer /etc/suifleet/signer.toml; chmod 640 /etc/suifleet/signer.toml; "
                  "chown root:suifleet /etc/suifleet/deployment.json /etc/suifleet/config.toml.new; "
                  "chmod 640 /etc/suifleet/deployment.json /etc/suifleet/config.toml.new; "
                  "systemctl stop suifleet-enrollment.path; "
                  "mv /etc/suifleet/config.toml.new /etc/suifleet/config.toml; "
                  "systemctl restart suifleet-provision.service; "
                  "test $(stat -c %U /etc/suifleet/device.key) = suifleet-signer; "
                  "test $(stat -c %a /etc/suifleet/device.key) = 600; "
                  "test $(sha256sum /etc/suifleet/device.key | cut -d' ' -f1) = $key_hash; "
                  "if su -s /bin/sh suifleet -c 'cat /etc/suifleet/device.key' >/dev/null 2>&1; then exit 1; fi; "
                  "systemctl is-active --quiet suifleet-signer.socket; "
                  "test $(stat -c %U:%G:%a /run/suifleet-signer/sign.sock) = suifleet-signer:suifleet:660; "
                  "if systemctl is-active --quiet suifleet-signer.service; then exit 1; fi; "
                  "chmod 644 /tmp/sf309-signer.js; "
                  + SIGNER_REQUESTS, timeout=240)
    print("PASS: service signer owns the unchanged key, daemon user cannot read it; a socket connection "
          "activated the signer, which answered the device address, refused a non-daemon signature and an "
          "unknown op", flush=True)
    # Return to the legacy unenrolled state for the next boot.
    guest.command("systemctl stop suifleet-signer.service suifleet.service; "
                  "rm /etc/suifleet/config.toml /etc/suifleet/deployment.json /etc/suifleet/signer.toml; "
                  "systemctl restart suifleet-provision.service; "
                  "test $(stat -c %U /etc/suifleet/device.key) = suifleet; "
                  "systemctl reset-failed; systemctl start suifleet-enrollment.path; sync")


def boot(qemu, kernel, disk, reboot=False, console_timeout=300, extended=False):
    guest = Guest(qemu, kernel, disk, console_timeout)
    try:
        guest.login()
        # Console login may precede completion of the automatic boot units.
        guest.command("attempt=0; until systemctl is-active --quiet suifleet-provision.service "
                      "&& systemctl is-active --quiet suifleet-enrollment.path; do "
                      "attempt=$((attempt + 1)); test $attempt -lt 60 || { "
                      "systemctl status suifleet-provision.service suifleet-enrollment.path --no-pager || :; "
                      "journalctl -u suifleet-provision.service --no-pager; exit 1; }; sleep 1; done")
        guest.command("systemctl is-active --quiet suifleet-provision.service; "
                      "systemctl is-active --quiet suifleet-enrollment.path; "
                      "test ! -e /etc/suifleet/config.toml; "
                      "if systemctl is-active --quiet suifleet.service; then exit 1; fi; "
                      "test $(systemctl show -p NRestarts --value suifleet.service) = 0; "
                      "test $(stat -c %a /etc/suifleet/device.key) = 600; "
                      "test $(stat -c %U /etc/suifleet/device.key) = suifleet; "
                      "test $(systemctl show -p User --value suifleet.service) = suifleet; "
                      "test $(suifleet --derive-address /etc/suifleet/device.key) = "
                      "$(cat /etc/suifleet/device.address)")
        guest.send("printf 'DEVICE:%s\\n' \"$(cat /etc/suifleet/device.address)\"")
        address = guest.expect(rb"DEVICE:(0x[0-9a-f]{64})")[1].decode()
        print("PASS: automatic unenrolled provisioning and unprivileged daemon unit", flush=True)
        if reboot:
            guest.command("test $(cat /etc/suifleet/device.address) = $(cat /var/lib/suifleet/boot-address); "
                          "test $(sha256sum /etc/suifleet/device.key | cut -d' ' -f1) = "
                          "$(cat /var/lib/suifleet/boot-key-hash); "
                          "test $(cat /var/lib/suifleet/packaging-journal) = preserved")
            print("PASS: same disk preserves key, address and state across reboot", flush=True)
        else:
            guest.command("mkdir -p /var/lib/suifleet; "
                          "cp /etc/suifleet/device.address /var/lib/suifleet/boot-address; "
                          "sha256sum /etc/suifleet/device.key | cut -d' ' -f1 > /var/lib/suifleet/boot-key-hash; "
                          "echo preserved > /var/lib/suifleet/packaging-journal")
            offline_enrollment(guest)
            print("PASS: config installation activates daemon; unavailable loopback node causes retry", flush=True)
            if extended:
                extended_checks(guest)
        # Stop normally and sync the disk before testing another boot.
        guest.command("systemctl restart suifleet-provision.service; sync")
        guest.send("poweroff")
        guest.process.wait(timeout=60)
        return address
    finally:
        guest.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--console-timeout", type=float, default=300,
                        help="Console/login/command deadline in seconds (default: 300)")
    parser.add_argument("--extended", action="store_true",
                        help="Also check unit parsing, the service signer and the harmless test updater")
    args = parser.parse_args()
    if args.console_timeout <= 0:
        parser.error("--console-timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="suifleet-packaging-boot-") as temporary:
        first, second = [Path(temporary) / name for name in ("first.ext4", "second.ext4")]
        shutil.copyfile(args.image, first)
        shutil.copyfile(args.image, second)
        address = boot(args.qemu, args.kernel, first, console_timeout=args.console_timeout,
                       extended=args.extended)
        assert boot(args.qemu, args.kernel, first, reboot=True, console_timeout=args.console_timeout) == address
        assert boot(args.qemu, args.kernel, second, console_timeout=args.console_timeout) != address, "image cloned the private identity"
    print("PASS: actual QEMU image boots unenrolled, retries offline startup, provisions independent keys, preserves identity/state across reboot")


if __name__ == "__main__":
    main()
