# macOS runtime and launchd service (SF-075)

SuiFleet's daemon runs on macOS as a system LaunchDaemon. It supervises
components with the same lifecycle, readiness, restart, rollback and IPC
semantics as on Linux ([supervisor](../suifleet-edge/src/supervisor.rs),
[COMPONENT_IPC.md](COMPONENT_IPC.md)). This is a native SuiFleet platform, not
AWS IoT Greengrass compatibility: the Greengrass nucleus officially supports
Linux and Windows only (see [references](#pinned-upstream-references)).

## Supported platform

| | Validated | Not validated |
|---|---|---|
| OS | macOS 15 (Sequoia) 15.7.x, Darwin 24, xnu-11417.140.69 (GitHub-hosted `macos-15` runner) | macOS 14 and earlier, macOS 26 |
| Architecture | arm64 (`aarch64-apple-darwin`) | Intel `x86_64-apple-darwin` (builds, untested) |
| Toolchain | Rust 1.94.0 (repository baseline), default features | `pkcs11` feature |

A Linux result does not establish macOS support, and this macOS result does not
establish any physical Mac, Intel or other release. Physical target acceptance
stays in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32).

## Install and service lifecycle

```bash
cargo build --locked --release --manifest-path suifleet-edge/Cargo.toml
sudo suifleet-edge/packaging/macos/install.sh suifleet-edge/target/release/suifleet
# write /etc/suifleet/config.toml and deployment.json (root-owned, 0644), then:
sudo launchctl bootstrap system /Library/LaunchDaemons/org.suifleet.daemon.plist
```

[install.sh](../suifleet-edge/packaging/macos/install.sh) (root) creates the
hidden `_suifleet` account (first free ID in 200–400) when missing, installs
`/usr/local/bin/suifleet` and the
[plist](../suifleet-edge/packaging/macos/org.suifleet.daemon.plist), creates
`/var/lib/suifleet` (0700) and `/var/log/suifleet`, and provisions the device
key on the machine: `/etc/suifleet/device.key` is generated once, kept on
reinstall and made `0600 _suifleet` with no ACL (`chmod -N` on the key,
`/etc/suifleet` and `/var/lib/suifleet`; macOS evaluates ACL entries before
the mode, and directories pass inheritable entries on). Only the public address is printed and
written to `device.address`. Packages must not contain keys, enrollment or state.
The installer writes no configuration and starts nothing.

| Operation | Command | Behaviour |
|---|---|---|
| Start | `launchctl bootstrap system <plist>` | Loads and starts the job (`RunAtLoad`). |
| Stop | `launchctl bootout system/org.suifleet.daemon` | SIGTERM: the daemon runs component shutdown commands (dependents first) and stops every descendant; launchd sends SIGKILL after `ExitTimeOut` (60 s). |
| Graceful stop, kept loaded | `launchctl kill SIGTERM system/org.suifleet.daemon` | A clean exit (status 0) is not restarted (`KeepAlive.SuccessfulExit = false`). |
| Restart | `launchctl kickstart -k system/org.suifleet.daemon` | |
| Status | `launchctl print system/org.suifleet.daemon` | `pid`, `runs`, `last exit code`. |
| Logs | `/var/log/suifleet/daemon.log` | Component output stays in each component's private `component.log`. |

A crash or failure exit is restarted after at least `ThrottleInterval` (5 s).
launchd has no readiness or watchdog protocol like systemd's `Type=notify` and
`WatchdogSec`: a hung (not exited) daemon is not restarted automatically.

## Process containment

macOS has no cgroups and no child subreaper. launchd starts every job in a
*resource coalition* of its own; each child joins its parent's coalition at
fork and stays in it through `setsid`, a cleared environment and reparenting to
launchd. Spawning into another coalition requires a privileged coalition or an
Apple entitlement. The daemon uses that coalition like the systemd unit's cgroup:

- At start (after reaping leftovers) it records its coalition in
  `supervised/instance.json` only when it is the coalition's **only member** and
  **not root**: the launchd job's own coalition. A daemon started from a shell
  shares its parent's coalition and records none, and logs a warning.
- Each run keeps its own process group, killed when it ends or stops. Every
  second, and after each run ends, the daemon kills any member of its coalition
  outside its own process tree, an orphan whichever way it detached.
  Without a recorded coalition, only orphans of the daemon user that still carry
  the state root's environment marker are found.
- After a crash (daemon SIGKILL), launchd restarts the job; launchd itself kills
  only the daemon's process group, so components survive the crash. The
  successor freezes and kills the recorded runs, their groups, marker carriers,
  their descendants and **every member of the previous instance's recorded
  coalition** (same boot, from `kern.bootsessionuuid`), then accepts the record
  only if that coalition now holds no process but itself. Without that proof
  (no sealed coalition recorded, or a survivor) it refuses to start components
  until a reboot or an operator clears the record, as on Linux.
- If the daemon cannot start at all after a crash (for example a configuration
  error), the earlier instance's components keep running until a daemon starts
  successfully or the host reboots. Inspect with `ps -U _suifleet`.

Coalition IDs increase monotonically within a boot and are never 0. Interrupted
installs are not replayed: their outcome stays indeterminate and unreported, as
on Linux ([PLATFORM_INSTALLERS.md](PLATFORM_INSTALLERS.md)).

**Boundary.** The coalition contains descendants of the daemon. It is not a
sandbox: components run as `_suifleet`, like the daemon, and can read its state
and talk to other local services. It also does not cover processes another
service starts on request. On the tested runner, as the daemon user,
`launchctl submit` and `launchctl bootstrap user/UID` or `gui/UID` were refused
and `at` jobs never ran (atrun is disabled by default), but **cron ran a job
outside the coalition**. `install.sh` therefore adds `_suifleet` to
`/usr/lib/cron/cron.deny` and `at.deny` (and refuses an allow file listing it),
and CI checks that a component's `crontab` and `at` requests are refused and
start nothing. Do not grant the daemon user other job-scheduling services.

## IPC authentication

The native IPC listener ([COMPONENT_IPC.md](COMPONENT_IPC.md)) authenticates
each connection by the kernel's Unix-socket peer credentials (`getpeereid`,
`LOCAL_PEERCRED` on macOS, `SO_PEERCRED` on Linux); the configuration and ACL
rules are identical. Use a daemon-owned, non-group/world-writable socket
directory the operator creates, for example `/var/run/suifleet` (`/var/run` is
cleared at boot; create it before every start, for example from the installer
of the IPC clients). Component principals need distinct non-root UIDs other than
`_suifleet`, provisioned by the platform.

