# Configured installers and application recovery

SuiFleet verifies recipe and artifact digests before handing an artifact to the
operator's `apply.command` (the path is appended as one argument), or an explicitly
enabled recipe install command. It does not discover or choose an updater. Use an
absolute executable path and explicitly configure its arguments. Direct commands
run as the daemon user; the separately configured privileged helper restricts
privileged execution to its root-owned command. See [packaging](YOCTO_PACKAGING.md)
for the helper's permissions and service boundaries.

Platform scripts/tools own OS installation, reboot decisions, boot confirmation
and firmware rollback. There is no SuiFleet reboot command, partition requirement,
boot-success inference or firmware recovery state machine. An Applied receipt
means the configured command returned exit zero (and, for supervised applications,
the run passed readiness). It never proves a platform boot or firmware health.
Keep `/var/lib/suifleet` and the configured helper spool on persistent storage
across any platform-controlled restart.

The helper persists explicit results and reconciles them after daemon restarts.
Interrupted direct commands, killed/timed-out commands and abandoned helper claims
remain unknown and unreported; their deployment IDs never run automatically again.
A helper request withdrawn before execution can accurately fail. Do not edit an
unknown outcome into Applied. Inspect the installer/platform's own evidence and
use a new authorized deployment after resolving ambiguous work. SuiFleet has no
API that fabricates platform boot confirmation.

Replacing the SuiFleet daemon itself uses a separate, explicitly configured handoff with service-manager fallback; see [daemon self-update](DAEMON_UPDATE.md).

## SWUpdate and RAUC examples

Choose one installer for the device image. Use a signed platform bundle as the
deployment's firmware artifact and publish its recipe and artifact digests
through the normal authorized deployment flow. Omit a recipe `install` command
and keep recipe commands disabled. The daemon checks the authenticated digests
before submitting the artifact. The helper copies it into its private work
directory, rechecks the staged SHA-256, and appends the absolute path to the
configured argv as one argument. The private filename ends in `.artifact`;
bundle verification still uses the platform installer's own trust configuration.

In the daemon's existing `/etc/suifleet/config.toml`, configure `[apply]` as follows:

```toml
[apply]
mode = "privileged-helper"
command = "/bin/false"
allow_recipe_commands = false
firmware_spool = "/var/lib/suifleet/firmware"
timeout_seconds = 30
```

The helper supplies the actual installer command. The daemon timeout covers
claiming the request with an additional 30-second startup allowance. The
helper's separate timeout covers the running installer. Keep the service
startup timeout above the helper timeout and allow margin for copying and
verifying the bundle. Keep the journal, spool and platform state on storage
that survives platform-controlled restarts. Use a local filesystem that
supports file and directory sync for the journal and spool. The helper syncs
the claim before invoking the installer and syncs the result before releasing it.

### SWUpdate

