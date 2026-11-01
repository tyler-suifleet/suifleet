# SF-196 enrollment-authority repair

Combined review job 3084 of PR #433 (candidate
`e73e088d074ad3b5cbe585ac27c2d667161527b5`, pinned base
`3b7d0c8d6a553647606e97ec780089782a676ae5`) reproduced a source defect in
PR #421 head `eaef0c7ec0203f9ac450655c89a3fec3048a99dd`. This repair stays
on that source branch; no merge/rebase for freshness or fabricated source CI
failure is involved.

Enrollment now uses its own authority epoch, with the existing dynamic-field key
under the Customer UID and the binding under each EnrollmentPolicy UID. Public
signatures, layouts, type origins and action-proof invalidation stay unchanged.
Routine additions, unchanged all-off saves and unrelated secured completion
preserve provisioned claims. Administrator removal/demotion, changed requirements,
factor-source configuration, handover and intermediate-authority migration revoke
claims. Current-factor add_claims can explicitly reauthorize remaining claims;
see SECOND_FACTOR_DESIGN.md for the scope and migration limitation.

Verification on the repaired working tree, 2026-11-01:

- `PATH=/tmp/sf196-host-tools:$PATH make INSIDE_DEV_CONTAINER=1 test-contracts`:
  core 350/350, storage 79/79 and data 60/60 passed. Nine new regression cases
  exercise three reported positive flows, administrator removal/demotion, factor
  replacement, protected-policy weakening, handover and explicit reauthorization.
- The three `claims_survive` tests copied onto the original HEAD fail 0/3 with
  customer EBinding, reproducing the defect independently of the repair.
- Same compiler PATH, `make INSIDE_DEV_CONTAINER=1 check-interfaces`: passed;
  reviewed public ABI/layouts and generated clients have zero drift.
- Same compiler PATH, `python3 scripts/check_move_security.py`: passed strict
  and unsuppressed compiler/lint baselines plus genuine authority-bypass
  mutation regressions.
- Disposable `suifleet-sf164:protocol140`, `--network none`,
  `python3 scripts/test_security_policy_localnet.py --base /base/contracts
  --evidence /workspace/docs/validation/sf196-enrollment-localnet.json`: passed
  16 effects-checked existing-object migration, old-customer rejection, publisher
  exclusion and current-publication transactions. Compiler 1.81.1, transaction
  CLI 1.83.0. [Sanitized receipt](sf196-enrollment-localnet.json) pins source hashes
  and disambiguates its original HEAD field from the repaired working tree.
  This is isolated-chain evidence, not a public-testnet run or sponsored live
  claim enrollment evidence.

Environment corrections: initial compile caught cross-module key construction;
key handling was moved into the defining module's package-only helpers. Initial
localnet setup failed before package publication because copied dependency Git
ownership was wrong; corrected cache ownership permitted the run. Root container
build output temporarily blocked host tests; ownership of generated build output
was restored before rerunning. An initial regression-probe invocation used an
unsupported flag; the corrected positional filter reproduced all three failures.
No success is inferred from these failed attempts.

No public-wallet mutations, deployments, hardware work, new issues or other model
workers. Fresh independent review and actual exact-head/current-base ordinary,
security and automatic public-testnet effects/spend evidence remain coordinator
merge gates. Hosted CI is pending after push and is not watched or polled.
Manual/real-wallet/provider/physical/mainnet acceptance remains unrun in #32/#159;
production hosting remains in #62. All four mechanism implementations remain
separate child-ticket work.
