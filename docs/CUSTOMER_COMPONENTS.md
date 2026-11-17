# Publish and deploy components

Use versioned components to distribute authenticated software for a specific operating system and architecture.

## Prerequisites

Connect the publishing account, have network and storage funds, an installed registered target, and reviewed artifact bytes with explicit lifecycle behavior. For confidential delivery, first create an [access policy](CUSTOMER_ACCESS_POLICIES.md) and configure verified key servers.

## Steps

1. Open **Components** and select **Publish version** (or **Publish your first version** for an empty library). If prompted, complete **Set up publishing** to create your publisher catalog. Enter the component name, version and platform manifests matching your targets; manifest selection uses the first matching OS/architecture entry.
2. Review the artifact and recipe. Enable recipe commands only on devices authorized to execute those commands. Recipes with neither install nor run delegate to the explicitly configured platform updater.
3. Choose confidential delivery and its policy for proprietary content. Public delivery must be an explicit acknowledged choice. Uploads consume storage even if a later wallet step fails.
4. Approve the version registration. Check successful effects and refresh the component before deploying it.
5. Open **Deploy component**, select the device or group and intended release. Review targets and rollout controls before approving. For a managed library, select desired versions deliberately rather than treating every new upload as a deployment.
6. Inspect each target's authenticated receipt and application readiness. Expand gradually only after the first harmless test succeeds.

## Expected result

The immutable version appears in the catalog and eligible devices select the matching manifest, authenticate recipe and artifact digests, then report their command/lifecycle result.

## Recovery and limits

If no manifest matches, publish a corrected version rather than changing immutable bytes. For download, policy or digest failures, repair the cause before a new deployment. Refresh after uncertain submission to avoid duplicates. An Applied receipt is a command/readiness result, never proof of healthy OS boot. Platform installers own reboot and firmware rollback.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](COMPONENT_CATALOG.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
