# OCI container components (SF-036)

The Linux supervisor selects one local engine per daemon state root. Set
`SUIFLEET_OCI_ENGINE` in the daemon service environment. The packaged service
reads `/etc/suifleet/env`. Recipes cannot select an engine.

| Selection | Unix socket | Network setting | Engine requirements |
| --- | --- | --- | --- |
| `docker` (default) | `/var/run/docker.sock` | `none`, `bridge` | Local Docker CLI and Docker Engine with resource limits |
| `podman-rootful` | `/run/podman/podman.sock` | `none`, `bridge` | `/usr/bin/podman-remote` and separately managed rootful API service, cgroup v2, CPU/memory/PID controllers and seccomp |
| `podman-rootless` | `/run/suifleet-podman/podman.sock` | `none` | `/usr/bin/podman-remote`, API service under the daemon UID, subordinate UID/GID mappings, delegated cgroup v2 CPU/memory/PID controllers and seccomp |

Run the Podman API service outside the SuiFleet unit. For both Podman modes,
provision `/run/suifleet-podman` and `/run/suifleet-podman/libpod` as mode 0700
and owned by the daemon account at boot. The socket client expects the
`libpod` directory to exist when it runs inside the read-only service sandbox.
For example, use these two systemd tmpfiles entries for the packaged account.

```text
d /run/suifleet-podman        0700 suifleet suifleet -
d /run/suifleet-podman/libpod 0700 suifleet suifleet -
```

With systemd, provision the system `podman.socket` for rootful mode or the
daemon account's user `podman.socket` for rootless mode. For rootless boot
operation, arrange that the account's user manager and runtime directory are
present without an interactive login. The SuiFleet sandbox hides `/run/user`.
Set the rootless socket's drop-in as follows. The engine still uses its normal
rootless runtime directory outside the daemon sandbox.

```ini
[Socket]
ListenStream=
ListenStream=/run/suifleet-podman/podman.sock
```

Rootful socket access grants root-equivalent engine authority.
Grant the daemon account socket access explicitly, including traversal of the
rootful socket directory. Restrict engine administration to trusted operators. Keep the API
local to Unix sockets. Configure a credential-empty engine service environment and
storage, with no injected default mounts, secrets or environment and an enforcing
default seccomp profile. For a rootless service, keep its store and UID/GID mappings
stable across daemon restarts. See [Podman system service](https://docs.podman.io/en/stable/markdown/podman-system-service.1.html)
and [Podman create](https://docs.podman.io/en/stable/markdown/podman-create.1.html).

The durable marker binds recovery to the engine selection. Retire all OCI
components and confirm cleanup with the old engine before switching. A legacy
marker implies Docker. A mismatched engine selection fails before cleanup;
restore the prior configuration and keep the marker. Before downgrading to
a daemon that predates Podman support, retire its Podman components and confirm
cleanup with the Podman-capable daemon. Remote hosts,
Docker Desktop, Windows and macOS are outside this adapter's scope. Native Linux
amd64, arm64 and ARMv7 image selection is implemented. Physical ARM/Pi
installation, crash/reboot and target limits remain acceptance in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
Image/Yocto builds leave engine installation and provisioning to the platform.

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
or an engine-supported `bridge` (which permits outbound traffic). Host networking is rejected.
The only optional application write space is a non-executable, nodev/nosuid `/tmp`
tmpfs. Container configuration/environment and the device's configuration files,
keys and registry credentials are never mounted into containers. This initial
adapter does not expose component IPC, persistent data volumes or secrets inside
the container; use an application-specific authenticated service for those needs.

Memory is 6 MiB–64 GiB with swap limited to the same combined total, CPUs are
1–64, PID count is 1–4096, and tmpfs cannot exceed the memory limit (zero disables
it). Engine or kernel admission failure fails deployment. Engine-managed mounts
include `/proc`, `/dev` and generated resolver files. Podman disables its automatic
writable tmpfs mounts and systemd mode. Docker uses the local driver with two
1 MiB log files. Podman uses the k8s-file log driver with a 1 MiB size limit.
Attached stdout/stderr also enters the supervisor's private component log.

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
Podman pulls use the same inline auth profile and require TLS verification. The
client uses an explicit empty auth file for anonymous pulls and clears its
inherited home and configuration lookup paths. The
Podman service is part of the trusted engine boundary and must run without
ambient registry logins or registry credential helpers.
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
so later daemon startup does not require an engine. Engine restart is
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

Repeat the host command with `SUIFLEET_OCI_ENGINE=podman-rootful` and
`SUIFLEET_OCI_ENGINE=podman-rootless` for the two local Podman profiles. Pre-cache
the same digest in each selected store. The Podman registry negative tests an
untrusted local endpoint, while the plain HTTP auth forwarding fixture runs on
Docker. The engine test covers actual create/start, readiness, non-root/capability and
filesystem restrictions, engine resource settings, crash/restart, a local HTTP
401 registry, missing auth profile, failed-readiness replacement rollback, clean
shutdown, orphaned engine-container cleanup after a killed client, and cache-only bootstrap. It uses no production credentials, public Sui
network, physical device or firmware updater. The script cleans its own labeled
containers even after a test assertion fails. Ordinary Rust tests include parser
negatives. `make test-ota` retains the full existing daemon/simulator regression.
