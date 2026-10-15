#!/usr/bin/env python3
"""Exercise the shipped provisioning helper using the real daemon, in a fresh container.

Run as root in a disposable container with the recipe files installed and no
existing key. No private key is printed. This checks packaging behavior; it
is not a substitute for booting the BitBake-built image.
"""
import os
from pathlib import Path
import pwd
import subprocess

assert Path("/.dockerenv").exists() and os.geteuid() == 0, "requires disposable root container"
config = Path("/etc/suifleet")
key = config / "device.key"
assert not key.exists(), "fresh container required; never replace an existing identity"
assert not (config / "config.toml").exists(), "fresh image must be unenrolled"
user = pwd.getpwnam("suifleet")
helper = "/usr/libexec/suifleet-provision"

def provision():
    # Match the service's bounded capabilities, including repeated chmod of a
    # key already owned by the unprivileged account.
    subprocess.run(["setpriv", "--bounding-set=-all,+chown,+dac_override,+fowner", helper],
                   check=True, stdout=subprocess.DEVNULL)
    return (config / "device.address").read_text().strip()

state = Path("/var/lib/suifleet")
state.mkdir(parents=True, exist_ok=True)
journal = state / "state.json"
journal.write_text('{"test":"persistent"}\n')
first = provision()
original = key.read_bytes()
assert len(first) == 66 and first.startswith("0x")
assert key.stat().st_uid == user.pw_uid and key.stat().st_gid == user.pw_gid
assert key.stat().st_mode & 0o777 == 0o600
assert (config / "device.address").stat().st_mode & 0o777 == 0o640
assert provision() == first and key.read_bytes() == original, "restart replaced identity"
assert journal.read_text() == '{"test":"persistent"}\n', "provisioning changed journal"
address = subprocess.check_output(["runuser", "-u", "suifleet", "--", "/usr/bin/suifleet",
                                   "--derive-address", str(key)], text=True).strip()
assert address == first
result = subprocess.run(["runuser", "-u", "suifleet", "--", "/bin/sh", "-c",
                         "echo unsafe > /etc/suifleet/config.toml"], capture_output=True)
assert result.returncode != 0 and not (config / "config.toml").exists()
# A corrupt existing identity is an explicit provisioning failure, never an
# excuse to regenerate a device with different on-chain authorization.
key.write_text("invalid-key\n")
assert subprocess.run([helper], capture_output=True).returncode != 0
assert key.read_text() == "invalid-key\n" and (config / "device.address").read_text().strip() == first
print("PASS: unique first-boot identity, unprivileged read, policy permissions, stable restart, corrupt-key refusal")
print(first)  # Public address, for comparison across independently fresh images.
