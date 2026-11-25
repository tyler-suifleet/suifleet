# Daemon self-update (SF-026)

SuiFleet can replace its own daemon through an explicitly configured handoff. A new daemon that fails to start is recovered by the service manager and the platform's base binary, never by the failed daemon. SuiFleet does not reboot the system, write a bootloader, or replace the platform-installed binary. System reboot, boot confirmation and OS rollback stay platform-owned ([platform installers](PLATFORM_INSTALLERS.md)).

## Configuration

```toml
[daemon_update]
enabled = true
component = "suifleet-daemon"   # deployments of this component carry a daemon binary
# install_dir = "/var/lib/suifleet/daemon"   # default: <state_dir>/daemon
max_start_attempts = 3          # starts a new daemon gets before rollback
preflight_timeout_seconds = 60
```

The shipped `suifleet.service` starts `ExecStart=/usr/bin/suifleet --launch`. Without `[daemon_update]`, or with it disabled, the launcher simply continues as the base binary, so this changes nothing until enabled. Keep the unit's `Type=notify`, `Restart=on-failure`, start timeout and watchdog: they are the recovery path.

The settings above use the legacy launcher and a daemon-writable install directory. With a service signer, use the managed portable profile below.

## Managed updates with the portable signer

Use the [portable signer and service manager](PORTABLE_LINUX_SERVICE.md).
Keep the base executable and launcher installed by the platform. The base
executable continues to run the signer, including while a candidate is on
trial. Both binaries must support the installed signer protocol, configuration,
manifest and journal format. Start from a fully settled legacy installation
before switching profiles.

In the daemon configuration, enable the managed profile:

```toml
[daemon_update]
enabled = true
managed = true
component = "suifleet-daemon"
```

Create a dedicated persistent directory such as
`/var/lib/suifleet-client-update` with owner root, mode 0700 and root-owned
ancestors. In the service manager, pass
`--client-update-dir /var/lib/suifleet-client-update` to the portable launcher.
The managed profile uses one trial per deployment with a 60-second daemon
startup deadline after signer startup. Its executable directory and trial
policy are owned by the launcher. Keep both the service manager configuration
and package update access under platform control.

1. The platform operator verifies the target's [signed release bundle](RELEASES.md)
   with the trusted installed verifier and target triple. Extract
   `suifleet/bin/suifleet` from the archive listed in that verified manifest.
   Install those bytes as a root-owned 0755 regular file named `candidate`
   in the managed directory. Use an atomic rename in that directory to publish
   the file.
2. Deploy the exact binary as the configured daemon component with no
   lifecycle commands or dependencies. The daemon verifies the recipe,
   artifact and current admission, then requests a replacement over the
   private inherited supervisor channel. It journals the pending deployment
   and stops its components before exiting for the update.
3. The supervisor accepts the request only from that daemon and only on its
   update exit. It checks the platform-approved binary's ELF ABI and SHA-256
   against the authenticated deployment digest. It starts the verified copy
   with a new gated process and binds the signer to that exact task. A component
   cannot replace the executable or write to the supervisor channel or state.
4. The supervisor marks the candidate current after receiving its startup
   acknowledgement. On startup failure, the supervisor restarts the previous
   confirmed binary, or the base binary on the first update. If the supervisor
   is interrupted during a trial, the next service start rolls back the
   persisted trial. The resulting daemon uses the same signer key and
   deployment journal and reports the update outcome.

The native systemd signer profile still requires the platform-installed
executable. Use its package manager for updates, or switch to the managed
portable signer profile before deploying a daemon component. Client binary
replacement is separate from application recipes and system-image installation.
Platform tooling owns OS reboot, boot confirmation and firmware recovery.

