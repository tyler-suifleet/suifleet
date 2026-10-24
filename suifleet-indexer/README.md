# Optional customer-hosted reference integration

This crate is not SuiFleet infrastructure and is not required by any product
view or device data path. SuiFleet operates no application servers.

Customers may run this API/indexer/HTTP telemetry/log ingress, webhook delivery
and legacy API CLI for their own integrations. They own deployment, credentials,
availability, retention, auditing and security operations. Product packaging does
not install/start it. Explicit Make targets retain its regression tests.

Use the static dApp and scripts/suifleet_ops.cjs for direct-chain operations.
See [product inventory/migration](../docs/SERVERLESS_PRODUCT.md) and
[reference API documentation](../docs/INDEXER.md).
