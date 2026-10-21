# OCI container components (SF-036)

The Linux supervisor supports Docker Engine through `/usr/bin/docker` and the
local `/var/run/docker.sock`. The daemon account needs explicit operator-provided
engine access. Engine access is a privileged trust boundary; install it only for
a trusted fleet operator. Podman, remote engines, Docker Desktop, Windows and
macOS are not supported by this adapter. The initial tested engine is Docker
29.8.0 with cgroup v2 on x86-64. Native Linux amd64, arm64 and ARMv7 image selection
is implemented; physical ARM/Pi installation, crash/reboot and target limits remain
unrun acceptance in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
Image/Yocto builds do not install or provision an engine automatically.

Use an ordinary authenticated native recipe with `lifecycle.run` exactly
`suifleet:oci`, `lifecycle.restart` set to `on-failure`, `always` or `never`, and no
install, shutdown or recipe readiness command. Set the existing device-local
`apply.allow_recipe_commands = true` only for trusted operators. Older daemons
fail this run command instead of silently treating the descriptor as firmware.
There is no new recipe version or chain API. Catalog platform manifests still
select the recipe/artifact before applying it.

The Walrus artifact is a JSON descriptor. Its SHA-256 and the recipe SHA-256 retain
the normal on-chain authentication (and SF-022 confidential-artifact handling).
Example for a native amd64 host; replace the placeholder with an actual image
manifest/index digest and upload/hash the descriptor bytes as the artifact:

```json
{
  "version": 1,
  "image": "docker.io/library/alpine@sha256:<64 lowercase hexadecimal characters>",
  "platform": "linux/amd64",
  "user": "1000:1000",
  "memoryBytes": 67108864,
  "cpus": 1,
  "pidsLimit": 32,
  "network": "none",
  "tmpfsBytes": 1048576,
  "command": ["sh", "-c", "echo ready >/tmp/ready; exec sleep 300"],
  "readiness": ["test", "-f", "/tmp/ready"]
}
```

Unknown fields fail. Images require an explicit repository and immutable SHA-256;
tags alone fail. Platforms must match the daemon: `linux/amd64`, `linux/arm64` or
`linux/arm/v7` (ARMv7 only; ARMv6 daemon targets are unsupported). There is no
automatic emulation or architecture fallback. The
engine's cached image platform and repository digest are checked before use.
Images declaring implicit `VOLUME`s fail. Choose images compatible with a non-root
numeric UID[:GID], read-only root filesystem, all capabilities dropped and
`no-new-privileges`. Host paths, sockets, devices, port publishing, privileged
mode and arbitrary engine flags are unavailable. `network` is explicitly `none`
or Docker's `bridge` (which permits outbound traffic); host networking is rejected.
The only optional application write space is a non-executable, nodev/nosuid `/tmp`
tmpfs. Container configuration/environment and the device's configuration files,
keys and registry credentials are never mounted into containers. This initial
adapter does not expose component IPC, persistent data volumes or secrets inside
the container; use an application-specific authenticated service for those needs.

Memory is 6 MiB–64 GiB with swap limited to the same combined total, CPUs are
1–64, PID count is 1–4096, and tmpfs cannot exceed the memory limit (zero disables
it). Docker/kernel admission failure fails deployment; there is no fallback to
an unconfined container. Docker's required `/proc`, `/dev` and generated resolver
mounts remain engine-managed. Engine logs use the local driver with two 1 MiB
files; attached stdout/stderr also enters the supervisor's private component log.

Registry authentication is optional `registryAuth: "profile_name"`; it names
`/etc/suifleet/oci/profile_name/config.json`, never credentials in the recipe.
Provision an inline Docker `auths` object at runtime, owned by root or the daemon
UID, mode 0600, under non-symlink directories owned by root or that UID and not
writable by group/others. The daemon must be able to read it. Only `auths` is
accepted: credential-store/plugins are rejected. Auth files are not placed in
workspaces or images, and engine error bodies are withheld from diagnostics.
The anonymous config location `/etc/suifleet/oci/public` should be absent or an
operator-controlled empty (`{}`) Docker configuration. No inherited `DOCKER_HOST`,
Docker context, home-directory credentials or environment secrets are used.
Production registry/account validation is deferred to the final external phase
in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

Deployment may pull a missing image using that profile, then verifies the cache
before stopping the current component. Authentication, missing/wrong digest and
platform failures leave the existing run intact. Subsequent start, daemon
bootstrap, crash restart and rollback use `--pull never`; cached images need no
registry or credentials. SuiFleet never prunes engine images. Retain active and
previous digests when performing external engine GC; missing/corrupt cache fails
closed and is not an offline-success result.

The supervisor owns restart budgets/backoff and readiness. Transient engine
cleanup/start failures consume that same budget and retry after backoff; a new
container cannot be created until component cleanup succeeds. After the last OCI
component is retired, confirmed cleanup releases the durable engine requirement,
so later daemon startup does not require Docker. Engine restart is
always disabled. Descriptor readiness is an argv command executed as the container
user; without one, the container must survive the configured startup interval.
A failed replacement restores the previous descriptor/image and confirms it only
after readiness. Shutdown uses the container's configured stop signal and the
supervisor stop timeout, then force removal. Writable container state is ephemeral;
rollback does not restore external application state or firmware/boot health.

Before any container create, a durable marker records engine use. Containers have
state-root and component labels and deterministic version names. Start reconciles
leftovers for that component; exit cleans up before a restart. Daemon startup
removes engine-owned leftovers for its state root after checking the prior daemon
is dead, before native-process recovery. Engine failures prevent cleanup/start;
existing native descendant-proof requirements remain in force. A crash may leave
a container alive until the successor reconciles it; no claim of immediate engine
cleanup during daemon downtime is made. Preserve the state root/marker and the
journal. Do not clear recovery records or prune container state to fabricate a
successful restart.

Run focused host verification with a cached digest:

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo test --locked --manifest-path suifleet-edge/Cargo.toml oci::
SF_OCI_TEST_IMAGE=docker.io/library/alpine@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 \
  RUSTUP_TOOLCHAIN=1.94.0 python3 scripts/test_oci.py
```

The engine test covers actual create/start, readiness, non-root/capability and
filesystem restrictions, engine resource settings, crash/restart, a local HTTP
401 registry, missing auth profile, failed-readiness replacement rollback, clean
shutdown, orphaned engine-container cleanup after a killed client, and cache-only bootstrap. It uses no production credentials, public Sui
network, physical device or firmware updater. The script cleans its own labeled
containers even after a test assertion fails. Ordinary Rust tests include parser
negatives. `make test-ota` retains the full existing daemon/simulator regression.
