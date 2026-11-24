# Virtual fleet rehearsal

The fleet runner starts a disposable Sui localnet, publishes the current Move
package, enrolls independent device signers, runs real edge daemons and checks
their authoritative chain receipts. It serves the fixture recipes and artifacts
over loopback. The virtual installer records the authenticated artifact and
writes a local copy for successful installs. It never executes the artifact.

Build the daemon and gRPC chain reader using the pinned development image, then
run the following inside a disposable Docker container with `--network none`
and the checkout mounted at `/workspace`. Mount a separate private writable
directory to retain results and identities across containers. Use the image's
normal unprivileged user for both commands.

```sh
cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bin suifleet --example chain_facts
python3 scripts/virtual_fleet.py --workspace /fleet --devices 3 --concurrent 2
```

Build with network access if you need to fetch locked Rust and Move
dependencies. Run `sui move build --path contracts` to populate the dev image's
Move cache, then mount that cache at `/home/dev/.move` in the network-none
container. The user inside the container must own the private results mount.
The runner owns the node, loopback blob server, faucet and virtual
daemons. Keep ports 9000 (RPC) and 9123 (faucet) free inside the container
and outside its ephemeral port range. Startup failures include the tail of
the run's `node.log`. The runner stops its processes on completion, failure,
Ctrl-C or SIGTERM. The local genesis, operator wallet and chain database are
disposable. Keep the private
workspace to reuse signer identities. Each invocation enrolls those signers on a
fresh genesis with fresh object IDs and per-run configs, journals, logs and
installer output. Daemon restarts within a run use the same journal and work
directories. Keep the workspace out of source control and share only selected
reports, since the workspace includes device keys.

## Profiles and sizing

```sh
python3 scripts/virtual_fleet.py --plan --devices 20 --concurrent 4 --poll-seconds 5
python3 scripts/virtual_fleet.py --workspace /fleet --profile healthy --devices 4 --concurrent 2
```

Both profiles verify enrollment, live group targeting, a non-member when the
wave has multiple devices, staged rollout deferral, the canary grant, and
post-apply health before the second cohort can proceed. Both gather daemon
heartbeats and inject synthetic component log input through the real daemon log
shipper. The health observations describe the fixture daemon on the test host.
The `faults` profile also kills the canary, publishes an authenticated failing
update while it is offline, waits for a Failed receipt after reconnect, and
restarts it again to check the attempt is not replayed.

The runner accepts 2–32 identities and runs at most 2–8 virtual daemons at once.
A wave uses at most the selected concurrency, so a final partial wave can have
one daemon. Device startup and operator writes are sequential. The real daemon
uses its own retry and poll backoff, and the summary includes the configured
poll and telemetry cadence. The plan estimates steady poll cycles and upper
bounds for intended transactions and operator gas budgets. Poll cycles can
contain multiple RPCs. Budgets reserve local test gas and actual operator effects
are itemized separately. The process RSS fields are spot samples. Increase
device count and concurrency separately, using the report from a small run
before increasing the active set. The runner spends zero public SUI.

Open the run's `report.html` in a browser and follow its `report.json` link
for chain identity, device IDs, installer attempts, deployment status maps,
selected receipt effects digests, observed heartbeat timestamps and resource
samples. Each virtual device's `daemon.log` and `state/state.json` provide
daemon-side evidence. The telemetry sink accepts fixture data on loopback and
does not authenticate it as a production telemetry server. Public network,
mainnet, hardware safety, real-wallet and scale throughput require their own
evidence.

## Mixed-device rehearsal

An operator can pass up to three separately provisioned signer addresses using
`--physical SIGNER_ADDRESS`. Add `--receipt-timeout 600` to allow time for
operator setup. The runner enrolls these devices in the first wave's group,
writes their public bindings to `external-NNN/identity.json` and writes their
localnet manifest to `external-NNN/deployment.json`. The operator supplies the
daemon, its existing independent test key, private state directory and explicitly
selected test-component installer. The operator starts and stops those daemons.

Use an authorized test device with disposable test state and an explicitly
configured harmless updater, such as the SF-030 test component updater. Configure
the daemon to use the exported device/cap IDs, its independent key and
`replay_history = true` for the fresh test genesis, so it can discover the
shared group update if it starts after publication. The identity binding appears
before the runner announces that external devices may connect. This is an
example device config. Replace the IDs with the values in `identity.json` and
supply absolute paths for the independent key and the installed test updater.

```toml
[sui]
manifest_path = "deployment.json"
[device]
keypair_path = "/path/to/private/test-device.key"
device_object_id = "DEVICE_ID"
device_cap_id = "CAP_ID"
[apply]
command = "/bin/sh /path/to/suifleet-test-updater --root /path/to/test-effects"
timeout_seconds = 10
[runtime]
state_dir = "/path/to/private/test-state"
replay_history = true
```

See the [edge configuration and test updater runbook](OPERATIONS.md). A network-none container
needs operator-managed scoped forwarding for the printed Sui RPC and blob
endpoints to the device's loopback at the same ports. Use a trusted point-to-point
tunnel into the container's network namespace. Leave the runner's faucet and
all other node ports private. Remove the forwards and stop external daemons when
the run ends.

The first group update checks authoritative receipts from both kinds of devices.
The report labels external receipts separately and leaves physical installation
and reboot evidence unobserved. Record host-side installer and platform evidence
separately when that testing is authorized. Physical and manual acceptance remain
tracked in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32), with
production-provider and mainnet acceptance in
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159). Public-testnet
acceptance uses the existing [testnet workflow and wallet controls](TESTNET.md).
