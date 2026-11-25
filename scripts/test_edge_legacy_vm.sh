#!/usr/bin/env bash
# Run the existing daemon CLI on a real Linux 5.15 guest, without a guest NIC.
set -euo pipefail

if [[ $# != 2 ]]; then
    echo "usage: $0 STATIC_MUSL_DAEMON LINUX_5_15_BZIMAGE" >&2
    exit 2
fi
daemon=$(realpath "$1")
kernel=$(realpath "$2")
repo=$(cd "$(dirname "$0")/.." && pwd)
python3 "$repo/scripts/edge_platform.py" linux-amd64-musl-5.15 --require-check legacy-vm-smoke

# A dynamic binary must not accidentally use the BusyBox userspace's loader.
elf=$(readelf -lW "$daemon")
if ! grep -q 'LOAD' <<<"$elf" || grep -q 'INTERP' <<<"$elf"; then
    echo "legacy guest requires a static Linux daemon ELF" >&2
    exit 2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/root/bin" "$work/root/proc" "$work/root/state"
install -m 0755 "$(command -v busybox)" "$work/root/bin/busybox"
install -m 0755 "$daemon" "$work/root/bin/suifleet"
strip --strip-debug "$work/root/bin/suifleet"
ln -s busybox "$work/root/bin/sh"
cp "$repo/manifests/fixture.localnet.json" "$work/root/deployment.json"
cat >"$work/root/config.toml" <<'TOML'
[sui]
manifest_path = "/deployment.json"
[device]
keypair_path = "/state/device.key"
device_object_id = "0x1"
device_cap_id = "0x2"
[runtime]
state_dir = "/state"
[apply]
command = "true"
TOML
cat >"$work/root/init" <<'INIT'
#!/bin/sh
set -eu
export PATH=/bin
umask 077
busybox mount -t proc proc /proc
test "$$" = 1
test "$(busybox uname -m)" = x86_64
case "$(busybox uname -r)" in
    5.15.*) ;;
    *) echo "unexpected legacy guest kernel" >&2; exit 1 ;;
esac
busybox uname -a
address=$(suifleet --provision /state/device.key)
test "${#address}" = 66
test "$(busybox stat -c %a /state/device.key)" = 600
test "$address" = "$(suifleet --derive-address /state/device.key)"
test "$address" = "$(suifleet --provision /state/device.key)"
suifleet --self-check /config.toml
printf '%s\n' 'invalid journal' >/state/state.json
if suifleet --self-check /config.toml >/state/check.log 2>&1; then
    echo "daemon accepted an unreadable journal" >&2
    exit 1
fi
busybox grep -q 'not readable by this daemon' /state/check.log
echo 'SUIFLEET_LEGACY_VM_PASS'
busybox poweroff -f
INIT
chmod 0755 "$work/root/init"
(cd "$work/root" && find . -print0 | cpio --quiet --null -o -H newc --owner 0:0) >"$work/initramfs"
# Provide the virtual CPU's RNG in this offline guest so key generation can
# initialize the kernel random pool even without external device activity.
timeout 180 qemu-system-x86_64 -accel tcg -cpu max -m 512 -nodefaults -no-reboot \
    -display none -serial stdio -nic none -kernel "$kernel" \
    -initrd "$work/initramfs" -append 'console=ttyS0 rdinit=/init random.trust_cpu=on quiet panic=1' |
    tee "$work/console"
if ! tr -d '\r' <"$work/console" | grep -Fx 'SUIFLEET_LEGACY_VM_PASS'; then
    echo "Linux 5.15 guest exited without completing the daemon smoke test" >&2
    exit 1
fi
