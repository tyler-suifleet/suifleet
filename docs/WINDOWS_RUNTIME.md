# Windows software runtime (SF-074)

The native Windows x86_64 backend runs operator-installed executable components
as a Windows service. Build `cargo +1.94.0 build --locked --manifest-path
suifleet-edge/Cargo.toml --no-default-features` on Windows. The same `suifleet`
binary selects the Windows backend with `cfg(windows)`; Linux continues to use
its existing daemon. The small `windows-runtime` crate can also build separately.
Windows Server 2022 (the `windows-2022` hosted software runner) is the acceptance
platform; this does not establish support for every Windows desktop/server build.

## Semantic and licensing baseline

Reviewed before implementation: Greengrass nucleus **2.5.0**, upstream tag commit
[`dbfe4447e6395625418d1ea66884c3e3fe893949`](https://github.com/aws-greengrass/aws-greengrass-nucleus/tree/dbfe4447e6395625418d1ea66884c3e3fe893949),
and the AWS [OS feature matrix](https://docs.aws.amazon.com/greengrass/v2/developerguide/operating-system-feature-support-matrix.html)
and [service configuration](https://docs.aws.amazon.com/greengrass/v2/developerguide/configure-greengrass-core-v2.html)
as accessed 2026-10-19. These establish Windows lifecycle/service scope, not API
compatibility or execution evidence. This is independently written Rust code;
no AWS source, binaries, SDK implementation or license-restricted fixtures are
included. The harmless component example is repository-authored.

Win32 API bindings are pinned to `windows-sys = 0.61.2`; Rust is 1.94.0.
The native design follows Microsoft's [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw),
[named-pipe security](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
and [client impersonation](https://learn.microsoft.com/en-us/windows/win32/ipc/impersonating-a-named-pipe-client)
contracts (accessed 2026-10-19). Jobs contain ordinary process descendants; they
are not a sandbox for privileged users or a replacement for OS account security.

## Provisioning and service lifecycle

Use a fresh local NTFS directory under `C:\ProgramData`. Before writing any
configuration, remove inherited ACLs and grant full control only to SYSTEM,
Administrators and the designated operator SID. Install the daemon there.
Provision a dedicated **non-administrator** local component account with logon
permission. Its password belongs only in this protected runtime configuration;
never embed it in packages, images, source, logs or command-line arguments.
The service runs as LocalSystem so it can obtain the component token and create
its process. Administrators/SYSTEM and the configured operator are trusted;
components must never use their account. One component principal is supported
per service instance; mutual isolation between multiple components is not claimed.

`config.json` directly inside that directory has exactly these fields:

```json
{
  "root": "C:\\ProgramData\\SuiFleet",
  "pipe": "suifleet-local",
  "operator_sid": "S-1-5-21-REPLACE-WITH-OPERATOR-SID",
  "component_sid": "S-1-5-21-REPLACE-WITH-COMPONENT-SID",
  "component_user": "suifleet-component",
  "component_password": "RUNTIME-ONLY-REPLACE"
}
```

The SID placeholders are deliberately invalid. Obtain real SIDs from
`Get-LocalUser`/`WindowsIdentity`. Create the service with elevated PowerShell:

```powershell
New-Service -Name SuiFleet -StartupType Automatic -BinaryPathName '"C:\ProgramData\SuiFleet\suifleet.exe" --service "C:\ProgramData\SuiFleet\config.json"'
Start-Service SuiFleet
Stop-Service SuiFleet
Start-Service SuiFleet
```

SCM startup errors fail closed. Stop/shutdown closes component Jobs; abrupt daemon
termination also closes their non-inheritable handles and kills descendants.
Persisted desired running state bootstraps on service restart. Process exit zero
is not interpreted as a healthy OS boot. SCM failure restart policy is operator
controlled; configure it with normal Windows service management if desired.

## Authenticated component selection and installation

Only the configured operator's actual token SID or SYSTEM may install/start/stop.
The component SID may request sanitized status only. The local-only named pipe
rejects remote clients, uses a protected DACL and reserves the first server
instance. After reading a request the service impersonates its caller, obtains
TokenUser, and reverts before executing work. Caller-supplied identity is never
authority. Pipe clients use specific data rights, excluding create-instance rights.
Unknown operations/fields fail closed. Requests and connections are bounded.

Write a JSON request into an operator-private file and invoke:

```powershell
& 'C:\ProgramData\SuiFleet\suifleet.exe' --request suifleet-local C:\private\request.json
```

An installation request selects exactly `windows-x86_64`; no shell, automatic
updater detection, downloaded scripts or implicit platform fallback is used:

```json
{
  "operation": "install",
  "artifact": "C:\\private\\app.exe",
  "manifest": {
    "schema_version": 1,
    "name": "example.app",
    "version": "1.0.0",
    "platform": "windows-x86_64",
    "sha256": "REPLACE-WITH-64-HEX-DIGEST",
    "install": ["install"],
    "run": ["run"],
    "shutdown": ["shutdown"],
    "timeout_seconds": 30,
    "max_restarts": 3
  }
}
```

The privileged operator authenticates the entire manifest through its token;
the artifact is copied from a local absolute path, size bounded and SHA-256
verified before stopping a running component. Cached executables are verified
again on start. Arguments go directly to that executable using Windows CRT
quoting, without a shell. Install/shutdown are optional invocations of the same
authenticated executable. Both have bounded duration; install cannot synchronously
call the runtime IPC server. Run is supervised with a bounded restart budget and
one-second delay. There is no readiness protocol; running means process launched.

Executable directories grant the component read/execute access; only its `work`
directory is writable. Service configuration/state remain private. Components
inherit neither daemon handles nor environment secrets, and have a minimal
environment. The runtime checks logon token SID and rejects elevated tokens.
Processes are created suspended, placed in a non-breakaway kill-on-close Job,
then resumed. Assignment failure terminates the suspended child.

`{"operation":"status"}`, `{"operation":"stop"}` and
`{"operation":"start"}` are the other operations. Stop runs the configured
shutdown invocation then closes the entire Job even if shutdown fails. State
writes use flushed temporary files and write-through atomic replacement. An
interrupted install is quarantined on startup; it is never replayed or started
automatically. An explicit authenticated reinstall is required. Preserve this
state across platform-controlled restarts.

## Explicit unsupported operations and acceptance

This backend is a native local software runtime, not full Greengrass parity.
It rejects Linux CLI operations and provides no Sui event reconciliation/signing,
Walrus downloading, Linux recipe/shell translation, dependency graph, fleet service
policy (SF-081), component SDK/pubsub/shadow/stream APIs, firmware helper,
self-update, PKCS#11 signer, arbitrary run-as identity or privileged component
execution. AWS recipe translation continues to omit Windows manifests; a native
manifest is required. OS installation, reboot decisions, boot confirmation and
firmware rollback remain platform-owned; the service implements none of them.
Do not use this subset to claim those unimplemented APIs are accepted.

`./scripts/test_windows_runtime.ps1` runs elevated on a disposable Windows runner.
It creates temporary accounts/service and uses real executables for installation,
shutdown, crash/restart, detached descendant containment, file/pipe/caller denials,
wrong digest/platform, timeout and interrupted-install recovery. It writes only
sanitized `windows-evidence.json`; config/passwords are deleted in cleanup and are
never uploaded. `.github/workflows/windows-runtime.yml` executes it without cloud
credentials. Cross-compilation and reference vectors alone do not establish any
of these Windows results. Record the actual run URL/head with the ticket evidence.

Physical target acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No live AWS acceptance or account mutation was authorized or run; existing
[SF-064/#109](https://github.com/tyler-suifleet/suifleet/issues/109) tracks remaining
functional scope. Normal CI and applicable automatic testnet gates remain in force.
