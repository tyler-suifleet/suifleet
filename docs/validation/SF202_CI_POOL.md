# SF-202 CI preparation and wallet-pool validation

Date: 2026-10-28. Implementation branch: `work/suifleet/sf-202-98034694`.

## Implemented behavior

Secretless preparation has no wallet environment or concurrency lock. It builds the pinned environment and locked tools, then publishes an image identified by immutable digest. Protected validation independently authenticates the exact revision/base, checks the image's controller/run/attempt/head/base labels, rechecks freshness before spending, and uses one selected dedicated wallet lane. Lane 0 preserves the original primary/manual lock and ciphertext name. Lanes 1/2 have independent identities, locks and encrypted histories. Retention cannot delete images belonging to queued/running producer workflows.

The two additional keys were generated locally into private mode-0700 setup directories/files mode 0600 and installed without printing their values into `live-package-upgrade` environment secrets `SUIFLEET_TESTNET_KEY_1` and `_2`. Repository variable `SUIFLEET_TESTNET_POOL_SIZE=3` is configured. These do not activate the old main workflow; reviewed-main rollout remains required.

## Local checks

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 156 tests PASS, one existing optional historical-revision test skipped. Coverage includes lane selection, primary fallback, distinct identities/locks/histories, cross-lane ciphertext discovery refusal, address mismatches, selected signing configuration and unchanged budgets, exact prepared-image provenance/digest rejection, package allowlist, active/foreign producer retention and workflow authority/ordering.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_live_common.py'`: 60 PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_security*.py'`: 23 PASS.
- `node scripts/test_testnet_cache.cjs`: authenticated roundtrip and wrong-key/header/nonce/data tampering PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_tracking.py'`: 10 PASS. `scripts/bootstrap_tracking.py`: offline plan PASS.
- Python compile, Node syntax, YAML parse, `git diff --check`: PASS.
- Checksum-verified actionlint v1.7.12: PASS except its unsupported `concurrency.queue` schema field. The lint invocation ignores only that specific diagnostic; `queue: max` is existing GitHub functionality and was retained, not removed to satisfy the older lint schema.
- `docker build --build-arg USER_UID=1001 --build-arg USER_GID=1001 -t suifleet-sf202:dev .`: PASS using this checkout's pinned Dockerfile.
- In the pinned image: `cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts`: PASS, 1m50s, two existing daemon warnings.
- Created the actual prepared context from installed locked dependencies and these compiled binaries, stripped debug symbols, built `suifleet-sf202:prepared`, then ran with `--network none`: daemon runtime identity provisioning and real Sui SDK resolution PASS. Local Dockerfile substituted only the private baseline image tag; production uses its same-job baseline tag. The context was 561MB including dependencies. No wallet files or fixture state entered it.

## Dedicated testnet identity and funding evidence

[Machine-readable receipts](sf202-ci-pool.json) record two effects-verified 1-MIST self-transfers and one fixed 0.3-SUI internal seed transfer from lane 1 to lane 2. All used verified chain `4c78adac`, pinned compiler/transaction tools and explicit wallet configurations. Each self-transfer had a 5,000,000-MIST ceiling. The seed transfer had a 305,000,000-MIST ceiling and charged its full 300,000,000-MIST funding plus gas. Total transaction gas: **4,976,099 MIST (0.004976099 SUI)**. The internal seed remains within the dedicated pool and is not a network fee.

Each initial readiness preflight had the existing full-suite 3.25-SUI ceiling, executed no signed transaction and made at most one faucet request. Fresh wallets required several separate bounded readiness runs before covering that ceiling; all unsuccessful funding checks, including one failed faucet request, remain in the receipt rather than being reported as success. Faucet credits did not replenish any run allowance. Both final readiness preflights passed without another faucet: each new wallet held at least 3.25 SUI. The machine-session `network:suifleet-testnet` lease was asserted for operations and released; coordination was posted on #11. No existing package/UpgradeCap/device fixture, production account, mainnet, hardware or DNS state was modified.

This establishes that the new keys derive the pinned identities, can sign actual transactions, and have initial full-allowance funding. It is not a fresh full package-upgrade/security/wallet suite run and is not hosted parallelism evidence.

## Required hosted rollout evidence

Normal independent review and exact-head CI still apply. After reviewed-main merge, confirm GHCR publication/access, same-run digest/provenance/pull, main-only environment and freshness refusals, per-wallet encrypted-history reuse, and simultaneous validation on different lanes. Measure queue time and execution time separately. No claim of a threefold speedup, hosted success or completed integration is made by these local checks. Mainnet, physical and manual production-wallet/provider acceptance remain separately tracked and unrun.

## PR 402 feedback repair

The initial Actions CodeQL report identified `actions/untrusted-checkout/critical`
at the preparation checkout and a nonblocking `actions/excessive-secrets-exposure`
at dynamic secret selection. Both revision checkouts now consume only the
main-sourced planner's independently authenticated immutable SHA, followed by the
existing checkout verification. Signing secret selection explicitly names only
the three dedicated pool keys. No scanner suppression or authority check was removed.

The feedback branch includes current main `120b5cb76e44a81cbaa09cdc2731c71907d0cee3`.
Validation: testnet regressions 157 tests (one historical skip), live-common 60,
security 23, authenticated cache tampering, YAML parse, diff check and actionlint
(excluding only the pre-existing unsupported `concurrency.queue` diagnostic).
An initial regression run failed because the validation checkout still used the
event SHA; that missed edit was corrected before the final regression run.
Hosted CodeQL and other exact-head CI, automatic testnet and independent review
remain pending; this repair does not claim those checks passed. No live writes
or hardware operations were performed during feedback repair. Deferred physical,
mainnet and final provider/wallet acceptance remain in #32 and #159.
