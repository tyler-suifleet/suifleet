# Run SuiFleet without systemd

Choose OpenRC, Debian-style SysV init, runit, or foreground operation. Each
profile runs the root-owned [portable signer launcher](KEY_ISOLATION.md#portable-linux-signer).
It starts the signer and verifies that its socket answers before releasing the
daemon's startup gate. The children run under separate unprivileged UIDs. An
exit from either child stops the pair. If the launcher is killed, Linux kills
both children. A subsequent start replaces the stale socket.

These examples assume Linux with procfs and `fs.suid_dumpable=0`, Python 3.9+
and a compatible SuiFleet binary and launcher from the same revision. Install
the appropriate manager through your distribution if you choose to use one.
Use only one profile per device. Stop and disable existing SuiFleet services
and sockets before switching profiles. Disable daemon self-update when using
the service signer.

## Install and provision

Run these commands as root on the installed target system. For image builds,
install only the verified binary, launcher and selected service files. Generate
the identity and supply fleet configuration after boot.

```sh
install -o root -g root -m 0755 /path/to/verified/suifleet /usr/bin/suifleet
install -D -o root -g root -m 0755 scripts/suifleet-portable-signer \
    /usr/libexec/suifleet-portable-signer

# Account creation on systems with shadow-utils. On other distributions use
# the platform's equivalent and keep the same account/group names.
for account in suifleet suifleet-signer; do
    if ! id "$account" >/dev/null 2>&1; then
        useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell "$(command -v nologin)" "$account"
    fi
done
install -d -o root -g root -m 0755 /etc/suifleet
install -d -o suifleet -g suifleet -m 0700 /var/lib/suifleet
```

Verify that existing service accounts have distinct nonzero UIDs and distinct
nonzero primary GIDs with the indicated group names. The launcher strips
supplementary groups. Configure device-node or token access for the signer's
UID or primary group where required. Put the fleet manifest in
`/etc/suifleet/deployment.json` as root-owned and readable by both children.
Keep private values out of that manifest.

For a software-backed identity, the following command creates a key only if
the path is absent. Stop all SuiFleet processes before moving a legacy key
to the signer account. For a PKCS#11 identity, configure a provisioned token
and protect its PIN with the signer account instead.

```sh
umask 077
/usr/bin/suifleet --provision /etc/suifleet/device.key
chown suifleet-signer:suifleet-signer /etc/suifleet/device.key
chmod 0600 /etc/suifleet/device.key
```

Use the fleet's existing device registration and manifest instructions to
configure the cap and object IDs. Start from the
[signer example](../suifleet-edge/meta-suifleet/recipes-edge/suifleet/files/signer.toml.example)
and replace `caller_unit` with `portable = true`. Install `signer.toml` as
`root:suifleet-signer` mode 0640 and `config.toml` as `root:suifleet` mode 0640.
Set `[runtime] state_dir = "/var/lib/suifleet"` and use this signer in the daemon
configuration. Use the same socket in any root enrollment configuration.

```toml
[device.signer]
kind = "service"
socket = "/run/suifleet-portable/signer.sock"
```

The launcher creates the socket directory under the root-owned `/run` and
sets the socket to `root:suifleet` mode 0660. Keep `/etc/suifleet` and
`/var/lib/suifleet` on persistent storage. On upgrades stop the pair, replace
the verified binary and launcher together, then start it using the same
configuration, key and journal. On rollback use a binary that supports the
existing manifest and journal format.

## Select a service manager

Run from the repository root as root. The examples manage the paired service
under the name `suifleet`.

### OpenRC

```sh
install -o root -g root -m 0755 suifleet-edge/packaging/portable/openrc/suifleet /etc/init.d/suifleet
rc-update add suifleet default
rc-service suifleet start
rc-service suifleet status
rc-service suifleet restart
rc-service suifleet stop
```

OpenRC's `supervise-daemon` retries crashes subject to the configured delay
and limit. After fixing a startup error that exhausts the limit, restart the
service. It orders after the local filesystem and network services when present.

### Debian-style SysV

This script uses dpkg's `start-stop-daemon`, util-linux `flock`,
and `/usr/bin/python3`. BusyBox's interchangeable command name does not imply
the same options. On Debian with SysV init, install and enable the script
with the following commands.

```sh
install -o root -g root -m 0755 suifleet-edge/packaging/portable/sysv/suifleet /etc/init.d/suifleet
update-rc.d suifleet defaults
/etc/init.d/suifleet start
/etc/init.d/suifleet status
/etc/init.d/suifleet restart
/etc/init.d/suifleet stop
```

This profile starts at the enabled runlevels. After a crash, use `start`
or `restart` to recover the pair.

OpenRC and SysV send service output to `/var/log/suifleet-pair.log`. Keep the
file root-owned and private. Use the platform's log retention policy and
restart the service when rotating to a new file.

### runit

Use your distribution's service directory watched by `runsvdir`.
The example uses `/etc/service`.

```sh
install -D -o root -g root -m 0755 suifleet-edge/packaging/portable/runit/suifleet/run /etc/sv/suifleet/run
ln -s /etc/sv/suifleet /etc/service/suifleet
sv -w 20 up /etc/service/suifleet
sv status /etc/service/suifleet
sv -w 20 restart /etc/service/suifleet
sv -w 20 down /etc/service/suifleet
```

While the service is up, runit restarts an exited pair. Provide your
distribution's logging service if you need persistent service output. Use
the supervisor's normal disable procedure to keep the service down across
boot.

### Foreground

```sh
/usr/libexec/suifleet-portable-signer
```

Run as root. Stop with Ctrl-C in the foreground or send SIGTERM to the
launcher. Wait for it to exit before starting it again. A custom supervisor
can use the same foreground command. Give it at least 15 seconds for
graceful shutdown and configure logging and restart behavior explicitly.

## Verify and recover

Manager status reflects process state. Use service logs to confirm daemon
initialization, then verify a signer-backed report for the device on its
configured network. If configuration or the signer fails, inspect the logs
before retrying.

When a platform command is interrupted, check the journal and inspect the
target before explicitly redeploying. An interrupted direct command has an
indeterminate outcome and is not replayed. These profiles supervise the
daemon and signer. Integrators that run other persistent processes need a
platform containment and cleanup policy for those processes, including
when the paired service is killed abruptly. The daemon can [refuse to
restart until component cleanup is established](OPERATIONS.md#supervised-components-sf-014).
The platform owns OS
installation, reboot, boot confirmation and firmware rollback.

For software validation, stage the built daemon and `grpc_sim_shim` under
`suifleet-edge/target/debug` and mount the checkout at `/workspace` in a
disposable Linux guest. Run the following as root with the selected manager
installed. Initialize the guest's OpenRC runlevel state before testing OpenRC.
Use a fresh guest for each profile.

```sh
python3 /workspace/scripts/test_key_isolation_systemd.py --inside --portable --portable-manager openrc
```

Substitute `sysv`, `runit`, or `manual` for the other profiles. The test uses
the real daemon, signer and manager with a loopback OTA simulator.
