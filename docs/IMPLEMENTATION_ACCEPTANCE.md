# Implementation and integration acceptance

The user authorizes merging independently reviewed implementations before physical
Pi, real extension-wallet, public Sui-network and provisioned production-release
integration acceptance. These external checks remain required in the linked tickets;
merging implementation does not establish them or authorize deployment.

| Implementation | Deferred integration |
|---|---|
| SF-006 publishing and SF-008 identity | [SF-010: public-network upgrade and wallet acceptance](https://github.com/tyler-suifleet/suifleet/issues/11) |
| SF-011 catalog and SF-013 desired state | SF-010 plus [SF-031: physical Pi OTA and recovery](https://github.com/tyler-suifleet/suifleet/issues/32) |
| SF-014 component supervisor | SF-031 physical Pi boot/reboot/power-loss component recovery, plus SF-010 extension-wallet lifecycle UI; real systemd/cgroup-v2 crash/containment/restart evidence under the shipped unit remains a premerge gate |
| SF-026 daemon self-update | SF-031 physical power loss during handoff and platform boot behavior; real systemd/cgroup-v2 handoff and fallback evidence remains a premerge gate |
| SF-053 device-key isolation | SF-031 physical Pi acceptance and hardware-token protection; real systemd/cgroup-v2 isolation evidence remains a premerge gate |
| SF-024 indexer | SF-010 public-node/archival integration |
| SF-025 packaging | SF-029 bootable Pi image and SF-031 physical acceptance; final BitBake/QEMU evidence remains required for the packaging implementation |
| SF-029 bootable Pi image | Physical flashing/boot, real-device identity persistence, networking and OTA remain SF-031/#32; a completed BitBake SD image and local partition/rootfs inspection remain required before Review. User will flash the image. |
| SF-039 signed releases | [SF-042: provisioned signing workflow and device trust](https://github.com/tyler-suifleet/suifleet/issues/62), plus SF-031 |
| SF-043 automatic testnet controller | First full automatic Actions execution requires reviewed main-only bootstrap; tracked in SF-043/#64 and observed before declaring the overall request complete |
| SF-045 testnet preflight repair | First full automatic Actions success after reviewed repair integration; exact-head CI, keyless Actions metadata probe and genuine current-head/base testnet effects/status remain premerge gates; tracked in #69/#64 |
| SF-046 encrypted fixture restore | First automatic post-integration execution of the updated main-sourced helper with unchanged-fixture reuse; exact-head CI and genuine current-head/base automatic testnet effects/status remain premerge gates; tracked in [#72](https://github.com/tyler-suifleet/suifleet/issues/72)/[#64](https://github.com/tyler-suifleet/suifleet/issues/64) |

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

The SF-043/SF-045 entries encode the direct user's one-time bootstrap ordering,
not a continuing exemption from automatic CI. The main-sourced controller cannot
execute a new repair before that repair exists on main. In recovery from the
observed preflight failure, a truthful exact-head/base testnet status may link to
a genuinely executed, locked machine-session receipt instead of claiming an
Actions run occurred. A real keyless Actions probe validates the repaired metadata
boundary before integration. No failed test is waived, no review/status receipt
is fabricated, and the first normal CI-triggered full testnet Actions success is
still required before the continuation declares the user's overall goal complete.

SF-046 records only the ordering needed to observe the updated main-sourced
artifact restore helper after reviewed integration. Existing automatic CI already
works. Source/security and artifact-ordering, producer and authentication regressions,
full exact-head ordinary CI, genuine successful exact-head/current-base automatic
testnet status with independently verified effects and budget evidence, fresh
independent review and sole-coordinator integration remain required. No failing
test or mandatory status is waived. Observe and record the actual automatic
updated-helper restoration and unchanged-fixture reuse after integration in
#72/#64 before declaring the overall SF-043 request complete.
