# SF-196 pinned integration conflict repair

The coordinator reported an actual merge conflict between source
`45aba03872837c857ea28e6a30b14f49bdee82bb` and pinned candidate base
`fe4bd72b04552118ae0760ceb3a8e650f76b7c2d`. This is integration repair, not
an assertion that the original source failed CI. The repair merges that exact
base without rebasing to advancing main.

Both organization security and connection-history settings remain. Manifest
validation retains security, rollout-audit and group-batch origins; generated
TypeScript/Rust interfaces contain both APIs. Toolchain tests and documentation
retain both protocol-140 repair records. Batch group changes call the protected
single-device operations; audit cleanup checks current administrator policy.

Checks on the resolved tree (2026-11-01 UTC):

- `PATH=/tmp/sf196-host-tools:$PATH make INSIDE_DEV_CONTAINER=1 test-contracts`:
  core 341/341, fleet_storage 79/79, fleet_data 60/60 passed.
- With the same pinned compiler PATH, `make INSIDE_DEV_CONTAINER=1 check-interfaces`:
  1,255 client references, zero drift; generation, negative ABI cases and all
  included Node interface tests passed.
- `PATH=/tmp/sf196-host-tools:$PATH python3 scripts/check_move_security.py`:
  strict/unsuppressed lint baselines and genuine failing authority mutations passed.
- `node scripts/test_security_policy.cjs`, `test_customer_fleets.cjs`,
  `test_group_batch.cjs`, `test_rollout.cjs`, `test_protocol.cjs`: passed.
- In `dapp`, `node node_modules/typescript/bin/tsc --noEmit` and
  `node node_modules/next/dist/bin/next build`: passed, static `/settings` included.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  174 tests, OK with one existing skip. These are offline fixtures.
- The same unittest command with `test_deployment_manifest.py` and
  `test_live_common.py`: 22 and 67 tests passed respectively.
- `CARGO_TARGET_DIR=/tmp/sf196-edge-target cargo +1.94.0 test --locked --offline
  --manifest-path suifleet-edge/Cargo.toml`: 462 passed, zero failed, four
  existing Docker Engine OCI tests ignored. Initial host-default/container-cache
  setup attempts failed before testing; the explicit pinned host run passed.
- `scripts/test_security_policy_localnet.py --base /base/contracts` in a
  disposable `suifleet-sf164:protocol140` container with `--network none`:
  16 effects-checked transactions passed. See [receipt](sf196-conflict-localnet.json).
  It tests the resolved working tree; its original HEAD field is explicitly
  disambiguated by `integration_context`. Initial container setup attempts lacked
  the chain-facts binary and Git metadata mounts; corrected attempts passed.

No public wallet mutations, production deployment, hardware work or new workers.
Existing public acceptance receipts remain historical evidence; fresh hosted
exact-head/current-base CI/testnet and independent review remain coordinator gates.
Manual/provider/physical/mainnet acceptance stays unrun in SF-031/#32 and
SF-087/#159 (production hosting in SF-042/#62).
