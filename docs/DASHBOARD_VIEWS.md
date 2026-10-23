# Dashboard: device services, logs and rollouts (SF-084)

The dApp shows device service health, alerts, archive log search and staged
rollout progress without an HTTP indexer. Every value comes from the chain or
from a device signature verified in the browser. Data that cannot be verified,
is out of date, or is never published is shown as **unavailable** with the
reason. The dashboard never shows stale or partial values in its place.

## Device services (`/services/?device=ID`)

The page needs the direct-chain fleet data configuration (`NEXT_PUBLIC_FLEET_DATA`;
see [serverless fleet data](SERVERLESS_FLEET_DATA.md)). It lists devices from the
fleet's on-chain index. The selected device's newest health summary is read
through `fetchChainHealth`, which checks the following before any value is used:

- the writer object and its anchor;
- the device's registry identity;
- the personal-message signature;
- the services section shape.

`lib/device-services.ts` turns that result into a view:

- **Values are shown** only for a `healthy` or `degraded` report that is not
  `offline` or `delayed` and that has a version-1 services section.
- **Everything else is unavailable**, with the reason: unverified (for example a
  forged signature), unreported, revoked, offline, delayed (buffered), a chain
  outage, a malformed section, or a report without a services section.
- **A failed or stalled refresh** makes the earlier result unavailable
  (`currentHealth`). The page never keeps it on screen.

| Section | Shown from the signed report | Not available, and why |
|---|---|---|
| Shadows | Number of shadows, refused delta notifications. Per synchronized shadow: conflicts, consecutive failures, last success, error category | Desired, reported and delta **documents**. Shadow documents stay on the device and are never published to Sui or Walrus ([SHADOWS.md](SHADOWS.md), [TELEMETRY.md](TELEMETRY.md)) |
| Streams | Bytes, maximum size, fill, messages, overwrite policy, rejected appends. Export checkpoint (messages behind), batches, losses, failures, last success, error category | Oldest and newest message times. The signed stream summary does not carry them |
| Messaging | Pub/sub subscriptions, queued, refused and dropped messages, per-principal queues | — |
| Bridge | Connection state and duration, connects, refused and rejected messages, error category | — |
| Alerts | This device's active SF-020 alerts from `chain-alerts.ts`, with links to the deployment and the device's logs | If any fleet device's health cannot be verified, every device's alerts are unavailable rather than partial |

Alerts on the dashboard link to the device's services page.

## Logs and diagnostic bundles (`/data/`)

- **Who can open archives.** Opening, searching and requesting diagnostics need
  read authority: the account holds the fleet `BudgetOwnerCap`, or the fleet's
  `support` list names it. The page checks this on chain before showing the
  controls. Any other account sees that it is not authorized and gets no
  archive actions.
- **How an archive is opened.** It is fetched and verified with
  `fetchVerifiedArchive`, using a wallet-signed Seal session limited to five
  minutes. Log search is a literal substring search (never a regular
  expression), optionally filtered by source and case. It is bounded to
  256-byte queries and 1,000 shown matches.
- **Where plaintext lives.** Decrypted records exist only in page memory.
  Nothing is written to web storage or React Query caches. They are discarded
  when you close the archive, change the account, network or device, or when
  the Seal session expires.
- **How access is recorded.** Each open (or refused open) is added to an
  in-memory access record for the session: time, account, authority (owner or
  support), device, archive index and kind. This record is not durable and is
  never sent anywhere. Each Seal key request carries the signed session.
  Diagnostic requests are durable on-chain records. The `fleet_data` package
  has no on-chain record of archive reads. Making that durable needs a producer
  ABI change, which was reported to SF-082/#160.

## Staged rollouts (`/rollouts/`)

The overview lists every schema-3 rollout the connected account created, with:

- its state;
- the current stage;
- how many members have been granted;
- applied and failed outcomes;
- the failure threshold, and whether it has been reached.

Selecting a rollout shows:

- each cohort's members, grants, members in flight, applied, failed, skipped and
  waiting;
- the remaining bake time;
- the full policy;
- the members' grant times;
- the newest audited `RolloutDecision` events.

An aborted rollout shows the audited abort reason.

`lib/rollout-progress.ts` checks the on-chain `Rollout` record against the
contract's invariants before displaying anything:

- the member vectors have the same length;
- cohorts are ordered and each meets its minimum sample;
- there are no duplicate members;
- no outcome exists without a grant;
- no skipped member was admitted;
- no grant is ahead of the current cohort;
- every state, outcome and policy value is within its bounds.

Decision events must have unique revisions no newer than the record. A record or
history that breaks any of these rules is reported as unavailable, never shown
in part.

## Validation

| Command | Checks |
|---|---|
| `node scripts/test_dashboard_views.cjs` (in `make test-manifest`) | **Services views:** run through the real `fetchChainHealth`, using exact BCS and SDK-signed schema-2 reports. Forged, tampered, out-of-vocabulary, unknown-field, offline, delayed, revoked, chain-outage and missing-section reports are unavailable and carry no values. A verified report shows every section and marks shadow documents and stream times unavailable. **Alerts:** filtered per device. **Rollouts:** stage and threshold arithmetic, bake time, aborted and completed states, and twelve inconsistent-record refusals. Decision filtering, ordering and inconsistency, plus bounded event paging. **Archive search:** literal, case and source filters, bounds and truncation. **Access record:** bounded |
| `CHROME_EXECUTABLE=… node scripts/test_dashboard_browser.cjs` | The real Device services, Rollouts and Logs pages with the real dApp Kit, chain reader and verifier, a fixture wallet and a fullnode fixture (localnet manifest, exact BCS). **Services:** values and the alert drill-down are shown. A forged report hides all values and makes alerts unavailable. **Rollouts:** the aborted rollout's overview, stages and audit are shown. **Logs:** an unauthorized account gets no archive controls. **Archive search:** search, the access record and discard all work, with no web storage used |

Production Seal providers, the real extension wallet and IdP sessions, physical
devices and Mainnet remain unrun. They are tracked in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). The browser
test uses a fixture wallet, not a manual wallet session.
