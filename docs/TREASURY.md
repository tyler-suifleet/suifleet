# Company treasury and pinned subscription payee (SF-185)

Subscription revenue (crypto and card settlement) belongs to the company treasury.
The registered `suifleet.sui` name currently targets the user's personal wallet;
it is a display/discovery name, never payment authority. The user reports registration
on 2026-10-27 and expiry on 2030-10-26. Recheck ownership, target and expiry before
any later change. This document proposes the custody procedure for user review;
no production treasury or hardware ceremony has been performed.

## User-only hardware ceremony

Use a native **2-of-3**, equal-weight Sui multisig: three independently generated
Ed25519 hardware keys, each weight 1, threshold 2. A single lost key leaves a
quorum; any two compromised keys authorize all funds. Three accounts derived from
one seed do not provide independent recovery or custody.

Before funding, the user must designate three company-approved key holders and
record their names, responsibilities and emergency contacts in a private company
custody register. Proposed roles: operating officer, second company officer, and
an independent recovery custodian. These are roles to fill, not claims that
people have been appointed. A sole operator may keep independently seeded devices
in separate secure locations during setup, but must record the resulting shared
custody risk. Do not represent that as independent approval by three people.

1. Each holder initializes their own supported hardware device privately, verifies
   the device/app/firmware and exports only the Sui public key and derivation path.
   Record device compatibility and test the actual hardware signing flow before
   accepting revenue. No agent receives a device PIN, seed, private key or backup.
2. Each holder stores their recovery seed offline, separately from the device and
   other holders' backups. Document physical access, succession and recovery
   arrangements privately. Do not put recovery information in GitHub or this repo.
3. Freeze the ordered descriptor: signature schemes, public keys (scheme-prefixed
   base64), weights `[1,1,1]`, threshold `2`. Preserve public copies with each
   holder; public keys alone do not spend funds. Key order is part of the address.
4. Independently derive the address on two trusted machines using reviewed Sui
   tooling and compare the full 32-byte address, descriptor and threshold:

   ```bash
   sui keytool multi-sig-address --pks PUBLIC_KEY_1 PUBLIC_KEY_2 PUBLIC_KEY_3 --weights 1 1 1 --threshold 2 --json
   ```

   This is offline public-key arithmetic, not an on-chain account-creation
   transaction. Save the public descriptor and tool versions for source review.
5. After separately authorizing mainnet testing, the user funds a small amount and
   verifies a spend with two hardware signers. Each signer inspects the same
   decoded bytes: chain, sender, gas owner/budget, recipient, amount and all PTB
   commands. Verify execution effects and recipient balance independently.
   Exercise alternate pairs and one-key refusal on testnet first. Agent software
   fixtures do not establish hardware acceptance.
6. After user approval of the descriptor/address, pin its full lowercase address as
   `treasury_payee` in the reviewed **mainnet deployment manifest**, regenerate
   the frontend environment and rebuild/release clients. Do not put a personal,
   placeholder or disposable testnet address in the mainnet manifest. No mainnet
   manifest is configured by SF-185.

The company custody register must also define payment reconciliation, spending
approvals and response contacts. Card providers must settle to this same reviewed
payee; a provider integration must reject a conflicting settlement address.
Provider commercial/account onboarding remains final external acceptance.

## Recovery and rotation

For one lost or suspect key, the remaining two holders create a new descriptor
with a fresh hardware key, independently verify its address, and authorize migration
while a quorum remains. Changing membership, weights, ordering or threshold changes
the native multisig address: there is no in-place signer rotation. Inventory and
transfer coins, owned objects and any treasury-held capabilities; explicitly update
shared-object authorities that reference the old address. Verify receipts and
balances before retiring it. With fewer than two recoverable keys, this descriptor
cannot authorize recovery; neither SuiFleet nor the name service can override it.

Coordinate a reviewed manifest/client release and pause new checkouts during
rotation. Old static clients retain their compiled payee: stop their distribution,
track outstanding payments and communicate the cutoff before moving funds. A
static pin prevents mutable-source substitution, but cannot remotely revoke an
already distributed old build. Preserve records and monitor the old treasury for
late receipts according to the company policy.

## Later user-only SuiNS actions

After the production treasury is verified, the user who owns `suifleet.sui` must
set its target to the treasury address and verify forward resolution. Setting a
name's default/reverse association is a separate transaction authorized by the
**target treasury**, using its signing quorum; changing the target alone does not
establish it. Use the current SuiNS-supported transaction flow, verify the name
object and authority, then check both forward and reverse resolution. If a wallet
UI cannot sign as the multisig, use reviewed multisig-compatible tooling; do not
route revenue through a personal account as a workaround. Decide separately
whether to transfer the registration object into company custody, and plan renewal
before expiry. Agents perform none of these mainnet actions.

