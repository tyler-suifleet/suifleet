# SF-082 implementation validation, 2026-10-22

Model: Codex, actual configured model/version `gpt-6.1-sol`. PR #233; SF-082/#160.
Historical receipts retain their actual source heads; they are not claims that every
historical run tested the final tree. Current-head CI/security/testnet must pass.

- `make test-contracts`: 168 core Move tests, 39 fleet-storage tests and 16 fleet-data tests passed.
- `cargo test --locked --offline`: 388 passed, 4 explicitly ignored systemd tests. PKCS11 feature: 390 passed, 4 ignored. Independent native Seal encryption/SDK decryption passed. After point-serialization adjustment, `cargo test --locked --offline seal::`: 6 passed, including independent SDK vectors.
- `make test-ota`: native OTA, lifecycle/supervision, daemon update, container, key isolation, telemetry and logs simulators passed. CloudWatch correctly refused root; reran CloudWatch, console, rollout and fleet-budget tests as UID 1001 successfully. Native fleet-data producer simulator passed with slow codec, prepaid certification/anchor restart recovery, diagnostic introduction origin and signing/browser checks. No physical/systemd result inferred from simulator tests.
- `make test-manifest && make check-interfaces && make dapp-typecheck`: passed; 628 client references, 13 real compiled modules, zero drift; generated interfaces/signing vectors passed. Checker regression suite: 17 tests including separately compiled missing/wrong fleet-data types and calls. Browser archive crypto, signature/replay/foreign-chain/revocation/rotation, alert parity, relay and complete gas-estimate tests passed.
- `scripts/test_protocol_localnet.py --fleet-data-upgrade`: real disposable network-none pinned 1.82.1 node / 1.81.1 compiler; base-to-head upgrade preserved existing objects and introduction origins, new DiagnosticKey resolved, revoked device attacks through both package versions failed in execution effects. Receipt: `sf082-data-upgrade.json`.
- Two `scripts/build_walrus_site.py` production builds, with actual Testnet manifest/fleet/public Seal settings and verified builder 2.12.2: identical full receipts, 121 resources. No secret runtime files exported.
- Dedicated Testnet `4c78adac`: native daemon health, SUI-prepaid certified non-deletable encrypted log/diagnostic blobs and anchors, restart without duplicate purchase; actual browser discovery/signature verification/decryption with signed owner Seal session and public provider keys; account closure/withdrawal/sweep. Receipts: `sf082-testnet-native.json`, `sf082-testnet-browser.json`, complete disposable sender effects `sf082-device-effects.json`. Failed attempts/funding effects remain `sf082-testnet-attempts.json`.
- Real Testnet RS2 quote: fixed encoding metadata 64,032,000 bytes; small three-epoch upload 822,339 FROST; daily x30 = 24,670,170 FROST/month, plus Sui gas. One packed batch and one 24-part quilt have equal fixed overhead; 24 individual uploads cost 24x. Receipt: `sf082-walrus-quote.json`. Estimator includes measured first/recurring health and registration+certification+anchor fees; quotes are not production invoices.
- Testnet Site published/updated/rolled back, then updated to the final reproducible export. Every resource byte/hash/range/header, exact dynamic-field count and 27 routes verified from Sui and full Walrus readback; independent CLI effects checked. Publisher used 476,600,946 MIST of a lifetime 700,000,000 MIST allowance, including 20,000,000 MIST WAL purchase. Credits/refunds never replenish allowance. Site identity retained privately for updates. Receipt: `sf082-site-publication.json`.

The upstream pinned Site builder still uses unavailable JSON-RPC for its wallet;
publication used the pinned-ABI gRPC adapter, not a fabricated upstream success.
No SuiFleet server is needed; canonical fullnode, storage nodes/portal and configured
Seal providers remain infrastructure dependencies. Optional webhook relay is not a
trust root. Site blobs expire after purchased epochs; extend/republish before expiry.

