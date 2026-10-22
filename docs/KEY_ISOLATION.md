# Device key isolation (SF-053)

Component lifecycle commands run as the daemon user, `suifleet`: install, run, shutdown, readiness, recovery, and the direct `apply.command`. File permissions under that same UID cannot protect a key the daemon itself reads. So the device key is held by a separate signing service under its own user, and the daemon only asks it for signatures.

## Design

| Piece | Runs as | Holds |
|---|---|---|
| `suifleet-signer.service` (`suifleet --signer-service /etc/suifleet/signer.toml`), socket-activated by `suifleet-signer.socket` | `suifleet-signer` | the device key (`/etc/suifleet/device.key`, 0600) or the PKCS#11 PIN; `signer.toml` (root:suifleet-signer 0640) |
| `suifleet.service`, the daemon | `suifleet` | no key: `[device.signer] kind = "service"` |
| component processes | `suifleet` (daemon children, same sandbox) | nothing of the signer's |
| `suifleet-provision`, `suifleet-firmware` | root | trusted platform boundary |

The service is not a signing oracle. It decodes every request itself, independently of the daemon's own verify-before-sign, and refuses everything except three intents:

- **Status reports** must meet all of these:
  - exactly one call to `deployment_manager::report_verified_status` or `report_verified_status_for_group`;
  - on the manifest's package, with the manifest's registry and this device's cap;
  - status Applied or Failed;
  - sent and paid by the device, within `max_gas_budget` (default 0.05 SUI).

  Signed only for the daemon's main process. With `caller_unit = "suifleet.service"`, the caller must be exactly the process systemd started as that unit's main process:
  - When the daemon is ready, the unit's root `ExecStartPost=+/usr/bin/suifleet --record-main-process` writes systemd's `$MAINPID` and that process's start time to `/run/suifleet-signer/daemon.main` (root-owned directory, 0644). It runs the read-only base binary, never a daemon update.
  - It records only a main process running that same base binary (`/proc/PID/exe` has the recorder's device and inode). Any other main process gets no record, and an earlier record is removed, so the service trusts nobody. The unit keeps running.
  - Daemon self-update (`[daemon_update]`, SF-026) cannot be combined with `kind = "service"`; the daemon refuses that configuration. The launcher execs whatever its install directory names, and that directory belongs to the daemon user, which components share. So a component could otherwise make its own executable the unit's main process.
  - A request qualifies only if the peer's PID and start time match the record and the peer is in the unit's cgroup. The service pins the peer with `SO_PEERPIDFD` (Linux 6.5+) and confirms it is still that live process after reading `/proc`, so a reused PID never qualifies.
  - Parent PID is not identity. A component that kills the daemon is reparented to PID 1 inside the unit's cgroup until systemd removes it; it is still not the recorded process. Components cannot change the main PID either (`NotifyAccess=main`).
  - Until the record is written, just after the daemon is ready, a report is refused and stays queued for the next poll.
- **Enrollment** (SF-021): exactly `enrollment::enroll` with the manifest's registry, sponsored by another address within the enrollment gas ceiling. Signed only for root, the provisioning operator.
- **Telemetry** (SF-019, [TELEMETRY.md](TELEMETRY.md)): a Sui personal message, never valid as a transaction, that is exactly the canonical JSON of one telemetry snapshot: known schema and fields only, at most 64 KiB, the manifest's chain identifier and registry, and this key as signer. Signed only for the daemon's main process, by the same rule as status reports. Its health content is the daemon's own report: a compromised daemon could misreport its own device's health, as it could misreport a deployment status, but cannot sign for another chain, registry or key.
- **Seal key requests** (SF-022, only when `signer.toml` has `[seal]`): only for the daemon's main process.
  - The service creates an ephemeral session key, certifies it with the device key as a Sui personal message for the fleet package's Seal namespace, and keeps it in memory.
  - It signs a key request only when its transaction is solely `seal_policy::seal_approve*` calls on the fleet package, with the identity first and read-only shared objects, and the response key pair is consistent.
  - The daemon receives the public certificate and the request signature, never the session or device key. See [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md).

- **Client-device CA binding** (SF-035, [CLIENT_DEVICES.md](CLIENT_DEVICES.md#discovery)): a Sui personal message that is exactly the canonical JSON `{schema: 1, kind: "client-device-ca", ...}` naming the manifest's chain and registry, this key as signer, a device ID and a SHA-256 CA fingerprint; at most 1 KiB. Signed only for the daemon's main process, by the same rule as telemetry. A compromised daemon could bind its own device to another CA, as it already controls that device's gateway, but cannot sign for another chain, registry or key.

- **Gas draws** (SF-083, only when `signer.toml` has `[budget]`, see [FLEET_BUDGET.md](FLEET_BUDGET.md)): exactly one `fleet_budget::draw_gas` on the manifest's package, with the configured budget as an immutable input (draws never write the fleet budget), a shared gas account (the contract binds it to this device and budget), the manifest's registry, this device's cap and the Clock, for at most `max_draw_mist`, sent and paid by the device within `max_gas_budget`. Signed only for the daemon's main process, by the same rule as status reports. The contract bounds draws again (float cap, period allowance, rate, pause, suspension, revocation), so a compromised daemon can take at most what the device itself is allowed to draw.

Any process may read the device's public address. There is no export, arbitrary-bytes or arbitrary personal-message operation; the only personal messages signed are telemetry snapshots, client-device CA bindings and the Seal session certificate above.

## Migration

The key's owner follows the daemon's configuration (`suifleet --key-holder /etc/suifleet/config.toml`). The shipped provisioning script sets it at boot and, as the daemon unit's root `ExecStartPre=+`, before every daemon start:
- `suifleet-signer` when `config.toml` uses `[device.signer] kind = "service"`, or, with no `config.toml` yet, when `signer.toml` is installed;
- otherwise `suifleet`, so a package update on a device whose `config.toml` still names `keypair_path` keeps working unchanged. Reverting a configuration also returns the key.

An unparsable `config.toml` stops provisioning instead of moving the key. The key itself never changes, so the device address, registry identity and journals stay the same. To migrate:

1. Install `signer.toml`, using `signer.toml.example`: the manifest, device cap, key path or `[pkcs11]`, and `caller_unit`.
2. Switch the daemon's `config.toml` to `[device.signer] kind = "service"`, and disable `[daemon_update]` if set.
3. Restart `suifleet.service`; no reboot is needed.

Fleet and admin keys, recovery phrases and provisioned credentials never belong in an image. Per-device software keys are still generated at first boot.

## PKCS#11

`signer.toml` can name a `[pkcs11]` token instead of a key file (build with `--features pkcs11`). The PIN file then belongs to `suifleet-signer` (0600). Know what this does and doesn't buy:

- **Non-extractable keys** resist key extraction even from the signer user. The token must be provisioned with `CKA_EXTRACTABLE=false` and `CKA_SENSITIVE=true`.
- **Unauthorized signing** is what this document's boundary controls: who may ask, and for what. A token does not prevent its authorized user from signing.
- **Software tokens** (SoftHSM2) prove the integration, not hardware protection. TPM or secure-element acceptance needs its own physical evidence. A device-node token needs a drop-in that relaxes `PrivateDevices` (see the unit).

## Boundary and limits

Protected against component processes:
- reading the key, `signer.toml` or the PIN (EACCES);
- ptracing the daemon, reading its `/proc` entries or taking its descriptors with `pidfd_getfd` (with `kind = "service"` the daemon makes itself non-dumpable; components regain the default at `execve`);
- seeing the signer's processes (the daemon's `ProtectProc=invisible`, and a different UID);
- inheriting key material through environment variables (cleared) or file descriptors (only stdin, stdout and stderr are passed);
- getting any signature from the socket.

Not protected:
- **Root, and the trusted platform scripts:** root and anything running as root.
- **A compromised daemon:** it can request status reports, which are bounded to this device's reports, but not arbitrary signatures, and it cannot read the key.
- **Components influencing the daemon:** components still run as the daemon's user and can interfere with it, for example by killing it or writing its state directory. Killing it gains no signature. Writing the daemon-update launcher state gains none either: the launcher ignores it with the service signer, and the recorder would not record another executable (see above). Component-level user separation, which would let self-update and the service signer coexist, is future work.

Seal sessions (SF-022) have their own intent and validation in this service, and are not handed to the daemon. Any further reusable credential needs the same treatment.

## Evidence

- **Rust unit tests:**
  - the report policy accepts direct and group reports, and rejects 12 forged variants: another sender or gas payer, an over-budget transaction, another package, an unverified report, another function or module, another registry or cap, status 3, two commands, and malformed arguments;
  - garbage bytes and trailing bytes are rejected;
  - the caller rules hold: the recorded main process versus an orphaned component in the unit's cgroup, a reused PID (other start time), another unit, no record, an exited peer, address requests, unknown operations;
  - the recorder writes PID and start time (0644) only for a process running the base binary, removes an earlier record otherwise, and a pidfd stops resolving once its process exits;
  - `[daemon_update]` with `kind = "service"` is refused;
  - the key holder follows each configuration case (none, `signer.toml` only, legacy `keypair_path`, PKCS#11, service, unparsable);
  - telemetry: a canonical snapshot is accepted; another chain, registry, signer or schema, trailing or unknown fields, an oversized message, a transaction and garbage are refused; only the daemon's main process gets a signature, and a full-size snapshot fits in one request;
  - a socket round trip through the daemon-side client.
- **`make test-key-isolation-systemd`** (host, needs Docker) runs the shipped units on real systemd 252 and cgroup v2, with the simulator's gRPC shim verifying every signature with Mysten's `sui-crypto`. It checks:
  - a package update with an unchanged legacy `config.toml` and no `signer.toml`: the key stays with the daemon user, which keeps reporting;
  - migration by installing `signer.toml`, switching `config.toml` and restarting the daemon (no reboot): the same key moves to `suifleet-signer`, with address and journal unchanged;
  - a supervised component probe in the daemon's sandbox failing to read the key or config, see the signer, read the daemon's environ, take the daemon's descriptors, or find key material in its environment or descriptors, and being refused a report or telemetry signature while it can still read the public address;
  - the daemon's reports signed and accepted, including after restarts of the signer and the daemon;
  - a component that SIGKILLs the daemon and, as an orphan with parent PID 1 in the unit's cgroup (verified as root), sends 8 signing requests: all refused. The shipped `KillMode=mixed` removes such orphans within milliseconds, so the test holds the orphan alive with a test-only drop-in (`KillMode=process`, `Restart=no`) and then restores the shipped unit; the restarted daemon reports again;
  - component-written launcher state naming the component's own executable: ignored, and the real binary refuses `[daemon_update]` with the service signer. A test drop-in then runs that executable, a full daemon, as the unit's main process (what an obeyed launcher would start). It is not recorded, and its otherwise valid report request is refused. The queued report is signed only once the base daemon runs again.

  The recorded run is [validation/sf053-systemd.json](validation/sf053-systemd.json).

Not covered here: physical Pi acceptance and hardware-token protection (SF-031). Live-chain checks need their own authorization.