Run the managed runtime regression inside a disposable Linux guest using the
same [test fixture setup as the portable signer](KEY_ISOLATION.md#portable-linux-signer):

```sh
python3 scripts/test_key_isolation_systemd.py --inside --portable --managed-update
```

The test exercises update deployment, signer rebinding, tamper rejection and
rollback against a loopback simulator. Set `SUIFLEET_TEST_UPDATE_CANDIDATE`
to a separately built compatible executable inside the guest to exercise two
build profiles.

## Publishing an update

Deploy the configured component (for example `suifleet-daemon 0.3.0`) to devices, like any component. The artifact is the daemon executable for the target platform. Its recipe must have no lifecycle commands or dependencies. The recipe and artifact digests are verified on chain and in Walrus as for every deployment.

## What happens on the device

1. **Stage.** After the downloads, the running daemon re-reads the deployment's admission, as for every apply. A cancellation, pause, expiry or closed maintenance window during a slow download, or an unavailable control read, leaves the deployment deferred, with nothing installed and no restart. Otherwise it installs the verified binary at `<install_dir>/versions/<version>-<deployment>/suifleet` (0755, synced).
2. **Preflight.** An ELF binary whose class, byte order or machine differs from the running daemon is refused first, before it is installed or run: an x86_64 kernel also runs i686 binaries, so an architecture change could pass the check below ([LINUX_X86.md](LINUX_X86.md)). It then runs `suifleet --self-check CONFIG` from the new binary. This is an offline check that the new build accepts this device's configuration, manifest (protocol and type origins), identity key and journal. A failure is reported as Failed and the running daemon is unchanged, with no restart.
3. **Hand off.** It records a pending handoff in `<install_dir>/handoff.json` (atomic, synced) and journals the deployment as pending. Pending deployments are never settled as interrupted applies. It then stops its components and exits with status 75, and the service manager restarts the unit.
4. **Launch.** The base binary (`--launch`) counts the attempt durably and `exec`s the new daemon. It keeps the same PID, so the unit's readiness, watchdog, cgroup and kill semantics apply to the new daemon.
5. **Confirm.** The new daemon confirms only after a complete local startup: configuration, identity, chain validation, journal and supervisor, then readiness. It records the handoff Confirmed, makes itself `current`, reports the deployment Applied, and removes all but the current and previous versions.
6. **Fall back.** The new daemon may exit, fail to execute, or never signal readiness (killed by the unit's start timeout or watchdog). The next start then finds an unconfirmed attempt. After `max_start_attempts` the launcher marks the handoff rolled back and starts the previous confirmed binary, or the base binary. That daemon reports the deployment Failed with the reason and removes the failed version. If a binary cannot be executed at all, the launcher falls back immediately.
7. **Settle.** Until the outcome is journaled, the handoff record, not `current`, decides which binary starts: a Confirmed record starts the new daemon, any other record the previous or base binary. Settling points `current` at that binary before removing the record, so an interruption at any step cannot leave a rejected daemon current.

Identity, journals and protocol compatibility are preserved: every binary uses the same configuration, key and state directory, and the preflight checks the journal binding before any handoff. A daemon started without `--launch` while an update is pending reports that update Failed rather than running it.

## Evidence

- Rust unit tests cover every launcher decision (base, confirmed, counted attempts, exhausted attempts, resolved records), startup resolution, confirmation and version cleanup, including interruption between the confirmation writes followed by rollback and a further restart.
- `scripts/test_daemon_update.py` (in `make test-ota`) runs the real daemon against the simulator, with a restart-on-failure loop standing in for the service manager. It covers:
  - a working update applied with the same identity and journal;
  - the confirmed update restarting after a crash;
  - a binary that passes preflight but fails every start, rolled back after two counted attempts and reported Failed;
  - an unrunnable binary rejected by preflight with no restart;
  - the same daemon rebuilt for another ELF architecture, refused before install or preflight with no restart;
  - cancellation and maintenance-window closure during a slow daemon download, with no preflight, handoff, restart or outcome; the reopened window then applies the update;
  - a daemon killed right after staging, whose next start completes the handoff.
- `make test-daemon-update-systemd` (host, needs Docker) runs `scripts/test_daemon_update_systemd.py` on real systemd 252 and cgroup v2 with the shipped units. The only change is a test drop-in shortening `TimeoutStartSec` to 15 s. It covers:
  - a working update exec'd as the unit's main process after a service-manager restart;
  - a new daemon that hangs before readiness, killed twice by systemd's start timeout and then rolled back to the previous daemon by the launcher;
  - a new daemon that exits at startup, rolled back the same way;
  - all outcomes reported, and `systemctl stop` leaving an empty cgroup.

  The recorded run is [validation/sf026-systemd.json](validation/sf026-systemd.json).

Not covered: power loss during a handoff on physical hardware, and platform-specific boot behavior. Record those separately (SF-031). The handoff files are written atomically and synced, but filesystem durability on the target storage is platform evidence.
