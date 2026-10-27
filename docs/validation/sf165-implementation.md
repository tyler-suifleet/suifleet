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
