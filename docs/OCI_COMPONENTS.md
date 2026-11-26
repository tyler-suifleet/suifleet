# OCI container components (SF-036 and SF-300)

The Linux supervisor selects one local engine per daemon state root. Set
`SUIFLEET_OCI_ENGINE` in the daemon service environment. The packaged service
reads `/etc/suifleet/env`. Recipes cannot select an engine.

| Selection | Unix socket | Network setting | Linux image platforms | Engine requirements |
| --- | --- | --- | --- | --- |
| `docker` (default) | `/var/run/docker.sock` | `none`, `bridge` | amd64, arm64, ARMv7 | Local Docker CLI and Docker Engine with resource limits |
| `podman-rootful` | `/run/podman/podman.sock` | `none`, `bridge` | amd64, arm64 | `/usr/bin/podman-remote` and separately managed rootful API service, cgroup v2, CPU/memory/PID controllers and seccomp |
| `podman-rootless` | `/run/suifleet-podman/podman.sock` | `none` | amd64, arm64 | `/usr/bin/podman-remote`, API service under the daemon UID, subordinate UID/GID mappings, delegated cgroup v2 CPU/memory/PID controllers and seccomp |

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
Docker Desktop, Windows and macOS are outside this adapter's scope. Both Podman
modes reject ARMv7 before contacting the engine because image inspection lacks
the ARM variant needed to verify the platform. Physical ARM/Pi
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
`linux/arm/v7` (Docker only; ARMv6 daemon targets are unsupported). There is no
automatic emulation or architecture fallback. The
engine's cached image platform and repository digest are checked before use.
Images declaring implicit `VOLUME`s fail. Choose images compatible with a non-root
numeric UID[:GID], read-only root filesystem, all capabilities dropped and
`no-new-privileges`. The descriptor cannot specify host paths, port publishing,
privileged mode or arbitrary engine flags. `network` is explicitly `none`
or an engine-supported `bridge` (which permits outbound traffic); host networking is rejected.
The descriptor can request a non-executable, nodev/nosuid `/tmp` tmpfs for writes.
The device's configuration files, signing keys and registry credentials stay
outside the container.

## Device-local grants for descriptor version 2

Version 2 adds three optional fields to the same authenticated descriptor.
Persistence, device and IPC grants require Docker. Both Podman modes reject
requests for these grants before contacting the engine.
Set `version` to 2 and `user` to the locally granted UID:GID. This example
requests all three grants for UID and GID 31001.

```json
{
  "persistence": "retain",
  "devices": ["ttyUSB0"],
  "ipc": true
}
```

Each request needs a matching entry in the device's local configuration. Use a
dedicated numeric UID and the same GID for the component. Reserve both IDs for
the component on the host, including across native applications and other local
services. Docker must run in its normal Linux rootful mode with user namespace
remapping disabled so that kernel ownership and peer UIDs agree with the grant.

```toml
[components.oci_grants.sensor]
uid = 31001
persistence = true
devices = ["ttyUSB0"]
ipc = true

[ipc]
socket = "/run/suifleet/ipc.sock"

[ipc.components.sensor]
uid = 31001
operations = ["health", "publish"]
publish = ["sensor/readings"]
```

- **Data.** Under the configured daemon state directory, provision `oci-data`
  owned by the daemon and mode 0700. Provision `oci-data/sensor` as a directory
  owned by 31001:31001 and mode 0700. The adapter binds that directory at
  `/var/lib/suifleet-data` without nested mounts. The state directory and socket
  must be visible at the same absolute paths to the daemon and Docker, under
  trusted host directories. Data survives container replacement, restart and
  retirement. The operator owns backup, disk quotas, format migrations and
  eventual deletion. Use the same name and IDs only when
  the new version is authorized to read retained data. A failed update can
  already have changed the data. Image rollback does not roll back application
  data. To delete it, first retire or stop the app and confirm Docker cleanup.
- **Devices.** The grant accepts exact Linux `ttyUSB0`–`ttyUSB255` or
  `ttyACM0`–`ttyACM255` character devices. It verifies the expected kernel
  major/minor and uses fixed paths like `/dev/suifleet-ttyUSB0` in the
  container. Set the host node's owner, group and mode for the component UID or
  GID so Docker can preserve those permissions. Use an operator-managed udev rule tied to the expected
  hardware and keep node assignments stable. The daemon must see the exact
  trusted node even with its private `/dev`. For the shipped systemd unit,
  provision a local service drop-in with `BindReadOnlyPaths=/dev/ttyUSB0`
  for the specific node. The Docker engine must see the same node on the host.
  Restart the service after replacing a node mounted in its private `/dev`
  so that the daemon validates the current host node. Each configured node
  belongs to one component. Block devices, arbitrary device paths and device-node
  creation permissions are unavailable.
- **IPC.** The adapter binds only the configured daemon socket at
  `/run/suifleet-ipc.sock`, read-only. The server uses the kernel's
  `SO_PEERCRED` UID and the existing component ACL on every request. Use the
  component's actual name and follow [the local IPC protocol](COMPONENT_IPC.md).
  Keep startup and readiness independent of request completion. The daemon
  serializes IPC dispatch with bootstrap and deployment, so applications should
  retry requests after startup or while another deployment is in progress.
  The container can connect to the socket with its network disabled. It cannot
  select a different principal by changing a request field.

The daemon checks grant requests during deployment preparation and again before
every start, including recovery. A rejected request leaves the running version
in place during preparation. Grant edits apply after a daemon restart, which
also reconciles leftover containers. Version 1 and version 2 descriptors with
no requests still run without these device-local grants. These facilities
apply to application components. Platform scripts retain responsibility for
firmware installation, boot selection, reboot and firmware rollback.

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
supervisor stop timeout, then force removal. Writable container state outside an explicitly retained data mount is ephemeral.
Rollback confirms application readiness only.

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
untrusted local endpoint. The plain HTTP auth forwarding fixture runs on Docker.
The Docker grant test covers retained data across replacement, restart and
retirement, real UID-based IPC authorization, a virtual character-device grant,
and sibling isolation. The engine lifecycle test covers actual create/start,
readiness, non-root/capability and filesystem restrictions, engine resource
settings, crash/restart, a local HTTP
401 registry, missing auth profile, failed-readiness replacement rollback, clean
shutdown, orphaned engine-container cleanup after a killed client, and cache-only bootstrap. It uses no production credentials, public Sui
network, physical device or firmware updater. The script cleans its own labeled
containers even after a test assertion fails. Ordinary Rust tests include parser
negatives. `make test-ota` retains the full existing daemon/simulator regression.
