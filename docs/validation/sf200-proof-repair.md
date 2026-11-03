# SF-200 proof-control repair

Functional repair: `924af4fe`, following reviewed head `85c2730158cc056032ae19a9a38e1f262d947600`; pinned base remains `df5d6d8005801034a75d4ce4180dcf713d82efd8`.

The size refactor replaced inline creator/version assertions with private helper calls. Two security mutants still targeted the old assertions and stopped before solver verification. Update only their exact-match targets in `scripts/evaluate_move_prover.py`: deployment-authority removes `require_creator(ctx.sender() == d.creator);`; receipts-future removes `require_version(receipts.version <= target_version);`. Each still deletes the actual guard in a disposable copy and must fail the unchanged obligation with the expected solver diagnostic. No contract source, bytecode, layout, public API or frontend changed; the prior [size/isolated-upgrade receipt](sf200-size-repair.md) remains historical evidence for the identical contract sources, not a new run.

## Local verification, 2026-11-02

Image `suifleet-sf164:protocol140`, workspace mounted read-only, host UID/GID, Sui `1.81.1-e04cf9aa915c`, Rust 1.94.0. Private proof cache copied to `/tmp/sf200-proof-repair/tools` and mounted at `/proof-tools`; cargo uses that cache with `CARGO_NET_OFFLINE=true` and two build jobs. No wallet mounted, no chain writes. Commands:

```sh
python3 scripts/build_move_prover.py --tools /proof-tools
python3 scripts/check_move_proofs.py --tools /proof-tools
python3 scripts/check_move_native_proofs.py --tools /proof-tools
python3 scripts/check_move_object_proofs.py --tools /proof-tools
python3 scripts/check_move_registry_proofs.py --tools /proof-tools
python3 scripts/check_move_security.py
python3 scripts/check_move_native_vm.py
python3 -m unittest discover -s scripts/tests -p test_build_move_prover.py -v
```

All PASS. Pinned prover/Boogie build completed (zero build warnings/errors). Core proof gate: 63 positive obligations and every negative control, including both repaired controls. Native gate: positive and missing-tag-clear, wrong-type-alias and unknown-occupancy controls. Object gate: 21 positive phase obligations and every mutation control. Registry gate: 42 positive phase obligations and all five controls (initializer, activation, custody, enrollment claim and quota). Unsupported UID reconstruction and same-Name overlap fixtures are rejected as expected, not proved.

Compiler/security gate: strict compiler/linter and unsuppressed baselines, deliberately failing compiler/linter fixtures, 376 core, 82 storage and 60 data tests, and all authority-bypass controls pass. Native VM: 19/19 tests. Download regressions: 5/5. A separate disposable-copy check confirmed each repaired mutation removes exactly its intended real guard call. `git diff --check` passes. Private local logs: `/tmp/sf200-proof-repair/gates.log`, `compiler.log`, `download-tests.log`.

Initial build attempts hit container Git ownership mismatch and an uncached default Cargo home; corrected by matching host UID/GID and using the private pinned Cargo cache. These setup attempts are not claimed as passing gates.

## Remaining acceptance

Hosted CI and automatic public-testnet acceptance for the pushed head are pending/unverified; this worker does not watch or poll hosted checks. Fresh independent source review, exact-head/current-base CI and funded automatic testnet with effects/spend and combined-package publication-size evidence remain coordinator gates. This repair is not merge approval and does not repair or waive the historical protected-CI wallet funding shortfall.

Manual/real-wallet, Pi/physical, production-provider and actual-mainnet acceptance remains unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159); production hosting remains [SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62). No public-testnet writes/faucet, production deployment, hardware programming, new issues, other model workers or merge occurred.
