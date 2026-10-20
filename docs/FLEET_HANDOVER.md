# Fleet administration handover (SF-089)

Use this flow while the current wallet can still sign. It moves administration to
another Sui address, including a native Sui multisig address, without changing
registered device IDs, signer keys, signer-bound DeviceCaps, journals or installed
component state. Losing every configured signing authority has no recovery path.
Set up stronger custody, test its signing procedure and retain its recovery
material **before** losing the onboarding wallet.

A `fleet_handover::Handover` contains source, recipient, expiry and a fixed typed
inventory. The recipient signs `confirm`; only the source can execute its steps.
Each step updates the authority and its progress marker in the same transaction.
A repeated step, wrong kind/ID, wrong party, unconfirmed/cancelled session or
expired session aborts. Neither an inventory entry nor recipient confirmation
substitutes for ownership: an owned capability must be a valid transaction input;
groups and deployment records independently check their current administrator.

The transfer is resumable, **not fleet-wide atomic**. Batch related steps in one
PTB where possible. Until a device's owner capability moves, its current owner is
still authorized. Afterwards, a group/record still controlled by the source fails
the daemon's current-owner checks. An already admitted lifecycle operation is not
killed by a transfer; allow it to settle before moving that component. Stop rollout
coordinators and pause admission before preparing the inventory. Cancellation or
expiry stops remaining work without undoing completed transfers. There is no
unilateral clawback. A reverse handover requires both wallets again.

## Authority inventory and cutover order

Inventory every item for the selected fleet, including references from desired
state, dependencies and all active rollouts. Shared registries are infrastructure,
not tenant-owned objects: do not transfer the registry, package UpgradeCap or
another tenant's objects. Ownership of a group does not imply ownership of every
device listed in it. Verify each selected device's owner cap against its registry
identity and check membership separately.

| Authority | Treatment |
|---|---|
| Package UpgradeCap / infrastructure administration | Separate organization governance, not tenant transfer. If the onboarding wallet also controls package upgrades or shared-service administration, move/revoke that access through the reviewed organization custody procedure before retiring it. Never give a customer the shared multi-tenant UpgradeCap. On a dedicated deployment, explicitly transfer its upgrade authority to the intended governance custody separately. This flow does not defend against a malicious future package upgrade by a retained upgrader. |
| DeviceOwnerCap | Kind 0: transfer the existing cap. The registry identity still names the same cap, Device and device signer. No enrollment or device secret changes. |
| DeviceGroup | Kind 1: change the stored admin. Membership, name, ID and dynamic desired-state fields remain. Old package group functions check the same admin field. |
| Native deployment / rollout / controls | Kind 2: change the stored creator. Targets, payload/digests, receipts, control settings and staged rollout state remain. The new owner explicitly restarts its coordinator; old-owner control/delete/advance attempts fail the creator check. Audit the Handover events alongside historical creation events. |
| Desired-state revision | Remains on the same group. Migrate every retained native record as well; a group alone does not confer record control. The recipient can then commit new revisions. Replace catalog-bound references before resuming admission. |
| DeployerRole | Kind 3: revoke the role ID through the registry. A role is not transferred or silently retargeted. The recipient explicitly issues new scoped roles after checking migrated groups. Discover all active roles, including those held by delegates. Older role entry points still attribute deployments to the old operator, which the migrated device/group rejects. |
| EnrollmentPolicy / claim sponsorship | Kind 4: permanently close each old policy. Existing enrollment counters/claim tables are historical. Recipient creates new policies/claim addresses explicitly; no old claim secret is copied. |
| ComponentCatalog and immutable ComponentVersion | **Supersede.** Frozen versions retain their publisher/catalog provenance. Create a recipient-owned catalog, review/publish its complete dependency closure, and create replacement catalog deployments under the recipient. In-place migration rejects schema-2 deployments: their original catalog index would otherwise remain an old-owner authority. Preserve historical catalogs/versions for audit; do not claim their publisher was revoked. |
| Seal / external decryption / credentials | No Seal access implementation exists in this baseline (SF-022). Inventory any out-of-band credentials, rotate them and reissue grants for the recipient, revoke old grants and use fresh ciphertext/keys for future confidential content. Previously disclosed plaintext cannot be revoked. Do not mark credential cutover complete merely because caps moved. Integrate the actual SF-022 API when implemented. |
| Walrus storage and renewal | Public content-addressed artifacts need no rewrite solely for ownership. Audit every blob object's actual renewal owner and expiry. Transfer transferable owned storage objects explicitly using the provider's supported tools, or arrange recipient-funded renewal/republish. Publisher-owned storage is not transferred by this protocol. |
| Prepaid gas/storage budgets, sponsorship and billing | Wallet coins, provider balances, sponsor credentials and off-chain accounts are separate. Revoke/replace sponsor access, transfer/refund supported balances explicitly, and fund recipient/device operations. No SF-083 budget policy API exists here; do not invent or imply one. |
| Telemetry/log/indexer/API access and deployed services | Rotate operator credentials and update external ACLs, coordinator wallet selection and service configuration. Chain authority transfer does not revoke an indexer token or an exported log. Device identity remains unchanged. |

Do not claim the old wallet is fully retired while it retains any upgrade,
provider or service-administration authority. The testnet fixture keeps its
UpgradeCap with the dedicated testing account: its old-owner negative tests
establish rejection by the tested package versions, not revocation of that
account's independent ability to authorize future code upgrades.

