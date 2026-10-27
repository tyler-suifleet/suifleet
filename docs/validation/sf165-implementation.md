# SF-165 implementation evidence

Codex running model/version: **gpt-6.1-sol**. Session:
`dispatch-371f0829aac544ffaa8408af51f62087`; branch
`work/suifleet/sf-165-bfc626f9`. Existing issue: [#331](https://github.com/tyler-suifleet/suifleet/issues/331).
No contract layout, interface schema, dependency or migration changed.

## Local verification (2026-10-27)

The pinned `suifleet-dev:latest` image reports Node 22.22.2 and pnpm 11.0.8.
The worktree and existing pnpm content store were mounted at their identical host
paths so pnpm's dependency verification did not attempt an unnecessary reinstall.
Container networking was disabled for the application build/offline suite.

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:$PWD" -w "$PWD" \
  -v /home/tyler/.local/share/pnpm/store:/home/tyler/.local/share/pnpm/store:ro \
  -e INSIDE_DEV_CONTAINER=1 suifleet-dev:latest make dapp-typecheck

docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:$PWD" -w "$PWD" \
  -v /home/tyler/.local/share/pnpm/store:/home/tyler/.local/share/pnpm/store:ro \
  -e INSIDE_DEV_CONTAINER=1 suifleet-dev:latest \
  bash -c 'cd dapp && pnpm typecheck && cd .. && make dapp-build'

node scripts/test_release_storage.cjs
node scripts/test_seal_frontend.cjs
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s scripts/tests -p test_live_release_storage.py
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_components_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_upload_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
```

All commands passed. `make dapp-typecheck` includes its offline testnet/controller,
manifest, signing, privacy/storage, data/browser and enrollment regressions; the
new signing-policy unittest reports 3 passing tests. The final production build
exports all pages successfully. The Components browser test changes the period
to 12 days and verifies every upload request receives 12 epochs. Upload browser
coverage includes explicit file/price review before upload, real progress events,
duplicate clicks, failed uploads, cancellation/retry and confirmed success. The
Wallet Standard fixture covers real provider/connect and application transaction
hooks with fixture effects, including rejection, failed effects and wrong chains.
Seal inspection tests use real encrypted envelopes for namespace/provider/AAD and
expiry negatives, an isolated key-server response for successful policy inspection,
digest verification, plaintext-byte wiping and bounded downloads. Catalog binding
tests cover ordinary/sharded introduction origins and foreign-value rejection.

Earlier attempts identified and repaired the probe's expanded return-value
expectation and the wallet harness's missing `process.env` browser shim. A first
container mount at `/workspace` mismatched the host-installed pnpm store; using
identical paths resolved that environment failure. Initial host dependency setup
used Node 24.21.0/pnpm 12.14.0, followed by frozen/offline pnpm 11.0.8 verification;
final application typecheck/build used the pinned image. No lockfile changed.
The build emits its existing Tailwind configuration module-type warning.

## Dedicated public Testnet

[The sanitized receipt](sf165-testnet.json) records actual chain `4c78adac`, the
dedicated agent account, protocol 139, checksum-pinned 1.82.1 transaction CLI and
pinned 1.81.1 release compiler. The run held/asserted/renewed
`network:suifleet-testnet`, coordinated in SF-010/#11 and released the lock after
mutations. It reused the **same** non-replenishing 150,000,000 MIST allowance across
harness repairs; no faucet, new wallet or package publication was used.

The reproducible runner is `scripts/live_release_storage.py WALLET EVIDENCE_JSON
BUILD_SUI NETWORK_SUI`, with `SUIFLEET_TESTNET_LOCK_TOKEN` supplied privately. It
uses the dedicated `client-host.yaml` path alias pointing at the same agent
keystore as the authorized `client.yaml` (which names `/w/agent` inside containers).
Never print or publish the lock token or keystore. The runner executes actual
dApp upload/catalog/extension builders, verifies bytes before CLI signing and
checks successful effects, current ownership and artifact readback. Its checkpoint
files contain no keys; unknown submissions require explicit reconciliation.

- Chosen period: 12 days / 12 Walrus epochs, ending at epoch 575.
- Version publication: `DtSL8jMPgDt1x6Jrs6o6qkmcYktmWS2D2RPFRbnudhEA`.
- Wallet extension: `8gLwNu9nLhDHzw4NCEvrcGxhSFT1HuGyNWE7Sf38L3N7`, ending at epoch 577.
- Independent effects: extension charged **375,858 FROST (0.000375858 WAL)** and
  **1,011,478 MIST** gas. Entire run used **12,513,030 MIST (0.01251303 SUI)**,
  including the 1,000,000 MIST swap input. The swap received 1,000,000 FROST WAL.
- Successful artifact/recipe storage was sponsored by the public publisher:
  5,262,012 FROST reported storage cost, **not a charge to the wallet**.
- Four submitted successful transactions, plus one verifier refusal before
  signing (no gas). The receipt preserves the failed harness attempts, including
  one earlier sponsored artifact left with finite retention; they are not hidden
  by a reset allowance. A resumed run reused the known uploads/catalog/version.

Only a new disposable catalog/version and new non-deletable test blobs were
created. Immutable versions and finite-lived blobs remain as acceptance evidence;
original application objects were preserved. No firmware/OS action was performed.

## Handoff and limits

Hosted ordinary/security CI and automatic Testnet checks are pending on the pushed
head; they were not watched or polled. Pending checks are not approval. The PR
coordinator must obtain exact-head/current-base checks and independent review.
All build and network resource leases were released before Review.

Expiry dates are projections from the current Walrus epoch schedule. Artifact-byte
prices exclude explicitly labeled recipe/encryption overhead and transaction gas;
public sponsorship is not a promise of paid production service. Confidential
artifact references require explicit account-policy-authorized inspection. Old
records without an authenticated recipe digest and foreign-owned storage remain
unknown/unrenewable in the current account. Extending retention never establishes
deployment, installation, boot health or production durability.

Manual real-wallet and production-provider acceptance, physical target testing and
actual Mainnet acceptance remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), under the
user-authorized implementation split. This work provisions no production
credentials and authorizes no physical programming or Mainnet writes.

## PR #356 feedback repair (2026-10-27)

The independent review found that direct `extend_blob` could move a retained
blob's end past the start of its prepaid renewal resource. The repair preserves
that boundary: Library/Deployments flag attached renewal policies, hide direct
extension and direct the operator to the Network budget renewal controls. The
fresh pre-signing read rejects those policies too. Every fleet release extension
PTB now reads `renewal` and consumes it with `option::destroy_none` before the
extension, so a concurrently attached policy aborts atomically before payment.
The Renewal introduction origin is resolved from the current package; failure
stops signing. No contract source, ABI or layout changed.

Retained publish/import/Deploy uploads whose chosen period exceeds the pool end
are refused before registration, with guidance to refill the pool or choose
wallet-paid storage. The pool read can become stale; the atomic renewal guard
still prevents stranding capacity if an unexpected post-upload extension is
needed. Such a failed upload may leave a certified finite-lived blob, but does
not register the version or change its prepaid renewal boundary.

Feedback verification:

- `node scripts/test_release_storage.cjs`: PASS, including guarded transaction
  ordering, introduction origin, missing-origin refusal, cross-page renewal
  discovery, retained direct-extension refusal and upload-period preflight.
- Pinned container `sui move test --path contracts/fleet_storage`: **76/76 PASS**,
  including the same guard aborting for an actual prepaid renewal and allowing
  an unretained blob extension to epoch 6. Host Move cache mounted into the
  disposable container; networking disabled.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_components_browser.cjs`:
  PASS; real browser, fixture chain/wallet/storage.

The snapshot base af805cc1736dd12849fa396d047a3ad29f99c5e0 was integrated
before the repair. No new public-network transaction was submitted for feedback;
the original publish/extension receipts above remain historical evidence for
this ticket. Current-head automatic testnet/ordinary/security CI and a fresh
independent review remain coordinator gates. Deferred manual wallet/provider,
physical and mainnet checks remain unrun in #32 and #159.

- Pinned `make dapp-typecheck`: PASS (full offline frontend prerequisites).
- Pinned `make dapp-build`: PASS, all static pages exported; existing Tailwind
  module-type warning only.
- The trailing Move command in that combined container invocation failed fetching
  dependencies with networking disabled because its Move cache was not mounted.
  The separate cache-mounted Move run above completed 76/76 tests successfully.
- `git diff --check`: PASS.

## Current-base feedback handoff (2026-10-27)

The next review reported no new actionable source defect and confirmed the
prepaid-renewal fix. Its remaining blockers were hosted CI and automatic Testnet.
The supplied snapshot additionally showed ordinary CI `check` cancelled and
base movement. Merged snapshot/current main
`85331590985644a113eb6750782121169ef03a4b` into this branch as `d70ea42c`.
The Greengrass importer merge was clean; its storage-period controls remain.
No additional functional change was necessary.

Verification after integration:

- Pinned, network-disabled `make dapp-typecheck && make dapp-build` using the
  container/mount command above: PASS; all 16 static pages exported. Existing
  Tailwind module-type warning remains.
- `node scripts/test_release_storage.cjs`: PASS.
- `node scripts/test_greengrass_compat.cjs`: PASS (90 recipe fixtures and 47
  managed-dependency fixtures).
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_greengrass_compat.py`: 40 tests PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_live_release_storage.py`: 3 tests PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS (fixture chain/wallet/storage).
- `git diff --check`: PASS.

No Move sources/tests changed in this handoff; the earlier 76/76 Move results
remain historical evidence, not a new run. No new public-network mutation was
performed. Hosted CI and automatic Testnet must run for the pushed head/current
base; they were not polled and this handoff is not approval. Fresh independent
review remains required. Deferred external acceptance remains unrun in #32/#159.

## CI process-scan repair (2026-10-27)

Failed run [40569575446](https://github.com/tyler-suifleet/suifleet/actions/runs/40569575446/job/122419887297)
reached the actual tunnel/systemd crash-cleanup check, then raised
`FileNotFoundError` reading `/proc/1031/comm`: the process exited between
`exists()` and `read_text()`. The scan now catches only missing/exited-process
errors around the read. Live matching processes remain visible and unexpected
errors still fail. No runtime daemon code or cleanup assertion changed.

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_tunneling_process_scan.py`: 2 tests PASS. The deterministic exit-during-read
  fixture reproduces the original scanner's `FileNotFoundError`; it checks both
  ENOENT/ESRCH, a surviving proxy, and permission-error propagation. Wired into
  the existing CI tunnel step and `make test-tunneling`.
- `python3 scripts/test_tunneling.py --binary
  /home/tyler/dev/codex/src/suifleet-worktrees/work-suifleet-sf-063-c57b23ee/suifleet-edge/target/debug/suifleet
  --package /tmp/pr193-tunnel.tar.gz --evidence /tmp/sf165-tunnel-repair.json`:
  all 9 actual proxy/systemd checks PASS in disposable network-none container,
  including SIGKILL descendant cleanup, TLS negatives and credential revocation.
  Uses the existing SF-063 binary, SHA-256
  `67d19a95474daf542ec4f652ae7664ffccab5d9880f82a3e379d60d7a84ca2cc`,
  and pinned proxy source `8a71618acf536923e882387e8d9eca9dbc7fdbe2`;
  this is harness verification, not a rebuild of the current daemon.
  Two earlier attempts with the old canonical-checkout cached binary failed
  before MQTT subscription because that binary has no tunnel-mode CLI support.
- Pinned network-none container command documented above, running
  `make dapp-typecheck && make dapp-build`: PASS (16 static pages).
- `node scripts/test_release_storage.cjs`: PASS.
- `git diff --check`: PASS. `git merge origin/main --no-edit`: already up to
  date; base `85331590985644a113eb6750782121169ef03a4b` is included.

No new public-network transactions or Move changes. Hosted exact-head/current-base
CI, automatic testnet and fresh independent review remain mandatory and pending;
this repair is not approval. Deferred external checks remain unrun in #32/#159.