Final production provider/domain/credential/fee/availability/recovery, manual wallet,
physical Pi and actual Mainnet acceptance remain explicitly unrun in #32/#159 under
the user-authorized implementation split. No production credentials, Mainnet writes
or hardware programming occurred. Fresh independent review remains mandatory.

Final retention regression: browser re-reads the actual fleet-owned BlobRecord before/after decryption. Owner-paid extension succeeds beyond the original anchor epoch; released, shortened, foreign-device/blob/type/budget and mid-download released records fail closed. Scoped archive tests, real-daemon producer simulator, typecheck and actual Testnet signed-session browser verification/decryption passed after this fix. Two final static builds again matched, and the retained Site was updated/read back using the descriptor-safe packer.

Recovery validation (actual model `gpt-6.1-sol`, same reserved session):

- Integrated reviewed SF-092 `2e9a7f4` and SF-116/current main `3957940`.
  The owner-approved finite device-retention extension preserves existing public
  signatures/layouts; its new dynamic fields retain their actual introduction origin.
- Network-none storage Move tests: 60 passed after the final additions
  (preserved `/tmp/sf082-retained-final-move.log`); core 168 and data 16
  passed earlier, with unchanged sources. Both real disposable-chain
  populated storage upgrade paths and fleet-data upgrade/adversarial checks passed
  (`/tmp/sf082-retained-real-upgrades.log`); their public receipts remain committed.
- `make test-manifest`, `make check-interfaces`, frontend typecheck and native
  producer simulator passed after integration. Compiled check: 637 references,
  13 modules, zero drift. Final recovery reran 72 offline testnet/toolchain tests,
  fleet-budget browser tests and frontend typecheck: all passed.
- Native Testnet continuation verified weighted storage-shard quorum, certified
  logs/diagnostics, signed owner-session browser decryption, restart without
  repurchase, finite protected renewal and account cleanup. Original charges and
  failed registration/certification attempts were retained. Total non-replenishing
  allowance used: 631,790,822 MIST of 800,000,000; net balance change includes
  returned reserves and never determines the allowance. See the updated native
  and device-effects receipts. This is Testnet evidence, not production acceptance.
- Old automatic package-upgrade run 39881586656 failed before upgrade submission;
  it remains failed evidence. Root fixtures now use pinned compiled bytecode, so
  separate storage/data packages do not cause identical core republication.
  Compiler/framework-bound verified cache migration and durable publication
  checkpoints retain unknown outcomes for reconciliation. Fresh current-head
  ordinary/security/automatic-Testnet checks are still mandatory.

- Final retained-storage export: two `build_walrus_site.py` runs with the actual
  deployment/fleet/storage/public-Seal bindings yielded identical complete receipts
  (122 resources). Updated the same owned Site in the sixth phase; full Walrus
  bytes/ranges/headers and 27 Sui routes read back, independent effects checked.
  Publisher lifetime used 485,105,631 of 700,000,000 MIST, with no new funding.
  The latest Site receipt supersedes earlier export/cost counts above.

- Automatic Testnet run 39894046078 at `9fbfd16` failed before any write (zero
  MIST) because fresh fixture setup referenced uninitialized `built` provenance.
  Fixed the setup to use its durable publication proof map directly. Added a
  regression driving actual `run()` through fresh and resumed setup to the
  pre-sign refusal boundary; both variants reject the original source and pass
  with the fix. All 73 offline testnet/toolchain tests passed. Final-head CI and
  automatic Testnet must pass anew; the failed run is not relabeled successful.

- Adjacent checkpoint recovery now verifies UpgradeCap against the recorded latest
  package/version, rather than incorrectly requiring the base package after a
  completed upgrade. Tests reject foreign package, version, cap owner and registry
  ownership without mutations; both version-1 and version-2 checkpoints pass.
  All 74 offline testnet/toolchain tests passed.
