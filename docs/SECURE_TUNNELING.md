# Configured secure tunnel destinations (SF-063)

The optional Linux `suifleet --secure-tunneling CONFIG_JSON` companion consumes
[AWS-shaped tunnel notifications](https://docs.aws.amazon.com/iot/latest/developerguide/configure-remote-device.html)
on the exact `$aws/things/THING/tunnels/notify` MQTT topic over authenticated
mutual TLS. It starts the Apache-2.0 AWS IoT Secure Tunneling Local Proxy in
destination mode only for administrator-configured IPv4 loopback services.
The daemon bridge refuses tunnel topics, including wildcard/prefix mappings,
so client access tokens (CATs) never enter its durable pub/sub or bridge spool.
This implements the `secure-tunneling` catalog adapter alongside platform-owned
SSH, firewall, service authentication and network configuration. It does not
configure those services, run notification-supplied commands or trigger reboot.

## Build and installation

```bash
python3 scripts/package_tunneling.py --output /tmp/suifleet-tunnel.tar.gz
```

The optional archive contains `localproxy`, its license, source revision, binary
SHA-256, build package versions, manifest, disabled example configuration,
connection settings and systemd unit. The build pins upstream revision
`8a71618acf536923e882387e8d9eca9dbc7fdbe2` (3.3.1), verifies the downloaded
archive digest, and compiles out the option that disables TLS host verification.
Boost is pinned by upstream; Protobuf/OpenSSL/zlib use the Ubuntu 22.04 build
packages recorded in `build-versions.txt`. This is a native Linux build, not an
ARM/Yocto or reproducible release-image claim. Install matching runtime libraries
(`libprotobuf23`, `libssl3`, `zlib1g`, `libstdc++6` on Ubuntu 22.04) and CA roots.
Build the SuiFleet daemon separately with the normal locked toolchain.

An administrator installs the reviewed artifact and daemon; the build script
never installs or enables anything. On a disposable or authorized Linux host:

```bash
# After extracting and verifying the archive in an administrator-owned directory:
sudo useradd --system --no-create-home --shell /usr/sbin/nologin suifleet-tunnel
sudo install -d -m 0755 /usr/libexec /usr/share/suifleet-tunnel/empty /etc/suifleet-tunnel
sudo install -o root -g root -m 0755 localproxy /usr/libexec/suifleet-localproxy
sudo install -o root -g root -m 0644 proxy-settings.json /usr/share/suifleet-tunnel/proxy-settings.json
sudo install -o root -g root -m 0644 suifleet-tunnel.service /etc/systemd/system/
sudo install -o root -g root -m 0644 config.example.json /etc/suifleet-tunnel/config.json
```

Provision an independent IoT certificate/key and policy at runtime. Never include
keys in a package/image. The private key must be mode 0600 and owned by
`suifleet-tunnel`; its parent directory, public CA/certificate and configuration
remain administrator-controlled. Use a distinct MQTT client ID from the daemon
and any other client, and scope `iot:Connect` to that ID, `iot:Subscribe` to the
exact notification topic filter, and `iot:Receive` to that exact topic. No
`iot:Publish`, broad topic wildcards or tunnel-control IAM permissions are needed
by the destination adapter. Only authorized AWS operators should have
OpenTunnel/RotateTunnelAccessToken/CloseTunnel authority. The device cannot
independently identify the remote operator from a CAT notification: that caller
restriction is enforced by AWS IAM and local service authentication. Use a
separate certificate/policy so a component/daemon cannot subscribe to CATs.

Edit `config.json`: set `enabled: true`, region, installed binary SHA-256,
explicit destination IDs and loopback addresses, MQTT endpoint/thing/client ID
and certificate paths. Limits are 1–16 configured services, 1–16 concurrent tunnel
processes and a 1–43200 second absolute session lifetime. Defaults in the example
are one tunnel and 300 seconds. The packaged, immutable proxy settings limit
connections per proxy to four and disable connection retries/reconnects. Region
selects the upstream AWS endpoint; notifications cannot supply an endpoint,
command, path, service address or settings override. The proxy environment is
cleared, so ambient endpoint/HTTP-proxy/client-token variables cannot override it.

Configuration, executable, empty working directory and proxy settings must be
absolute paths with only root-owned, non-group/world-writable, non-symlink
ancestors. Keep the working directory empty to exclude upstream `./config`
discovery. Only run the service under its exclusive unprivileged user. Other
components must never run as this user, join its group, gain root or inspect its
process namespace. The service cannot access the device-key/config/state paths.

After checking the configuration and policy, an administrator can explicitly run
`systemctl daemon-reload` and `systemctl enable --now suifleet-tunnel.service`.
Configuration/binary/component replacement requires `systemctl restart
suifleet-tunnel.service`. `systemctl stop suifleet-tunnel.service` revokes active
local sessions. The unit is independent of the normal daemon and is never
automatically enabled by a recipe or inferred from installed SSH software.

## Token, lifecycle and failure behavior

Notifications must contain only `clientAccessToken`, `clientMode`, `region` and
`services`; mode must be `destination`, region must match, service IDs must be
unique and allow-listed. Retained messages, oversized/invalid JSON and unknown
fields are refused. MQTT uses a clean session, with no offline CAT queue.

CATs travel in the child environment, never argv, logs, component IPC or disk.
The notification string is wiped after spawning; process memory/environment can
still contain CATs for the lifetime of a session. An exclusive service UID,
restricted proc access and disabled core dumps provide the boundary; root or a
same-UID process remains trusted. Proxy stdout/stderr are discarded because
upstream errors may include request data. Errors from untrusted notifications
are reduced to a fixed diagnostic. This intentionally trades proxy diagnostics
for avoiding accidental token disclosure.

Only token hashes are retained for replay detection, bounded to 4096 per adapter
process. Exhaustion fails closed until administrator/service restart. Tokens
refused at capacity or during launch failure are also consumed, never queued.
Replay hashes are memory-only: an adapter restart loses that history. AWS token
single-use semantics and clean MQTT sessions remain necessary across restarts;
there is no claim of durable global replay prevention. A reconnect requires a
fresh service-issued notification/token, not an automatic proxy restart.

Absolute timeout, child exit, MQTT disconnect/keepalive loss, invalid/removed or
rotated credentials, explicit stop and service replacement kill local proxies.
The proxy starts in its own process group; normal cleanup kills and reaps it and
its descendants. The shipped unit uses `KillMode=control-group`, so adapter
SIGKILL/crash and replacement also remove descendants before systemd restarts.
MQTT writes time out in one second so a blocked peer cannot indefinitely suspend
session expiry. Credential checks default to 30 seconds (minimum five). Revoking
an AWS policy/certificate closes the connection through the service; local
credential removal is detected at the next check. Local cleanup does not call
AWS CloseTunnel or revoke the source CAT: administrators still own remote tunnel
closure and service authentication.

## Local evidence and remaining acceptance

```bash
make test-tunneling
# Or use an existing debug build and package:
python3 scripts/test_tunneling.py --binary suifleet-edge/target/debug/suifleet \
  --package /tmp/suifleet-tunnel.tar.gz --evidence /tmp/tunnel-evidence.json
```

The harness boots the shipped unit on real systemd/cgroup v2 inside a disposable
privileged Docker container with `--network none`. It uses generated mTLS
certificates, AWS-shaped MQTT notifications, an independently encoded
TLS/WebSocket/protobuf endpoint, the actual pinned proxy binary, and a loopback
echo service. It verifies byte forwarding, denied services/modes/regions/fields,
CAT argv/journal/cross-UID proc negatives, concurrency, timeout/replay, connection
failure, credential removal, MQTT outage, replacement and adapter SIGKILL cleanup.
No host services, credentials or firewall configuration are changed.

Real AWS service/IAM/tunnel-rotation/account acceptance is **unrun**. It remains
in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159) under the
user-authorized final external acceptance phase. Physical Pi/ARM packaging,
networking and reboot/power-loss acceptance are also unrun. This offline evidence
does not establish AWS certification, production credentials or hardware safety.
