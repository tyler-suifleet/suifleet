# Wallet-to-edge acceptance harness

SF-065 implements the runner for [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32): `scripts/edge_acceptance.py`. It drives one acceptance run from the wallet through Sui and Walrus to a device running the real daemon and the [SF-030 test component](../tests/components/suifleet-test-app/README.md), and judges each case from on-chain receipts and the device's version/run-ID receipts.

The harness is tested without a Raspberry Pi. **It does not establish SF-031.** Physical Pi, real extension-wallet and SF-022 encrypted-delivery acceptance stay open in #32 until a real run records them. A run that lacks any of them ends `incomplete`, never `passed`.

## Commands

```bash
python3 scripts/edge_acceptance.py plan      --profile PROFILE.json   # offline: every case and what this mode leaves open
python3 scripts/edge_acceptance.py preflight --profile PROFILE.json   # read-only checks
python3 scripts/edge_acceptance.py run       --profile PROFILE.json --evidence OUT.json
python3 scripts/edge_acceptance.py summarize RUN1.json RUN2.json      # the combined SF-031 matrix
```

- **Hardware runs** (`device.mode = "ssh"`) also need `--hardware` and a lock identity: `--agent Claude --model MODEL --session SESSION [--ticket SF-031]`.
- **Testnet runs** need the same lock identity, or an existing `network:suifleet-testnet` lock token in `SUIFLEET_TESTNET_LOCK_TOKEN`.
- **`make edge-acceptance PROFILE=… EVIDENCE=… [HARDWARE=1] [LOCK_ARGS="--agent … --model … --session …"]`** runs on the host. It needs `sui`, `ssh` and the `ticket` CLI on `PATH`.

| Exit code | Meaning |
|---|---|
| 0 | Verdict `passed` (every baseline case passed). With `--allow-incomplete`: nothing failed or was blocked at run time. |
| 1 | A case failed. |
| 2 | The profile was refused. Nothing ran. |
| 3 | Verdict `incomplete`: some cases were skipped or blocked. |

## Profiles

A profile is a JSON file. Relative paths resolve against the profile's directory. Templates are in [`tests/acceptance/profiles/`](../tests/acceptance/profiles/). The harness discovers nothing: there is no default SSH host, wallet or disk. These rules are enforced before anything runs:

- **Networks.** `profile` is `localnet` or `testnet`. Mainnet and every other network are refused.
  - The manifest must pass `deployment_manifest.validate`, name the same network and, on testnet, chain `4c78adac`.
  - A localnet profile needs a loopback node and, if `faucet_url` is set, a loopback faucet.
- **Wallet.** `wallet.client_config` is always explicit, never the default wallet. `wallet.address` must be that config's active address; preflight checks it and the active environment's chain.
  - On testnet, the address must be one of the two dedicated accounts in [TESTNET.md](TESTNET.md), and `budget.max_spend_mist` is required (at most 0.5 SUI).
  - Spending uses the [non-replenishing allowance](../scripts/testnet_budget.py). Device gas counts as a grant.
- **No secrets.** A field named like a key, token, password or recovery phrase is refused, as is any value that looks like a Sui private key.
- **Walrus.**
  - `stand-in` serves this run's blobs over HTTP. A local device needs a loopback bind. A Pi needs this host's explicit LAN address; loopback and `0.0.0.0` are refused.
  - `http` names a real publisher and aggregator. Both must equal the manifest's endpoints.
