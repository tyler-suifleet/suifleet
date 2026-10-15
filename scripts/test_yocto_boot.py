#!/usr/bin/env python3
"""Boot the SF-025 QEMU test image and verify first-boot/reboot identity behavior.

Requires an aarch64 QEMU system emulator, kernel and ext4 image built using
kas/packaging-qemu.yml. Copies the disk into a temporary directory; never
writes the input image or a physical device. Guest networking is disabled.
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


def boot(qemu, kernel, disk, reboot=False, console_timeout=300):
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
    args = parser.parse_args()
    if args.console_timeout <= 0:
        parser.error("--console-timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="suifleet-packaging-boot-") as temporary:
        first, second = [Path(temporary) / name for name in ("first.ext4", "second.ext4")]
        shutil.copyfile(args.image, first)
        shutil.copyfile(args.image, second)
        address = boot(args.qemu, args.kernel, first, console_timeout=args.console_timeout)
        assert boot(args.qemu, args.kernel, first, reboot=True, console_timeout=args.console_timeout) == address
        assert boot(args.qemu, args.kernel, second, console_timeout=args.console_timeout) != address, "image cloned the private identity"
    print("PASS: actual QEMU image boots unenrolled, retries offline startup, provisions independent keys, preserves identity/state across reboot")


if __name__ == "__main__":
    main()
