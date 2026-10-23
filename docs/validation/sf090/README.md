# SF-090 bounded encrypted fleet software envelope

The SF-082 prerequisite is merged as `39ea150a19a469c0edbb1b61ad822a80287a6f47`.
This branch integrated actual `origin/main` with merge `ffb1c630e5f69f4003855d79729f6e75f3747cbc`.
The `merged-*.json` receipts identify their executed revisions. Codex actual model
identifier: `gpt-6.1-sol`; no finer runtime build identifier is exposed.

The supported **isolated software regression envelope** is 16 enrolled synthetic
devices, four concurrent device operations, one 1 MiB encrypted artifact per device,
three local Seal test providers with a two-share threshold, one signed health report
and one authoritative application receipt per device. A separate consumer workload
uses 128 real signing identities, concurrency four, three payload sizes (16 KiB,
256 KiB, 1 MiB), and 384 signed/encrypted archives. A real Chromium fixture tests
128-device pagination in five rounds with a 100-device page bound.

These are bounded pilot-sized software experiments, **not authorization to admit a
paid production fleet**. Public provider throughput/quotas, certified storage at fleet
scale, long retention and availability are unestablished. No paid-pilot SLA, public
fleet capacity, invoice or production endpoint promise follows from these numbers.
Final provider/manual-wallet/Mainnet acceptance remains in [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159),
and physical/manual-wallet acceptance in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).

The workloads and guards are versioned in `workload.json`, `isolated-workload.json`
and `public-read-workload.json`: 30-second isolated-stage p95, 5-second consumer
crypto/fetch/health p95, 1-second index/page-recovery p95, 5-second initial browser
render, zero unexpected failures, 1 GiB Node-process peak RSS, and at most three
explicit transient driver attempts. The public sample is eight Testnet effects reads,
concurrency two, 10-second p95, zero writes and zero new gas. RPC, gateway and Seal
paid quotas remain unquoted/null. Candidate operational cadence is hourly health,
daily log archives and three purchased retention epochs; the short tests do not
establish elapsed-hour/day/epoch retention availability. Browser size and Node RSS
are separate from device/kernel/full-browser resource use.

## Integrated and separate evidence

`merged-chain.json` connects real enrollment and gas floats, on-chain device policy,
shipped browser Seal encryption, official SDK signed sessions, verified test-server
identity, encrypted artifact/recipe fetch, authenticated synthetic file application,
signed health anchoring, verified browser-reader visibility and authoritative
receipts on **one disposable chain**. Application digests are independently read
from the deployment and its recipe-digest dynamic field. Repeat selection must not
create the application file again. Applied receipts are submitted only after apply.
The recipe/file oracle is a synthetic-device implementation, not the native updater.

Three local test keyservers simulate the actual Move device approval as the signed
session user before releasing shares. An ungranted key cannot decrypt another
device's release. Two keyserver processes are stopped: threshold failure cannot
apply anything; restarting the same servers must recover. The loopback encrypted
gateway injects HTTP 429 until three driver attempts are exhausted, then recovers.
This tests actual local protocol/authorization, not production keyserver quotas.

The same chain races four independent gas accounts against the one-draw allowance,
and four storage registrations against a pool sized for exactly one blob. Failed
effects must identify the quota/storage check; storage count must remain one.
Upstream Walrus is initialized with a **test-only deterministic one-member committee,
synthetic WAL and rates**. Storage registration/accounting is real upstream code;
certified storage-node throughput is not measured. All transaction gas, including
expected failed effects, is retained; synthetic local gas is not a public price.

`merged-fixtures.json` separately exercises 128 signers and actual SDK encryption,
shipped archive verification and bounded reconnect scheduling; injected RPC,
gateway, keyserver and delayed-index failures exhaust and recover. Foreign-fleet
and digest substitutions fail before keys are requested. Driver concurrency/retry
is not represented as daemon/browser scheduling. `merged-browser.json` exercises
the shipped FleetDataPage in Chromium with fixture chain and wallet hooks, including
page failure/recovery without fabricated or duplicate devices.

