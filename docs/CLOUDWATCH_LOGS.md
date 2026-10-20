# CloudWatch Logs export (SF-060)

The `cloudwatch-logs-export` adapter uploads the daemon's log and supervised components' logs to Amazon CloudWatch Logs. It is pinned to `aws.greengrass.LogManager` **2.3.14** and executes the declared contract in [GREENGRASS_MANAGED_COMPONENTS.md](GREENGRASS_MANAGED_COMPONENTS.md#cloudwatch-logs-export-contract) and [`cloudwatch-logs-cases.json`](../interfaces/compat/cloudwatch-logs-cases.json). It is a clean-room implementation of the documented CloudWatch Logs JSON API with SigV4 signing. It uses no AWS SDK and no AWS component code.

It is a separate process, `suifleet --cloudwatch-logs CONFIG_JSON`, in the daemon binary. It runs under its own Unix user, never as root or as the daemon's user. Its AWS credentials are the scoped credentials of the [SF-057 credential adapter](AWS_CREDENTIALS.md); no other credential source is read.

This is local adapter behaviour tested against a local emulator. It is not end-to-end AWS LogManager parity, and no live AWS account has been used (see [Evidence](#evidence-and-acceptance-boundary)).

## What is uploaded where

| Source | File read (and its rotated `.1`) | Log group | Log stream |
|---|---|---|---|
| `System` (`systemLogsConfiguration`) | `<stateDir>/logs/daemon.log` | `/aws/greengrass/GreengrassSystemComponent/{region}/System` | `/{yyyy/MM/dd}/thing/{thingName}` |
| Component `NAME` (`componentLogsConfigurationMap`) | `<stateDir>/supervised/NAME/component.log` | `/aws/greengrass/UserComponent/{region}/NAME` | same |

- **Selection.** Only configured sources are read. A component that is not configured, including a sibling of a configured one, is never opened.
- **Stream date.** The date is the event's UTC day. In the thing name, `:` becomes `+`.
- **Level and time.** A line's level is its first word, after an optional leading RFC 3339 UTC time: `DEBUG`, `INFO`, `WARN`/`WARNING`, `ERROR`, also written as `[WARN]` or `ERROR:`. `TRACE` is below every minimum. A line naming no level is `INFO`.
  - The leading time, as written by the daemon (`2026-10-17T12:00:00.123456Z  INFO …`) or a Greengrass-style logger, is the event time. A line without one takes the time it was read.
  - Lines below the source's `minimumLogLevel` (default `INFO`) are not uploaded. Empty lines are skipped.
- **Line length.** A line of up to 1 MiB is read whole and redacted. A longer line becomes a marker line. A line over 256 KiB (262,144 bytes, counting CloudWatch's 26 bytes per event) is split into consecutive events with the same time, cut only at UTF-8 character boundaries, as LogManager 2.2.2 and later do.
- **Strict reading.** Files are opened with `openat2(RESOLVE_NO_SYMLINKS)`, so no path component may be a symbolic link. A file must be a regular file with exactly one link. A component that replaces its log, its `.1` or its directory with a link to a sibling's log, the device key or anything else is refused. That source is skipped with a warning; the others go on.
  - This needs Linux 5.6 or later. Without `openat2`, reading fails closed.
  - `stateDir` must therefore be a symlink-free path.
- **Rotation.** Per-source cursors (inode, offset and first bytes) follow a rotation into `.1`, as the SF-020 shipper does. Lines lost to two rotations between reads are replaced by a marker event. Reading starts where the daemon's retained `start` cursors say, so component lines written before the daemon recorded confidential values are never read.

## Redaction, before the spool

Every line is redacted whole, before it is split or spooled:
- the [SF-020 rules](LOGS.md) (keys, tokens, secret-named values, hex and base64 runs, recovery phrases, every line of a PEM private-key block);
- the configured `redact` literals;
- every confidential value in the daemon's `<stateDir>/logs/confidential.json` (SF-015 overlays).

The confidential values fail closed. If the file is missing, unreadable or invalid, nothing is read and no cursor moves; the next pass retries. Lines are read before the values are loaded, so a value a line can hold was recorded first.

The adapter remembers every value it has seen, in its own spool, and keeps redacting it after the daemon retires it. It remembers at most 65,536 values; beyond that, collection stops. A value the daemon recorded and pruned while the adapter was not running for more than an hour is never seen by the adapter. Run the adapter whenever the daemon runs.

With `"redactConfidentialValues": false`, the daemon's values are not used. Only use that for a deployment without confidential configuration.

## Spool and delivery

The spool is `<spoolDir>/spool.journal` (0600; the directory is forced to 0700). It is a journal of JSON records, `fsync`ed one record at a time. Each read pass is one record holding its events, the source cursors and newly seen values, so a crash keeps both the events and the cursors, or neither. A record torn by a crash is discarded on start; any other unreadable record stops the adapter (fail closed). The journal is compacted into a snapshot by atomic rename when it exceeds twice the last snapshot plus 1 MiB. A compaction that fails before the rename leaves the journal as it was. Once the rename succeeds, records go to the new journal; if the directory then cannot be synced, the spool refuses further records and the adapter exits so its unit restarts it, since a crash could otherwise bring back the old journal without them.

**Bounds:**
- `spoolLimitBytes` (default 10 MiB) bounds the spooled events, counted as CloudWatch does: UTF-8 bytes plus 26 per event.
- When the spool is full, the oldest events are dropped and counted. A line whose events alone exceed the whole limit is dropped by itself and counted; it evicts nothing.
- On disk the journal stays within about twice its JSON-encoded events, plus 1 MiB and one read pass.
- A failed write (for example a full disk) is undone; nothing is spooled and the cursors do not move, so the lines are read again later.

**Uploads**, every `periodicUploadIntervalSec` (default 300 s):
- Each source's events go out in time order, equal times in arrival order.
- Each `PutLogEvents` batch holds at most 10,000 events and 1,048,576 bytes, all of one UTC day.
- A missing group or stream (`ResourceNotFoundException`) is created and the call retried once.

What happens to a batch depends on the answer:

| Answer | The batch |
|---|---|
| Success | Leaves the spool. Events refused through `rejectedLogEventsInfo` (too old, too new, expired) are counted as `rejected` |
| `InvalidParameterException`, `DataAlreadyAcceptedException`, `SerializationException` | Resending cannot succeed: leaves the spool, counted as `rejected`, and a warning is logged |
| Throttling, HTTP 5xx, connection failure, no or partial answer (`response-lost`), credential or authorization errors, anything else | Stays. That source sends nothing more this cycle; the next cycle resends the same events unchanged. Other sources go on |

Delivery is therefore **at least once**: an answer lost after CloudWatch accepted a batch causes a duplicate, never a gap. The spool, drops and rejections survive restarts.

## Credentials and endpoint

The adapter reads `AWS_CONTAINER_CREDENTIALS_FULL_URI` and `AWS_CONTAINER_AUTHORIZATION_TOKEN`, which the SF-057 wrapper sets for a granted component. The URI must be HTTPS, or HTTP to a loopback address.

Credentials are fetched with the token as `Authorization`. They stay in memory only and are refreshed 300 seconds before they expire. Unexpired cached credentials stay usable while a refresh fails. An expired-token or signature error drops the cache.

The process is made non-dumpable, so other processes of its user cannot read its memory. It refuses to run as root.

**Endpoint.** Requests go to `https://logs.{region}.amazonaws.com/`, or to `endpoint` if configured: an origin with no path, HTTPS, or plain HTTP only to a loopback address (a local emulator). Proxies from the environment and redirects are not used.

Give the role only what the adapter calls, on its own groups:

```json
{
  "Effect": "Allow",
  "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
  "Resource": [
    "arn:aws:logs:eu-west-1:ACCOUNT:log-group:/aws/greengrass/GreengrassSystemComponent/eu-west-1/System:*",
    "arn:aws:logs:eu-west-1:ACCOUNT:log-group:/aws/greengrass/UserComponent/eu-west-1/sensor:*"
  ]
}
```

## Configuration

`/etc/suifleet/cloudwatch-logs.json`, readable by the adapter's user. It holds no credentials, but `redact` literals are secrets, so make it 0600:

```json
{
  "region": "eu-west-1",
  "thingName": "fleet:pi-1",
  "stateDir": "/var/lib/suifleet",
  "spoolDir": "/var/lib/suifleet-cloudwatch",
  "periodicUploadIntervalSec": 300,
  "logsUploaderConfiguration": {
    "systemLogsConfiguration": {"uploadToCloudWatch": true, "minimumLogLevel": "INFO"},
    "componentLogsConfigurationMap": {"sensor": {"minimumLogLevel": "WARN"}}
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `region`, `thingName` | required | Group and stream names. Region: lowercase letters, digits, `-`. Thing name: letters, digits, `_`, `:`, `-` (1–128) |
| `stateDir` | required | The daemon's `[runtime] state_dir` (absolute, no symbolic links) |
| `spoolDir` | required | The adapter's own spool, outside `stateDir` (absolute) |
| `logsUploaderConfiguration` | required | LogManager's `systemLogsConfiguration` (`uploadToCloudWatch`, default false; `minimumLogLevel`) and `componentLogsConfigurationMap` (`minimumLogLevel`) |
| `periodicUploadIntervalSec` | 300 | Upload interval, more than 0 and at most 86,400 |
| `readIntervalSec` | 5 | How often logs are read (1–3600); SuiFleet-specific |
| `spoolLimitBytes` | 10485760 | See [Spool and delivery](#spool-and-delivery) |
| `redactConfidentialValues` | true | Use the daemon's confidential values |
| `redact` | `[]` | Further literal values to redact; none empty |
| `endpoint` | regional | See [Credentials and endpoint](#credentials-and-endpoint) |

Unknown keys are rejected rather than ignored, including the LogManager options this adapter does not implement: `logFileRegex`, `logFileDirectoryPath`, `diskSpaceLimit`, `diskSpaceLimitUnit`, `deleteLogFileAfterCloudUpload` and `multiLineStartPattern`. `componentLogsConfigurationMap` cannot name `System`. Component names follow the catalog rules (1–128 letters, digits, `.`, `_`, `-`; not `.` or `..`).

## Deployment

The adapter needs read access to the daemon's logs, and AWS credentials that SF-057 denies to the daemon's user. The platform therefore provisions these, nothing of which is installed automatically:

1. **The daemon.** Set `[runtime] log_group_read = true`. The daemon then writes its own log, and makes its logs and `confidential.json` 0640 and `logs/`, `supervised/` and `supervised/NAME/` 0750, past its umask. Give the daemon's unit `StateDirectoryMode=0750`, so its group can enter the state directory. Everything else there stays private under `UMask=0077`. On an existing installation the option takes effect at the daemon's next start: the existing logs and retained values are reconciled to 0640 even when nothing new is retained, and back to 0600 when it is turned off.
2. **The adapter's user.** A dedicated user (for example `suifleet-cwlogs`) whose only supplementary group is the daemon's group. No other user may be in that group: its members can read every component log and the confidential values.
3. **Credentials.** Grant that user in the SF-057 adapter's policy. Its UID must differ from the daemon's UID, as SF-057 requires.
4. **The service.** Run the adapter through the SF-057 wrapper, as a platform service under its own user:

```ini
[Service]
User=suifleet-cwlogs
SupplementaryGroups=suifleet
StateDirectory=suifleet-cloudwatch
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
LimitCORE=0
ProtectSystem=strict
ReadOnlyPaths=/var/lib/suifleet
ExecStart=/usr/bin/python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component cloudwatch-logs -- \
  /usr/bin/suifleet --cloudwatch-logs /etc/suifleet/cloudwatch-logs.json
Restart=on-failure
```

The adapter reads only; it never deletes, truncates or rotates a log (LogManager's `deleteLogFileAfterCloudUpload` is not offered). It changes no daemon lifecycle and triggers no reboot. SIGTERM stops it; the spool is kept.

## Evidence and acceptance boundary

| Command | Covers |
|---|---|
| `make edge-test` (`cloudwatch_logs` tests) | All 8 declared `cloudwatch-logs-cases.json` sequences (69 steps) run against the adapter's spool, batching and real HTTP uploader. The request-checking emulator receives exactly the fixture's expected `PutLogEvents` calls, with no check violation. One step (a non-text message) is decided while decoding the fixture into the adapter's text interface. Also: HMAC (RFC 4231) and SigV4 (AWS documentation) vectors, no secret in debug output, batch limits, `rejectedLogEventsInfo` counting, LogManager configuration names and rejections, spool restart, torn-tail recovery, fail-closed corruption, compaction, a failure after the compaction rename (no record lost, further writes refused until a reopen) and write-failure rollback; the retained values' mode reconciled when `log_group_read` is turned on or off for an existing state directory. Collection: redaction of keys, secret-named values, PEM blocks and confidential values, also after the daemon retired them; levels and times; rotation; start cursors; fail-closed confidential values; symlinked, hard-linked and directory-link sibling refusal. Outcomes: stream creation, partial and full rejection, connection failure, credential refusal, cached credentials |
| `python3 scripts/test_cloudwatch_logs.py` (in `make test-ota`) | The real adapter process. With the real daemon and a supervised component, first started without and then with `log_group_read`: the existing logs and retained values become 0640 (directories 0750), redaction before the spool, DEBUG filtered, names, and a 300,000-byte line split into 262,118 + 37,882 bytes. With a test-written state directory: an outage (503), a lost response resent, SIGKILL with spooled lines, rotation, symlinked and hard-linked sibling logs refused, a 2,000-byte spool dropping and counting the oldest with a bounded journal, and refusal to start without container credentials or with `logFileRegex` |
| `make test-cloudwatch-logs-uids` (`--distinct-uids`, root in a disposable network-isolated container) | The same real-daemon part with the daemon and the adapter under separate non-root UIDs and `UMask=0077`, the adapter only in the daemon's group: it exports the logs and is denied the device key |
| `make test-compat` | The catalog receipt and the reference model of the same fixtures |

The [runtime receipt](validation/sf060-cloudwatch-logs.json) records the exact runs.

**Not established; tracked in [SF-060/#105](https://github.com/tyler-suifleet/suifleet/issues/105):**
- A real AWS account upload. It needs a separate user authorization for an AWS account; none exists, and it has not run.
- Credentials from the actual SF-057 adapter process (the tests use a stand-in endpoint speaking the same protocol) and a real IAM policy.
- The shipped systemd units: the distinct-UID run drops privileges directly, not through `User=`/`SupplementaryGroups=`/`StateDirectoryMode=`.
- Physical Pi and target integration ([SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)).

**Not implemented:**
- `SystemLogForwarder` journal forwarding: the `System` source is the daemon's log file, not the systemd journal.
- Platform-managed (non-supervised) component log files, regex file selection and deletion after upload.
- LogManager's multi-line event grouping.
