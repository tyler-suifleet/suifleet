# Fund and monitor fleet network costs

Prepay bounded device transactions and storage. Depositing funds does not bypass device quotas or guarantee service availability.

## Prerequisites

Connect the budget owner on the intended network. Have SUI for transactions and the funding source supported by your deployment. Storage requires a configured fleet-storage package and WAL/pool capacity; do not assume every network build exposes it.

## Steps

1. Open **Network budget**, review workload estimates and create a budget with an initial deposit, or deposit into the selected existing budget. Confirm account, network, amount and fees in the wallet.
2. Enroll only the intended devices, with explicit float and reserve limits. Ongoing report gas is separate from single-use claim enrollment sponsorship.
3. Supply the correct budget settings to the daemon and signer using the technical configuration reference. Both must refer to the same fleet budget when reporting and gas support are combined.
4. Review balance, runway, per-device usage and low/paused alerts. Refill or reclaim reserves deliberately; refresh after partial batch enrollment before resuming.
5. Where **Prepaid Walrus storage** is available, fund WAL and refill the pool after reviewing live capacity, expiry and cost. Monitor storage separately from SUI report gas.

## Expected result

Confirmed deposits and enrollment appear after refresh. Authorized devices draw only within configured contract limits and storage consumers remain subject to quota and expiry.

## Recovery and limits

For an empty reserve, paused budget or expired pool, resolve that specific condition rather than repeatedly deploying. A displayed estimate or submitted digest is not proof of payment. Withdraw only unallocated funds; pause/resume and removal have distinct effects. Do not publish credentials or fund an unverified account. Card/provider availability and final commercial terms require confirmation for your environment.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](FLEET_BUDGET.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
