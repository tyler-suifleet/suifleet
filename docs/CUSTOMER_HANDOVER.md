# Transfer fleet administration safely

Transfer a reviewed inventory of authority to a new account without sharing private keys. A proposal alone moves no authority.

## Prerequisites

Verify both accounts, network, current manifest and complete authority inventory. Include device caps, groups, retained native records, catalog, access policy, budget/storage and external credentials in the cutover plan. Some domains require separate supersession or rotation; the handover is not a universal atomic transfer.

## Steps

1. Open **Administration** as the source account. Under **Choose what to transfer**, enter the **New administrator’s account address** and select the checkboxes under **Resources you manage**. Review the selected inventory with the recipient before signing.
2. If verified resources are missing from the list, open **Advanced: specify additional resources** and enter typed JSON in **Additional resource list**, using actual object IDs. Kind `0` transfers a DeviceOwnerCap; `1` changes a DeviceGroup administrator; `2` changes a native deployment/rollout/control record creator. Do not include catalog-bound records as kind 2 or shared registries/package upgrade authority in a tenant handover. Do not duplicate selected resources.
3. Select **Create transfer request** (24-hour expiry). After confirmation, share the displayed transfer reference and reviewed inventory through a trusted channel. Creating a request alone moves no authority.
4. The recipient connects their account on **Administration**, enters the **Transfer reference** and selects **Review transfer**. They check source, recipient, inventory and expiry, then select **Accept as new administrator**. Multisig accounts need their real threshold-signing workflow.
5. The source opens the same reference with **Review transfer** and selects **Transfer next N resources**, where N is the pending batch size (at most 16). Refresh after interruption, check **Completed** and **Pending** progress, and submit only pending entries; large fleets require several reviewed sessions.
6. The recipient verifies current ownership, membership, desired state and old-owner rejection; completes catalog/credential/storage/budget changes; and tests a harmless deployment.
7. Either administrator selects **Finish transfer** only after all recorded chain steps complete. Retain the external cutover checklist separately.

## Expected result

The reviewed inventory's chain steps complete and the recipient verifies effective authority. No administrator secret is sent to devices or the recipient.

## Recovery and limits

If the manifest lacks handover support, ask the publisher for a compatible release rather than guessing origins. Stop on missing inventory or unexpected ownership. Confirmed batches remain completed after interruption; a new session is not permission to replay them. Finished does not prove provider credentials, real wallets or hardware were tested.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](FLEET_HANDOVER.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
