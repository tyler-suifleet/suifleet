# Install on macOS

Install the native LaunchDaemon for evaluation on macOS 15 Apple silicon. Hosted runner evidence does not establish acceptance on your physical Mac.

## Prerequisites

Have administrative access, a reviewed checkout with Rust 1.94.0 or an authenticated supplied bundle, and connection settings for your fleet. macOS reads its software key directly; it does not use the Linux signing service.

## Steps

1. From the reviewed checkout, build and install:

```sh
cargo build --locked --release --manifest-path suifleet-edge/Cargo.toml
sudo suifleet-edge/packaging/macos/install.sh suifleet-edge/target/release/suifleet
```

2. Save the public address printed by the installer and [register or enroll](INSTALL_DEVICE_CLIENT.md#register-by-address). The installer creates `_suifleet` and a unique local key, but writes no fleet configuration and starts nothing.
3. Download connection settings in the console and install `deployment.json` and `config.toml`, root-owned mode 0644. Use the actual Device/DeviceCap IDs. Under `[device]`, set `keypair_path = "/etc/suifleet/device.key"` instead of Linux `[device.signer]`.
4. Check configuration, then start:

```sh
sudo suifleet --check-config /etc/suifleet/config.toml
sudo launchctl bootstrap system /Library/LaunchDaemons/org.suifleet.daemon.plist
launchctl print system/org.suifleet.daemon
```

## Expected result

The LaunchDaemon runs and sends authenticated reports for the registered device. Logs are in `/var/log/suifleet/daemon.log`.

## Recovery and limits

Inspect launchd state, logs, IDs and funding before retrying. Preserve the device key and state. Linux systemd/cgroup isolation and privileged platform helpers do not apply on macOS; consult the technical reference for native containment and unsupported APIs. Never claim hardware-key protection from this software-key installation.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](MACOS_RUNTIME.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