The [Sui multisig documentation](https://docs.sui.io/develop/transactions/transaction-auth/multisig)
describes weighted thresholds and public-key address derivation. The
[SuiNS integration guide](https://docs.sui.io/sui-stack/suins/developer) and
[transaction reference](https://docs.sui.io/sui-stack/suins/developer/sdk/transactions)
distinguish setting the target and setting the default name.

## Software payment boundary

`treasury_payee` is an additive schema-1 manifest field. It must be exactly `0x`
followed by 64 lowercase hex digits, and nonzero. Missing fields permit older
nonpayment deployments, but `treasuryPayee()` refuses payments with a visible
`treasury payee mismatch` error. Present malformed fields reject the manifest.
Payment/checkout implementations must obtain their recipient using this guard
and pass any proposed/provider recipient to `treasuryPayee(expectedPayee)` for
comparison before signing or opening checkout. They must also apply the existing
chain/deployment verification before transactions. Never derive a recipient from
SuiNS, an environment override, connected wallet, URL parameter or provider reply.
There is no subscription checkout in this revision; SF-181–184 consume this boundary.

`deployment_manifest.py resolve --treasury-payee ADDRESS` pins the reviewed payee;
`redeploy.sh --treasury-payee ADDRESS` does the same for publication. Upgrades
preserve it when the option is omitted, including pending-record recovery.
Generating a bundle carries the address into `NEXT_PUBLIC_DEPLOYMENT_MANIFEST`.
An address string cannot prove hardware custody: descriptor review and the ceremony
above remain separate acceptance.

## Disposable testnet fixture

The public testnet descriptor belongs at `deployments/testnet/treasury.json` after
a successful run. It is a payment configuration overlay, **not** a deployed package
manifest: copy its `treasury_payee` into the verified testnet deployment manifest
using `resolve --treasury-payee`, then generate/rebuild the clients. It uses the
same 2-of-3 equal-weight Ed25519 shape with disposable software keys. Never use
these keys or this address for company funds. Private state stays under the
operator's private state directory outside the checkout and all public artifacts.

To reproduce, use the [testnet run controls](TESTNET.md): coordinate in SF-010/#11,
acquire `network:suifleet-testnet`, export its token privately as
`SUIFLEET_TESTNET_LOCK_TOKEN`, assert before mutation and release promptly. With
pinned CLIs, built `chain_facts` and locked frontend dependencies:

```bash
python3 scripts/live_treasury.py \
  --wallet /home/tyler/.config/suifleet/testnet/agent/client-host.yaml \
  --state /home/tyler/.local/state/suifleet/sf185-treasury \
  --output /tmp/sf185-testnet.json
```

On the host, use the existing `client-host.yaml` for the dedicated agent (its
keystore path resolves on the host); `client.yaml` is the container-path variant.
Both must derive the dedicated agent address in TESTNET.md.

The executor verifies testnet `4c78adac` and the dedicated agent account, funds
0.01 SUI, then signs one 1-MIST transfer back using two fixture keys. The fixed
0.03-SUI allowance includes funding and failed/unknown transaction reservations;
refunds do not replenish it. The script refuses to fund keys that differ from the
reviewed public descriptor and independently re-derives its address with the pinned
CLI. If the private fixture is lost, generate a replacement offline and review its
public descriptor change before funding; do not silently redirect test clients. No faucet or package publication is needed. Execution
effects, digests, gas and toolchain provenance go to the public evidence file;
private keys never do. There are no package/UpgradeCap/type origins involved in
these native SUI transfers. Retained fixture funds support SF-181–184 test payments.

### Verified software/testnet evidence (2026-10-28 UTC)

The [sanitized receipt](validation/sf185-testnet.json) records the pinned
2-of-3 fixture funding and signed 1-MIST spend, both independently read back
with successful effects at checkpoints 399994894 and 399995146. Total net gas
was 2,988,198 MIST. The operator allowance consumed 11,000,099 MIST including
the 10,000,000-MIST fixture grant, below the unchanged 30,000,000-MIST ceiling;
the child spent 1,988,099 MIST from that already charged grant. No faucet,
package publication or original application objects were involved.

The initial spend builder rejected an untyped sender before signing/submission.
After correcting the CLI address prefix, recovery reverified the funding effects
and balance and continued the original allowance without funding again. The
receipt retains the zero-cost refusal and both source heads. The wallet lease
was asserted for mutations and released after acceptance.

The key utility now uses exclusive creation and validates/reads one no-follow
file descriptor. Offline regressions reject symlinks, hardlinks, public file
permissions, directories and FIFOs, and deterministically replace the pathname
after validation to prove reads retain the validated inode. Real signature tests
cover alternate quorum, insufficient quorum, modified bytes and wrong sender.
These repair the two confirmed `js/file-system-race` findings; fresh hosted
CodeQL/security CI and independent review remain required and are not claimed
passed by local tests.

Recovery validation: pinned network-none `make INSIDE_DEV_CONTAINER=1 test-manifest`
passed (one pre-existing historical-receipt test skipped because its revision is
absent in this shallow checkout); 20 Python manifest tests, 38 publication tests,
and 13 budget tests passed. The corrected sender syntax was exercised by the
actual pinned-CLI testnet spend above. No frontend source or dependencies changed
in recovery; the original pinned typecheck/build evidence remains historical.

Pending external acceptance: user review of this runbook, named key holders,
hardware ceremony/spend, mainnet address pin, SuiNS target/default, and production
card settlement. Keep these in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159); none is
claimed passed here.