## Secrets and permissions

| Path | Owner, mode | Enforced by |
|---|---|---|
| `/etc/suifleet/device.key` | `_suifleet`, 0600 | installer; the daemon refuses to start on macOS with a key that is not a regular file owned by its user, any group/other access, or any extended ACL entry (an allow entry can grant another user a 0600 file) |
| `/etc/suifleet/config.toml`, `deployment.json` | root, 0644 | operator; the daemon cannot change its own policy |
| `/var/lib/suifleet` | `_suifleet`, 0700 | installer |
| component `component.log`, `configuration.json` | `_suifleet`, 0600 | daemon (umask 077 and explicit modes) |

Other local users cannot read the key, state, journal or component logs
(checked against `nobody` in CI). The service signer that would keep the key
away from component processes on Linux ([KEY_ISOLATION.md](KEY_ISOLATION.md)) is
not available on macOS, so components can read the device key there.

## Component selection

The catalog's platform OS vocabulary is `linux`, `macos` (Rust's
`std::env::consts::OS`) and `*`; architectures are `x86_64`, `aarch64`, `arm`
and `*` (Apple silicon is `aarch64`). The contract's private validator, the
daemon and the dApp accept the same values, and the shared vectors in
[component-cases.json](../interfaces/component-cases.json) cover macOS
selection. The daemon selects the first manifest whose OS and architecture
match exactly or by `*`; a version with no macOS manifest fails on a Mac with
an unsupported-platform receipt and no download.

Compatibility: `macos` needs the upgraded package; older package versions keep
rejecting it. Daemons released before this change reject a whole version that
lists a `macos` manifest, so update Linux fleets before publishing mixed
versions (or publish macOS builds as separate versions). AWS recipes name macOS
`darwin`; the Greengrass recipe importer still omits those manifests.

## Platform-owned operations

macOS updates, reboots, firmware and boot health stay with the platform (for
example `softwareupdate` or an MDM). SuiFleet runs only explicitly configured
commands, never reboots the Mac and has no OS-installation state machine.

## Unsupported platform APIs on macOS

Refused at configuration load (the daemon exits instead of weakening them):

