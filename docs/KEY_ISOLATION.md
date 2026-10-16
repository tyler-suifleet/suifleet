# Device key isolation (SF-053)

Component lifecycle commands run as the daemon user, `suifleet`: install, run, shutdown, readiness, recovery, and the direct `apply.command`. File permissions under that same UID cannot protect a key the daemon itself reads. So the device key is held by a separate signing service under its own user, and the daemon only asks it for signatures.

## Design

| Piece | Runs as | Holds |
|---|---|---|
| `suifleet-signer.service` (`suifleet --signer-service /etc/suifleet/signer.toml`), socket-activated by `suifleet-signer.socket` | `suifleet-signer` | the device key (`/etc/suifleet/device.key`, 0600) or the PKCS#11 PIN; `signer.toml` (root:suifleet-signer 0640) |
| `suifleet.service`, the daemon | `suifleet` | no key: `[device.signer] kind = "service"` |
| component processes | `suifleet` (daemon children, same sandbox) | nothing of the signer's |
| `suifleet-provision`, `suifleet-firmware` | root | trusted platform boundary |

The service is not a signing oracle. It decodes every request itself, independently of the daemon's own verify-before-sign, and refuses everything except two intents:

- **Status reports** must meet all of these:
  - exactly one call to `deployment_manager::report_verified_status` or `report_verified_status_for_group`;
  - on the manifest's package, with the manifest's registry and this device's cap;
  - status Applied or Failed;
  - sent and paid by the device, within `max_gas_budget` (default 0.05 SUI).

  Signed only for the daemon's main process. With `caller_unit = "suifleet.service"`, the caller must be a direct child of PID 1 inside that unit's cgroup. Component processes share the cgroup but are descendants of the daemon, which is their child subreaper, and `ProtectControlGroups` keeps them from leaving it. So they never qualify while the daemon lives.
- **Enrollment** (SF-021): exactly `enrollment::enroll` with the manifest's registry, sponsored by another address within the enrollment gas ceiling. Signed only for root, the provisioning operator.

Any process may read the device's public address. There is no export, arbitrary-bytes, personal-message or session operation.

## Migration

The shipped provisioning unit runs before the daemon on every boot. When the `suifleet-signer` user exists, it hands an existing `device.key` to that user unchanged, so the device address, registry identity and journals stay the same. Then:

1. Install `signer.toml`, using `signer.toml.example`: the manifest, device cap, key path or `[pkcs11]`, and `caller_unit`.
2. Switch the daemon's `config.toml` to `[device.signer] kind = "service"`.

Fleet and admin keys, recovery phrases and provisioned credentials never belong in an image. Per-device software keys are still generated at first boot.

## PKCS#11

`signer.toml` can name a `[pkcs11]` token instead of a key file (build with `--features pkcs11`). The PIN file then belongs to `suifleet-signer` (0600). Know what this does and doesn't buy:

- **Non-extractable keys** resist key extraction even from the signer user. The token must be provisioned with `CKA_EXTRACTABLE=false` and `CKA_SENSITIVE=true`.
- **Unauthorized signing** is what this document's boundary controls: who may ask, and for what. A token does not prevent its authorized user from signing.
- **Software tokens** (SoftHSM2) prove the integration, not hardware protection. TPM or secure-element acceptance needs its own physical evidence. A device-node token needs a drop-in that relaxes `PrivateDevices` (see the unit).

## Boundary and limits

Protected against component processes:
- reading the key, `signer.toml` or the PIN (EACCES);
- seeing the signer's processes (the daemon's `ProtectProc=invisible`, and a different UID);
- inheriting key material through environment variables (cleared) or file descriptors (only stdin, stdout and stderr are passed);
- getting any signature from the socket.

Not protected:
- **Root, and the trusted platform scripts:** root and anything running as root.
- **A compromised daemon:** it can request status reports, which are bounded to this device's reports, but not arbitrary signatures, and it cannot read the key.
- **Components influencing the daemon:** components still run as the daemon's user and can interfere with it, for example by writing its state directory or reading its environment, which holds no key. Component-level user separation is future work.

Reusable session credentials, such as Seal sessions for SF-022, must get their own explicit intent and validation in this service. They must not be handed to the daemon.

## Evidence

- **Rust unit tests:**
  - the report policy accepts direct and group reports, and rejects 12 forged variants: another sender or gas payer, an over-budget transaction, another package, an unverified report, another function or module, another registry or cap, status 3, two commands, and malformed arguments;
  - garbage bytes and trailing bytes are rejected;
  - the caller rules hold: the main process versus a component, another unit, no running unit, address requests, unknown operations;
  - a socket round trip through the daemon-side client.
- **`make test-key-isolation-systemd`** (host, needs Docker) runs the shipped units on real systemd 252 and cgroup v2, with the simulator's gRPC shim verifying every signature with Mysten's `sui-crypto`. It checks:
  - migration of an old-layout key with the address unchanged;
  - a supervised component probe in the daemon's sandbox failing to read the key or config, see the signer, or find key material in its environment or descriptors, and being refused a signature while it can still read the public address;
  - the daemon's reports signed and accepted, including after restarts of the signer and the daemon.

  The recorded run is [validation/sf053-systemd.json](validation/sf053-systemd.json).

Not covered here: physical Pi acceptance and hardware-token protection (SF-031). Live-chain checks need their own authorization.