`merged-daemon.json` runs four independent native archive/restart drills at concurrency
two: real native signatures/ciphertext, official SDK threshold decrypt and browser
verification, storage exhaustion, certification ambiguity/pruned history, restart,
cadence, diagnostics/crash archives and no duplicate registration. They use separate
simulators and a hashed prebuilt binary; this is not a shared-chain native fleet or
a fresh Rust build. A separate direct `test_fleet_data_ota.py` run also passed.
`merged-public-read.json` freshly verifies eight historical Testnet effects and gas;
zero writes/new spend. It measures neither new enrollment nor provider load.

## Reproduction and scope

Use the pinned dev toolchain and frozen frontend dependencies. Mount this worktree,
its linked Git common directory, frontend dependencies, pinned Move cache, and built
`chain_facts`, `seal_key_server`, `grpc_sim_shim` and `suifleet` binaries. Use disposable
Docker `--network none` for all tests except the explicitly read-only public sample:

```bash
make INSIDE_DEV_CONTAINER=1 test-manifest
make test-tracking
python3 scripts/bootstrap_tracking.py  # offline dry run
make INSIDE_DEV_CONTAINER=1 pilot-fixtures
CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell \
  make INSIDE_DEV_CONTAINER=1 pilot-browser
make INSIDE_DEV_CONTAINER=1 pilot-localnet
python3 scripts/pilot_daemon_soak.py \
  --binary suifleet-edge/target/debug/suifleet \
  --shim suifleet-edge/target/debug/examples/grpc_sim_shim \
  --receipt /tmp/suifleet-pilot/daemon.json
make INSIDE_DEV_CONTAINER=1 pilot-public-read
```

Receipts default to `/tmp/suifleet-pilot`; set `PILOT_EVIDENCE_DIR` for Make targets.
The local runner checks pinned tools, refuses non-loopback interfaces/existing node
ports, creates private temporary chain/wallet state and terminates the node on exit.
The parent holds `build:suifleet` only during actual compilation and renews/releases
it. No shared/public wallet is mounted. Temporary keys/plaintexts are never evidence.
No service installation, production credential, hardware programming or Mainnet
operation occurred. No application ABI, layout or deployment manifest changed.

Exact resumed commands used `suifleet-dev:sf037` (Node 22.22.2, compiler 1.81.1,
network-none node 1.82.1) and `suifleet-dev:sf010-chrome`; image IDs are unchanged
from the original runs recorded below. The linked Git metadata mount was initially
missing: that fixture attempt failed before measurement, then passed with the mount.
The first new integrated setup failed with an arity mismatch (missing registry in
the harness grant call); the harness was corrected to the actual merged ABI and
rerun. Neither failure is counted as passing acceptance or public spending.
The first final-head JavaScript CodeQL run also blocked three predictable receipt
writes. Receipt publication now uses exclusive mode-0600 creation, refusing
intervening symlinks or existing files. Regression checks preserve the symlink
target and existing receipt; no scanner finding was suppressed.

Tracking tests pass 10/10 and the offline bootstrap dry run passes after preserving
both branches' published catalog entries during merge. The merged manifest suite
passes its Python/frontend/claim regressions. Current-head ordinary/security and
automatic Testnet CI and fresh independent review remain mandatory. Production,
manual real-wallet, hardware, multi-day soak, full fleet certified-storage/native
throughput, paid quotas and unsupported scale remain explicitly unrun/unestablished.

The earlier `fixture-run.json`, `isolated-run.json`, `browser-run.json`, `daemon-run.json`
and `public-read-run.json` remain historical premerge facets, not relabeled reruns.
Their original source revisions and numbers remain in `measurements.md` below the
new merged-source table. The old `producer_pin` in the original fixture input identifies
that input's provenance only; the receipt revision identifies executed code.