| Feature | Reason |
|---|---|
| `[device.signer] kind = "service"` ([KEY_ISOLATION.md](KEY_ISOLATION.md)) | Trusts the daemon by systemd unit, cgroup and pidfd |
| `apply.mode = "privileged-helper"` | A systemd path-activated root unit |
| `apply.max_memory_bytes` | macOS does not enforce `RLIMIT_AS` |
| `[daemon_update]` ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)) | Handoff and rollback not validated under launchd |

Not provided, without a substitute: the systemd sandbox of `suifleet.service`
(`ProtectSystem`, `NoNewPrivileges`, system-call filter, `MemoryMax`,
`TasksMax`), the readiness/watchdog protocol, per-component execution users
(also absent on Linux), Yocto packaging and the Raspberry Pi image. PKCS#11
signing builds but is untested on macOS.

## Pinned upstream references

Read on 2026-10-16 for semantics only; no AWS or Apple code is copied.
Implementation uses public interfaces (libproc, sysctl, launchd property lists).

- AWS IoT Greengrass V2 [component recipe reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/component-recipe-reference.html):
  `Platform.os` "Common platforms include … `linux`, `windows`, `darwin` (macOS)".
- [Greengrass nucleus](https://docs.aws.amazon.com/greengrass/v2/developerguide/greengrass-nucleus-component.html):
  installs on Linux and Windows; macOS is not an officially supported platform.
- [Configure the Core software](https://docs.aws.amazon.com/greengrass/v2/developerguide/configure-greengrass-core-v2.html):
  system service via systemd (Linux) or a Windows service; `runWithDefault`
  component users. SuiFleet's macOS service has no Greengrass counterpart.
- `launchd.plist(5)` (Darwin manual dated 30 July 2019):
  `KeepAlive`/`SuccessfulExit`, `ExitTimeOut` (SIGTERM then SIGKILL),
  `UserName`/`GroupName`, `Umask`, `ThrottleInterval` (default 10 s), resource
  limits, and "When a job dies, launchd kills any remaining processes with the
  same process group ID as the job" (unless `AbandonProcessGroup`).
- XNU [`xnu-11417.140.69`](https://github.com/apple-oss-distributions/xnu/tree/xnu-11417.140.69)
  (the runner's kernel): `bsd/kern/kern_exec.c` `posix_spawn` refuses
  (`EPERM`) a coalition other than the caller's unless the caller is in a
  privileged coalition or holds the coalition-spawn entitlement;
  `bsd/kern/kern_fork.c` `fork1` (non-spawn children always inherit the
  parent's coalitions); `osfmk/kern/coalition.c` (`coalition_next_id`,
  monotonically increasing; no coalition 0); `bsd/sys/proc_info_private.h`
  (`PROC_PIDCOALITIONINFO` = 20, `struct proc_pidcoalitioninfo`);
  `bsd/kern/proc_info.c` (no same-user check for that flavor).

## Evidence

The `macOS runtime (launchd)` CI job runs on every pull request:

- `cargo test --locked` on macOS (platform table, coalition inheritance and
  proof vectors, supervisor lifecycle/containment, refused features, key checks);
- [test_supervisor_launchd.py](../scripts/test_supervisor_launchd.py) (root,
  disposable runner only): the shipped installer and plist as a real job against
  the local chain/Walrus simulator: sealed coalition as `_suifleet`; grouped,
  setsid, env-cleared and reparented descendants contained; a component's
  `posix_spawnattr_setcoalition_np` escape refused; component crash; daemon
  SIGKILL with launchd restart and successor reap/accept; SIGKILL during an
  install (indeterminate, never replayed); key/state/log permissions against
  another user; a group-readable key stopping the daemon; `launchctl bootout`
  with graceful shutdown and an empty coalition. Evidence JSON is uploaded as
  the `macos-logs` artifact;
- [test_ipc.py](../scripts/test_ipc.py) through `sudo`: kernel-authenticated
  IPC across distinct UIDs, caller/topic denial and idle-flood limits.

First full pass: [CI run 39011599674](https://github.com/tyler-suifleet/suifleet/actions/runs/39011599674)
(macOS 15.7.9 24G830 arm64, rustc 1.94.0): 166 unit tests, 10 launchd checks
(the component's coalition spawn failed with `EPERM`; its `crontab` and `at`
were refused) and 4 IPC checks passed.

These are hosted-runner software results. No physical Mac, AWS account, paid
service or production credential is involved.
