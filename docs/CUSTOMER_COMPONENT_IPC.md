# Connect applications to native IPC

Use the local Unix-stream API to read component configuration, report health and exchange explicitly authorized messages. This is not AWS IPC wire or SDK compatibility.

## Prerequisites

Use Python 3.10+ for the supplied SDK, an installed daemon and separate platform-managed Unix identities for IPC clients. Shared daemon-user components cannot receive individual component ACLs. Provisioning users and directories is the platform's responsibility.

## Steps

1. Choose a daemon-owned, non-group/world-writable socket parent outside component workspaces. For the shipped strict systemd unit, add a drop-in with `RuntimeDirectory=suifleet` and `RuntimeDirectoryMode=0755` under `[Service]`, then reload units during a maintenance window.
2. Configure `[ipc]` and only the required component operations/topics. The technical reference provides the full ACL example. Use each client's real separate UID; never make the daemon UID an administrator.
3. Check device configuration and restart through the platform service manager. Run the supplied CLI as an explicitly authorized administrator:

```sh
python3 sdk/python/suifleet_ipc.py --socket /run/suifleet/ipc.sock list
```

4. Test a permitted request and a denied request from the component's actual OS user before deploying the application.

## Expected result

The kernel peer identity, configured component name and operation/topic ACL determine access. No claimed JSON name or bearer token substitutes for that identity.

## Recovery and limits

Unknown callers are denied. Inspect UID/ACL and directory ownership rather than widening permissions. A preexisting socket is not automatically deleted: stop the old service, confirm no listener remains and inspect the stale path before removal. Root and daemon-owned configuration are trusted; this is not isolation from malicious code running as the daemon user.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](COMPONENT_IPC.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
