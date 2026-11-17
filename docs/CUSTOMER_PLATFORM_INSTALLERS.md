# Configure platform installers and application recovery

Connect an explicitly selected platform installer to authenticated artifacts. SuiFleet does not select an updater or decide when to reboot.

## Prerequisites

Install and test your platform tool independently. Choose an absolute executable path, daemon-user permissions and persistent journal storage. OS installation, partitions, boot confirmation and firmware rollback belong to that tool.

## Steps

1. Configure the intended command in the device configuration. This safe starting value refuses installation until you choose a real tool:

```toml
[apply]
command = "/usr/bin/false"
timeout_seconds = 600
allow_recipe_commands = false
```

2. Replace the command only after reviewing its arguments and required privileges. The artifact path is appended as one argument. Direct commands run as the daemon user; privileged helpers need their separately configured root-owned command.
3. Run `sudo suifleet --check-config /etc/suifleet/config.toml` and test a harmless artifact on an isolated target.
4. Preserve `/var/lib/suifleet` and any configured helper spool across platform-controlled restarts. For supervised application recovery, configure only a reviewed per-component recovery command and retained version, as described in the technical reference.

## Expected result

The client authenticates the recipe and artifact, executes the configured command and records its reported result. Exit zero is not proof of healthy firmware boot.

## Recovery and limits

An interrupted destructive command may have an unknown outcome. Inspect platform evidence and journals; never replay it automatically, delete the journal or change an unknown result to Applied. Use a new authorized deployment after resolving the actual state. Application process recovery is separate from OS rollback.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](PLATFORM_INSTALLERS.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
