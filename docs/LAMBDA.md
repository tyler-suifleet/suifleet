# Function invocation and legacy-style subscriptions (SF-069)

[`scripts/lambda_manager.py`](../scripts/lambda_manager.py) is an opt-in,
platform-managed Python 3.10+ Linux companion. It receives durable
[native pub/sub](MESSAGING.md) messages, routes them through explicit
source/subject subscriptions and executes real function processes, on demand or
long-lived, with timeouts, bounded concurrency and queues, process isolation and
versioned invocation/result envelopes. Function code uses the dependency-free
handler runtime [`sdk/python/suifleet_function.py`](../sdk/python/suifleet_function.py).

This is a clean-room functional alternative written from public documentation. It
is not AWS Lambda, the Greengrass Lambda manager/launcher/runtimes or legacy
subscription router, their recipes, SDKs or wire protocols, and it executes no AWS
component code, binaries or runtime images. Recipes declaring the Lambda component
type are still rejected by the [SF-037 translator](GREENGRASS_COMPATIBILITY.md);
package the function code with this manager instead.

The versioned contract and upstream references reviewed before implementation are
in [`lambda-v1.json`](../interfaces/compat/lambda-v1.json):
[Lambda manager 2.3.9](https://docs.aws.amazon.com/greengrass/v2/developerguide/lambda-manager-component.html),
[legacy subscription router 2.1.17](https://docs.aws.amazon.com/greengrass/v2/developerguide/legacy-subscription-router-component.html),
[Lambda launcher 2.0.13](https://docs.aws.amazon.com/greengrass/v2/developerguide/lambda-launcher-component.html),
[Lambda runtimes 2.0.9](https://docs.aws.amazon.com/greengrass/v2/developerguide/lambda-runtimes-component.html)
and the [function parameters](https://docs.aws.amazon.com/greengrass/v2/developerguide/import-lambda-function-cli.html)
(reviewed 2026-10-19). Only functional semantics were taken from them.

## Operator configuration

Install the manager, `sdk/python/suifleet_ipc.py` and function code outside writable
component workspaces, read-only to functions. Provision a dedicated manager UID as an
IPC component with `subscribe`, `receive`, `ack` and `unsubscribe` plus a
`SubscribeToTopic` policy for each subscription subject, and `publish` with a
`PublishToTopic` policy for each `result_topic`. Do not give it legacy `subscribe`
lists: messages without a durable `id` are ignored. Each source component needs
its own `PublishToTopic` grant; the daemon authenticates the sender by peer UID.

```json
{
  "version": 1,
  "socket": "/run/suifleet/ipc.sock",
  "component": "functions",
  "state_dir": "/var/lib/suifleet-functions/state",
  "work_dir": "/var/lib/suifleet-functions/work",
  "max_pending": 256,
  "functions": {
    "enrich": {
      "mode": "on-demand",
      "command": ["/usr/bin/python3", "/usr/libexec/suifleet-functions/enrich.py"],
      "timeout_ms": 3000,
      "max_concurrency": 4,
      "max_queue": 100,
      "result_topic": "functions/enrich/results",
      "environment": {"UNITS": "metric"},
      "run_as": {"uid": 2201, "gid": 2201},
      "memory_bytes": 134217728
    },
    "aggregate": {
      "mode": "long-lived",
      "command": ["/usr/bin/python3", "/usr/libexec/suifleet-functions/aggregate.py"],
      "timeout_ms": 1000,
      "result_topic": "functions/aggregate/results",
      "run_as": {"uid": 2202, "gid": 2202}
    }
  },
  "subscriptions": [
    {"source": "sensor", "subject": "sensors/temperature", "target": "enrich"},
    {"source": "@bridge/LocalMqtt", "subject": "lan/readings", "target": "aggregate"}
  ]
}
```

Run in the foreground under a platform unit as the manager UID. For `run_as`, grant
only `AmbientCapabilities=CAP_SETUID CAP_SETGID CAP_CHOWN` (bounding set the same),
or pre-create each work directory owned by its function UID with mode 0700 and omit
`CAP_CHOWN`. These capabilities let the manager assume any UID, so the manager,
its configuration and its code are trusted infrastructure. Use `KillMode=control-group`
(the default) so stopping or crashing the unit ends every function descendant. Set
`UMask=0077` and disable core dumps. No `CAP_KILL` is needed: when the manager may not
signal a function UID directly, a short-lived helper running as that UID sends the
SIGKILL.

```sh
python3 /usr/libexec/suifleet/lambda_manager.py --config /etc/suifleet/functions.json
```

A root manager refuses any function without `run_as`. Without `run_as` (non-root
manager), functions share the manager UID and could use its IPC identity and
journal; that is not an isolation boundary. Give each function a distinct non-root
UID, which may also be provisioned as its own IPC component. Function UIDs cannot
read the manager's private state directory (0700) or authenticate as the manager.

`state_dir` must be manager-owned, mode 0700, on a local filesystem supporting
flock and fsync. One manager holds an exclusive lock; state is bound to the socket
and component and refuses another binding. Configuration changes to functions are
allowed; queued work for a removed function completes as `unknown-function`.

## Version 1 semantics

**Routing.** A message from authenticated sender S on topic T invokes the sorted,
unique set of targets whose subscription source equals S and subject equals T
(component name, `@operator/UID` or `@bridge/NAME`). Subjects are exact topic
names in v1; wildcard filters, `cloud` targets, AWS ARNs and function-to-function
chaining are not supported. An unmatched message is acknowledged without invocation.
AWS `component:NAME` sources correspond to the plain authenticated component name.

**Delivery.** Each message is journaled (SQLite, `synchronous=FULL`) together with
one invocation row per target before it is acknowledged. A redelivery of a journaled
message is acknowledged without a second invocation. After a manager crash,
interrupted invocations run again with `attempt` incremented: delivery to functions
is at least once, so handlers must tolerate `attempt > 1`. Completed results are
republished after a restart without reinvocation. Results publish in completion order;
a refused publication (for example `backpressure`) is retained and retried with
backoff from 0.5 to 30 seconds, never dropped. `max_pending` bounds outstanding
work; beyond it messages stay queued in the daemon.

**Lifecycle.** An on-demand function runs one new process per invocation (no
reuse; `maxIdleTimeInSeconds` is effectively zero): it receives one request line
and end of input, and after its response, a one-second exit grace and then its
process group and any escaped descendants are killed. A long-lived function starts
with the manager and serves invocations serially (`max_concurrency` must be 1). It is
restarted after it exits (backoff 1 to 30 seconds), immediately after a timeout,
and after an invalid response. Unlike AWS pinned functions, a handler exception
returns an `error` response without stopping the process.

**Admission.** Per function, in FIFO order: invoke below `max_concurrency`, else
queue up to `max_queue` (default 1000, matching the AWS default; 0 rejects at once),
else `busy` with no invocation. The SF-064 v1 reference vectors are this rule with a
queue of zero.

**Timeouts.** Measured from dispatch, including process start: elapsed milliseconds
at or above `timeout_ms` (1..900000) is `timeout`, even for a late successful
response; the process group is killed. Unlike AWS `timeoutInSeconds`, the unit is
milliseconds.

**Envelopes.** The function receives one compact JSON line:
`version`, `invocation_id` (`SUBJECT#MESSAGE_ID/FUNCTION`), `function`, `mode`,
`source`, `subject`, `message_id`, `attempt`, `timeout_ms`, `payload`. It must answer
with exactly `{"version":1,"invocation_id":…,"status":"ok","result":…}` or
`{"version":1,"invocation_id":…,"status":"error","error":{"type":…,"message":…}}`
(type 1..128, message ≤1024 characters) on one line of at most 64 KiB; diagnostics go
to stderr. Only strict UTF-8 JSON is accepted: `NaN`/`Infinity`, lone surrogate
escapes, other encodings and nesting deeper than 64 are `invalid-response`, and a
message payload that is not strict JSON is acknowledged without invocation. Payloads are JSON only (`inputPayloadEncodingType` binary is unsupported).
The manager publishes to `result_topic`: `version`, `invocation_id`, `function`,
`source`, `subject`, `message_id`, `attempt` (0 if never started), `status`,
`duration_ms`, plus `result`, `error`, `exit_code` or `signal`.

| Status | Meaning |
|---|---|
| `ok`, `error` | Valid handler response |
| `timeout` | Deadline reached; process group killed |
| `busy` | Concurrency and queue bounds reached; not invoked |
| `crashed` | Process ended or closed stdout without a response |
| `invalid-response` | Malformed, mismatched, oversized or extra-field response |
| `unsupported-version` | Response version other than integer 1 |
| `spawn-failed` | Process could not start (diagnostic logged) |
| `unknown-function` | Queued for a function no longer configured |
| `result-too-large` | Envelope above the 4096-byte IPC payload limit; result omitted |

Unknown configuration versions (including boolean `true`) are rejected at startup.

**Cleanup failure.** If a function process or escaped descendant has not been killed
and reaped 5 seconds after SIGKILL, or the helper cannot run, the manager stops with
exit status 1 instead of continuing beside a live function. The platform unit's
control-group kill then ends what remains, and the interrupted invocation runs again
with `attempt` incremented after the restart.

**Isolation (Linux).** Functions start in a new session with a cleared environment
(`PATH`, `LANG`, `HOME`, `SUIFLEET_FUNCTION`, `SUIFLEET_FUNCTION_MODE` and the
configured variables only), a private work directory, closed descriptors, no
supplementary groups, no ambient capabilities, `no_new_privs`, no core files,
optional `RLIMIT_AS` (`memory_bytes`) and a parent-death SIGKILL. The manager is a
child subreaper and kills descendants that escape their process group. Stderr goes
to a bounded per-function log (1 MiB plus one rotation) in the private state
directory. There are no containers, cgroups, mount, network or device namespaces
(AWS `GreengrassContainer` mode); use the platform unit for those restrictions.

## Handler runtime

```python
from suifleet_function import FunctionError, serve

def handler(payload, context):
    if 'celsius' not in payload:
        raise FunctionError('Validation', 'celsius required')
    return {'fahrenheit': payload['celsius'] * 9 / 5 + 32, 'attempt': context['attempt']}

serve(handler)
```

Any language can implement the line protocol directly.

## Evidence and limits

Build the debug daemon and gRPC simulator shim, then from the host run:

```sh
make test-lambda SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

The network-none root container runs 23 tests of the real manager and function
processes against a local durable IPC v1 endpoint: the contract file, SF-064 lambda vectors from this
runtime's own decisions, configuration rejection, on-demand fresh processes and
cleared environment, long-lived state, crash, timeout and restart, every error
status, strict-JSON rejection (NaN, Infinity, surrogates, invalid UTF-8, nesting)
without stopping the manager or unrelated functions, prompt failure on denied cleanup,
signalling as the function user, process-group and escaped-descendant cleanup, concurrency/queue/busy bounds,
fan-out and unrouted messages, manager SIGKILL reinvocation with `attempt` 2,
duplicate redelivery, retained results across backpressure and restart, graceful
stop, exclusive state, memory limit, root `run_as` enforcement and identity/state
privacy. It then runs [`scripts/test_lambda.py`](../scripts/test_lambda.py) with the
real daemon and distinct component UIDs: a capability-limited manager, function UID
without capabilities, peer-UID refusal of impersonation, ACL refusal of an
unauthorized publisher, unrouted authorized source, long-lived state, error/timeout,
on-demand and long-lived functions that would outlive the test killed promptly
at their deadline together with an escaped descendant (the manager cannot signal
their UID directly), later invocations after those timeouts, and manager plus daemon SIGKILL recovery without duplicate results. Ordinary CI runs
the same target. The receipt is [sf069-lambda.json](validation/sf069-lambda.json).

Not covered: AWS Lambda/Greengrass recipe, SDK, IPC or wire compatibility; live AWS
(unrun and unauthorized; tracked in [SF-069/#123](https://github.com/tyler-suifleet/suifleet/issues/123));
non-Linux platforms; container/cgroup isolation; physical Pi operation and
power-loss, deferred to [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No AWS account mutations, paid services, production credentials, physical image or
full-parity result is claimed. Sui contracts and transactions are unchanged.
