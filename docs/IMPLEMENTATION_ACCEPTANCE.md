# Implementation and integration acceptance

The user authorizes merging independently reviewed implementations before physical
Pi, real extension-wallet, public Sui-network and provisioned production-release
integration acceptance. These external checks remain required in the linked tickets;
merging implementation does not establish them or authorize deployment.

| Implementation | Deferred integration |
|---|---|
| SF-006 publishing and SF-008 identity | [SF-010: public-network upgrade and wallet acceptance](https://github.com/tyler-suifleet/suifleet/issues/11) |
| SF-011 catalog and SF-013 desired state | SF-010 plus [SF-031: physical Pi OTA and recovery](https://github.com/tyler-suifleet/suifleet/issues/32) |
| SF-024 indexer | SF-010 public-node/archival integration |
| SF-025 packaging | SF-029 bootable Pi image and SF-031 physical acceptance; final BitBake/QEMU evidence remains required for the packaging implementation |
| SF-039 signed releases | [SF-042: provisioned signing workflow and device trust](https://github.com/tyler-suifleet/suifleet/issues/62), plus SF-031 |

The versioned machine-readable scope is
[`tracking/acceptance-policy.json`](tracking/acceptance-policy.json). Copy its object
into the selected project's private PR coordinator `acceptance_policy` configuration
to enable that scope. Other projects and tickets retain their original requirements.
Policy changes alter the review fingerprint: previous blocked results and approvals
are invalidated and a fresh independent exact-head/base review is required. The
coordinator must publish the fresh approval; it cannot relabel old reviews as approved.

Code/security defects, failure-path regressions, supported local-chain existing-object
and package-upgrade compatibility, relevant builds/tests and current-head CI remain
merge gates. Disposable isolated local-chain publication is authorized test setup.
Public/shared-network transactions, hardware programming and production signing-key
provisioning are separate operations requiring their actual deployment authorization.
Private keys must never appear in source, issues, logs or acceptance evidence.

The dependency audit found no implementation tickets depending on SF-010 or SF-031.
The blocker was blanket review acceptance rather than those graph edges. Retain real
API dependencies: SF-010 follows the client/contracts foundation, SF-029 follows
SF-025 packaging, and SF-031 follows the image and OTA test component. SF-042 follows
SF-039 implementation. Do not remove these or guess an unmerged dependency's API.

The coordinator remains the sole merger while enabled. Serial merges update the base,
requiring subsequent owners to integrate that base and obtain fresh tests/review.
Verified merges close implementation issues and reconcile atomic/Project status;
SF-010, SF-031 and SF-042 stay open until their own acceptance evidence is complete.