Do not put catalog-bound records into a kind-2 inventory. Their replacement is an
explicit deployment/revision operation by the recipient, not an undocumented
change in what an existing immutable recipe means. Review all retained native
recipe commands for embedded endpoints/credentials too.

## dApp procedure

1. Verify the network and latest deployment manifest, including the introduction
   origin of `fleet_handover::Handover`. Upgrade compatible contracts and regenerate
   both client manifests before using the new flow; do not replace older origins
   with the current call package. Existing manifests without this optional origin
   continue to support existing features and cannot initiate handover.
2. In **Fleet handover**, connect the source and enter the recipient and a typed
   JSON inventory, for example `[{"objectId":"0xCAP","kind":0},
   {"objectId":"0xGROUP","kind":1},{"objectId":"0xRECORD","kind":2}]`.
   IDs here are placeholders. Prepare external supersession/rotation separately.
3. Propose (24-hour expiry), obtain the created Handover ID from the successful
   transaction's effects, and share the ID and reviewed inventory/runbook with the
   recipient. A proposal alone moves no authority.
4. Recipient loads the Handover from the verified chain, checks source, recipient,
   complete inventory and expiry, then signs **Confirm as recipient**. A native
   multisig must use its threshold signing workflow; the source cannot confirm
   on its behalf. The app never asks for private keys. Wallets without native
   multisig signing can use the CLI/PTB procedure below.
5. Source executes batches. Refresh after any interruption and submit only pending
   entries. Prefer a single PTB for a device's cap, group and retained native
   records when it fits transaction limits. The UI batches 16 entries at a time.
   For fleets beyond 512 authority records, use several reviewed sessions; this
   does not create a global atomic cutover.
6. Recipient verifies current on-chain owners, membership, desired state, controls,
   receipts and old-owner rejection; completes catalog/credential/storage/budget
   supersession; deploys and observes a harmless component before resuming normal
   operations. Either party can **Finish** only after every chain step completed.
   Finished means that inventory's chain steps completed, not that off-chain
   provider credentials or hardware were tested.

## Sui CLI / native multisig

Use an explicitly selected `client.yaml`, verified manifest call package and the
manifest's registry. `CLOCK=0x6`. Check chain/account, UpgradeCap and type origins
using the release runbook first. Mainnet/production operations need their own
operator authorization; this implementation's tests authorize neither.

The following PTB shapes use `sui client --client.config /path/client.yaml ptb`.
Keep quotes around vector/string arguments; do not interpolate untrusted text
through a shell. `PACKAGE`, IDs, addresses and absolute millisecond expiry are
reviewed inputs, not inferred from historical creation-event owners.

```text
--move-call PACKAGE::fleet_handover::propose @RECIPIENT
  vector[@CAP,@GROUP,@RECORD] vector[0u64,1u64,2u64] EXPIRY_MS @0x6
--move-call PACKAGE::fleet_handover::confirm @HANDOVER @0x6
--move-call PACKAGE::fleet_handover::device @HANDOVER @CAP @0x6
--move-call PACKAGE::fleet_handover::group @HANDOVER @GROUP @0x6
--move-call PACKAGE::fleet_handover::deployment @HANDOVER @RECORD @0x6
--move-call PACKAGE::fleet_handover::revoke_role @HANDOVER @REGISTRY @ROLE_ID @0x6
--move-call PACKAGE::fleet_handover::close_enrollment @HANDOVER @POLICY @0x6
--move-call PACKAGE::fleet_handover::finish @HANDOVER
--move-call PACKAGE::fleet_handover::cancel @HANDOVER
```

Propose/steps use the source sender; confirm uses the recipient sender. For a
native multisig, build the recipient PTB with `--sender MULTISIG_ADDRESS` and
`--serialize-unsigned-transaction`, verify the decoded transaction's chain,
package, Handover ID, sender, gas owner/budget and commands, collect the configured
threshold signatures, then submit the combined native multisig signature with
`execute-signed-tx`. Keep private keys/recovery material out of files shared with
the other party or devices. Inspect execution effects and re-read Handover state
before retrying; a digest alone is not success. Do not substitute a multisig
member's single-signature address for the multisig sender.

## Verification boundaries

Move tests cover party/tenant/progress/desired-state authority; the real-daemon
simulator covers live owner changes, partial cutover, old-owner rejection and
restart without resetting keys or journals. The isolated frontend acceptance
exercises real Ed25519 and 2-of-2 native multisig signatures through the generated
builders. Record compatible upgrades/old-package attacks and authorized testnet
effects separately. No physical or production acceptance is established by these
software tests. SF-022/SF-083 remain separate work rather than invented interfaces.

Exact local entry points (inside the dev container with disposable loopback-only
networking for live tests):

```bash
make test-contracts check-interfaces test-manifest edge-test test-ota dapp-typecheck dapp-build
make test-protocol-live                    # includes Ed25519 + native multisig handover
make test-handover-upgrade-localnet        # compatible upgrade + old-package effects
```

The bounded `scripts/live_fleet_handover.py` runner reuses an already published,
verified dedicated-wallet package fixture. It requires the machine-session
`network:suifleet-testnet` lock and explicit package, UpgradeCap, registry, base
package and evidence path. It checks effects and charges source/child gas to the
strict non-replenishing run allowance; it never publishes a duplicate package or
requests faucet funds. Its disposable Ed25519 recipient is distinct from the
local native-multisig fixture. Public testnet results must name their actual
receipt and cannot be inferred from the local fixtures.
