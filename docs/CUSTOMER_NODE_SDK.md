# Build components with the Node.js SDK

Use the TypeScript/Node.js package to read component configuration, report health, exchange messages, keep durable streams and follow device shadows over the device's native IPC socket. This is not AWS IPC wire or SDK compatibility.

## Prerequisites

- Node.js 20 or newer on a Linux device with an installed daemon and native IPC enabled as described in [Connect applications to native IPC](CUSTOMER_COMPONENT_IPC.md).
- A separate platform-managed Unix user for each application, with an operator ACL granting only the operations, topics, streams and shadows it needs. Applications running as the daemon user are denied.
- The package tarball built from a reviewed release commit, with its recorded SHA-256. It is not on the public npm registry yet.

## Steps

1. Get the tarball one of two ways. Either way, its SHA-256 must match the release record before you install it:

   - **Verify a supplied tarball.** Hash the file you were given. It does not need to be rebuilt:

     ```sh
     sha256sum suifleet-component-ipc-1.0.0.tgz
     ```

   - **Rebuild it from the reviewed commit.** Check out that commit and use the pinned dev image (its Node.js, pnpm and npm versions are part of the recorded build). The build uses the committed lockfile and packs from a staging copy with fixed file modes, so checkout permissions and umask do not change the hash. It prints the SHA-256:

     ```sh
     make sdk-node
     ```

     A different hash from the same commit means a different toolchain or a modified tree. Do not install that tarball.

2. Install the verified tarball into your application:

   ```sh
   npm install /path/to/suifleet-component-ipc-1.0.0.tgz
   ```

3. Connect with the component name the operator assigned to your application's Unix user:

   ```js
   import { Client, IpcError, TransportError } from '@suifleet/component-ipc';

   const client = new Client({ socketPath: '/run/suifleet/ipc.sock', component: 'sensor', connectAttempts: 20 });
   const configuration = await client.configuration();
   await client.health('healthy');
   await client.publish('sensor/events', { celsius: 21 });
   ```

4. Start from the [runnable examples](../sdk/typescript/examples): a sensor using a durable stream, a durable message consumer and a shadow reporter. Each lists the ACL it needs.
5. Before deploying, run one permitted and one denied request from the application's actual Unix user.

## Expected result

Permitted requests return the daemon's data. Denied requests raise `IpcError` with code `request_failed` and have no effect. A request from an unconfigured Unix user is closed without a response.

## Recovery and limits

- `TransportError` with outcome `not_sent` means nothing reached the daemon, for example during a daemon restart; it is safe to retry, and `connectAttempts` does so automatically.
- Outcome `unknown` means the request may have run. Do not repeat a change blindly: check status, the stream's newest sequence or the shadow version first.
- Backpressure (`full`, a full inbox or subscription queue) is a refusal with nothing stored. `OutboundBuffer` holds a bounded number of messages in memory and resends them in order; it does not survive a process restart. It stops instead of resending when a delivery is uncertain, including a generic `request_failed` from a stream append, which may follow a stored record.
- Acknowledge durable messages only after processing; unacknowledged messages are delivered again, including after a daemon restart.
- macOS is not yet tested with this package, and Windows uses a different interface.

## Technical reference

For integrators and auditors: [API, error semantics, distribution/provenance and validation](NODE_SDK.md), and the [IPC protocol](COMPONENT_IPC.md). These include engineering history; they are not a claim of production acceptance.
