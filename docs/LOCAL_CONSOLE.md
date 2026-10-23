# Local support console (SF-079)

The daemon can serve an opt-in local support console: a versioned HTTP API and
a small static UI for on-device diagnosis. It shows inventory, health,
deployment outcomes, component logs and committed configuration. It can also run
the support operations the device enables: `start`, `stop` and `restart` of a
supervised component. It is off unless `[console]` is configured. There is no
unauthenticated mode and no non-loopback listener.

## Upstream reference and scope

The clean-room functional reference is `aws.greengrass.LocalDebugConsole`
**2.4.8**, the latest changelog entry on the
[AWS component page](https://docs.aws.amazon.com/greengrass/v2/developerguide/local-debug-console-component.html),
reviewed on 2026-10-17. No AWS code, UI or recipe is used or redistributed. These
upstream semantics are adopted:

| Upstream (2.4.x) | SuiFleet |
|---|---|
| `bindHostname` default `localhost`; remote use through `ssh -L 1441:localhost:1441` | Loopback only, default `127.0.0.1:1441`. A non-loopback `listen` (including `0.0.0.0`) is a configuration error. Remote use is SSH port forwarding. |
| `port` 1441 | Same default. There is no separate WebSocket port: the UI polls the HTTP API. |
| `httpsEnabled` (self-signed certificate) | Plain HTTP on loopback only. Browsers treat `http://127.0.0.1` and `http://localhost` as secure contexts, and SSH carries the remote hop. |
| User `debug` with a temporary 43-character password from `greengrass-cli get-debug-password`, valid for 8 hours | Named users, each with a 256-bit token from `suifleet --console-token`. The device stores only its SHA-256. An optional `expires_unix` limits the token, and a session lasts at most 8 hours. |
| Single debug user with full access | Per-user grants. A support operation requires both the device's `support_operations` and the user's grant. |
| Development-only recommendation | Same recommendation. Enable it only on devices that need it. |

Not implemented: the upstream configuration editor, pub/sub and MQTT debug
client, and stream manager views. Configuration changes only through
authenticated deployments ([OPERATIONS.md](OPERATIONS.md)). Local messaging
remains on [IPC](COMPONENT_IPC.md). This is a native functional equivalent, not
AWS API, UI or recipe compatibility.

## Configuration

```toml
[console]
listen = "127.0.0.1:1441"                 # loopback address, fixed port
# origins = ["http://localhost:9000"]     # default: 127.0.0.1 and localhost on the listen port
support_operations = ["restart"]          # subset of start, stop, restart; default none
# session_idle_seconds = 900              # 60..=3600
# session_max_seconds = 28800             # 60..=28800
# audit_max_bytes = 1048576               # 64 KiB..=64 MiB, then rotated once

[console.users.support]
token_sha256 = "<from suifleet --console-token>"
grants = ["inventory", "health", "deployments", "logs", "configuration", "audit", "restart"]

[console.users.viewer]
token_sha256 = "<another token's hash>"
grants = ["inventory", "health"]
expires_unix = 1798761600                 # optional
```

`suifleet --console-token` prints `{"token": "sfc_…", "token_sha256": "…"}`. Give
the token to the operator and put only the hash in `/etc/suifleet/config.toml`.
Only tokens in the generated format are accepted, so an operator-chosen password
cannot be configured. Configuration is validated at load and by `--check-config`.
Unknown fields, unknown grants, duplicate token hashes, a support grant that the
device does not enable, a non-loopback listener or origin, and out-of-range limits
are all rejected. When an origin is listed, the browser address must use that
origin (for example a different forwarded local port). If the console cannot bind
(for example, the port is taken), the daemon logs `Local support console
unavailable` and keeps running without a console.

## Protocol v1

Every API call is a `POST` with a JSON body of at most 4 KiB and
`Content-Type: application/json`.

| Path | Purpose |
|---|---|
| `GET /`, `/console.js`, `/console.css` | Static UI. It holds no device data. |
| `POST /api/v1/session` | `Authorization: Bearer <token>` returns `{user, grants, csrf}` and sets the `suifleet_console` cookie (`HttpOnly; SameSite=Strict; Path=/api/v1`) |
| `POST /api/v1/logout` | Ends the session |
| `POST /api/v1/request` | `{"version": 1, "operation": …, "component"?, "limit"?, "after"?}` |

Responses are `{"version": 1, "status": …, "data"?: …}`.

| Status | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | Success; `data` holds the result |
| `invalid-request` | 400 | Unknown body field, bad component name or out-of-range `limit` |
| `unsupported-version` | 400 | `version` is not the integer 1 |
| `too-large` | 400 | Response exceeds 64 KiB; use a smaller `limit` |
| `unauthenticated` | 401 | Missing, wrong, expired or ambiguous credential, or bad session/CSRF |
| `invalid-origin` | 403 | Origin, fetch-metadata or Content-Type check failed |
| `denied` | 403 | User lacks the grant, or the operation is not enabled |
| `not-found` | 404 | Unknown component or path |
| `invalid-host` | 421 | Host is not a configured origin |
| `failed` | 500 | Support operation failed |
| `unknown` | 500 | Support operation not answered in 60 s; whether it ran is not known |
| `busy`, `unavailable` | 503 | Control queue full, or audit/state unreadable |

Failure details stay in the daemon log. Unknown operations are `denied`; they
are never in a grant.

Checks are applied in the [SF-064 contract](GREENGRASS_REMAINING_ROADMAP.md)
order: version, authentication, origin, then grant. The reference console
vectors run against this implementation's decision function and field
allowlist (`cargo test console`).

- **Authentication.** A call carries either a bearer token (CLI use) or a
  session cookie with the matching `X-SuiFleet-CSRF` header. A call that carries
  both is rejected. Sessions live only in daemon memory, so a daemon restart
  ends them. At most 32 sessions exist; when full, the least recently used is
  evicted. Bearer failures do not lock the account: tokens are generated with
  256 bits, so guessing is infeasible, and a lockout would let any local process
  deny the operator access.
- **Origin and CSRF.** The Host header must name a configured origin, which
  defeats DNS rebinding. A cookie-authenticated call also requires a configured
  `Origin`, `Sec-Fetch-Site` of `same-origin` or `none` when sent, a JSON
  content type (which a cross-site form or simple request cannot send) and the
  session's CSRF token. Login checks the same, without requiring Origin for CLI
  use. CORS is never granted, and `OPTIONS` returns 405.
- **Operations.**

  | Operation | Result | Bounds |
  |---|---|---|
  | `inventory` | Components as `component`, `version`, `health`, `deployment`, `installed`, `supervised`, `restarts`, `since_ms`, `last_exit` | `limit` ≤ 100; resume with `after` = `next` |
  | `health` | `healthy`/`degraded`, reasons, sync state and pending receipts | 32 reasons |
  | `deployments` | Journal outcomes: status, reported, indeterminate and error | `limit` ≤ 100, default 20; paged like `inventory` |
  | `logs` | Last lines of a supervised component's log | 8 KiB tail, `limit` ≤ 200 lines, 512 bytes per line |
  | `configuration` | Committed public configuration; confidential pointers removed. Withheld entirely (`withheld`) when the recipe was Seal-encrypted ([CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md)) | Response size |
  | `audit` | Recent audit records | `limit` ≤ 100 |
  | `start`, `stop`, `restart` | Run by the poll-loop owner through the existing local-control path, including dependency checks | Not replayed if the console stopped waiting |

  Read views come from the daemon's state files, as telemetry does, so they
  answer during a long apply. Support operations wait for the poll loop.
- **Redaction.** Output is an explicit field allowlist. Values under
  secret-looking names are replaced with `[redacted]`. Matching names contain
  key, token, secret, password, credential, private, auth, cookie, session,
  signature, mnemonic, seed, pin, cert or csrf. Free text is also scrubbed of:
  - `name=value` and `name: value` pairs whose name looks secret
  - `Bearer`/`Basic` credentials
  - PEM blocks. Log tails are redacted before lines are selected, and a
    block's body lines are redacted with its markers. A tail that starts
    inside a block is detected from the up to 1 MiB of log before it: when
    the last marker there is a BEGIN, every tail line up to the next END is
    redacted, including short and logger-prefixed lines, and with no END
    the whole tail. A block opened further back is far larger than any key
    and is not treated as open. Independently, everything before an
    unmatched END line is redacted, as is any bare base64 line of 40 or more
    characters.
  - `suiprivkey…` keys, console tokens, AWS access key IDs and JWT-like values
  - bare hex of 64 or more characters (raw key material; `0x` object IDs are kept)

  A secret pair's or credential's whole value is replaced. A quoted value runs
  to its closing unescaped quote, spaces included, or to the end of the text
  when unterminated. An unquoted value runs to the next whitespace,
  punctuation included. An authorization scheme before the value is kept.
  In a log tail a quoted value may span lines: every line up to its closing
  quote is redacted, and an unterminated value redacts the rest of the tail.
  A value opened in the up to 1 MiB of log before the tail carries into it,
  so its continuation lines are redacted whatever line limit is requested.

  Over-redaction is preferred to a leak. Redaction is defense in depth:
  components must still not log secrets.
- **Audit.** Each API decision appends a JSON line to
  `<state_dir>/console/audit.jsonl`. The file is mode 0600 in a 0700 directory,
  opened with `O_NOFOLLOW` and rotated once to `audit.jsonl.1` at
  `audit_max_bytes`. Each record holds the time, peer, user, operation, component
  and status:
  - Logins, logouts and every allowed, denied, unauthenticated or invalid call
    are recorded.
  - Unknown operation names are recorded as `other`.
  - Credentials, CSRF tokens and request bodies are never recorded.
  - A support operation first writes a synced `requested` record and does not run
    if that write fails. Its outcome is recorded afterwards.
- **HTTP limits.**
  - At most 16 concurrent connections. Each has 5 s to send its request, then
    is closed with `Connection: close`.
  - Headers are limited to 8 KiB.
  - `Transfer-Encoding`, folded headers, repeated
    Host/Origin/Cookie/Authorization/Content-Length/CSRF headers and anything
    other than HTTP/1.0 or 1.1 are rejected.
  - Every response carries `no-store`, `nosniff`, `X-Frame-Options: DENY`, a
    `default-src 'none'` CSP and same-origin resource/opener policies.

## Trust boundary

Console authentication protects the device against:

- other local users
- non-component local processes
- browsers, through CSRF and DNS rebinding

It does not isolate the daemon from components that run under the daemon's own
UID. Such a component already has daemon-equivalent access to the state
directory ([COMPONENT_IPC.md](COMPONENT_IPC.md)). The configuration file holds
only token hashes. Keep tokens off the device, and give operators only the
grants they need.

## Verification

```bash
make edge-test                         # includes cargo test console
python3 scripts/test_console.py        # real daemon + chain simulator; part of make test-ota
# Also drive the UI in Chrome (after make dapp-install; not in the CI image):
CHROME_EXECUTABLE=/path/to/chrome python3 scripts/test_console.py --browser
```

`scripts/test_console.py` runs the real daemon against the local chain/Walrus
simulator, with no network beyond loopback and no keys or gas. It covers:

- non-loopback configuration refusal
- daemon survival when the console cannot bind, and console service after restart
- token, session, Origin, Host, fetch-metadata and CSRF denial
- grant, version, field and limit validation
- redaction of real component logs and configuration
- inventory and deployment paging
- stop/start/restart through the daemon
- hostile and malformed HTTP, and release after an idle flood
- audit contents, permissions and persistence across a restart, with sessions
  ended by the restart
- with `--browser`, in headless Chrome:
  - the UI signs in, renders views and redacted logs, restarts a component and
    signs out, with no CSP violation or script-readable cookie
  - a page on another loopback port (same-site for cookies) cannot drive the API
    from the signed-in browser with a simple fetch, a JSON fetch or a form POST

Recorded results are in [validation/sf079-console.json](validation/sf079-console.json).

Not covered, and tracked separately:

- Browsers other than the headless Chrome build used by `--browser`. Unit tests
  also check that the assets contain no inline script, HTML sinks or storage use.
- Physical Pi/target acceptance ([SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)).
- Any AWS account or service. None is involved or authorized.
