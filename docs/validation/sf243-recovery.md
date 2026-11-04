# SF-243 protected fixture recovery

Implemented against main `4aa4344c0a0b0749348ed78544531ed28ac857d3`.
Codex running model/version: `gpt-6.1-sol`; dispatch session
`dispatch-a2487fe54d714842879aee49988efb47`.

The cached initializer still enforces the current policy even when already
initialized. Read existing policy state instead of making that wallet-only call.
Retain the original private keys, use an enrolled sponsor for enrollment/policy
changes, and checkpoint completed steps so a resumed recovery does not re-enroll
a primary already removed by the test. Expired possession proofs can be renewed
with the same identity after a read confirms expiry.

Every setup/funding/native transaction persists its digest before submission.
Pending recovery reads that exact digest before further passkey mutations;
unreadable, mismatched, incomplete or unexpectedly failed outcomes stay blocked.
Recovered reservations (including funding) are fully charged, with no rebate
credit. Existing failed-call gas, cached funding grants, run ceilings and encrypted
private-state preservation remain in force. Missing retained keys refuse
generation, including inside the Node subprocess. A completed cycle clears only
its step checkpoints, preserving fixture identity and keys.

## Real disposable-chain evidence

[Sanitized effects](sf243-passkey-localnet.json) record **PASS**, chain
`3cf3fc21`, 21 ledger entries, 1,636,624,594 MIST net spending and
**1,743,751,446 / 1,800,000,000 MIST** charged. This is an isolated local chain,
not public-testnet acceptance. The test executes a compatible package upgrade,
enrolls both retained identities, then interrupts immediately after validators
successfully enable protection but before the local success checkpoint.
The resume reads digest
`72L23rPcyAzhvuH4CPmZdiTVMK9bJj6DUpwZRBKMx6q2`, charges its full 20M
reservation and completes authorized action, wallet-only/old-package/removed-key
refusals, backup recovery, disable and final removal. The earlier unknown ledger
entry remains historical; the explicit reconciled entry records its known effects.
The initializer is not replayed. Wallet-only refusal also exercises coinless
address-balance gas.

Exact command (host checkout at this branch; private warmed Move cache):
```bash
docker run --rm --network none --name sf243-passkey-local \
  --user "$(id -u):$(id -g)" -v "$PWD:/workspace" \
  -v /home/tyler/dev/codex/src/suifleet/.git:/home/tyler/dev/codex/src/suifleet/.git:ro \
  -v /tmp/sf182-container-inspect/dapp/node_modules:/workspace/dapp/node_modules:ro \
  -v /tmp/sf243-move:/home/dev/.move -v /tmp/sf198-base:/base:ro \
  -v /tmp/sf196-network-183/sui:/opt/sui-network/sui:ro \
  -v /tmp/sf196-edge-target/debug/examples/chain_facts:/opt/chain_facts:ro \
  -e SUIFLEET_CHAIN_FACTS=/opt/chain_facts \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
  -e 'GIT_CONFIG_VALUE_0=*' -w /workspace suifleet-sf221:localtest \
  python3 scripts/test_security_policy_localnet.py \
  --runner scripts/live_passkey_factor.py --base /base/contracts \
  --evidence /workspace/docs/validation/sf243-passkey-localnet.json
```

Compiler: pinned Sui 1.81.1; transaction/node CLI: checksum-pinned 1.83.0;
SDK: locked 2.36.0. No public wallet, credentials or network were mounted.
The build resource was leased only during local validation.

Earlier attempts are not passed results: a stale reused SDK directory (1.24.0)
stopped before publication; combining extra enrollment/repeated full cycles in
one local ledger correctly exhausted the unchanged ceiling. The final scenario
uses one interrupted/resumed cycle. Offline tests separately exercise full repeat
execution and interruption after primary removal without restoring that key.

## Offline verification and limits

- `python3 -m unittest discover -s scripts/tests -p test_live_passkey.py`:
  **15 passed**, including expired enrollment, protected removal/repeat,
  unknown/failed/mismatched pending effects, grants and missing-key refusals.
- Network-none container with locked SDK: live suite, testnet suite (218 tests,
  no skips), `node scripts/test_passkey_fixture.cjs` and
  `node scripts/test_passkey_payment.cjs`: **passed**.
- Host tracking suite: **10 passed**;
  `python3 scripts/bootstrap_tracking.py`: **passed dry run**, 181 prepared
  issues, no tracker reads/writes or local tracking mutations.
- Full coordinator suite and exact committed combined-main validation are
  recorded in the issue/PR handoff.

No public transactions were submitted. A fresh pinned combined candidate must
run funded automatic testnet after reviewed integration/merged-only runtime
activation. Hosted checks remain pending at handoff; no polling or approval is
claimed. Physical/manual-wallet acceptance remains unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32), and
mainnet/production-provider acceptance in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
