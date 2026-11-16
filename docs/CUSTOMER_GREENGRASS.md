# Migrate from AWS IoT Greengrass

Plan migration by capability and application behavior. SuiFleet provides native fleet management and local services; it does not promise drop-in AWS SDK, IPC or control-plane compatibility.

## Prerequisites

Inventory recipes, lifecycle scripts, IPC calls, cloud credentials and target platforms. Prepare a test device and authenticated artifact bindings. Keep the old deployment recoverable while testing.

## Steps

1. Separate native capabilities from AWS protocol dependencies. Native component supervision, configuration, messaging, shadows, streams and LAN client-device services have implementations. AWS cloud adapters require explicit configuration and separate credentials; local fixture evidence does not establish live AWS account acceptance.
2. On **Components**, open **Import AWS Greengrass recipe**. Supply a JSON/YAML recipe and bind every required artifact. The translator does not fetch S3 URIs automatically.
3. Review every rejection and reported difference. The supported subset translates install/run/shutdown and selected configuration/dependencies; bootstrap/startup/recover/uninstall and unsupported privilege/environment features require porting.
4. Port AWS IPC clients to the [native IPC interface](CUSTOMER_COMPONENT_IPC.md). Provision separate Unix identities and explicit ACLs; unmodified AWS component SDKs are not compatible.
5. Test installation, readiness, configuration changes, restart and failure handling with harmless content before moving a group. Retain your platform's recovery procedure.

## Expected result

A supported recipe becomes a validated native version with explicit differences. An unsupported recipe fails with reasons instead of silently changing semantics.

## Recovery and limits

Keep rejected recipes out of production. Fix or port the reported feature rather than suppressing validation. Native service availability is not a full Greengrass parity claim. Physical targets, production providers and manual real-wallet/mainnet acceptance require separate validation. The technical specification below contains historical status statements; use the current implementation matrix and service references when evaluating individual capabilities.

See the [machine-readable compatibility matrix](../interfaces/compat/greengrass-compat.json), [messaging reference](MESSAGING.md), [shadows reference](SHADOWS.md) and [streams reference](STREAMS.md).

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](GREENGRASS_COMPATIBILITY.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