- **Device.**
  - `local` runs the real daemon on this host with a throwaway key. It is a stand-in, never hardware evidence.
  - `ssh` names the Pi explicitly: `host`, `user`, `identity_file`, and `host_key_sha256` (the `SHA256:` fingerprint, verified on the Pi's console). Wildcards, `auto`, loopback and `user@host` forms are refused. The key and state paths must be the SF-029 image's: `/etc/suifleet/device.key` and `/var/lib/suifleet`.
- **`enrollment`** (optional) reuses an existing device, cap and owner cap instead of registering a new device. The harness verifies their owners on chain.
- **`artifact_profile`** is `public`, the baseline. `sf022-encrypted` is accepted but blocks the whole run without executing anything, until SF-022 lands.

## What a run does

1. **Preflight.** Checks the node's chain against the manifest and verifies the manifest's package, type origins, UpgradeCap and registry over gRPC. Then checks the wallet's active address and chain, funding, and the device's facts.
   - A hardware run also checks the image: `aarch64`, the packaged daemon hash (against `expected_binary_sha256` when given), the test updater, key mode `600`, first-boot provisioning, clock sync and `build-info`.
2. **Artifacts.** Builds deterministic v1/v2 artifacts plus failure variants with a fresh run ID. It uploads each artifact and recipe and reads them back by SHA-256.
3. **Enrollment.**
   - Registers the device's address, issues its cap and activates it: through the scripted account, or the operator's extension wallet. It verifies the cap is owned by the device and the owner cap by the wallet.
   - Creates a group, and a decoy device on scripted runs.
   - Creates a deployment **before** enrollment, then installs the configuration and starts the enrolled daemon before any test deployment. On a Pi this installs `deployment.json`, `signer.toml`, then `config.toml` last, each root-owned and mode 0640. A device already enrolled with other IDs is not reconfigured unless `device.allow_replace_enrollment` is set.
4. **Cases.** See the matrix below. Each outcome is judged from both the device journal and the deployment's authoritative on-chain receipt, read with the Sui CLI independently of the daemon. A device receipt must name this run ID and the artifact SHA-256.
5. **Cleanup.** Deletes this run's deployments, group and decoy (storage rebate). The device identity, key and journal stay. If cleanup cannot finish, the evidence lists `leftover_objects`.

**Evidence.** The file is rewritten after every transaction, so an interrupted run still names what it created. It records tool versions, chain, operator, device facts, enrollment IDs, artifacts and blob IDs, and every transaction's digest, status and gas. It also holds a redacted device-log tail. Evidence containing a private key, or the stand-in's key, is refused.

**Key and journal safety.**
- On a Pi the harness only reads a digest of the device key, to prove it is unchanged.
- It never copies or replaces the key, deletes a journal or flashes anything. It runs only fixed, shell-quoted commands, over SSH with `StrictHostKeyChecking=yes` and a known-hosts file holding only the pinned key.

**Locks.**
- `hardware:raspberrypi3bplus` is taken only for a hardware preflight or run. It is asserted before each device command, renewed every four minutes and released at the end.
- `network:suifleet-testnet` is asserted before every testnet transaction. A run refuses to mutate if that assertion fails.

## Acceptance matrix

| Case | Needs | Checks |
|---|---|---|
| `preflight` | | chain, manifest, account, funding, device facts |
| `image-identity` | Pi | SF-029 image, packaged hashes, first-boot identity, key mode |
| `artifacts` | | uploads read back by SHA-256 |
| `enrollment` | | wallet registration, verified owners, daemon enrolled before test deployments |
| `baseline-skip` | | a deployment created before enrollment never runs (needs fresh device state) |
| `direct-v1`, `group-v2` | | Applied on chain; device receipt v1/v2 with this run ID and artifact hash |
| `report-retry` | scripted | an unfunded device applies v1 but cannot report; the report succeeds once gas arrives |
| `updater-install-failure`, `updater-health-failure` | scripted | Failed on chain; receipts distinguish install from health failure; previous version kept |
| `recipe-tamper`, `artifact-tamper` | scripted | bytes not matching the authenticated digest: Failed, updater never invoked |
| `unauthorized-target` | scripted | a deployment for another device never runs here |
| `offline-catch-up` | scripted | a deployment created while the daemon is stopped is applied after it starts |
| `daemon-restart` | | journal kept, no applied work replayed |
| `key-journal-preserved` | | key digest, address and journal unchanged |
| `cleanup` | | test-owned objects deleted, device kept |
| `extension-wallet` | extension | operator-signed transactions verified on chain |
| `encrypted-delivery` | SF-022 | always blocked until SF-022 is implemented |
| `supervised-lifecycle`, `component-configuration`, `desired-state`, `daemon-self-update`, `platform-restart-recovery` | growth | listed but not yet exercised; add a case as acceptance grows |

Mode gaps are explicit:
- A local stand-in skips `image-identity`.
- A scripted wallet leaves `extension-wallet` blocked.
- An extension-wallet run skips the deliberately invalid deployments, because the dApp computes digests correctly.
- `encrypted-delivery` stays blocked.

So SF-031 needs at least two hardware runs on the same Pi: one extension-wallet run and one scripted run. `summarize` combines them. A case counts as `passed` only from a hardware run; stand-in passes show as `local-only`. The SF-031 verdict stays `incomplete` while SF-022 is missing.

### Extension-wallet runs

The operator performs each step in the dApp at `wallet.dapp_url`: register the address, create a group, add the device, then deploy each version. The harness prints exact values: address, version, recipe blob, artifact and recipe SHA-256.

The operator enters each transaction digest, on stdin or through `--wallet-input FILE`. The harness then verifies:
- the transaction succeeded;
- it was signed by `wallet.address`;
- it created the expected objects;
- each deployment carries this run's version, recipe and artifact.

A missing digest blocks the case.

Device report gas comes from the scripted `client_config` account; that transaction is labelled `scripted` in the evidence. On testnet the extension must hold a dedicated account; another account needs explicit user authorization. The harness verifies digests with `sui client tx-block`; that path is exercised only by offline tests so far.

## Tests

- **`make test-edge-acceptance`** (offline, in `make ci`/`make check`): `scripts/tests/test_edge_acceptance.py` covers:
  - profile refusals and the matrix and verdicts;
  - lock acquire/assert/renew/release and refusal;
  - SSH host-key pinning and mismatch refusal, command quoting, and the enrollment install order;
  - evidence redaction, and checkpointed evidence after an interrupted run;
  - full runs against a fake chain and a fake daemon that executes the real SF-030 updater.

  Injected faults include tampered input that runs, failures reported as Applied, untargeted or pre-enrollment work that runs, reports without gas, replay after restart, a changed key, failed effects, wrong chain or account, an exhausted allowance, a foreign enrollment, missing or wrongly signed extension digests and the encrypted profile. Each must fail or block; none may pass.
- **`make test-edge-acceptance-localnet`** (disposable localnet, started first as for the other `*-localnet` targets): `scripts/test_edge_acceptance_localnet.py` publishes the actual contracts and runs the harness three times:
  - a fresh enrollment, where every case a stand-in can establish must pass and the verdict must stay `incomplete`;
  - a run reusing that enrollment, key and journal, with no replay;
  - the encrypted profile, blocked with no transaction sent.

## Recorded runs (2026-10-16, Claude claude-opus-5-5)

Local and public-testnet runs used the local stand-in device. They are not Pi, extension-wallet or public-Walrus evidence.

- **Localnet** (network-none container, Sui 1.81.1): `test_edge_acceptance_localnet.py` passed all three phases. All 16 baseline cases a stand-in and scripted wallet can establish passed, including report retry, both tamper paths, offline catch-up and restart. `image-identity` was skipped; `extension-wallet` and `encrypted-delivery` were blocked. Evidence: [validation/sf065-localnet.json](validation/sf065-localnet.json).
- **Public testnet** (chain `4c78adac`, dedicated agent account, `network:suifleet-testnet` held, loopback Walrus stand-in):
  - **Fixture.** The current contracts (content hash `0329a8409243c63eff18cea9`) were published as package `0x3950bbbc…b634` by the live gate. It costs 0.185 SUI of package storage and is cached for reuse. The gate's on-chain checks passed, but its final Node effects proof (`testnet_receipt_effects.cjs`) exited 1 on this host, so that gate run is not a full pass.
  - **Harness run.** The run reused the stand-in enrollment from an attempt interrupted by a full host disk; that attempt's leftovers were deleted in transaction `Bqt7ugyEUh1zFpoTw2ep7Z38jbf8SbsWWWbw2hZYwtuH`. It passed preflight, artifacts, enrollment, report-retry, direct-v1, group-v2, both updater failures, both tamper cases, unauthorized-target, offline catch-up, restart, key/journal preservation and cleanup. `baseline-skip` was skipped because of the existing cursor.
  - **Cost and evidence.** All 11 transactions succeeded. The net balance change was 0.040 SUI, including 0.030 SUI of report gas granted to the stand-in device. Evidence: [validation/sf065-testnet.json](validation/sf065-testnet.json).