Install a SWUpdate build with signed images enabled and RSA signature
verification configured. Provision the release verification public key at
`/etc/swupdate/signing-public.pem` under root control. Use the
[SWUpdate helper configuration](../suifleet-edge/examples/platform-installers/swupdate.toml),
which calls `/usr/bin/swupdate -k /etc/swupdate/signing-public.pem -i <private-artifact>`.
Ship a signed `sw-description` with the member image hashes in the `.swu`.
Configure compatible hardware and the required handlers and storage access in
the platform image. Confirm the chosen handlers work with the helper's local
filesystem and device access and offline service sandbox.
[SWUpdate signed-image documentation](https://sbabic.github.io/swupdate/signed_images.html)

### RAUC

Install the RAUC CLI and system service, with the service's root-owned
`system.conf`, compatible identifier, trust keyring and platform slot and
bootloader configuration. Provision a signed local RAUC bundle matching that
configuration. Use the
[RAUC helper configuration](../suifleet-edge/examples/platform-installers/rauc.toml),
which calls `/usr/bin/rauc install <private-artifact>` and waits for the CLI's
exit status. The helper runs as root. Allow it to reach the local system D-Bus
socket under the device's bus policy, and ensure the RAUC service can read the
helper's root-only work directory through its own mount and service sandbox.
D-Bus service execution can outlive the CLI. After an interruption, check the
running service and its logs before taking further action.
[RAUC installation and D-Bus documentation](https://rauc.readthedocs.io/en/stable/using.html)

On devices with the shipped systemd units, copy the chosen configuration and
enable the path unit as the operator:

```sh
sudo install -o root -g root -m 0600 /path/to/chosen-example.toml /etc/suifleet/firmware.toml
sudo systemctl enable --now suifleet-firmware.path
sudo systemctl restart suifleet.service
```

Review the packaged unit and configured executable paths on your image before
activation. Keep configuration, verification keys and binaries under operator
control. Serialize all update entry points on the platform, including external
maintenance tools. Platform install hooks, activation policy, reboot decisions,
boot confirmation and firmware rollback remain with the platform. SuiFleet
requires no particular partition layout.

| Observed command or helper outcome | SuiFleet receipt |
|---|---|
| Installer exits zero | Applied, meaning the configured command completed successfully |
| Installer returns a nonzero exit or fails to start | Failed, describing the command result. Inspect the platform for partial work or detached service activity |
| Request is withdrawn before the helper claims it | Failed, with no installer execution |
| Timeout, signal, or abandoned claim without a published result | Unknown, with no receipt or automatic replay of that deployment |
| Helper or daemon restarts after the result was published | The journal reconciles the existing command result and acknowledges it after recording it |

Inspect `journalctl -u suifleet-firmware.service -u suifleet.service` and the
platform installer logs when reconciling an unknown outcome. Resolve any
ongoing service work and confirm actual platform state before authorizing a new
deployment.

## Explicit application recovery

For a trusted supervised application, configure a recovery command by component
name in the device's operator-owned configuration:

```toml
[apply]
command = "/usr/local/libexec/my-platform-installer --explicit-option"
allow_recipe_commands = true

[components.recovery_commands]
myapp = "/usr/local/libexec/restore-myapp {artifact} {artifacts}/configuration.json"
```

This setting applies only to supervised applications with a retained previously
committed version. It is never used to recover a platform installer. Commands run
with the same cleared environment, bounds and daemon-user permissions as install
commands. `{artifact}` and `{artifacts}` are shell-quoted paths in the previous
version's workspace; the working directory is that workspace. The application
installer must save its version-specific configuration snapshot there. SuiFleet
retains that workspace and its lifecycle specification, rechecks the previous
artifact digest, and persists the previous confirmed component identity before
replacement. Managed recipe `configuration` remains a separate feature; SF-016
does not reinterpret or enable that wire field.

An explicit installation failure or deployment readiness failure invokes this
command, restarts the previous lifecycle and checks readiness. Once a successfully
committed replacement exhausts its runtime restart budget, recovery similarly
restores its retained previous version, once. Local runtime recovery runs before
network polling, so a network outage does not disable it. The failed deployment's
historical command receipt is preserved; `supervised/recovered.json`, the current
component journal and supervisor status record the recovered version separately.
With no configured recovery command, existing process replacement behavior remains.
No application recovery runs after an ambiguous installer interruption.

`supervised/recovery-intent.json` is synced before recovery starts and cleared only
after the previous application is ready (and runtime recovery is committed).
Interruption, failed recovery, failed digest inspection or failed readiness leaves
this marker: new deployments/recovery and daemon startup refuse to proceed. This
is an application execution guard, not a firmware boot state machine. An operator
must stop the service, inspect the recorded command/version and actual application
state, finish or repair recovery using their tooling, ensure no child remains,
and only then clear the marker and restart. Never clear it automatically.
Existing pre-SF-016 workspaces have no saved artifact checksum; explicit recovery
fails closed until a new successful deployment has established a retained version.

## Evidence boundaries

`python3 scripts/test_ota.py --platform-installer` exercises the actual daemon's
configured handoff to [the Linux installer script](../tests/components/platform-installer/install.sh),
which explicitly uses `/usr/bin/install` to deliver a harmless artifact. It also
checks digest rejection, restart deduplication and explicit command receipts.
`make test-ota` includes this integration, helper restart reconciliation, and
application artifact/configuration/journal restoration in `test_supervisor.py`.
Rust tests cover runtime rollback after restart, failed/interrupted recovery,
unknown installer interruption and command timeouts.

`make test-ota` also runs `scripts/test_firmware_restart.py --installer swupdate`
and `--installer rauc`. These use the real Linux daemon and helper with a
harmless recording process in place of each platform executable. They cover
argv and artifact handoff, digest rejection, explicit exit results, timeout,
helper interruption, restart reconciliation and no replay. Validate vendor
binaries, the actual service sandbox and platform installation and recovery
against the target image. Physical acceptance is tracked in SF-031, with
mainnet and final provider acceptance in SF-087.
[SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159)
