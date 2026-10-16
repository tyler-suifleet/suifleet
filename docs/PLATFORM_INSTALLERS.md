# Configured installers and application recovery (SF-016)

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

This is Linux installer-script integration evidence, not SWUpdate/RAUC execution,
a Yocto rebuild, physical Pi recovery, firmware rollback, or successful device
boot. Those platform-specific integrations must record their own tool versions,
configuration, installation and physical recovery evidence. SF-016's lifecycle
infrastructure does not require a particular OS-update architecture or universal
physical platform acceptance.
