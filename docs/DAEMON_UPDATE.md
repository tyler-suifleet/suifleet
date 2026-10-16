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

## Publishing an update

Deploy the configured component (for example `suifleet-daemon 0.3.0`) to devices, like any component. The artifact is the daemon executable for the target platform. Its recipe must have no lifecycle commands or dependencies. The recipe and artifact digests are verified on chain and in Walrus as for every deployment.

## What happens on the device

1. **Stage.** The running daemon installs the verified binary at `<install_dir>/versions/<version>-<deployment>/suifleet` (0755, synced).
2. **Preflight.** It runs `suifleet --self-check CONFIG` from the new binary. This is an offline check that the new build accepts this device's configuration, manifest (protocol and type origins), identity key and journal. A failure is reported as Failed and the running daemon is unchanged, with no restart.
3. **Hand off.** It records a pending handoff in `<install_dir>/handoff.json` (atomic, synced) and journals the deployment as pending. Pending deployments are never settled as interrupted applies. It then stops its components and exits with status 75, and the service manager restarts the unit.
4. **Launch.** The base binary (`--launch`) counts the attempt durably and `exec`s the new daemon. It keeps the same PID, so the unit's readiness, watchdog, cgroup and kill semantics apply to the new daemon.
5. **Confirm.** The new daemon confirms only after a complete local startup: configuration, identity, chain validation, journal and supervisor, then readiness. It makes itself `current`, reports the deployment Applied, and removes all but the current and previous versions.
6. **Fall back.** The new daemon may exit, fail to execute, or never signal readiness (killed by the unit's start timeout or watchdog). The next start then finds an unconfirmed attempt. After `max_start_attempts` the launcher marks the handoff rolled back and starts the previous confirmed binary, or the base binary. That daemon reports the deployment Failed with the reason and removes the failed version. If a binary cannot be executed at all, the launcher falls back immediately.

Identity, journals and protocol compatibility are preserved: every binary uses the same configuration, key and state directory, and the preflight checks the journal binding before any handoff. A daemon started without `--launch` while an update is pending reports that update Failed rather than running it.

## Evidence

- Rust unit tests cover every launcher decision (base, confirmed, counted attempts, exhausted attempts, resolved records), startup resolution, confirmation and version cleanup.
- `scripts/test_daemon_update.py` (in `make test-ota`) runs the real daemon against the simulator, with a restart-on-failure loop standing in for the service manager. It covers:
  - a working update applied with the same identity and journal;
  - the confirmed update restarting after a crash;
  - a binary that passes preflight but fails every start, rolled back after two counted attempts and reported Failed;
  - an unrunnable binary rejected by preflight with no restart;
  - a daemon killed right after staging, whose next start completes the handoff.
- `make test-daemon-update-systemd` (host, needs Docker) runs `scripts/test_daemon_update_systemd.py` on real systemd 252 and cgroup v2 with the shipped units. The only change is a test drop-in shortening `TimeoutStartSec` to 15 s. It covers:
  - a working update exec'd as the unit's main process after a service-manager restart;
  - a new daemon that hangs before readiness, killed twice by systemd's start timeout and then rolled back to the previous daemon by the launcher;
  - a new daemon that exits at startup, rolled back the same way;
  - all outcomes reported, and `systemctl stop` leaving an empty cgroup.

  The recorded run is [validation/sf026-systemd.json](validation/sf026-systemd.json).

Not covered: power loss during a handoff on physical hardware, and platform-specific boot behavior. Record those separately (SF-031). The handoff files are written atomically and synced, but filesystem durability on the target storage is platform evidence.
