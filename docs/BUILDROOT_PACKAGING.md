# Buildroot edge client

The [external tree](../suifleet-edge/packaging/buildroot) packages the Rust edge
client, CA certificates, and the Python launcher for the
[portable signer](KEY_ISOLATION.md#portable-linux-signer). It creates separate
unprivileged daemon and signer accounts. BusyBox and SysV init use
`/etc/init.d/S95suifleet`. For a different init system, run
`/usr/sbin/suifleet-portable-signer` in the foreground as root and supervise
the pair as one service.

## Build

Use Buildroot 2026.08.2 with the host tools from its manual. Verify the release
archive before extraction.

```sh
printf '%s  %s\n' bc9497c8e3a734c277e541e2f7fcf9a6275dc92f75bd2f753b05981c54be8705 \
  buildroot-2026.08.2.tar.xz | sha256sum -c -
make -C buildroot-2026.08.2 BR2_EXTERNAL=/path/to/suifleet/suifleet-edge/packaging/buildroot \
  O=/absolute/path/to/output-glibc suifleet_x86_64_glibc_defconfig
make -C /absolute/path/to/output-glibc
```

Use `suifleet_x86_64_musl_defconfig` and a second output directory for musl.
Both profiles use Bootlin's pinned 2025.08-1 stable toolchain and produce
`images/rootfs.cpio` for use with a board kernel or a VM kernel with initramfs,
procfs, devtmpfs, tmpfs and Unix sockets enabled. Supply the kernel and boot
arguments from your board configuration. For your own image, enable
`BR2_PACKAGE_SUIFLEET` in the Buildroot configuration with the tree attached.
Buildroot vendors the pinned source revision's `Cargo.lock` at download time,
then builds with `--locked --offline`. Run `make legal-info` for the source
and license archive, including the vendored Rust sources. Inspect individual
crate notices in that archive when preparing redistribution materials.

The basic package uses rustls with the system CA store and the file-backed
portable signer. The Python launcher uses the standard library and requires
neither OpenSSL nor the optional Python compression modules. Add any
decompressor, interpreter, libc or shared library required by an individual
application artifact to the customer image. The package builds without the
optional PKCS#11 and systemd Cargo features. Supervised lifecycle commands have
the daemon's own [containment requirements](KEY_ISOLATION.md) and should be
validated separately on the target init setup.

## Provision after boot

Keep `/etc/suifleet` and `/var/lib/suifleet` on persistent storage. Use a
root-owned, traversable `/etc/suifleet` with individual file permissions.
The init hook leaves an unconfigured device idle. Prepare settings and keys
on the target as root.

```sh
umask 077
install -d -m 0755 /etc/suifleet
suifleet --provision /etc/suifleet/device.key
```

Use the root-only per-device bundle and `suifleet --enroll /secure/enroll.toml`
from the [enrollment flow](ENROLLMENT.md). For bootstrap, the enrollment file
references the root-only device key. Keep its claim key and enrollment files
outside reusable images. After successful enrollment, install the public
deployment manifest as root-owned mode 0644. Move ownership of `device.key`
to `suifleet-signer:suifleet-signer` and keep mode 0600. Install
`signer.toml` as `root:suifleet-signer` mode 0640 with `portable = true`,
the returned DeviceCap ID, and the key and manifest paths. Install
`config.toml` last as `root:suifleet` mode 0640, with the enrolled Device and
DeviceCap IDs and `[device.signer]` set to `kind = "service"` and
`socket = "/run/suifleet-portable/signer.sock"`. Keep daemon self-update disabled.

Mount procfs for the service's PID namespace and keep `fs.suid_dumpable=0`.
Start with `/etc/init.d/S95suifleet start` and use the same script to stop or
restart. The foreground launcher stops both children when either exits. If the
site requires automatic restart after a failure, supervise the foreground pair
with the board's service manager. Platform scripts own firmware installation,
reboot, boot confirmation and firmware rollback.

## Disposable VM test

Use a network-isolated Sui 1.81.1 node with a faucet and a warm, private
Move dependency cache. Build the host `chain_facts` example and host
`suifleet` executable with `cargo build --locked --bin suifleet --example
chain_facts` in `suifleet-edge`. Set `SUIFLEET_CHAIN_FACTS` to that example's
path. Run the fixture server in the node's namespace, with the checkout
available for the Move sources and test component.

```sh
python3 scripts/test_buildroot_localnet.py --claim-tool /path/to/host/suifleet
```

After `SF296_HOST_READY`, boot the Buildroot initramfs in the same namespace.
Append a test-only CPIO archive containing the test script at
`/sf296/test.py`, the kernel's matching `e1000.ko` at
`/sf296/e1000.ko`, and the following executable at
`/etc/init.d/S99sf296`. Keep the test archive separate from the release rootfs and omit the existing
`/etc` and `/etc/init.d` directory entries so their permissions are preserved.

```sh
#!/bin/sh
[ "$1" = start ] || exit 0
set -e
insmod /sf296/e1000.ko
ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up
python3 /sf296/test.py --guest
echo SF296_GUEST_PASS
poweroff -f
```

```sh
(cd overlay && printf '%s\n' sf296 sf296/test.py sf296/e1000.ko etc/init.d/S99sf296 \
  | cpio --quiet -o -H newc --owner 0:0) > test-overlay.cpio
cat output/images/rootfs.cpio test-overlay.cpio > test-initramfs.cpio
qemu-system-x86_64 -m 1024 -smp 2 -nographic -no-reboot \
  -kernel /path/to/vmlinuz -initrd test-initramfs.cpio \
  -append 'console=ttyS0 suifleet_sf296_test=1' -nic user,model=e1000
```

The fixture checks a fresh identity, claim-sponsored enrollment effects and
cap ownership, harmless installation, and an authoritative Applied receipt
from the node. It starts, restarts and stops the packaged service and checks
that the signer endpoint disappears on stop. This uses an HTTP Walrus
stand-in inside the isolated namespace. It exercises an application install
in emulation. Physical and manual-wallet acceptance remains in
[SF-031](https://github.com/tyler-suifleet/suifleet/issues/32), with mainnet
and production providers in [SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).

## Validation on 2026-11-25

Built both defconfigs in separate clean Buildroot output directories with
`make -C OUTPUT BR2_DL_DIR=/tmp/sf296-build/dl -j10` (glibc) and
`make -C OUTPUT BR2_DL_DIR=/tmp/sf296-build/dl -j7` (musl). Buildroot 2026.08.2
used Bootlin GCC 14.3.0, Rust 1.97.1, Python 3.14.10, and BusyBox 1.38.0.
`make -C OUTPUT BR2_DL_DIR=/tmp/sf296-build/dl suifleet-legal-info` verified
the MIT license hash and collected the vendored source on the glibc output.
Each CPIO contained the executable, launcher, init hook and CA bundle, with no
device keys, configuration, enrollment or journal directory. The glibc daemon
uses the glibc loader and the musl daemon is static PIE.

Both initramfs images booted with QEMU/KVM and Debian's
`6.1.0-56-amd64` kernel (`6.1.192-2`), then passed the VM test above against
a disposable network-none Sui 1.81.1 node (chain `da76ddb6`, protocol 138).
The two guests generated different identities. Each enrolled with claim-paid
gas and the expected cap owners, applied the authenticated test app with
passing health, and reported an independently read authoritative Applied
receipt. Each completed duplicate start, restart and stop through the shipped
init hook.

| Image | Rootfs SHA-256 | Enrollment transaction | Deployment transaction |
|---|---|---|---|
| x86-64 glibc | `e9b980ceecd6608d1507dc2550389ca225ce706df464500d4d0ee644c2c825ec` | `6Qq4xrt88MwoTaTPxDXHPXJSFdkUouEdd2f8rVBAtZQF` | `AJXd7B1jz3fs2mZ1wcDjuzLo3bguPvRfRzgcFoUpNzUR` |
| x86-64 musl | `78a0f15f62290c2ed895696329dd7fb5ea3c3666a2f3c93d90085aa8bcd69f1a` | `CDaSc2PyDFx1Bz7hrxptxw8pvmHfvGMegpDBaCM7AMLo` | `Ew56dyt73ND2YZKetpYLno6Qd1VYiwfBuKU5NPT9aZMX` |

The VM evidence covers BusyBox init and the two x86-64 profiles. Separate
SysV, other architectures, PKCS#11, public testnet and deferred external
acceptance were outside these local runs.
