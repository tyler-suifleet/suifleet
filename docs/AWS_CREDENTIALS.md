# Scoped AWS component credentials

SF-057 supplies an opt-in Linux companion adapter in
[`scripts/aws_credentials.py`](../scripts/aws_credentials.py) and an exec wrapper in
[`sdk/python/suifleet_aws.py`](../sdk/python/suifleet_aws.py). Python 3.10+ and Linux 6.5+
SO_PEERPIDFD support, system libacl and POSIX ACL support on the runtime filesystem are required. The adapter implements the container credential endpoint for
TokenExchangeService **2.0.3**, rechecked against the
[AWS component reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/token-exchange-service-component.html)
on 2026-10-16. It uses the documented
[SDK container provider](https://docs.aws.amazon.com/sdkref/latest/guide/feature-container-credentials.html)
and [IoT X.509 role-alias exchange](https://docs.aws.amazon.com/iot/latest/developerguide/authorizing-direct-aws.html).
It does not translate AWS recipes or implement IAM Roles Anywhere's signed
CreateSession protocol. This implementation selects the declared IoT provider option.

## Provisioning and invocation

Provision a dedicated trusted adapter Unix user, distinct component Unix users,
and an AWS IoT certificate/private key, CA and authorized role alias separately.
Sui/Seal identities and the Sui PKCS#11 signer create no AWS credentials and are
never used here. Protected AWS identity distribution, if selected, must go through
SF-022; this adapter only loads operator-provisioned PEM files. No production
credential, image, user account or cloud resource is created by this change.

The execution boundary follows [SF-027](COMPONENT_IPC.md): each component needs
one distinct non-root UID, different from both the adapter and the SuiFleet daemon.
Use the same component name/UID mapping as the native IPC policy. Credential grants
are a separate, explicit operator policy on the credential socket; ordinary native
IPC permissions never imply a cloud grant. The daemon's shared-UID supervisor
cannot isolate components and is deliberately denied credentials. Run these clients
as platform-managed services under their own UIDs. Their lifecycle is supervised
by the platform service manager; this adapter does not change daemon lifecycle,
reboot or firmware recovery behavior.

Install the reviewed Python files as root-owned, read-only executable code outside
component workspaces. Store this JSON configuration at an absolute path, for example
`/etc/suifleet-aws/provider.json`, owned by the adapter user and mode 0600:

```json
{
  "socket": "/run/suifleet-aws/credentials.sock",
  "daemon_uid": 1001,
  "endpoint": "https://EXAMPLE.credentials.iot.us-east-1.amazonaws.com/role-aliases/sensor/credentials",
  "ca": "/etc/suifleet-aws/ca.pem",
  "certificate": "/etc/suifleet-aws/client.pem",
  "key": "/etc/suifleet-aws/client.key",
  "thing_name": "sensor-1",
  "token_ttl_seconds": 3600,
  "components": {"sensor": 2001, "ungranted": 2002},
  "grants": ["sensor"]
}
```

Replace example UIDs/endpoints with the actual provisioned values. All PEM files
must also be owner-only and adapter-owned. File ancestors must be root/adapter-owned,
non-writable by group/others, and contain no symlinks. The adapter grants socket access only to its exact configured component UIDs through a POSIX ACL; neither the owning group nor other users receive access. The socket directory must
be adapter-owned, traversable by its configured component UIDs (for example mode 0755, or 0700 plus named UID execute ACLs), with equally protected ancestors. Configuration must
list the actual daemon UID to prevent mapping shared daemon processes as a component.
Never allow untrusted code under the adapter, daemon or another component's UID.
Root, the adapter identity and operator policy/code remain trusted infrastructure.
Use one adapter/config/socket per IAM role scope; all granted components on that
adapter receive the same upstream role credentials. IAM policies control AWS actions.
A local grant is permission to obtain that scope, not a per-request IAM policy.

A platform service can run:

```sh
python3 /usr/libexec/suifleet/aws_credentials.py --config /etc/suifleet-aws/provider.json
```

For systemd, provision `User=suifleet-aws`, `RuntimeDirectory=suifleet-aws`,
`RuntimeDirectoryMode=0755`, `UMask=0077`, `NoNewPrivileges=yes`,
`CapabilityBoundingSet=`, `LimitCORE=0`, `ProtectSystem=strict`, `ProtectHome=yes`,
`PrivateTmp=yes` and `Restart=on-failure` in the platform-owned unit. Keep the
component and adapter in the same network namespace. The adapter needs outbound
HTTPS/DNS and `/proc/net/tcp`; do not hide that kernel table. There is no automatic
package/service installation. Disable core dumps for both adapter and components;
use platform swap protection and do not configure environment dumps or SDK debug
logging containing credentials. This is software process separation, with no HSM,
TPM or regulated-data certification claim.

As the dedicated **component** UID, use the wrapper in its platform service's
`ExecStart` (or equivalent foreground launcher):

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component sensor -- /opt/sensor/bin/sensor
```

The wrapper retrieves a fresh token without printing or storing it and execs the
client, preserving its kernel process handle. It removes inherited AWS credential
provider environment settings, sets `AWS_EC2_METADATA_DISABLED=true`, and supplies
`AWS_CONTAINER_CREDENTIALS_FULL_URI` and `AWS_CONTAINER_AUTHORIZATION_TOKEN` only
for granted components. It preserves region and ordinary application settings.
Provision no alternative AWS profiles/credential files in the component home:
unmodified SDK default chains may otherwise select them. Do not run the wrapper
through a shell that backgrounds the client: the token ends when the exec task
exits, even if descendants remain. A service restart obtains a new token. Tokens
expire after the configured 1–86400 seconds; restart the component to obtain a new
per-start token before/after that bound. AWS credentials refresh independently.

## Local protocol and failure behavior

The separate Unix control socket accepts one bounded newline JSON request with
exact fields `version: 1`, `operation`, and `component`. The wrapper's `start`
request is authenticated with kernel SO_PEERCRED and can only address its mapped
component. It revokes that component's preceding token and returns `env` or an empty
`env` for an ungranted component. `stop` revokes its token. `grant`/`revoke` are
restricted to the adapter service UID, operate only on configured principals,
and revoke any preceding token. These changes are volatile; edit the protected
configuration and restart to persist policy. Native daemon install/list/log APIs
never return credentials. Unknown caller fields, operations and versions fail closed.

HTTP listens on an ephemeral IPv4 loopback port at the exact path
`/2016-11-01/credentialprovider/`. GET returns the AWS `AccessKeyId`,
`SecretAccessKey`, `Token` and UTC-Z `Expiration` JSON fields. Both bearer token
and the kernel TCP connection UID must match the same configured principal.
Peer IP or a claimed caller header alone grants nothing. This Linux implementation
requires an unmodified, visible `/proc/net/tcp` in the same network namespace;
lookup ambiguity fails closed. Distinct Unix users prevent other components from
reading token environment/memory; tokens alone cannot cross the UID boundary.
Processes intentionally sharing a component UID are one trusted principal.

The adapter holds tokens and AWS credentials only in memory. It writes no journal,
cache, log or token file, emits no HTTP request diagnostics and refuses existing
socket paths. After an unclean service termination, inspect/remove the stale socket
before restart; never unlink an unknown active socket automatically. Adapter restart
invalidates all tokens and clears its cache. Already issued AWS credentials can remain
usable until their AWS expiry; local revocation cannot recall them from an SDK.

An upstream exchange starts within 300 seconds of expiry. Failures back off for
120 seconds (4xx), 60 (5xx), or 300 (transport/malformed/other responses), using a
monotonic clock. The 300-second transport backoff can postpone the next attempt
until expiry after a brief outage near the refresh boundary; operators must allow
for that availability limitation. Unexpired cached credentials remain available
both during backoff and while another caller holds the refresh lock;
expired/malformed credentials are never returned. HTTP 503 carries `Retry-After`
when no valid credentials exist. Tokens are rechecked after upstream I/O so an
intervening revocation wins. TLS validates CA/hostname and presents the configured
client identity; redirects and environment proxies are not used. Connect/socket
and total response deadlines, response/frame limits and bounded client slots
limit resource use. Connect and TLS handshake each use a five-second socket
timeout; the five-second total response timer begins after connection setup.
HTTP rejects unknown UIDs before reading request bytes and allows two concurrent
requests per configured component UID, within a global 16-request cap. Control
has its own two slots per component and separate administrator capacity. These
limits prevent one component from consuming all workers; simultaneous traffic
from multiple configured UIDs can still exhaust the global allowance. No
availability SLA is implied.

## Repeatable evidence and acceptance boundary

Run from the host with Docker, Python pip and the built pinned dev image:

```sh
make test-aws-credentials SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

Public SDK dependency preparation uses the network, hash-locked boto3 requirements
and a separate Rust SDK Cargo.lock (baseline Rust 1.94.0). The actual adapter test
then runs as root in a disposable `--network none` container with a read-only
checkout, distinct UIDs and a disposable local CA/mTLS upstream. It executes all
seven declared `credential-provider-cases.json` sequences against a separate adapter
process, plus wrong-UID/token/expiry, rotation, process/adapter restart, private-file
access, missing-client-certificate and wrong-CA cases. The production CLI is also
started and stopped, with no secret output or journal files. Unmodified boto3
1.43.114/botocore 1.43.114 and AWS SDK for Rust aws-config 1.8.18 resolve credentials
through their default chains from actual exec-wrapper clients. No SDK is patched.
Test clocks exist only in constructor-injected harness processes; production CLI,
HTTP and control requests cannot select test time or injected upstream results.

These adapter execution checks are separate from the existing reference-model
`make test-compat` checks. They establish local Linux adapter behavior and SDK
resolution, not a live AWS role alias/service call, general Greengrass compatibility,
physical Pi acceptance or a deployed production credential service. Live AWS
account/service acceptance remains **unrun and unauthorized in SF-057/#102**;
physical target integration remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
