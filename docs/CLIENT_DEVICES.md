# LAN client devices (SF-035)

With `[client_devices]`, the daemon is a gateway for LAN client devices ("things"): sensors, controllers and other MQTT clients that do not run SuiFleet. It runs a local certificate authority, enrolls each thing with a TLS client certificate, and stands in front of the local MQTT broker ([MESSAGING.md](MESSAGING.md)) as an authorizing proxy: every connection is mutual TLS, every CONNECT, will, PUBLISH and SUBSCRIBE is checked against `aws.greengrass.clientdevices.Auth`-shaped device groups, and revoked, expired or superseded certificates are refused, including on live sessions. Clients find the gateway by a LAN broadcast and fetch authenticated endpoints, the CA and a device-signed CA binding over HTTPS. Nothing goes on chain or to Walrus. The rules follow the SF-028 specification ([GREENGRASS_COMPATIBILITY.md](GREENGRASS_COMPATIBILITY.md#client-devices-sf-035)).

```
 LAN client ──mTLS :8883──▶ gateway (daemon) ──plain MQTT, loopback──▶ broker 127.0.0.1:1883 ◀── SF-032 bridge ◀▶ pub/sub ◀▶ components
 LAN client ──mTLS :8443──▶ discovery (GET /discover), rotation (POST /rotate)
 LAN client ──UDP  :8442──▶ unauthenticated discovery hint
```

## Configuration

```toml
[ipc]                                    # required: enrollment and revocation are administrator IPC
socket = "/run/suifleet/ipc.sock"
administrators = [0]

[client_devices]
device_groups_file = "/etc/suifleet/client-device-groups.json"  # relative paths resolve from this file
broker = "127.0.0.1:1883"                # the local broker; must be a loopback address
broker_username = "gateway"              # optional broker credentials used for every proxied session
broker_password_file = "/etc/suifleet/gateway-mqtt-password"
mqtt_listen = "0.0.0.0:8883"             # MQTT over mutual TLS
https_listen = "0.0.0.0:8443"            # discovery and rotation over mutual TLS
udp_discovery = "0.0.0.0:8442"           # optional; omit to disable the LAN hint
certificate_validity_seconds = 604800    # client certificates: 60..=31536000
server_certificate_validity_seconds = 604800
rotation_grace_seconds = 300             # 0..=86400: how long a rotated-out certificate stays usable
min_rotation_interval_seconds = 60       # 0..=86400: a younger certificate cannot rotate
discovery_ttl_seconds = 3600             # 60..=86400: how long clients may cache a discovery answer
hostnames = ["gateway.local"]            # extra DNS names for the server certificate and discovery
include_loopback = false                 # advertise loopback addresses (tests only)
include_link_local = false
max_connections = 64                     # concurrent TLS connections, both listeners
max_devices = 1000                       # enrolled things
```

The device groups file is the AWS `deviceGroups` object (format `2021-03-05`):

```json
{
  "formatVersion": "2021-03-05",
  "definitions": {
    "sensors": {"selectionRule": "thingName: sensor-*", "policyName": "sensor"},
    "controllers": {"selectionRule": "thingName: ctl-* OR thingName: ops-admin", "policyName": "controller"}
  },
  "policies": {
    "sensor": {
      "connect": {"operations": ["mqtt:connect"], "resources": ["mqtt:clientId:${iot:Connection.Thing.ThingName}"]},
      "publish": {"operations": ["mqtt:publish"], "resources": ["mqtt:topic:sensors/${iot:Connection.Thing.ThingName}/*"]},
      "subscribe": {"operations": ["mqtt:subscribe"], "resources": ["mqtt:topicfilter:commands/+"]}
    },
    "controller": {
      "all": {"operations": ["mqtt:connect", "mqtt:subscribe"],
              "resources": ["mqtt:clientId:${iot:Connection.Thing.ThingName}", "mqtt:topicfilter:sensors/#"]}
    }
  }
}
```

`suifleet --check-config CONFIG_PATH` and startup refuse an unreadable or invalid groups file (unknown fields, another format version, unknown operations, resources without an `mqtt:clientId:`, `mqtt:topic:` or `mqtt:topicfilter:` prefix, selection rules other than `thingName:` terms with `*` only at the start or end, a group naming a missing policy), a non-loopback broker, out-of-range bounds, invalid host names, and `[client_devices]` without IPC administrators.

The broker must listen only on loopback (or otherwise be unreachable from the LAN): a LAN client that reached it directly would bypass authorization. With Mosquitto: `listener 1883 127.0.0.1`, `allow_anonymous false` and a `password_file` holding the gateway's and the bridge's users. The gateway never forwards a device's own username or password; it substitutes `broker_username`/`broker_password_file`.

## Identities and authorization

- **Things.** A thing name is 1-128 of `[A-Za-z0-9:_-]` (AWS thing-name rules). A thing may connect only with its own name as MQTT client ID. The bridge's client ID is reserved: no thing may be enrolled with it, since connecting would take over the bridge's broker session.
- **Authentication.** The TLS handshake requires a client certificate that chains to the gateway's CA. The registry then identifies the exact certificate by its SHA-256 fingerprint: unknown certificates (for example, of a removed thing) are refused, as are those of revoked things, expired ones and ones superseded by a rotation once their grace period ends.
- **Policies.** These follow `aws.greengrass.clientdevices.Auth` 2.5.x: the union of every group whose selection rule matches the thing applies, and anything not granted is denied. `mqtt:connect` pairs with `mqtt:clientId:`, `mqtt:publish` with `mqtt:topic:` and `mqtt:subscribe` with `mqtt:topicfilter:`. `*` is a glob, `+` and `#` in a resource match literally, and `${iot:Connection.Thing.ThingName}` is the connecting thing's name. A statement's `"*"` operation or resource grants every operation or resource.
- **Enforcement.** CONNECT: client ID and, if present, the will topic (as `mqtt:publish`); a refusal is CONNACK 5. MQTT 5 and 3.1 CONNECTs get CONNACK 1 (unacceptable protocol version): the gateway speaks MQTT 3.1.1. SUBSCRIBE: each filter separately; refused filters get return code 0x80, granted ones go to the broker and their codes are merged into one SUBACK. PUBLISH (including retained and QoS 2): MQTT 3.1.1 has no refusal for a publish, so an unauthorized one ends the session without being forwarded. Live sessions are re-checked every second and end when their certificate is revoked, expires or is retired. Packets are limited to 256 KiB.
- **Wills.** When the gateway ends a session for authorization (revocation, expiry, retirement or an unauthorized publish) it first sends the broker DISCONNECT, so the broker discards the will instead of publishing it on the denied client's behalf. A client that simply vanishes gets its (authorized) will published as usual.
- **Persistent sessions.** A broker session opened with clean session 0 keeps the subscriptions granted when it was created. The gateway resumes one only under the device-group policy it was opened with: the first connection after the groups file changes (or after a thing is re-enrolled) gets a clean broker session (CONNACK session present 0), so a client re-subscribes under the new policy.

`client-device-cases.json` (SF-028) runs against the daemon's registry and policy engine in `make edge-test`.

## Certificates

The CA (ECDSA P-256, ten years) is created on first start in `<state_dir>/clientdevices/` (`ca.key` mode 0600 in a 0700 directory, `ca.pem`, `registry.json`). A CA certificate without its key, or the reverse, and an unreadable registry stop startup rather than silently forgetting revocations. Keep the directory with the journal; restoring an older registry would undo revocations.

Issued client certificates carry only what the CA decides: subject `CN=<thing>`, `extendedKeyUsage=clientAuth`, no CA rights and no SANs, whatever the CSR requests. The CSR's self-signature is verified (proof of possession). Supported keys are those rustls accepts for client authentication, such as ECDSA P-256/P-384 and Ed25519.

The gateway's server certificate is issued by the same CA with a fresh in-memory key at each start. Its names are `hostnames` and the addresses the listeners serve: on a wildcard listener, the interface addresses the daemon detects (an `aws.greengrass.clientdevices.IPDetector` equivalent; loopback and link-local only when included). Every 30 seconds the daemon re-detects them and reissues the certificate when they change or two thirds of its lifetime have passed.

### Operator enrollment, revocation and removal

Administrators use the `client_devices` IPC operation ([COMPONENT_IPC.md](COMPONENT_IPC.md)); components cannot be granted it.

```python
from suifleet_ipc import Client
admin = Client('/run/suifleet/ipc.sock')
issued = admin.client_devices('enroll', thing='sensor-1', csr=open('sensor-1.csr').read())
open('sensor-1.crt', 'w').write(issued['certificate']); open('ca.pem', 'w').write(issued['ca'])
admin.client_devices('revoke', thing='sensor-2')     # persistent; ends live sessions within a second
admin.client_devices('remove', thing='sensor-2')     # forget it; its certificates become unknown
admin.client_devices('list')                         # things, certificates, live sessions and denial counters
```

`enroll` takes an optional `validity_seconds` (60 up to `certificate_validity_seconds`). Enrolling an existing thing is refused; a revoked thing must be removed before it can be enrolled again. The device generates its key and CSR itself, for example `openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout sensor-1.key -out sensor-1.csr -subj /CN=sensor-1`; the private key never leaves it.

### Rotation

An authenticated client posts a new CSR to `POST /rotate` (body: the PEM CSR) with its current certificate. It receives `{thing, certificate, ca, sha256, not_after_ms}`: a certificate with the lifetime the thing was enrolled with. The previous certificate stays usable for `rotation_grace_seconds` (its live sessions end then); anything older is dropped at once, and a superseded certificate cannot rotate again. A certificate younger than `min_rotation_interval_seconds` cannot rotate, which bounds registry writes. Rotate once `rotate_after_ms` (two thirds of the lifetime, reported by discovery) has passed. A certificate that expires before it is rotated cannot be renewed: the operator must remove and re-enroll the thing.

## Discovery

- **UDP hint.** A datagram beginning `SUIFLEET-DISCOVER/1`, at least 512 bytes long, sent to `udp_discovery` (for example the subnet's broadcast address) is answered with `{format, service, authenticated: false, gateway, ca_sha256, mqtt, https}`. Replies are never larger than the request and are limited to 20 per second, so the responder cannot amplify spoofed traffic. Nothing in it is trusted: clients use it only to find endpoints, and check `ca_sha256` against the CA they were enrolled with.
- **`GET /discover`** over mutual TLS returns `{document, ca_binding}`. `document` has the gateway's chain, registry, device object ID and address, the thing's name, the CA (`pem`, `sha256`), the `mqtt` and `https` endpoints (`host:port`, hostnames first), the presented certificate's `sha256`, `not_after_ms`, `retire_ms` and `rotate_after_ms`, and `issued_ms`/`expires_ms`. Its authenticity comes from the channel: the server certificate chains to the CA the client holds.
- **CA binding.** `ca_binding` is `{message, signature, signer}`: base64 of the canonical JSON `{schema: 1, kind: "client-device-ca", chain_identifier, registry, device_id, signer, ca_sha256, created_ms}` and its Sui personal-message signature by the device key, made at startup. A client or operator that looks the device up on chain can verify that this CA belongs to that device without another trust root. With the SF-053 signing service, the service signs only exactly this statement for its own chain, registry and key ([KEY_ISOLATION.md](KEY_ISOLATION.md)).
- Refusals are JSON with status 403 (`{"error": "revoked" | "expired" | "superseded" | "unknown"}`), 400 for a refused rotation and 404 for other paths.

## Offline behaviour and caching

- **Gateway.** Authentication, authorization, enrollment, rotation and revocation are local: the CA, registry and policies are on the device, and no chain, Walrus or Internet access is involved. A running gateway keeps serving when the chain is unreachable. Expiry is judged by the device's clock (certificates are back-dated five minutes for skew); a wrong clock can admit or refuse certificates early or late. **Daemon startup still verifies the deployment on chain**, so a gateway that restarts while the chain is unreachable does not start until it is reachable again.
- **Clients.** Keep the key, certificate, CA and the last discovery answer. Reuse cached endpoints while `expires_ms` has not passed and fall back to the UDP hint or configured addresses afterwards; endpoint data never needs to be trusted, because every connection verifies the server certificate against the CA. Rotate after `rotate_after_ms` whenever the gateway is reachable. Without rotation the certificate stops working at `not_after_ms`, offline or not.
- **Revocation** takes effect immediately on the gateway that recorded it, and survives restarts. There is no CRL or OCSP distribution: certificates are only accepted by this gateway, whose registry is the authority.

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | All 16 `interfaces/compat/client-device-cases.json` requests against the registry and policy engine (and through the certificate path). Policy parsing, selection rules, the thing-name policy variable and literal MQTT wildcards. Bounds. Enrollment, CSR proof of possession, refused duplicate/reserved/invalid enrollments, rotation grace, the minimum interval and the dropped older certificate, persistent sessions resuming only under the same policy, revocation persisting across reopen, refusing an unreadable registry or a half-present CA. Issued certificates: client-only, no SANs or CA rights, chaining to the CA under rustls. The device-signed CA binding and what the signing service refuses. The gateway against a loopback broker: no certificate, an untrusted one, a foreign client ID, credentials substituted, partial SUBACK merging, byte-exact forwarding, revocation ending a live session and an unauthorized publish ending the session unforwarded, each with DISCONNECT to the broker, HTTPS discovery and rotation, 403 after revocation, server-certificate names and UDP anti-amplification |
| `make test-client-devices` | The real daemon as root with real component UIDs, the chain simulator and Eclipse Mosquitto 2.0.20 (loopback, password authentication) on an internal Docker network with no Internet route. Clients: the host's Python `ssl` with raw MQTT 3.1.1 (P-256) and `mosquitto_sub` 2.0.20 with OpenSSL in another container (Ed25519); keys and CSRs from `openssl`. UDP broadcast discovery; HTTPS discovery with the server certificate verified for the detected address and `gateway.test`, and the CA binding verified independently (BLAKE2b intent digest, Ed25519 with `openssl`); client-to-client and client-to-component traffic in both directions through the SF-032 bridge; every denial path above; a will published for a vanished client but not for a revoked one; live revocation; rotation, its grace and minimum interval; a persistent session resuming, then restarting clean after a policy change so a no-longer-granted subscription delivers nothing; restart persistence; the chain simulator stopped, then a 60-second certificate's live session ending at expiry. Receipt: [sf035-client-devices.json](validation/sf035-client-devices.json) |

**Not yet covered:**
- physical Pi and second-device LAN acceptance (#32); the tests use Docker networks on one host;
- MQTT 5 clients; the AWS discovery REST API and AWS IoT-issued certificates (`aws_api` remains undecided); `attribute:`-based selection rules;
- CA rotation (a new CA requires re-enrolling every thing) and hardware-protected CA keys. The CA key is as protected as the daemon's state directory: components running as the daemon's user could read it, so give components their own users, as IPC ACLs already require;
- gateway restarts while the chain is unreachable (see above).
