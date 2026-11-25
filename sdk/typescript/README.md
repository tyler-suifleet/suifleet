# @suifleet/component-ipc

TypeScript/Node.js client for the native SuiFleet component IPC v1 protocol:
configuration, health, local pub/sub (volatile and durable), durable streams,
device shadows, opaque data intake and administrator management.

This is a native SuiFleet interface. It is **not** AWS Greengrass IPC wire,
SDK or CLI compatible. Access is decided by the daemon from the kernel peer UID
of the calling process and the operator's ACL; no token or claimed name
authenticates a client.

- Node.js 20 or newer, ESM, no runtime dependencies.
- Linux is tested against the real daemon. macOS daemons expose the same
  protocol but this package is not yet tested there.

```js
import { Client, IpcError } from '@suifleet/component-ipc';

const client = new Client({ socketPath: '/run/suifleet/ipc.sock', component: 'sensor' });
const configuration = await client.configuration();
await client.health('healthy');
await client.publish('sensor/events', { celsius: 21 });
```

Runnable examples are in `examples/`. Installation, distribution/provenance,
error and retry semantics, and the full API are documented in
[docs/NODE_SDK.md](../../docs/NODE_SDK.md); the protocol is in
[docs/COMPONENT_IPC.md](../../docs/COMPONENT_IPC.md).
